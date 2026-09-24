# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""`status`, `install`, `uninstall`, the .prom opt-in, and the exit contract."""

import json
import os
import pathlib
import subprocess
import sys

import pytest

from fx_updater import cli
from fx_updater import config
from fx_updater import promfile
from fx_updater import status
from fx_updater import systemd


@pytest.fixture(name="home", autouse=True)
def fixture_home(tmp_path, monkeypatch):
    """Private state and config dirs; the host's real ones are never read."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def _main(capsys, *argv):
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


# --- status: one golden line per outcome --------------------------------------

_DOCS = {
    "ok": (
        {"reason": "12 commits pulled in 40.0s; core.x64 exit 0 in 900.0s"},
        "ok 2026-01-02T05:30:00: 12 commits pulled in 40.0s; "
        "core.x64 exit 0 in 900.0s",
    ),
    "skipped_wip": (
        {"reason": "work in progress in the checkout; not updating"},
        "skipped_wip 2026-01-02T05:30:00: work in progress in the checkout; "
        "not updating",
    ),
    "skipped_disk": (
        {"reason": "80 GB free is below the 100 GB floor; not touching the tree"},
        "skipped_disk 2026-01-02T05:30:00: 80 GB free is below the 100 GB "
        "floor; not touching the tree",
    ),
    "update_failed": (
        {"reason": "jiri update rc=1 (hard) after 1 attempt(s)"},
        "update_failed 2026-01-02T05:30:00: jiri update rc=1 (hard) after 1 "
        "attempt(s)",
    ),
    "status_failed": (
        {
            "reason": "jiri status itself failed; the work-in-progress check could"
            " not run, so the tree was not touched"
        },
        "status_failed 2026-01-02T05:30:00: jiri status itself failed; the "
        "work-in-progress check could not run, so the tree was not touched",
    ),
    "build_failed": (
        {"reason": "0 commits pulled in 0.0s; core.x64 exit 1 in 3.0s"},
        "build_failed 2026-01-02T05:30:00: 0 commits pulled in 0.0s; "
        "core.x64 exit 1 in 3.0s",
    ),
}


@pytest.mark.parametrize("outcome", sorted(_DOCS))
def test_status_golden_line_per_outcome(capsys, outcome):
    extra, expected = _DOCS[outcome]
    status.write_status(
        status.default_status_path(),
        {"timestamp": "2026-01-02T05:30:00", "outcome": outcome, **extra},
    )
    rc, out, _ = _main(capsys, "status")
    assert rc == cli.EXIT_OK
    assert out == expected + "\n"


def test_status_golden_never_run(capsys):
    rc, out, _ = _main(capsys, "status")
    assert rc == cli.EXIT_EMPTY
    assert out == (f"never_run: no status document at {status.default_status_path()}\n")


def test_status_json_is_one_document_and_types_the_empty_case(capsys):
    rc, out, err = _main(capsys, "status", "--json")
    assert rc == cli.EXIT_EMPTY
    doc = json.loads(out)
    assert doc["outcome"] == "never_run"
    assert doc["empty"] is True
    assert doc["last_run"] is None
    assert err == ""


def test_status_json_carries_the_last_run(capsys):
    status.write_status(
        status.default_status_path(),
        {"timestamp": "2026-01-02T05:30:00", "outcome": "ok", "reason": "r"},
    )
    rc, out, _ = _main(capsys, "status", "--json")
    doc = json.loads(out)
    assert rc == 0
    assert (doc["outcome"], doc["empty"]) == ("ok", False)
    assert doc["last_run"]["reason"] == "r"
    assert doc["lock"] is None


def test_status_reports_a_held_lock(capsys, hold_lock):
    lock = status.lock_path()
    with hold_lock(lock) as holder:
        _, out, _ = _main(capsys, "status")
        assert out.splitlines()[1] == f"lock: {lock} held by pid {holder.pid} (running)"
        _, out, _ = _main(capsys, "status", "--json")
        assert json.loads(out)["lock"] == {
            "path": str(lock),
            "pid": holder.pid,
            "pid_alive": True,
        }


def test_status_ignores_a_lock_file_nobody_holds(capsys):
    """What M2 called a stale lock: a file left by a dead run. It blocks nothing."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    lock = status.lock_path()
    lock.parent.mkdir(parents=True)
    lock.write_text(str(proc.pid))
    _, out, _ = _main(capsys, "status", "--json")
    assert json.loads(out)["lock"] is None
    _, out, _ = _main(capsys, "status")
    assert "lock:" not in out


