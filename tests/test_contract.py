# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""The consumer contract: the lock, the status schema, and the hook.

These are the promises docs/contract.md makes to other programs, so each
test is phrased as what a consumer observes, not how `run` achieves it.
"""

import json
import pathlib
import shlex
import sys

import pytest

from fx_updater import cli
from fx_updater import contract
from fx_updater import promfile
from fx_updater import status

from test_cli import _calls, _fake, _run, _status, fixture_tree  # noqa: F401  # pylint: disable=unused-import

# A hook that records what it saw, as a consumer would see it.
_PROBE = """
import json, os, pathlib, sys
from fx_updater import contract
seen = {
    "busy": contract.tree_busy(),
    "env": {k: os.environ.get(k) for k in contract.HOOK_ENV},
    "status": contract.read_status(pathlib.Path(os.environ["FX_UPDATER_STATUS_FILE"])),
    "cwd": os.getcwd(),
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(seen))
sys.exit(int(sys.argv[2]))
"""


def _probe_hook(tmp_path, rc=0) -> tuple[str, pathlib.Path]:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE)
    out = tmp_path / "seen.json"
    return shlex.join([sys.executable, str(script), str(out), str(rc)]), out


def test_hook_runs_under_the_lock_and_sees_the_documented_env(tree, tmp_path):
    hook, out = _probe_hook(tmp_path)
    assert _run(tree, "--hook", hook) == 0
    seen = json.loads(out.read_text())
    assert seen["busy"] is True, "a consumer must see the tree busy during the hook"
    env = seen["env"]
    assert set(env) == set(contract.HOOK_ENV)
    assert env["FX_UPDATER_SCHEMA_VERSION"] == str(contract.SCHEMA_VERSION)
    assert env["FX_UPDATER_OUTCOME"] == "ok"
    assert env["FX_UPDATER_FUCHSIA_DIR"] == str(tree.resolve())
    assert env["FX_UPDATER_BUILD_DIRS"] == "core.x64"
    assert env["FX_UPDATER_STATUS_FILE"] == str(status.default_status_path())
    assert float(env["FX_UPDATER_RUN_START"]) > 0
    assert pathlib.Path(env["FX_UPDATER_LOG"]).exists()
    for key in ("PRE", "POST"):
        assert env[f"FX_UPDATER_INTEGRATION_{key}"] is not None
        assert env[f"FX_UPDATER_TREE_{key}"] is not None
    assert seen["cwd"] == str(tree.resolve())
    # The hook read the document this run had already written.
    assert seen["status"]["outcome"] == "ok"
    assert "hook" not in seen["status"]
    # ...and the run then recorded the hook's result in it.
    doc = _status()
    assert doc["hook"]["exit"] == 0
    assert doc["hook"]["argv"] == shlex.split(hook)
    assert not contract.validate_status(doc)
    assert not contract.tree_busy()


def test_hook_failure_changes_neither_outcome_nor_exit(tree, tmp_path, capsys):
    hook, _ = _probe_hook(tmp_path, rc=3)
    assert _run(tree, "--hook", hook) == 0
    doc = _status()
    assert doc["outcome"] == "ok"
    assert doc["hook"]["exit"] == 3
    assert "warning: hook failed: exited 3" in capsys.readouterr().err
    log_text = pathlib.Path(doc["log"]).read_text(encoding="utf-8")
    assert "hook failed: exited 3" in log_text


def test_hook_that_cannot_start_is_recorded(tree):
    assert _run(tree, "--hook", "/nonexistent/hook") == 0
    hook = _status()["hook"]
    assert hook["exit"] is None
    assert hook["error"].startswith("could not start")


def test_hook_that_hangs_is_killed_and_the_lock_released(tree, monkeypatch):
    monkeypatch.setattr(cli, "HOOK_TIMEOUT_SECS", 0.5)
    assert _run(tree, "--hook", "sleep 30") == 0
    hook = _status()["hook"]
    assert hook["exit"] is None
    assert "timed out" in hook["error"]
    assert not contract.tree_busy()


def test_hook_runs_after_a_skip_too(tree, tmp_path):
    _fake(tree, "status", ".: \nBranch: main\n M src/mine.cc\n")
    hook, out = _probe_hook(tmp_path)
    assert _run(tree, "--hook", hook) == 0
    env = json.loads(out.read_text())["env"]
    assert env["FX_UPDATER_OUTCOME"] == "skipped_wip"
    assert env["FX_UPDATER_INTEGRATION_POST"] == ""


def test_hook_does_not_mask_a_build_failure(tree, tmp_path):
    _fake(tree, "build_rc", "1")
    hook, out = _probe_hook(tmp_path)
    assert _run(tree, "--hook", hook) == cli.EXIT_BUILD_FAILED
    assert json.loads(out.read_text())["env"]["FX_UPDATER_OUTCOME"] == "build_failed"


def test_a_daemon_the_hook_leaves_running_does_not_hold_the_lock(tree):
    """The lock fd is not inherited: long-lived build daemons are a real case."""
    assert _run(tree, "--hook", "sh -c 'sleep 30 >/dev/null 2>&1 &'") == 0
    assert _status()["hook"]["exit"] == 0
    assert not contract.tree_busy()


def test_hook_comes_from_the_config_and_an_empty_flag_disables_it(tree, tmp_path):
    hook, out = _probe_hook(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"fuchsia_dir = {json.dumps(str(tree))}\n"
        f"min_free_gb = 0\npost_build_hook = {json.dumps(hook)}\n"
    )
    assert cli.main(["run", "--config", str(cfg)]) == 0
    assert out.exists()
    out.unlink()
    assert cli.main(["run", "--config", str(cfg), "--hook", ""]) == 0
    assert not out.exists()
    assert "hook" not in _status()


def test_unbalanced_hook_quote_is_a_usage_error(tree):
    assert _run(tree, "--hook", "echo 'oops") == cli.EXIT_USAGE


def test_hook_ok_gauge(tree, tmp_path):
    prom = tmp_path / "prom"
    prom.mkdir()
    assert _run(tree, "--hook", "false", "--prom-dir", str(prom)) == 0
    text = (prom / promfile.FILENAME).read_text()
    assert "fx_updater_hook_ok{} 0" in text
    assert "fx_updater_last_run_ok{} 1" in text


# --- every document a run writes conforms to the schema ----------------------


@pytest.mark.parametrize(
    "setup,extra",
    [
        ({}, ()),
        ({"status": ".: \nBranch: main\n M src/mine.cc\n"}, ()),
        ({"status_rc": "1"}, ()),
        ({"update_rcs": "1\n", "update_msg": "fatal: merge conflict\n"}, ()),
        ({"build_rc": "1"}, ()),
        ({}, ("--min-free-gb", "1e12")),
    ],
    ids=[
        "ok",
        "skipped_wip",
        "status_failed",
        "update_failed",
        "build_failed",
        "skipped_disk",
    ],
)
def test_every_outcome_writes_a_valid_document(tree, setup, extra):
    for name, text in setup.items():
        _fake(tree, name, text)
    _run(tree, *extra)
    doc = _status()
    assert not contract.validate_status(doc), doc
    assert doc["schema_version"] == contract.SCHEMA_VERSION


def test_validate_names_each_problem():
    good = {
        "schema_version": 1,
        "timestamp": "2026-01-02T05:30:00",
        "outcome": "ok",
        "reason": "r",
        "log": "/l",
        "fuchsia_dir": "/f",
        "builds": [
            {
                "build_dir": "a",
                "build_start": 1.0,
                "build_secs": 2,
                "build_exit": 0,
                "regen": None,
            }
        ],
    }
    assert not contract.validate_status(good)
    assert contract.validate_status([]) == ["not a JSON object"]
    bad = dict(good, outcome="exploded", commits_pulled=True, schema_version=2)
    del bad["log"]
    bad["builds"] = [{"build_dir": "a"}, "x"]
    errors = contract.validate_status(bad)
    assert "missing log" in errors
    assert "unknown outcome 'exploded'" in errors
    assert "commits_pulled has the wrong type" in errors
    assert "schema_version 2 is not 1" in errors
    assert "builds[0] missing build_exit" in errors
    assert "builds[1] is not an object" in errors


# --- waiting on the lock --------------------------------------------------------


def test_wait_until_idle(tmp_path, hold_lock):
    lock = tmp_path / "lock"
    assert contract.wait_until_idle(0, path=lock) is True  # no file at all
    waits = []
    with hold_lock(lock):
        assert (
            contract.wait_until_idle(
                0.2, poll_secs=0.05, on_wait=lambda: waits.append(1), path=lock
            )
            is False
        )
    assert waits
    assert contract.wait_until_idle(0, path=lock) is True  # file left, nobody holds it


def test_probing_never_creates_the_lock_file(tmp_path):
    lock = tmp_path / "lock"
    assert contract.tree_busy(lock) is False
    assert not lock.exists()


def test_a_run_started_during_a_probe_still_gets_the_lock(tmp_path, monkeypatch):
    """A consumer's probe holds LOCK_SH for an instant; `run` must retry past it."""
    lock = tmp_path / "lock"
    real_flock = status.fcntl.flock
    calls = {"n": 0}

    def flaky(fd, op):
        calls["n"] += 1
        if calls["n"] == 1 and op & status.fcntl.LOCK_EX:
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return real_flock(fd, op)

    monkeypatch.setattr(status.fcntl, "flock", flaky)
    with status.Lock(lock):
        assert calls["n"] == 2


