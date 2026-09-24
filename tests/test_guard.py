# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""The WIP guard and the mode-only-dirt restore (fx_updater/guard.py).

The functions decide whether an unattended run touches the tree at all. They
err toward doing nothing: an unparseable status means "skip", and a project
that cannot be proven clean is left alone.
"""

import pathlib
import subprocess

from fx_updater import guard

# Verbatim shape observed on a build host 2026-08-27: project header line, branch
# line, then `git status -s` rows.
_DIRTY_STATUS = """.: \n\
Branch: DETACHED-HEAD(d5741bb33d8 [net-cli] Rewrite capture subcmd tests)
M build/config/rbe/BUILD.gn
 M build/rbe/output_leak_scanner.py
 M build/rbe/remote_action.py
"""


def test_clean_tree_is_empty_summary():
    assert guard.wip_summary("") == ""
    assert guard.wip_summary("\n  \n") == ""


_DELETED_PROJECT_WARN = (
    "[05:30:36.168] WARN: Found 1 deleted project(s), run with -d flag to list them.\n"
)


def test_jiri_warning_alone_is_not_wip():
    """Verbatim `jiri status` output on a build host, 2026-08-29: upstream dropped
    third_party/jinja2 from the manifest, jiri warned, and the unattended
    update skipped the night as "work in progress" with nothing of the user's touched.
    """
    assert guard.wip_summary(_DELETED_PROJECT_WARN) == ""


def test_jiri_warning_does_not_hide_real_wip():
    summary = guard.wip_summary(_DELETED_PROJECT_WARN + _DIRTY_STATUS)
    assert "build/config/rbe/BUILD.gn" in summary
    assert "deleted project" not in summary


def test_dirty_tree_summary_names_the_files():
    summary = guard.wip_summary(_DIRTY_STATUS)
    assert "build/config/rbe/BUILD.gn" in summary
    assert "DETACHED-HEAD" in summary


def test_long_status_is_truncated_with_a_count():
    lines = "\n".join(f" M file{i}.c" for i in range(50))
    summary = guard.wip_summary(lines, limit=20)
    assert summary.count("\n") == 20
    assert "(30 more lines)" in summary


# --- mode-only dirt ---------------------------------------------------------
#
# A fresh checkout reports files as modified whose content matches the index
# exactly; only the mode differs, 100755 where git recorded 100644. (What
# sets the bit is still open.) The WIP guard
# must not read that as someone's work, or it skips every run silently - and
# it must restore them rather than ignore them, because jiri update refuses
# any project with uncommitted changes.

_FRESH_CHECKOUT_STATUS = """.: \n\
Branch: DETACHED-HEAD(af7a1e2672a [mobly] Move commands library to Lacewing)
M src/graphics/drivers/gfxstream-vulkan/metadata.json
 M src/power/power-manager/node_config/base_node_config.json5

third_party/android/platform/external/avb: \n\
Branch: DETACHED-HEAD(ca76427 [android][avb] Suppress compiler warnings)
M test/data/atx_metadata.bin
"""


def test_project_headers_parsed_branch_lines_are_not():
    assert guard.jiri_status_projects(_FRESH_CHECKOUT_STATUS) == [
        ".",
        "third_party/android/platform/external/avb",
    ]


def test_project_headers_ignore_jiri_log_lines():
    noise = "[05:30:36.168] WARN: Found 1 deleted project(s), run with -d.\n"
    assert guard.jiri_status_projects(noise) == []


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _git_status(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo_with_committed_file(tmp_path, name="data.json", body="{}\n"):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    path = repo / name
    path.write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", "seed")
    return repo, path


def test_mode_flip_alone_is_not_work_in_progress(tmp_path):
    repo, path = _repo_with_committed_file(tmp_path)
    path.chmod(0o755)  # the shape seen on a fresh checkout
    assert guard.project_dirt_is_mode_only(repo) is True


def test_clean_project_is_mode_only_vacuously(tmp_path):
    repo, _ = _repo_with_committed_file(tmp_path)
    assert guard.project_dirt_is_mode_only(repo) is True


def test_edited_content_is_work_in_progress(tmp_path):
    repo, path = _repo_with_committed_file(tmp_path)
    path.write_text('{"edited": true}\n')
    assert guard.project_dirt_is_mode_only(repo) is False


def test_edited_binary_is_work_in_progress(tmp_path):
    """`--numstat` renders binary edits and mode flips identically ("-\t-"),
    which is why the check hashes content instead of counting diff lines."""
    repo, path = _repo_with_committed_file(tmp_path, "blob.bin", "\x00\x01seed\n")
    path.write_bytes(b"\x00\x01edited\n")
    assert guard.project_dirt_is_mode_only(repo) is False


def test_mode_flip_plus_content_edit_is_work_in_progress(tmp_path):
    repo, path = _repo_with_committed_file(tmp_path)
    path.write_text('{"edited": true}\n')
    path.chmod(0o755)
    assert guard.project_dirt_is_mode_only(repo) is False


def test_untracked_file_is_work_in_progress(tmp_path):
    repo, _ = _repo_with_committed_file(tmp_path)
    (repo / "scratch.txt").write_text("notes\n")
    assert guard.project_dirt_is_mode_only(repo) is False


def test_staged_mode_flip_is_work_in_progress(tmp_path):
    """Staging is a deliberate act, so it counts as work even when the only
    difference is the mode."""
    repo, path = _repo_with_committed_file(tmp_path)
    path.chmod(0o755)
    _git(repo, "add", "--chmod=+x", "data.json")
    assert guard.project_dirt_is_mode_only(repo) is False


def test_deleted_file_is_work_in_progress(tmp_path):
    repo, path = _repo_with_committed_file(tmp_path)
    path.unlink()
    assert guard.project_dirt_is_mode_only(repo) is False


def test_not_a_git_repo_is_work_in_progress(tmp_path):
    assert guard.project_dirt_is_mode_only(tmp_path / "nope") is False


_ONE_PROJECT_STATUS = ".: \nBranch: DETACHED-HEAD(abc)\n M data.json\n"


def test_restore_puts_the_mode_back_and_keeps_content(tmp_path):
    repo, path = _repo_with_committed_file(tmp_path)
    before = path.read_bytes()
    path.chmod(0o755)
    assert guard.restore_modes(repo, _ONE_PROJECT_STATUS) == 1
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o111 == 0
    assert guard.wip_summary(_git_status(repo)) == ""


def test_restore_touches_nothing_when_real_work_is_present(tmp_path):
    repo, path = _repo_with_committed_file(tmp_path)
    path.chmod(0o755)
    (repo / "scratch.txt").write_text("real work\n")
    assert guard.restore_modes(repo, _ONE_PROJECT_STATUS) is None
    assert path.stat().st_mode & 0o111 != 0  # left dirty on purpose


def test_restore_never_discards_an_edit(tmp_path):
    """The guarantee that makes restoring safe: content is never rewritten."""
    repo, path = _repo_with_committed_file(tmp_path)
    path.write_text('{"precious": true}\n')
    path.chmod(0o755)
    assert guard.restore_modes(repo, _ONE_PROJECT_STATUS) is None
    assert path.read_text() == '{"precious": true}\n'


def test_clean_project_restores_nothing(tmp_path):
    repo, _ = _repo_with_committed_file(tmp_path)
    assert guard.restore_modes(repo, _ONE_PROJECT_STATUS) == 0


def test_no_projects_parsed_means_do_not_touch_the_tree():
    """An unparseable status must skip the run, never wave it through."""
    assert guard.restore_modes(pathlib.Path("/nonexistent"), "???") is None
