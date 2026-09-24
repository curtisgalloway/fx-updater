# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""`fx-updater run` end to end, against a fake tree.

The fake tree has shell-script stand-ins for `jiri` and `fx` that record
their calls and replay scripted results, so the order of the guardrails and
the status document are tested without a Fuchsia checkout.
"""

import json
import pathlib
import subprocess
import sys

import pytest

from fx_updater import cli
from fx_updater import status

_FAKE_JIRI = """#!/bin/sh
echo "jiri $*" >> .fake/calls
case "$1" in
  status)
    cat .fake/status 2>/dev/null
    exit "$(cat .fake/status_rc 2>/dev/null || echo 0)"
    ;;
  update)
    rc=$(head -n1 .fake/update_rcs 2>/dev/null || echo 0)
    [ -f .fake/update_rcs ] && sed -i 1d .fake/update_rcs
    [ "$rc" != 0 ] && cat .fake/update_msg 2>/dev/null
    exit "${rc:-0}"
    ;;
esac
"""

_FAKE_FX = """#!/bin/sh
echo "fx $*" >> .fake/calls
exit "$(cat .fake/build_rc 2>/dev/null || echo 0)"
"""


@pytest.fixture(name="tree")
def fixture_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Never read the host's real config: it would redirect runs at a real tree.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "fuchsia"
    (root / ".jiri_root/bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / ".fake").mkdir()
    for path, body in (
        (root / ".jiri_root/bin/jiri", _FAKE_JIRI),
        (root / "scripts/fx", _FAKE_FX),
    ):
        path.write_text(body)
        path.chmod(0o755)
    (root / ".fx-build-dir").write_text("out/core.x64\n")
    return root


def _fake(tree, name, text):
    (tree / ".fake" / name).write_text(text)


def _calls(tree):
    path = tree / ".fake/calls"
    return path.read_text().splitlines() if path.exists() else []


def _run(tree, *extra):
    return cli.main(["run", "--fuchsia-dir", str(tree), "--min-free-gb", "0", *extra])


def _status():
    return json.loads(status.default_status_path().read_text())


def test_clean_tree_updates_then_builds_the_default_dir(tree):
    assert _run(tree) == 0
    assert _calls(tree) == [
        "jiri status -check-head=false",
        "jiri update",
        "fx --dir out/core.x64 build",
    ]
    doc = _status()
    assert doc["outcome"] == "ok"
    assert [b["build_dir"] for b in doc["builds"]] == ["core.x64"]
    assert doc["builds"][0]["build_exit"] == 0
    assert pathlib.Path(doc["log"]).exists()
    assert not status.lock_path().exists()


def test_work_in_progress_skips_with_exit_zero(tree):
    _fake(tree, "status", ".: \nBranch: main\n M src/mine.cc\n")
    assert _run(tree) == 0
    assert _calls(tree) == ["jiri status -check-head=false"]
    doc = _status()
    assert doc["outcome"] == "skipped_wip"
    assert "src/mine.cc" in doc["wip"]


def test_failed_jiri_status_is_its_own_outcome(tree):
    """Not skipped_wip: the WIP check never ran, and that must be visible."""
    _fake(tree, "status_rc", "1")
    assert _run(tree) == cli.EXIT_BUSY
    assert _status()["outcome"] == "status_failed"
    assert "jiri update" not in _calls(tree)


def test_disk_floor_touches_nothing(tree):
    rc = cli.main(["run", "--fuchsia-dir", str(tree), "--min-free-gb", "1e12"])
    assert rc == cli.EXIT_SETUP
    assert _calls(tree) == []
    assert _status()["outcome"] == "skipped_disk"


def test_held_lock_refuses_and_leaves_it(tree):
    lock = status.lock_path()
    lock.parent.mkdir(parents=True)
    lock.write_text("12345")
    assert _run(tree) == cli.EXIT_BUSY
    assert _calls(tree) == []
    assert lock.exists()
    assert not status.default_status_path().exists()


def test_transient_update_failure_is_retried(tree):
    _fake(tree, "update_rcs", "1\n0\n")
    _fake(tree, "update_msg", "fatal: Could not resolve host: example\n")
    assert _run(tree, "--retry-wait-secs", "0") == 0
    assert _calls(tree).count("jiri update") == 2
    assert _status()["outcome"] == "ok"


def test_hard_update_failure_is_not_retried(tree):
    _fake(tree, "update_rcs", "1\n0\n")
    _fake(tree, "update_msg", "CONFLICT (content): Merge conflict in a.cc\n")
    assert _run(tree, "--retry-wait-secs", "0") == cli.EXIT_PARTIAL
    assert _calls(tree).count("jiri update") == 1
    doc = _status()
    assert doc["outcome"] == "update_failed"
    assert doc["update_rc"] == 1
    assert "(hard)" in doc["reason"]


def test_hard_failure_after_transient_one_is_not_retried(tree):
    """Each attempt is classified from its own output only: attempt 1's
    network error must not make attempt 2's conflict look transient."""
    _fake(tree, "update_rcs", "1\n1\n0\n")
    (tree / ".fake/update_msg").write_text("fatal: Could not resolve host: x\n")
    jiri_bin = tree / ".jiri_root/bin/jiri"
    jiri_bin.write_text(
        _FAKE_JIRI.replace(
            "cat .fake/update_msg 2>/dev/null",
            "cat .fake/update_msg 2>/dev/null;"
            " echo 'CONFLICT (content): in a.cc' > .fake/update_msg",
        )
    )
    assert _run(tree, "--retry-wait-secs", "0") == cli.EXIT_PARTIAL
    assert _calls(tree).count("jiri update") == 2
    assert "(hard) after 2 attempt(s)" in _status()["reason"]


