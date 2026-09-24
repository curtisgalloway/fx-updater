# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""fx-updater - keep a Fuchsia checkout updated and built, with guardrails.

`fx-updater run` performs one unattended update-and-build:

  fx-updater run                           # tree = current dir, fx default build dir
  fx-updater run --fuchsia-dir ~/fuchsia --build-dir core.x64 --build-dir bringup.x64
  fx-updater run --no-update               # build only; never checks for WIP

Settings not given as flags come from the config file (`fx-updater install`
writes it; see fx_updater/config.py), then from the defaults.

Order of operations, each step able to stop the run:

1. The lock. A second concurrent run refuses to start.
2. The disk floor (`--min-free-gb`). Below it, nothing is touched.
3. The WIP guard (`jiri status -check-head=false`). Mode-only dirt is
   restored; anything else skips the run. `--force-update` overrides and
   `--no-update` never checks.
4. `jiri update`, retrying network-shaped failures only.
5. `fx build` per build dir, with one `fx gen` if the graph is stale.

Every outcome is written to the status document (see fx_updater/status.py),
and to `fx_updater.prom` when a prom dir is configured. A WIP skip exits 0: a
dirty tree on most mornings must not read as a failed scheduled job, and the
status document is the record.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import pathlib
import subprocess
import sys
import time
from importlib.resources import files

from fx_updater import __version__
from fx_updater import build
from fx_updater import config
from fx_updater import guard
from fx_updater import jiri
from fx_updater import promfile
from fx_updater import status
from fx_updater import systemd

# Exit statuses, per the portable contract: 0 success, 1 empty answer, 2-9
# deterministic (retrying is futile), 20s transient, 100+ this tool's own.
# The decade is the coarse signal; a caller can branch on `code // 10`.
# jiri's and fx's own return codes are recorded in the status document and
# never propagated: a caller could not tell whose code it was reading.
EXIT_OK = 0  # ok; also skipped_wip, which is a correct outcome, not a failure
EXIT_EMPTY = 1  # status: no run has written a status document yet
EXIT_USAGE = 2  # bad flags or values (argparse also exits 2)
EXIT_SETUP = 3  # jiri/fx/config/build dir missing; below the disk floor
EXIT_BUSY = 20  # lock held, status_failed, or systemctl failed before any change
EXIT_PARTIAL = 21  # update_failed (tree may be part-updated); install half-done
EXIT_BUILD_FAILED = 100  # build_failed: the tree is updated, a build broke

_EXIT_EPILOG = """\
exit status:
  0    ok, or skipped_wip (work in progress; nothing touched) - see `status`
  1    status: no run has happened yet (never_run)
  2    usage error, including an invalid flag value
  3    missing precondition: jiri/fx, config, build dir, systemctl or
       systemd-analyze not found; below the disk floor (skipped_disk); the
       status document is unreadable; a unit of that name exists that
       fx-updater did not write
  20   nothing done, retry is free: another run holds the lock; `jiri
       status` itself failed (status_failed); a systemctl call failed
       before install/uninstall changed any file
  21   partially done: `jiri update` failed (the tree may be part-updated;
       the next run reconciles), or install/uninstall wrote or removed a
       file (unit or config) and then a systemctl call failed
  100  a build failed; the tree is updated
"""

_TAIL = 4000


# `--prom-dir ""`. pathlib.Path("") is ".", which would quietly mean "the
# current directory", so the empty flag gets a sentinel of its own.
_NO_DIR = "<none>"


def _optional_dir(text: str) -> pathlib.Path | str:
    return pathlib.Path(text) if text else _NO_DIR


class _UsageError(Exception):
    """A flag value is invalid; the command exits EXIT_USAGE."""


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, not {value}")
    return value


def _run_logged(cmd: list[str], cwd: pathlib.Path, log) -> int:
    log.write(f"+ {' '.join(cmd)}\n")
    log.flush()
    proc = subprocess.run(
        cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=False
    )
    return proc.returncode


