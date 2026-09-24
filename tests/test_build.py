# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""The stale-build.ninja recovery (fx_updater/build.py).

The rule under test: regenerate exactly once, and only for the one ninja
message that a regen fixes. Everything else is somebody's compile error and
must come back untouched.
"""

import pathlib

from fx_updater import build as fxbuild

# Verbatim from the 2026-09-01 unattended run's log (checkout path shortened).
_STALE = (
    "ninja: Entering directory `/work/fuchsia/out/local-base'\n"
    "ninja: error: rebuilding 'build.ninja': '../../build/beads/.agent/skills/"
    "migrating_host_tool_to_bazel/examples/go/after/BUILD.gn', needed by "
    "'build.ninja.stamp', missing and no known rule to make it\n"
    "Hint: run `fx build` with the option `--log LOGFILE` to generate a debug "
    "log if you are reporting a bug.\n"
)
_COMPILE_ERROR = (
    "[12/5073] CXX obj/src/foo.o\nFAILED: obj/src/foo.o\n"
    "../../src/foo.cc:3:1: error: expected ';'\nninja: build stopped: "
    "subcommand failed.\n"
)
_FX = pathlib.Path("/tree/scripts/fx")


def test_stale_input_is_extracted_from_the_ninja_message():
    assert fxbuild.stale_regen_input(_STALE) == (
        "../../build/beads/.agent/skills/migrating_host_tool_to_bazel/examples/"
        "go/after/BUILD.gn"
    )


def test_other_failures_are_not_stale_inputs():
    assert fxbuild.stale_regen_input(_COMPILE_ERROR) is None
    assert fxbuild.stale_regen_input("") is None


class _Tree:
    """A fake fx: scripted return codes per verb, and a log that fills in."""

    def __init__(self, build_rcs, gen_rc=0, first_failure_log=_STALE):
        self.build_rcs = list(build_rcs)
        self.gen_rc = gen_rc
        self.first_failure_log = first_failure_log
        self.calls = []
        self.log = ""

    def run(self, cmd):
        self.calls.append(cmd[-1])
        if cmd[-1] == "gen":
            return self.gen_rc
        rc = self.build_rcs.pop(0)
        if rc != 0 and not self.log:
            self.log = self.first_failure_log
        return rc

    def tail(self):
        return self.log


def test_clean_build_never_consults_the_log():
    tree = _Tree([0])
    reads = []
    res = fxbuild.build_with_regen(
        _FX, "local-base", tree.run, lambda: reads.append(1) or tree.tail()
    )
    assert res.returncode == 0 and res.regen is None
    assert tree.calls == ["build"]
    assert not reads


def test_stale_graph_is_regenerated_once_then_rebuilt():
    tree = _Tree([1, 0])
    res = fxbuild.build_with_regen(_FX, "local-base", tree.run, tree.tail)
    assert res.returncode == 0
    assert res.regen.endswith("examples/go/after/BUILD.gn")
    assert res.regen_returncode == 0
    assert tree.calls == ["build", "gen", "build"]


def test_compile_errors_are_not_retried():
    tree = _Tree([1], first_failure_log=_COMPILE_ERROR)
    res = fxbuild.build_with_regen(_FX, "local-base", tree.run, tree.tail)
    assert res.returncode == 1 and res.regen is None
    assert tree.calls == ["build"]


def test_failed_gen_reports_the_original_build_failure():
    tree = _Tree([1], gen_rc=2)
    res = fxbuild.build_with_regen(_FX, "local-base", tree.run, tree.tail)
    assert res.returncode == 1
    assert res.regen is not None and res.regen_returncode == 2
    assert tree.calls == ["build", "gen"]


def test_second_build_failure_after_regen_is_final():
    # A regen fixed the graph and then a genuine error surfaced: no third try.
    tree = _Tree([1, 1])
    res = fxbuild.build_with_regen(_FX, "local-base", tree.run, tree.tail)
    assert res.returncode == 1 and res.regen is not None
    assert tree.calls == ["build", "gen", "build"]


def test_commands_target_the_named_out_dir():
    tree = _Tree([0])
    seen = []
    fxbuild.build_with_regen(
        _FX, "bringup_with_tests.x64", lambda c: (seen.append(c), 0)[1], tree.tail
    )
    assert seen == [[str(_FX), "--dir", "out/bringup_with_tests.x64", "build"]]
