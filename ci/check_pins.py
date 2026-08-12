#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Every submodule pin must be fetchable by the public, from the branch it declares.

This repository is a lockfile. Its whole job is to name one commit per tool such
that `git clone --recurse-submodules` reproduces a known-good combination, and
that job has exactly one way to fail catastrophically: a gitlink pointing at a
commit nobody outside this machine can fetch. Then the clone fails for everyone,
on a repository whose entire purpose is being clonable, and nothing local ever
notices because locally the commit is right there.

It has happened. A pin was written from a local branch that had never been
pushed, and the only reason it did not ship was that the branch was still
unpushed when somebody looked.

Two properties are checked, per submodule:

  reachable   the pinned sha resolves on the remote at all. This is the one
              that breaks recursive clone.
  on-branch   the pinned sha is contained in the branch `.gitmodules` declares.
              `git submodule update --remote` follows that declaration, so a
              pin that is not on it gets silently dragged elsewhere the first
              time anyone runs it. The plugin's `main` and `dev` have no common
              ancestor at all, which is what makes this more than pedantry
              here: the wrong declaration moves the pin onto a disjoint
              history.

Both use the GitHub API rather than a fetch. Proving containment by fetching
would mean pulling the history of seven repositories, one of which is KiCad,
to answer a question two HTTP requests answer.

Usage:
    check_pins.py
    check_pins.py --self-test
"""

import argparse
import re
import subprocess
import sys

# Accepted answers to "is the pin contained in the declared branch". `behind`
# means the pin is an ancestor of the branch head; `identical` means it is the
# head. `ahead` and `diverged` both mean the branch does not contain it.
CONTAINED = {"identical", "behind"}


def git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


# One API client for the whole ci/ directory, so the retry and the
# "unreachable is not a finding" distinction hold everywhere rather than in
# whichever copy was edited last.
from resolve_refs import Unreachable, make_api, token          # noqa: E402


def submodules():
    """[(path, owner/repo, declared branch or None, pinned sha)]."""
    cfg = {}
    for line in git("config", "-f", ".gitmodules", "--list").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k] = v

    names = sorted({k.split(".")[1] for k in cfg if k.startswith("submodule.")})
    out = []
    for name in names:
        path = cfg.get("submodule.%s.path" % name)
        url = cfg.get("submodule.%s.url" % name, "")
        branch = cfg.get("submodule.%s.branch" % name)
        m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
        slug = "%s/%s" % (m.group(1), m.group(2)) if m else None
        sha = None
        entry = git("ls-tree", "HEAD", path).strip() if path else ""
        if entry.startswith("160000"):
            sha = entry.split()[2]
        out.append((path, slug, branch, sha))
    return out


def check(entries, api):
    failures = []
    for path, slug, branch, sha in entries:
        if slug is None:
            failures.append("%s: url is not a github.com repository, so the pin "
                            "cannot be checked" % path)
            continue
        if sha is None:
            failures.append("%s: no gitlink at this path in HEAD" % path)
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            failures.append("%s: gitlink %r is not a 40-hex sha" % (path, sha))
            continue

        status, _ = api("/repos/%s/commits/%s" % (slug, sha))
        if status != 200:
            failures.append(
                "%s: %s@%s does not resolve on the remote (HTTP %d). "
                "`git clone --recurse-submodules` fails for everyone while this "
                "pin is published; the commit exists only where it was written."
                % (path, slug, sha[:10], status)
            )
            continue

        if not branch:
            failures.append(
                "%s: .gitmodules declares no branch. `git submodule update "
                "--remote` then follows the repository's default branch, which "
                "is not necessarily the track this superproject is on."
                % path
            )
            continue

        status, body = api("/repos/%s/compare/%s...%s" % (slug, branch, sha))
        if status != 200:
            failures.append(
                "%s: cannot compare %s against branch %r (HTTP %d). A branch that "
                "does not exist is a declaration nothing can follow."
                % (path, sha[:10], branch, status)
            )
            continue
        if body.get("status") not in CONTAINED:
            failures.append(
                "%s: %s@%s is not contained in the declared branch %r (compare "
                "says %r). `git submodule update --remote` would move this pin "
                "off the commit that was tested."
                % (path, slug, sha[:10], branch, body.get("status"))
            )
    return failures


def self_test():
    """Each failure mode, against a stub API. No network."""
    cases = []
    good_sha = "a" * 40
    other_sha = "b" * 40

    def stub(reachable=(good_sha, other_sha), compare="behind", branch_ok=True):
        def api(path):
            m = re.match(r"/repos/[^/]+/[^/]+/commits/([0-9a-f]+)$", path)
            if m:
                return (200, {}) if m.group(1) in reachable else (422, None)
            if "/compare/" in path:
                if not branch_ok:
                    return 404, None
                return 200, {"status": compare}
            raise AssertionError(path)
        return api

    base = [("tools/adk", "o/r", "dev", good_sha)]

    def expect(name, entries, api, want_fail, substring=None):
        got = check(entries, api)
        ok = bool(got) == want_fail
        if ok and substring:
            ok = any(substring in g for g in got)
        cases.append((name, ok, got))

    expect("a reachable pin on its declared branch passes", base, stub(), False)
    expect("an unreachable pin fails", base, stub(reachable=()), True,
           "does not resolve")
    expect("a pin ahead of its declared branch fails", base,
           stub(compare="ahead"), True, "not contained")
    expect("a pin on a disjoint history fails", base,
           stub(compare="diverged"), True, "not contained")
    expect("a declared branch that does not exist fails", base,
           stub(branch_ok=False), True, "cannot compare")
    expect("a submodule with no declared branch fails",
           [("tools/adk", "o/r", None, good_sha)], stub(), True,
           "declares no branch")
    expect("a non-github url fails",
           [("tools/adk", None, "dev", good_sha)], stub(), True,
           "not a github.com repository")
    expect("a missing gitlink fails",
           [("tools/adk", "o/r", "dev", None)], stub(), True, "no gitlink")
    expect("a truncated sha fails",
           [("tools/adk", "o/r", "dev", "abc123")], stub(), True, "not a 40-hex")

    ok = True
    for name, passed, got in cases:
        print("%-52s %s" % (name, "ok" if passed else "FAILED"))
        if not passed:
            ok = False
            print("   checker returned: %r" % (got,))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    entries = submodules()
    if not entries:
        print("FAIL: no submodules found, so this check would pass having "
              "verified nothing", file=sys.stderr)
        return 1

    api = make_api(token())
    try:
        failures = check(entries, api)
    except Unreachable as e:
        # Exit 2: no verdict. A runner that loses TLS for a few seconds must not
        # read as a repository with an unfetchable pin.
        print("NO VERDICT: could not reach the GitHub API (%s). The pins were "
              "not checked; this says nothing about them." % e, file=sys.stderr)
        return 2

    for path, slug, branch, sha in entries:
        print("%-40s %s@%s on %s" % (path, slug, (sha or "?")[:10], branch))
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("\n%d pins are publicly reachable and on their declared branch"
          % len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