def _jiri_status(jiri_bin: pathlib.Path, fuchsia: pathlib.Path, log, note: str = ""):
    st = subprocess.run(
        [str(jiri_bin), "status", "-check-head=false"],
        cwd=fuchsia,
        capture_output=True,
        text=True,
        check=False,
    )
    log.write(f"+ jiri status -check-head=false{note}\n")
    log.write(st.stdout + st.stderr)
    log.flush()
    return st


def _load_config(args: argparse.Namespace) -> config.Config | None:
    """The config file `args` points at, or the default one if it applies.

    An explicit `--config` is always used, and one that does not exist is
    an error. The default file is used only when it describes the tree this
    run is for: with `--fuchsia-dir` naming another tree, its build dirs and
    floor were chosen for a different checkout. In that case it is skipped,
    and so is a broken one, rather than failing a flags-only run.
    """
    if args.config is not None:
        return config.load(args.config)
    path = config.default_path()
    if not path.exists():
        return None
    if args.fuchsia_dir is None:
        return config.load(path)
    try:
        cfg = config.load(path)
    except config.ConfigError as e:
        print(f"warning: ignoring the default config: {e}", file=sys.stderr)
        return None
    if cfg.fuchsia_dir.resolve() != args.fuchsia_dir.resolve():
        return None
    return cfg


def _apply_config(args: argparse.Namespace, cfg: config.Config | None) -> None:
    """Fill every run setting not given as a flag: config, then default."""
    for name in args.build_dir:
        try:
            config.check_build_dir(name)
        except config.ConfigError as e:
            raise _UsageError(f"--build-dir: {e}") from e
    if args.fuchsia_dir is None:
        args.fuchsia_dir = cfg.fuchsia_dir if cfg else pathlib.Path.cwd()
    if not args.build_dir and cfg:
        args.build_dir = list(cfg.build_dirs)
    if args.min_free_gb is None:
        args.min_free_gb = cfg.min_free_gb if cfg else config.DEFAULT_MIN_FREE_GB
    if args.prom_dir is None and cfg:
        args.prom_dir = cfg.prom_dir
    elif args.prom_dir == _NO_DIR:
        args.prom_dir = None


def cmd_run(args: argparse.Namespace) -> int:
    """One update-and-build. Returns the process exit status."""
    try:
        _apply_config(args, _load_config(args))
    except _UsageError as e:
        print(e, file=sys.stderr)
        return EXIT_USAGE
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return EXIT_SETUP
    fuchsia = args.fuchsia_dir.resolve()
    jiri_bin = fuchsia / ".jiri_root/bin/jiri"
    fx = fuchsia / "scripts/fx"
    for tool in (jiri_bin, fx):
        if not tool.exists():
            print(f"missing: {tool}", file=sys.stderr)
            return EXIT_SETUP

    status_file = args.status_file or status.default_status_path()
    log_dir = args.log_dir or status.default_log_dir()

    try:
        with status.Lock(status.lock_path()):
            return _run_locked(args, fuchsia, jiri_bin, fx, status_file, log_dir)
    except status.LockHeld as e:
        print(
            f"lock exists ({e}); another run is in progress? "
            "`fx-updater status` shows whether its pid is still running",
            file=sys.stderr,
        )
        return EXIT_BUSY


