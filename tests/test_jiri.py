# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""jiri update failure classification (fx_updater/jiri.py).

An unrecognized failure must mean "do not retry": a retry only repeats a
rebase conflict or a corrupt checkout.
"""

from fx_updater import jiri


def test_network_failures_classify_transient():
    for tail in (
        "fatal: unable to access 'https://fuchsia.googlesource.com/'",
        "Could not resolve host: fuchsia.googlesource.com",
        "error: RPC failed; curl 56 recv failure",
        "The remote end hung up unexpectedly",
        "fatal: The requested URL returned error: 503",
    ):
        assert jiri.classify_update_failure(tail) == "transient"


def test_conflicts_and_unknowns_classify_hard():
    for tail in (
        "CONFLICT (content): Merge conflict in src/foo.cc",
        "error: cannot rebase: You have unstaged changes.",
        "",
    ):
        assert jiri.classify_update_failure(tail) == "hard"


def test_digits_that_look_like_a_status_are_hard():
    """A bare "503" used to match jiri timestamps and commit hashes."""
    for tail in (
        "[05:30:36.503] ERROR: rebase conflict in project fuchsia",
        "Branch: DETACHED-HEAD(a503bc1 [foo] Bar)\nCONFLICT (content)",
    ):
        assert jiri.classify_update_failure(tail) == "hard"