def test_unreadable_status_exits_setup(capsys):
    path = status.default_status_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    rc, out, _ = _main(capsys, "status", "--json")
    assert rc == cli.EXIT_SETUP
    doc = json.loads(out)
    assert (doc["outcome"], doc["empty"]) == (None, False)
    assert "error" in doc


# --- the .prom opt-in -----------------------------------------------------------


@pytest.fixture(name="tree")
def fixture_tree(home):
    root = home / "fuchsia"
    (root / ".jiri_root/bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    for tool in (root / ".jiri_root/bin/jiri", root / "scripts/fx"):
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    (root / ".fx-build-dir").write_text("out/core.x64\n")
    return root


def _prom_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.rglob("*.prom"))


def test_no_prom_file_without_a_prom_dir(capsys, home, tree):
    rc, _, _ = _main(capsys, "run", "--fuchsia-dir", str(tree), "--min-free-gb", "0")
    assert rc == 0
    assert _prom_files(home) == []


def test_prom_dir_flag_writes_the_gauges(capsys, home, tree):
    prom = home / "prom"
    prom.mkdir()
    _main(
        capsys,
        "run",
        "--fuchsia-dir",
        str(tree),
        "--min-free-gb",
        "0",
        "--prom-dir",
        str(prom),
    )
    assert _prom_files(home) == [prom / "fx_updater.prom"]
    text = (prom / "fx_updater.prom").read_text()
    assert "fx_updater_last_run_ok{} 1\n" in text
    assert 'fx_updater_last_run_outcome{outcome="ok"} 1\n' in text
    assert 'fx_updater_build_exit{build_dir="core.x64"} 0\n' in text


def test_prom_dir_from_config_and_a_skip_is_reported(capsys, home, tree):
    prom = home / "prom"
    prom.mkdir()
    config.write(
        config.default_path(),
        config.Config(fuchsia_dir=tree, min_free_gb=1e12, prom_dir=prom),
    )
    rc, _, _ = _main(capsys, "run")
    assert rc == cli.EXIT_SETUP
    text = (prom / "fx_updater.prom").read_text()
    assert "fx_updater_last_run_skipped{} 1\n" in text
    assert "fx_updater_last_run_ok{} 0\n" in text


def test_prom_status_failed_is_neither_ok_nor_skipped():
    """So the failed-run alert fires: a broken jiri status is not a skip."""
    samples = promfile.samples_for(
        {"timestamp": "2026-01-02T05:30:00", "outcome": "status_failed"}
    )
    got = {s.name: s.value for s in samples if not s.labels}
    assert got["fx_updater_last_run_ok"] is False
    assert got["fx_updater_last_run_skipped"] is False


def test_prom_samples_for_a_failed_build():
    samples = promfile.samples_for(
        {
            "timestamp": "2026-01-02T05:30:00",
            "outcome": "build_failed",
            "builds": [
                {"build_dir": "a", "build_secs": 3.0, "build_exit": 1, "regen": "x"}
            ],
        }
    )
    got = {(s.name, tuple(s.labels.items())): s.value for s in samples}
    assert got[("fx_updater_last_run_ok", ())] is False
    assert got[("fx_updater_last_run_skipped", ())] is False
    assert got[("fx_updater_build_regen", (("build_dir", "a"),))] is True


def test_unwritable_prom_dir_warns_and_the_run_still_succeeds(capsys, home, tree):
    rc, _, err = _main(
        capsys,
        "run",
        "--fuchsia-dir",
        str(tree),
        "--min-free-gb",
        "0",
        "--prom-dir",
        str(home / "missing"),
    )
    assert rc == 0
    assert "warning: could not write" in err


# --- run reads the config ---------------------------------------------------------


def test_run_takes_tree_and_build_dirs_from_config(capsys, tree):
    config.write(
        config.default_path(),
        config.Config(fuchsia_dir=tree, build_dirs=["x"], min_free_gb=0),
    )
    rc, _, _ = _main(capsys, "run", "--no-update")
    assert rc == 0
    doc = json.loads(status.default_status_path().read_text())
    assert doc["fuchsia_dir"] == str(tree)
    assert [b["build_dir"] for b in doc["builds"]] == ["x"]


def test_a_flag_beats_the_config(capsys, tree):
    config.write(
        config.default_path(),
        config.Config(fuchsia_dir=tree, build_dirs=["x"], min_free_gb=0),
    )
    _main(capsys, "run", "--no-update", "--build-dir", "y")
    doc = json.loads(status.default_status_path().read_text())
    assert [b["build_dir"] for b in doc["builds"]] == ["y"]