def _run_locked(
    args: argparse.Namespace,
    fuchsia: pathlib.Path,
    jiri_bin: pathlib.Path,
    fx: pathlib.Path,
    status_file: pathlib.Path,
    log_dir: pathlib.Path,
) -> int:
    started = datetime.datetime.now()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{started:%Y-%m-%d_%H%M%S}.log"

    # Failure classification and the regen check must read only the output of
    # the command that just failed. The log is shared by every update attempt
    # and every build dir, so a tail of the whole file can carry an earlier
    # command's network error or stale-graph message into a later decision.
    mark = {"offset": 0}

    def run_cmd(cmd: list[str]) -> int:
        log.flush()
        mark["offset"] = log_path.stat().st_size
        return _run_logged(cmd, fuchsia, log)

    def log_tail() -> str:
        """The end of the most recent run_cmd's output, at most _TAIL chars."""
        with log_path.open("rb") as f:
            f.seek(mark["offset"])
            return f.read().decode(errors="replace")[-_TAIL:]

    def report(outcome: str, reason: str, **extra) -> None:
        payload = {
            "timestamp": started.isoformat(timespec="seconds"),
            "outcome": outcome,
            "reason": reason,
            "log": str(log_path),
            "fuchsia_dir": str(fuchsia),
        }
        payload.update(extra)
        status.write_status(status_file, payload)
        if args.prom_dir and not promfile.write(
            args.prom_dir, promfile.samples_for(payload)
        ):
            print(f"warning: could not write {args.prom_dir}", file=sys.stderr)
        print(f"{outcome}: {reason}")

    rc = EXIT_OK
    with log_path.open("w") as log:
        build_dirs = args.build_dir
        if not build_dirs:
            fx_build_dir = fuchsia / ".fx-build-dir"
            if not fx_build_dir.exists():
                print("no --build-dir and no .fx-build-dir", file=sys.stderr)
                return EXIT_SETUP
            build_dirs = [fx_build_dir.read_text().strip().removeprefix("out/")]

        free_gb = guard.disk_free_gb(fuchsia)
        if free_gb < args.min_free_gb:
            report(
                "skipped_disk",
                f"{free_gb:.0f} GB free is below the "
                f"{args.min_free_gb:.0f} GB floor; not touching the tree",
                disk_free_gb=round(free_gb, 1),
            )
            return EXIT_SETUP

        if not args.no_update and not args.force_update:
            st = _jiri_status(jiri_bin, fuchsia, log)
            if st.returncode != 0:
                report(
                    "status_failed",
                    "jiri status itself failed; the work-in-progress check "
                    "could not run, so the tree was not touched",
                    detail=(st.stdout + st.stderr)[-2000:],
                )
                return EXIT_BUSY
            wip = guard.wip_summary(st.stdout)
            if wip:
                # Mode flips with identical content are not anyone's work.
                # Unhandled they skip every run, silently, because a skip
                # exits 0. They must be restored rather than ignored: jiri
                # update refuses a project with any uncommitted change.
                restored = guard.restore_modes(fuchsia, st.stdout)
                if restored:
                    msg = (
                        f"restored {restored} file mode(s) that differed "
                        "from the index with identical content; not work "
                        "in progress"
                    )
                    print(msg, file=sys.stderr)
                    log.write(f"+ {msg}\n")
                    st = _jiri_status(jiri_bin, fuchsia, log, " (rechecked)")
                    wip = guard.wip_summary(st.stdout) if st.returncode == 0 else wip
            if wip:
                report(
                    "skipped_wip",
                    "work in progress in the checkout; not updating "
                    "(--force-update overrides)",
                    wip=wip,
                )
                return EXIT_OK

        pre = jiri.integration_rev(fuchsia)
        tree_pre = jiri.tree_rev(fuchsia)
        update_secs = 0.0
        if not args.no_update:
            for attempt in range(args.update_retries + 1):
                t0 = time.monotonic()
                update_rc = run_cmd([str(jiri_bin), "update"])
                update_secs += time.monotonic() - t0
                if update_rc == 0:
                    break
                kind = jiri.classify_update_failure(log_tail())
                if kind != "transient" or attempt == args.update_retries:
                    report(
                        "update_failed",
                        f"jiri update rc={update_rc} ({kind}) after "
                        f"{attempt + 1} attempt(s)",
                        update_rc=update_rc,
                        update_secs=round(update_secs, 1),
                    )
                    return EXIT_PARTIAL
                log.write(
                    f"transient failure; retrying in {args.retry_wait_secs:.0f}s\n"
                )
                log.flush()
                time.sleep(args.retry_wait_secs)
        post = jiri.integration_rev(fuchsia)
        tree_post = jiri.tree_rev(fuchsia)
        pulled = jiri.commits_between(fuchsia, pre, post)

        builds = []
        for name in build_dirs:
            build_start = time.time()
            res = build.build_with_regen(
                fx,
                name,
                run=run_cmd,
                log_tail=log_tail,
            )
            if res.regen:
                log.write(
                    f"stale build.ninja input {res.regen}: ran fx gen "
                    f"(rc={res.regen_returncode}) and rebuilt\n"
                )
                log.flush()
            builds.append(
                {
                    "build_dir": name,
                    "build_start": round(build_start, 3),
                    "build_secs": round(res.secs, 1),
                    "build_exit": res.returncode,
                    "regen": res.regen,
                }
            )
            if res.returncode != 0:
                rc = EXIT_BUILD_FAILED

        summary = ", ".join(
            f"{b['build_dir']} exit {b['build_exit']} in {b['build_secs']}s"
            for b in builds
        )
        report(
            "ok" if rc == 0 else "build_failed",
            f"{pulled} commits pulled in {round(update_secs, 1)}s; {summary}",
            integration_pre=pre,
            integration_post=post,
            tree_pre=tree_pre,
            tree_post=tree_post,
            commits_pulled=pulled,
            update_secs=round(update_secs, 1),
            builds=builds,
            disk_free_gb=round(guard.disk_free_gb(fuchsia), 1),
        )
    return rc