def test_earlier_dirs_stale_graph_does_not_regen_a_later_dir(tree):
    """Dir a hits the stale-graph error and regens; dir b then fails with a
    compile error. b must not get an `fx gen` on a's message."""
    fx = tree / "scripts/fx"
    fx.write_text(
        "#!/bin/sh\n"
        'echo "fx $*" >> .fake/calls\n'
        'case "$*" in\n'
        '  "--dir out/a build")\n'
        "    if [ ! -f .fake/a_regen ]; then\n"
        "      echo \"ninja: error: rebuilding 'build.ninja': 'gone/BUILD.gn',"
        " needed by 'build.ninja.stamp', missing and no known rule to make it\"\n"
        "      exit 1; fi; exit 0 ;;\n"
        '  "--dir out/a gen") touch .fake/a_regen; exit 0 ;;\n'
        '  "--dir out/b build") echo "error: expected ;"; exit 1 ;;\n'
        "esac\n"
        "exit 0\n"
    )
    rc = _run(tree, "--no-update", "--build-dir", "a", "--build-dir", "b")
    assert rc == cli.EXIT_BUILD_FAILED
    assert _calls(tree) == [
        "fx --dir out/a build",
        "fx --dir out/a gen",
        "fx --dir out/a build",
        "fx --dir out/b build",
    ]
    builds = {b["build_dir"]: b for b in _status()["builds"]}
    assert builds["a"]["regen"] == "gone/BUILD.gn"
    assert builds["b"]["regen"] is None


def test_negative_retries_is_a_usage_error(tree):
    with pytest.raises(SystemExit) as e:
        _run(tree, "--update-retries", "-1")
    assert e.value.code == 2
    assert _calls(tree) == []


def test_build_failure_exits_100_and_records_fx_code(tree):
    """fx's own code goes in the document, never out as the exit status."""
    _fake(tree, "build_rc", "7")
    assert _run(tree, "--build-dir", "a", "--build-dir", "b") == 100
    doc = _status()
    assert doc["outcome"] == "build_failed"
    assert [b["build_exit"] for b in doc["builds"]] == [7, 7]
    # Every requested dir is still attempted.
    assert [b["build_dir"] for b in doc["builds"]] == ["a", "b"]


def test_no_update_never_checks_or_updates(tree):
    _fake(tree, "status", ".: \n M src/mine.cc\n")
    assert _run(tree, "--no-update") == 0
    assert _calls(tree) == ["fx --dir out/core.x64 build"]


def test_missing_tools_exit_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert cli.main(["run", "--fuchsia-dir", str(tmp_path)]) == cli.EXIT_SETUP
    assert not status.lock_path().exists()


def test_run_help_is_the_installed_entry_point():
    script = pathlib.Path(sys.executable).parent / "fx-updater"
    assert script.exists(), "console script not installed; run `uv sync`"
    done = subprocess.run(
        [str(script), "run", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert "--fuchsia-dir" in done.stdout


def test_skill_is_embedded(capsys):
    assert cli.main(["--skill"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nname: fx-updater")
    assert "{{VERSION}}" not in out


def test_lock_is_released_when_the_pid_write_fails(tmp_path, monkeypatch):
    def full_disk(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(status.os, "write", full_disk)
    lock = tmp_path / "lock"
    with pytest.raises(OSError):
        with status.Lock(lock):
            pass
    assert not lock.exists()


def test_status_write_ignores_a_planted_temp_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    (tmp_path / "status.tmp").symlink_to(victim)
    target = tmp_path / "status.json"
    status.write_status(target, {"outcome": "ok"})
    assert victim.read_text() == "precious\n"
    assert json.loads(target.read_text())["outcome"] == "ok"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "status.json",
        "status.tmp",
        "victim",
    ]


def test_unknown_outcome_is_refused(tmp_path):
    with pytest.raises(ValueError):
        status.write_status(tmp_path / "s.json", {"outcome": "fine"})