def test_a_default_config_for_another_tree_is_ignored(capsys, home, tree):
    other = home / "other"
    other.mkdir()
    config.write(
        config.default_path(),
        config.Config(fuchsia_dir=other, build_dirs=["x"], prom_dir=home),
    )
    rc, _, _ = _main(
        capsys, "run", "--fuchsia-dir", str(tree), "--no-update", "--min-free-gb", "0"
    )
    assert rc == 0
    doc = json.loads(status.default_status_path().read_text())
    assert [b["build_dir"] for b in doc["builds"]] == ["core.x64"]
    assert _prom_files(home) == []


def test_a_broken_default_config_does_not_break_a_flags_only_run(capsys, tree):
    path = config.default_path()
    path.parent.mkdir(parents=True)
    path.write_text("bogus = 1\n")
    rc, _, err = _main(
        capsys, "run", "--fuchsia-dir", str(tree), "--no-update", "--min-free-gb", "0"
    )
    assert rc == 0
    assert "ignoring the default config" in err


@pytest.mark.parametrize("name", ["../x", "a/b", ".."])
def test_run_rejects_a_build_dir_that_is_a_path(capsys, tree, name):
    rc, _, err = _main(capsys, "run", "--fuchsia-dir", str(tree), "--build-dir", name)
    assert rc == cli.EXIT_USAGE
    assert "--build-dir" in err


def test_an_explicit_missing_config_exits_setup(capsys, home):
    rc, _, err = _main(capsys, "run", "--config", str(home / "nope.toml"))
    assert rc == cli.EXIT_SETUP
    assert "no config file" in err


def test_a_broken_default_config_exits_setup(capsys):
    path = config.default_path()
    path.parent.mkdir(parents=True)
    path.write_text("bogus = 1\n")
    rc, _, err = _main(capsys, "run")
    assert rc == cli.EXIT_SETUP
    assert "unknown key" in err


# --- install / uninstall -------------------------------------------------------------


class FakeSystemd:

    def __init__(self, rcs=None, linger="yes"):
        self.calls: list[list[str]] = []
        self.rcs = rcs or {}
        self.linger = linger

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "loginctl":
            return 0, self.linger + "\n"
        return self.rcs.get(cmd[0] + " " + cmd[-1], self.rcs.get(cmd[0], 0)), "err"


@pytest.fixture(name="fake_systemd", autouse=True)
def fixture_fake_systemd(monkeypatch):
    fake = FakeSystemd()
    monkeypatch.setattr(cli, "systemd_runner", fake)
    return fake


def _units(home):
    d = home / "config/systemd/user"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_install_writes_config_and_units(capsys, home, tree, fake_systemd):
    rc, out, err = _main(
        capsys,
        "install",
        "--fuchsia-dir",
        str(tree),
        "--build-dir",
        "core.x64",
        "--schedule",
        "*-*-* 04:00:00",
    )
    assert rc == 0, err
    cfg = config.load(config.default_path())
    assert (cfg.fuchsia_dir, cfg.build_dirs, cfg.schedule) == (
        tree,
        ["core.x64"],
        "*-*-* 04:00:00",
    )
    assert _units(home) == ["fx-updater.service", "fx-updater.timer"]
    service = (home / "config/systemd/user/fx-updater.service").read_text()
    assert f"--config {config.default_path()}" in service
    assert ["systemctl", "--user", "enable", "--now", "fx-updater.timer"] in (
        fake_systemd.calls
    )
    assert out.splitlines()[0] == f"write {config.default_path()}"
    assert err == ""


def test_install_twice_is_unchanged(capsys, tree):
    _main(capsys, "install", "--fuchsia-dir", str(tree))
    rc, out, _ = _main(capsys, "install")
    assert rc == 0
    assert [ln.split()[0] for ln in out.splitlines()[:3]] == ["unchanged"] * 3


def test_install_dry_run_touches_nothing(capsys, home, tree, fake_systemd):
    rc, out, _ = _main(capsys, "install", "--fuchsia-dir", str(tree), "--dry-run")
    assert rc == 0
    assert not config.default_path().exists()
    assert _units(home) == []
    assert all(ln.startswith("would ") for ln in out.splitlines())
    assert [c for c in fake_systemd.calls if c[0] == "systemctl"] == [
        ["systemctl", "--version"]
    ]


def test_install_without_a_tree_the_first_time_is_usage(capsys):
    rc, _, err = _main(capsys, "install")
    assert rc == cli.EXIT_USAGE
    assert "--fuchsia-dir is required" in err


def test_install_refuses_a_non_checkout(capsys, home):
    rc, _, err = _main(capsys, "install", "--fuchsia-dir", str(home))
    assert rc == cli.EXIT_SETUP
    assert "not a Fuchsia checkout" in err
    assert not config.default_path().exists()