def cmd_status(args: argparse.Namespace) -> int:
    """Report the last run's outcome and whether a run holds the lock."""
    path = args.status_file or status.default_status_path()
    lock = status.lock_holder(status.lock_path())
    try:
        doc = status.read_status(path)
    except status.StatusUnreadable as e:
        if args.json:
            print(json.dumps({"outcome": None, "empty": False, "error": str(e)}))
        else:
            print(e, file=sys.stderr)
        return EXIT_SETUP
    if args.json:
        print(
            json.dumps(
                {
                    "outcome": doc["outcome"] if doc else status.NEVER_RUN,
                    "empty": doc is None,
                    "status_file": str(path),
                    "last_run": doc,
                    "lock": lock,
                },
                indent=1,
            )
        )
    else:
        print(status.summary_line(doc, path))
        if lock:
            state = {True: "running", False: "NOT running: stale, remove it"}
            print(
                f"lock: {lock['path']} held by pid {lock['pid']} "
                f"({state.get(lock['pid_alive'], 'state unknown')})"
            )
    return EXIT_OK if doc else EXIT_EMPTY


# The systemd seam; tests replace it with a fake.
systemd_runner: systemd.Runner = systemd.run_command


def _print_actions(actions: list[str], dry_run: bool) -> None:
    for action in actions:
        print(f"would {action}" if dry_run else action)


def _install_config(args: argparse.Namespace, path: pathlib.Path) -> config.Config:
    """The existing config at `path` with this invocation's flags over it."""
    cfg = config.load(path) if path.exists() else None
    if cfg is None and args.fuchsia_dir is None:
        raise _UsageError("no config file yet: --fuchsia-dir is required")
    cfg = cfg or config.Config(fuchsia_dir=args.fuchsia_dir)
    if args.fuchsia_dir is not None:
        cfg.fuchsia_dir = args.fuchsia_dir.absolute()
    if args.build_dir:
        cfg.build_dirs = list(args.build_dir)
    if args.schedule is not None:
        cfg.schedule = args.schedule
    if args.min_free_gb is not None:
        cfg.min_free_gb = args.min_free_gb
    if args.prom_dir is not None:
        # An empty --prom-dir turns .prom output back off.
        cfg.prom_dir = None if args.prom_dir == _NO_DIR else args.prom_dir.absolute()
    try:
        return config.validate(cfg)
    except config.ConfigError as e:
        # The file itself loaded and validated above, so a flag is to blame.
        raise _UsageError(str(e)) from e


def _check_tree(cfg: config.Config) -> str | None:
    """Why a scheduled run of this config would fail at once, or None."""
    for tool in (".jiri_root/bin/jiri", "scripts/fx"):
        if not (cfg.fuchsia_dir / tool).exists():
            return f"{cfg.fuchsia_dir} is not a Fuchsia checkout: no {tool}"
    if not cfg.build_dirs and not (cfg.fuchsia_dir / ".fx-build-dir").exists():
        return "no --build-dir given and the tree has no .fx-build-dir"
    return None


def _conflict_message(e: systemd.UnitConflict) -> str:
    return (
        f"{e} exists and was not written by fx-updater; not touching it. "
        "Remove it or choose another --unit-name."
    )


