<!-- SPDX-FileCopyrightText: 2026 Curtis Galloway -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
Where to file: https://issues.fuchsia.dev (Fuchsia > Tools > Jiri). A fix
  would go to https://fuchsia-review.googlesource.com (project: jiri) with the
  issue number in its Bug: line.
Status: DRAFT — not ready
Tracking: fx-updater bugs/fuchsia/001
Fix: none written. The likely fix is the one-branch change under "Fix" below.
Evidence: observed 2026-10-14 by fx-updater's scheduled run on a build host,
  jiri 749c5089cc2b (self-updated at the start of the run), with one jiri
  worktree registered since 2026-10-11. The last good run was 2026-10-11 with
  an older jiri (revision not recorded) and no worktree registered. The log
  is in the host's fx-updater state directory, run 2026-10-14 10:22.
  Logging in to CIPD (a Google account without internal access) does not
  help: the 10:29 run fails on the same 29 packages, with "doesn't exist or
  user:<account> is not allowed to see it". That fits the cause below, since
  the unfiltered packages are never checked for access, logged in or not.
  Code reading: project/worktree_package_index.go at 749c5089cc2b, lines
  94-99; commit 9961ac1c7dc0 message.
To file: confirm the cause. Unregister the worktree (or test in a checkout
  that never had one), run `jiri update` while logged out of CIPD, and check
  that it succeeds with the "skipped by cipd due to lack of access" warning.
  Then register a worktree again and check that it fails. Record both results
  here and change Status to ready.
Everything below this header is written for filing.
-->

# `jiri update` fails on internal CIPD packages when a worktree is registered

## Summary

In a Fuchsia checkout used by someone who can't read the internal CIPD
packages, `jiri update` used to skip those packages with a warning:

```
WARN: Some packages are skipped by cipd due to lack of access, you might want to run ".jiri_root/bin/cipd auth-login" and try again
```

With jiri 749c5089cc2b it fails instead, once any jiri worktree is registered
for the checkout. Every `internal="true"` package in `//manifests/prebuilts`
reports an error, jiri skips its hooks, and the update exits 1. Because jiri
updates itself before failing, unattended update jobs can't get back to a
working version without `-autoupdate=false` and a manual pin.

## Reproduction

Conditions: public Fuchsia checkout (`.jiri_manifest` imports `flower` from
`integration`), not logged in to CIPD (`cipd auth-info` prints `Not logged
in.`), one worktree registered with `jiri worktree add` / `fx worktree add`,
jiri 749c5089cc2b.

```
$ jiri update
...
Failed to fetch fuchsia_internal/firmware/gdc:3zL46KF8N1tNniE1QlADLBrnm5Lu3T0WsFtS91P1ZiEC: prefix "fuchsia_internal/firmware/gdc" doesn't exist or not visible to unauthenticated callers, run `cipd auth-login` to login or relogin
... (one per fuchsia_internal/* package)
Jiri hooks are not run due to fatal errors when updating projects or packages
ERROR: exit status 1
```

The failing installs are all into `instances/<id>`, which is the shared
package cache. Logging in to CIPD with an account that lacks internal access
changes only the wording (`doesn't exist or "user:<account>" is not allowed
to see it`); the update still fails on the same packages.

## Cause

`LoadWorktreePackageIndex` in `project/worktree_package_index.go` collects the
packages of the main checkout and of every registered worktree. Only the
current workspace's packages go through the ACL filter:

```go
var pkgsToUse Packages
if rt == checkout.Root {
    pkgsToUse = activePkgs       // already ACL-filtered by the caller
} else {
    _ = FilterOptionalProjectsPackages(jiri.WithCheckout(ctx, wsCheckout), wsCheckout.FetchingAttrs, manifestProjects, pkgs)
    pkgsToUse = pkgs             // never ACL-filtered
}
```

So when the main checkout updates, the worktree's internal packages go into
the shared cache's ensure file, and `cipd ensure` fails for the whole index.

Until recently this didn't bite a checkout with the package cache disabled
(the `jiri init` default), because that checkout fetched without the shared
index. 9961ac1c7dc0 ("[cache] Drop package-cache=false support", 2026-10-12)
removed that path, so every checkout now builds the index, and the
unfiltered branch above is reached whenever a worktree exists.

The same branch also breaks `jiri worktree add` for these users: when the
new worktree syncs, the *main* checkout is the "other" workspace and its
internal packages go in unfiltered.

## Fix

Run the other workspaces' packages through the ACL filter as well, as the
caller already does for the current workspace (`Packages.FilterACL`,
`project/manifest.go`), before
`collectWorkspaceDestinations`. A regression test: a fake root plus one
registered worktree, both pinning a package the fake CIPD denies, where
`jiri update` in the root must succeed and skip that package.