def test_install_refuses_a_tree_with_no_build_dir(capsys, tree):
    (tree / ".fx-build-dir").unlink()
    rc, _, err = _main(capsys, "install", "--fuchsia-dir", str(tree))
    assert rc == cli.EXIT_SETUP
    assert ".fx-build-dir" in err


def test_install_refuses_a_bad_schedule(capsys, tree, monkeypatch):
    monkeypatch.setattr(cli, "systemd_runner", FakeSystemd({"systemd-analyze": 1}))
    rc, _, err = _main(
        capsys, "install", "--fuchsia-dir", str(tree), "--schedule", "someday"
    )
    assert rc == cli.EXIT_USAGE
    assert "--schedule 'someday'" in err


def test_install_refuses_a_bad_unit_name(capsys, tree):
    rc, _, _ = _main(
        capsys, "install", "--fuchsia-dir", str(tree), "--unit-name", "a/b"
    )
    assert rc == cli.EXIT_USAGE


def test_install_refuses_to_clobber_a_foreign_unit(capsys, home, tree):
    d = home / "config/systemd/user"
    d.mkdir(parents=True)
    (d / "fx-updater.timer").write_text("[Timer]\n")
    rc, _, err = _main(capsys, "install", "--fuchsia-dir", str(tree))
    assert rc == cli.EXIT_SETUP
    assert "not written by fx-updater" in err
    assert (d / "fx-updater.timer").read_text() == "[Timer]\n"
    # A refused install changes nothing, the config included.
    assert not config.default_path().exists()


def test_install_with_a_failing_systemctl_is_partial(capsys, tree, monkeypatch):
    monkeypatch.setattr(
        cli, "systemd_runner", FakeSystemd({"systemctl fx-updater.timer": 1})
    )
    rc, _, err = _main(capsys, "install", "--fuchsia-dir", str(tree))
    assert rc == cli.EXIT_PARTIAL
    assert "files had already been changed" in err


def test_a_failing_systemctl_on_a_no_op_reinstall_is_busy(capsys, tree, monkeypatch):
    _main(capsys, "install", "--fuchsia-dir", str(tree))
    monkeypatch.setattr(
        cli, "systemd_runner", FakeSystemd({"systemctl fx-updater.timer": 1})
    )
    rc, _, err = _main(capsys, "install")
    assert rc == cli.EXIT_BUSY
    assert "no file was changed" in err


def test_a_failing_systemctl_after_a_config_only_change_is_partial(
    capsys, tree, monkeypatch
):
    """Units byte-identical, config rewritten: something did change."""
    _main(capsys, "install", "--fuchsia-dir", str(tree))
    monkeypatch.setattr(
        cli, "systemd_runner", FakeSystemd({"systemctl fx-updater.timer": 1})
    )
    rc, _, _ = _main(capsys, "install", "--min-free-gb", "50")
    assert rc == cli.EXIT_PARTIAL


@pytest.mark.parametrize(
    "flags", [["--build-dir", "../x"], ["--min-free-gb", "-1"], ["--schedule", " "]]
)
def test_install_bad_flag_values_are_usage_errors(capsys, tree, flags):
    rc, _, _ = _main(capsys, "install", "--fuchsia-dir", str(tree), *flags)
    assert rc == cli.EXIT_USAGE
    assert not config.default_path().exists()


def test_install_over_a_broken_config_file_is_setup(capsys, tree):
    path = config.default_path()
    path.parent.mkdir(parents=True)
    path.write_text("bogus = 1\n")
    rc, _, _ = _main(capsys, "install", "--fuchsia-dir", str(tree))
    assert rc == cli.EXIT_SETUP


def test_uninstall_whose_disable_fails_is_busy(capsys, home, tree, monkeypatch):
    _main(capsys, "install", "--fuchsia-dir", str(tree))
    monkeypatch.setattr(
        cli, "systemd_runner", FakeSystemd({"systemctl fx-updater.timer": 1})
    )
    rc, _, _ = _main(capsys, "uninstall")
    assert rc == cli.EXIT_BUSY
    assert _units(home) == ["fx-updater.service", "fx-updater.timer"]


def test_missing_systemd_analyze_is_setup_not_usage(capsys, tree, monkeypatch):
    monkeypatch.setattr(cli, "systemd_runner", FakeSystemd({"systemd-analyze": 127}))
    rc, _, _ = _main(capsys, "install", "--fuchsia-dir", str(tree))
    assert rc == cli.EXIT_SETUP
    assert not config.default_path().exists()


