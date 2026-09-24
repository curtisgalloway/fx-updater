# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""fx-updater - keep a Fuchsia checkout updated and built, with guardrails.

`fx-updater run` performs one unattended update-and-build:

  fx-updater run                           # tree = current dir, fx default build dir
  fx-updater run --fuchsia-dir ~/fuchsia --build-dir core.x64 --build-dir bringup.x64
  fx-updater run --no-update               # build only; never checks for WIP

Order of operations, each step able to stop the run:

1. The lock. A second concurrent run refuses to start.
2. The disk floor (`--min-free-gb`). Below it, nothing is touched.
3. The WIP guard (`jiri status -check-head=false`). Mode-only dirt is
   restored; anything else skips the run. `--force-update` overrides and
   `--no-update` never checks.
4. `jiri update`, retrying network-shaped failures only.
5. `fx build` per build dir, with one `fx gen` if the graph is stale.

Every outcome is written to the status document (see fx_updater/status.py).
A WIP skip exits 0: a dirty tree on most mornings must not read as a failed
scheduled job, and the status document is the record.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import subprocess
import sys
import time
from importlib.resources import files

from fx_updater import __version__
from fx_updater import build
from fx_updater import guard
from fx_updater import jiri
from fx_updater import status

# Exit statuses. These match the script this tool was extracted from, so the
# behavior is unchanged; the documented contract replaces them later.
EXIT_OK = 0
EXIT_SETUP = 1  # jiri/fx missing, or no build dir to build
EXIT_LOCKED = 2  # another run holds the lock
EXIT_DISK = 3  # below the disk floor
EXIT_STATUS_FAILED = 4  # `jiri status` itself failed
# update_failed and build_failed exit with jiri's or fx's own return code.

_TAIL = 4000


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


def cmd_run(args: argparse.Namespace) -> int:
    """One update-and-build. Returns the process exit status."""
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
        print(f"lock exists ({e}); another run is in progress?", file=sys.stderr)
        return EXIT_LOCKED


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
            return EXIT_DISK

        if not args.no_update and not args.force_update:
            st = _jiri_status(jiri_bin, fuchsia, log)
            if st.returncode != 0:
                report(
                    "skipped_wip",
                    "jiri status itself failed; treating the tree as "
                    "work-in-progress and not updating",
                    detail=(st.stdout + st.stderr)[-2000:],
                )
                return EXIT_STATUS_FAILED
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
                        update_secs=round(update_secs, 1),
                    )
                    return update_rc
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
                rc = res.returncode

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


def emit_skill() -> int:
    """Print the tool's agent-facing doc, embedded so it cannot drift."""
    text = files("fx_updater").joinpath("SKILL.md").read_text(encoding="utf-8")
    sys.stdout.write(text.replace("{{VERSION}}", __version__))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fx-updater",
        description=__doc__.splitlines()[0],
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    r.add_argument(
        "--fuchsia-dir",
        type=pathlib.Path,
        default=pathlib.Path.cwd(),
        help="the Fuchsia checkout (default: the current directory)",
    )
    r.add_argument(
        "--build-dir",
        action="append",
        default=[],
        help="out dir name under out/ (repeatable); default: .fx-build-dir",
    )
    r.add_argument(
        "--no-update", action="store_true", help="skip jiri update; build only"
    )
    r.add_argument(
        "--force-update",
        action="store_true",
        help="update even when jiri status reports work in progress",
    )
    r.add_argument(
        "--min-free-gb",
        type=float,
        default=100.0,
        help="skip the run if the tree's filesystem has less free (default 100)",
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
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.skill:
        return emit_skill()
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_SETUP
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