def _systemctl_failed(e: systemd.SystemctlError) -> int:
    """Report a failed systemd call; the exit code says what had changed.

    "Something changed" is checked first: a half-done change is what the
    caller most needs to know, even when the cause is a missing binary.
    """
    if e.changed:
        print(f"{e}; files had already been changed", file=sys.stderr)
        return EXIT_PARTIAL
    if e.returncode == 127:
        print(f"{e}: is systemd installed?", file=sys.stderr)
        return EXIT_SETUP
    print(f"{e}; no file was changed", file=sys.stderr)
    return EXIT_BUSY


def cmd_install(args: argparse.Namespace) -> int:
    """Write the config and the units, and enable the timer."""
    if not systemd.valid_unit_name(args.unit_name):
        print(f"invalid --unit-name {args.unit_name!r}", file=sys.stderr)
        return EXIT_USAGE
    # Absolute: the unit runs in the tree, not where install ran, so a
    # relative --config would be looked up in the wrong directory.
    path = (args.config or config.default_path()).absolute()
    if config.has_control_char(str(path)):
        print("--config must not contain control characters", file=sys.stderr)
        return EXIT_USAGE
    try:
        cfg = _install_config(args, path)
    except _UsageError as e:
        print(e, file=sys.stderr)
        return EXIT_USAGE
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return EXIT_SETUP
    problem = _check_tree(cfg)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_SETUP
    unit_dir = systemd.unit_dir()
    try:
        systemd.check_available(systemd_runner)
        bad_schedule = systemd.check_calendar(systemd_runner, cfg.schedule)
        # Before the config is written, so a refused install changes nothing.
        systemd.check_no_conflict(args.unit_name, unit_dir)
    except systemd.SystemctlError as e:
        return _systemctl_failed(e)
    except systemd.UnitConflict as e:
        print(_conflict_message(e), file=sys.stderr)
        return EXIT_SETUP
    if bad_schedule:
        print(f"--schedule {cfg.schedule!r}: {bad_schedule}", file=sys.stderr)
        return EXIT_USAGE

    # The venv's interpreter, not its resolved target: resolving the symlink
    # would lose the venv and with it this package.
    python = str(pathlib.Path(sys.executable).absolute())
    if "/.cache/" in python or "/tmp/" in python:
        print(
            f"warning: the unit will run {python}, which looks like a "
            "temporary environment; install the tool with `uv tool install` "
            "and re-run install from there",
            file=sys.stderr,
        )
    exec_argv = [python, "-m", "fx_updater", "run", "--config", str(path)]
    service = systemd.render_service(exec_argv, cfg.fuchsia_dir)
    timer = systemd.render_timer(cfg.schedule)

    rendered = config.render(cfg)
    unchanged = path.exists() and path.read_text(encoding="utf-8") == rendered
    actions = [f"{'unchanged' if unchanged else 'write'} {path}"]
    if not args.dry_run and not unchanged:
        config.write(path, cfg)
    try:
        actions += systemd.install(
            args.unit_name,
            service,
            timer,
            run=systemd_runner,
            directory=unit_dir,
            dry_run=args.dry_run,
        )
    except systemd.UnitConflict as e:
        # Only if a unit appeared since check_no_conflict above.
        print(_conflict_message(e), file=sys.stderr)
        return EXIT_SETUP
    except systemd.SystemctlError as e:
        # The config file counts: it was rewritten even if the units were not.
        e.changed = e.changed or (not unchanged and not args.dry_run)
        return _systemctl_failed(e)
    _print_actions(actions, args.dry_run)
    if systemd.linger_enabled(systemd_runner, getpass.getuser()) is False:
        print(
            "warning: linger is off, so the timer only fires while you are "
            f"logged in. Enable it with: loginctl enable-linger {getpass.getuser()}",
            file=sys.stderr,
        )
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Disable the timer and remove the units; the config file is kept."""
    directory = systemd.unit_dir()
    try:
        actions = systemd.uninstall(
            args.unit_name,
            run=systemd_runner,
            directory=directory,
            dry_run=args.dry_run,
        )
    except systemd.UnitConflict as e:
        print(_conflict_message(e), file=sys.stderr)
        return EXIT_SETUP
    except systemd.SystemctlError as e:
        return _systemctl_failed(e)
    if not actions:
        print(f"not installed: no {args.unit_name} units in {directory}")
    _print_actions(actions, args.dry_run)
    return EXIT_OK


def emit_skill() -> int:
    """Print the tool's agent-facing doc, embedded so it cannot drift."""
    text = files("fx_updater").joinpath("SKILL.md").read_text(encoding="utf-8")
    sys.stdout.write(text.replace("{{VERSION}}", __version__))
    return EXIT_OK