# --- review fixes (M3a) ---------------------------------------------------------


def test_blank_hook_means_no_hook(tree):
    """F4: whitespace split to [] and crashed subprocess.run with IndexError."""
    assert _run(tree, "--hook", "   ") == 0
    assert "hook" not in _status()


def test_update_failed_carries_the_pre_revisions(tree, tmp_path):
    """F15: they were read before the update and must reach the hook."""
    _fake(tree, "update_rcs", "1\n")
    _fake(tree, "update_msg", "fatal: merge conflict\n")
    hook, out = _probe_hook(tmp_path)
    assert _run(tree, "--hook", hook) == cli.EXIT_PARTIAL
    doc = _status()
    assert "integration_pre" in doc and "tree_pre" in doc
    env = json.loads(out.read_text())["env"]
    assert env["FX_UPDATER_INTEGRATION_PRE"] == doc["integration_pre"]
    assert env["FX_UPDATER_INTEGRATION_POST"] == ""


def test_fx_build_dir_naming_a_path_is_refused(tree):
    """F13: the hook's space-separated BUILD_DIRS relies on plain names."""
    (tree / ".fx-build-dir").write_text("out/a b\n")
    assert _run(tree) == cli.EXIT_SETUP
    assert _calls(tree) == []


def test_lock_file_is_private_and_never_followed(tmp_path):
    """F1/F2: a planted symlink is not truncated; the file is 0600."""
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    lock = tmp_path / "state" / "lock"
    lock.parent.mkdir()
    lock.symlink_to(victim)
    with pytest.raises(OSError):
        with status.Lock(lock):
            pass
    assert victim.read_text() == "precious\n"
    lock.unlink()
    with status.Lock(lock):
        assert lock.stat().st_mode & 0o777 == 0o600