def test_relative_config_path_is_made_absolute(capsys, home, tree, monkeypatch):
    """The unit runs in the tree; a relative --config would miss there."""
    monkeypatch.chdir(home)
    rc, _, _ = _main(
        capsys, "install", "--fuchsia-dir", str(tree), "--config", "fx.toml"
    )
    assert rc == 0
    service = (home / "config/systemd/user/fx-updater.service").read_text()
    assert f"--config {home / 'fx.toml'}" in service
    assert (home / "fx.toml").exists()


def test_install_warns_about_a_temporary_interpreter(capsys, tree, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/x/.cache/uv/env/bin/python")
    rc, _, err = _main(capsys, "install", "--fuchsia-dir", str(tree))
    assert rc == 0
    assert "temporary environment" in err


def test_install_without_systemctl_is_setup(capsys, tree, monkeypatch):
    monkeypatch.setattr(cli, "systemd_runner", FakeSystemd({"systemctl": 127}))
    rc, _, _ = _main(capsys, "install", "--fuchsia-dir", str(tree))
    assert rc == cli.EXIT_SETUP
    assert not config.default_path().exists()


def test_install_warns_when_linger_is_off(capsys, tree, monkeypatch):
    monkeypatch.setattr(cli, "systemd_runner", FakeSystemd(linger="no"))
    rc, _, err = _main(capsys, "install", "--fuchsia-dir", str(tree))
    assert rc == 0
    assert "linger is off" in err


def test_empty_prom_dir_turns_prom_off(capsys, home, tree):
    _main(capsys, "install", "--fuchsia-dir", str(tree), "--prom-dir", str(home))
    assert config.load(config.default_path()).prom_dir == home
    _main(capsys, "install", "--prom-dir", "")
    assert config.load(config.default_path()).prom_dir is None


def test_uninstall_removes_units_and_keeps_config(capsys, home, tree):
    _main(capsys, "install", "--fuchsia-dir", str(tree))
    rc, out, _ = _main(capsys, "uninstall")
    assert rc == 0
    assert _units(home) == []
    assert config.default_path().exists()
    assert "remove" in out


def test_uninstall_when_not_installed_says_so(capsys):
    rc, out, _ = _main(capsys, "uninstall")
    assert rc == 0
    assert out.startswith("not installed: no fx-updater units")


def test_scratch_unit_name_leaves_the_default_alone(capsys, home, tree):
    _main(capsys, "install", "--fuchsia-dir", str(tree), "--unit-name", "fxu-test")
    assert _units(home) == ["fxu-test.service", "fxu-test.timer"]
    _main(capsys, "uninstall", "--unit-name", "fxu-test")
    assert _units(home) == []


# --- the contract, driven the way a program drives it -------------------------------


def test_no_subcommand_is_a_usage_error(capsys):
    assert _main(capsys)[0] == cli.EXIT_USAGE


def test_help_documents_every_exit_code():
    text = cli.build_parser().format_help()
    for code in (0, 1, 2, 3, 20, 21, 100):
        assert f"\n  {code} " in text


def test_no_exit_code_in_the_reserved_range():
    codes = [v for k, v in vars(cli).items() if k.startswith("EXIT_")]
    assert all(0 <= c < 125 for c in codes)


def test_installed_entry_point_piped_with_stdin_closed():
    """Piped stdout, closed stdin, NO_COLOR: JSON only, and no hang."""
    script = pathlib.Path(sys.executable).parent / "fx-updater"
    env = dict(os.environ, NO_COLOR="1")
    done = subprocess.run(
        [str(script), "status", "--json"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert done.returncode == cli.EXIT_EMPTY
    assert json.loads(done.stdout)["outcome"] == "never_run"


def test_python_dash_m_is_the_units_entry_point():
    done = subprocess.run(
        [sys.executable, "-m", "fx_updater", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert done.stdout.startswith("fx-updater ")


def test_unit_dir_follows_xdg(home):
    assert systemd.unit_dir() == home / "config/systemd/user"


def test_empty_prom_dir_on_run_writes_nothing(capsys, home, tree, monkeypatch):
    """`--prom-dir ""` must not mean the current directory."""
    monkeypatch.chdir(home)
    rc, _, _ = _main(
        capsys,
        "run",
        "--fuchsia-dir",
        str(tree),
        "--min-free-gb",
        "0",
        "--prom-dir",
        "",
    )
    assert rc == 0
    assert _prom_files(home) == []


def test_a_change_outranks_a_missing_binary():
    err = systemd.SystemctlError(["systemctl"], 127, "", changed=True)
    assert cli._systemctl_failed(err) == cli.EXIT_PARTIAL  # pylint: disable=protected-access