def _add_config_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=None,
        help="config file (default: $XDG_CONFIG_HOME/fx-updater/config.toml)",
    )


def _add_unit_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--unit-name",
        default=systemd.DEFAULT_UNIT_NAME,
        help=f"systemd unit name, without suffix (default {systemd.DEFAULT_UNIT_NAME})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the actions, in order, without taking them",
    )


def _add_tree_flags(parser: argparse.ArgumentParser, verb: str) -> None:
    parser.add_argument(
        "--fuchsia-dir",
        type=pathlib.Path,
        default=None,
        help=f"the Fuchsia checkout to {verb}",
    )
    parser.add_argument(
        "--build-dir",
        action="append",
        default=[],
        help="out dir name under out/ (repeatable); default: the config's "
        "build_dirs, else .fx-build-dir",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=None,
        help="skip the run if the tree's filesystem has less free "
        f"(default: the config's value, else {config.DEFAULT_MIN_FREE_GB:.0f})",
    )
    parser.add_argument(
        "--prom-dir",
        type=_optional_dir,
        default=None,
        help='write fx_updater.prom here after every run; "" for none '
        "(default: the config's prom_dir, else none)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fx-updater",
        description=__doc__.splitlines()[0],
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--skill",
        action="store_true",
        help="print this tool's agent-facing usage document and exit",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser(
        "run",
        help="update and build the tree once",
        description=__doc__.split("\n\n", 1)[1],
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_config_flag(r)
    _add_tree_flags(r, "update (default: config, then the current directory)")
    r.add_argument(
        "--no-update", action="store_true", help="skip jiri update; build only"
    )
    r.add_argument(
        "--force-update",
        action="store_true",
        help="update even when jiri status reports work in progress",
    )
    r.add_argument(
        "--update-retries",
        type=_non_negative_int,
        default=2,
        help="retries for network-shaped jiri update failures (default 2)",
    )
    r.add_argument("--retry-wait-secs", type=float, default=120.0)
    r.add_argument(
        "--status-file",
        type=pathlib.Path,
        default=None,
        help="status document, overwritten each run "
        "(default: $XDG_STATE_HOME/fx-updater/status.json)",
    )
    r.add_argument(
        "--log-dir",
        type=pathlib.Path,
        default=None,
        help="per-run logs (default: $XDG_STATE_HOME/fx-updater/logs)",
    )
    r.set_defaults(func=cmd_run)

    s = sub.add_parser(
        "status",
        help="the last run's outcome, and whether a run is in progress",
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument(
        "--json",
        action="store_true",
        help="one JSON document on stdout; branch on its fields, not the exit code",
    )
    s.add_argument(
        "--status-file",
        type=pathlib.Path,
        default=None,
        help="(default: $XDG_STATE_HOME/fx-updater/status.json)",
    )
    s.set_defaults(func=cmd_status)

    i = sub.add_parser(
        "install",
        help="write the config and a systemd user timer that runs `run`",
        description="Write the config file (flags over any existing one), "
        "then <unit-name>.service and .timer, and enable the timer.",
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_config_flag(i)
    _add_tree_flags(i, "keep updated (required the first time)")
    i.add_argument(
        "--schedule",
        default=None,
        help="when to run, in systemd OnCalendar syntax "
        f"(default {config.DEFAULT_SCHEDULE!r})",
    )
    _add_unit_flags(i)
    i.set_defaults(func=cmd_install)

    u = sub.add_parser(
        "uninstall",
        help="disable the timer and remove the units (keeps the config)",
        epilog=_EXIT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_unit_flags(u)
    u.set_defaults(func=cmd_uninstall)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.skill:
        return emit_skill()
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
