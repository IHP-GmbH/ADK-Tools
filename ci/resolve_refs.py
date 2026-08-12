#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Work out which commit of each repository the integration job checks out, and
refuse to hand any of them over until it is known to exist.

The integration job checks out nine repositories by explicit `repository`, `ref`
and `path`, never through submodules, because one of them is chiplet-spec, which
is not a submodule of anything and is exactly where the vendored-reader
contracts have to be evaluated. That means a ref is computed and then passed to
`actions/checkout`, and a computed ref has a failure mode a written one does not:

    ref: ${{ fromJSON(needs.resolve.outputs.refs).adk }}

If the lookup that produced it returned nothing, this is `ref:` with an empty
value, which is not an error. `actions/checkout` takes the default branch, the
job runs to completion against the wrong tree, and every contract passes having
compared the wrong things. There is no red tick anywhere.

So every ref is 40 hex and every ref is confirmed to resolve on its remote
before it leaves this script. A lookup that fails is a failure of the run, not
an empty string that flows downstream.

Two modes:

  --pinned    the combination this repository names. Submodule gitlinks for the
              seven tools, `ci/integration-refs.json` for chiplet-spec and the
              docs. This is what the required check runs on, so it moves only
              when somebody commits a bump here.
  --floating  every repository at the head of its declared branch. This is the
              weekly run, which is allowed to be red and never blocks anything.

Usage:
    resolve_refs.py --pinned
    resolve_refs.py --floating
    resolve_refs.py --pinned --github-output   # writes refs= and slugs= to $GITHUB_OUTPUT
    resolve_refs.py --self-test
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
HERE = pathlib.Path(__file__).resolve().parent

# Logical id -> the submodule path in this repository, for the seven that are
# pinned as submodules. The ids match ci/contracts.py.
SUBMODULE_ID = {
    "tools/adk": "adk",
    "tools/OpenIntM4TM2": "interposer",
    "tools/IHP-Interconnect-IntM4TM2": "interconnect",
    "tools/chiplet_kicad_plugin": "plugin",
    "tools/gds_to_kicad": "gds2kicad",
    "tools/chiplet-studio": "studio",
    "tools/kicad": "kicad",
}


def git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def token():
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class Unreachable(Exception):
    """The API could not be reached at all.

    Deliberately not a RuntimeError and deliberately not a status code: "the
    pin is bad" and "we could not ask" are different facts, and a runner that
    loses TLS for four seconds must not be reported as a repository with a
    broken pin. A hosted runner has been observed handing back a self-signed
    certificate mid-run, twice in one minute, and passing on the next attempt.
    """


def make_api(auth, attempts=3, sleep=time.sleep):
    def call(path):
        req = urllib.request.Request(API + path)
        req.add_header("Accept", "application/vnd.github+json")
        if auth:
            req.add_header("Authorization", "Bearer " + auth)
        last = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return e.code, None
                last = "HTTP %d" % e.code
            except (urllib.error.URLError, OSError) as e:
                last = str(e)
            if attempt + 1 < attempts:
                sleep(2 ** attempt)
        raise Unreachable("%s: %s (after %d attempts)" % (path, last, attempts))
    return call


def declared(gitmodules_text, extra):
    """[(id, slug, branch, pinned sha or None)] for every repository checked out.

    Submodules bring their own pin; the entries in `extra` carry theirs from
    ci/integration-refs.json.
    """
    cfg = {}
    for line in gitmodules_text.splitlines():
        line = line.strip()
        m = re.match(r'^\[submodule "([^"]+)"\]$', line)
        if m:
            current = m.group(1)
            continue
        if "=" in line and not line.startswith("["):
            k, v = (x.strip() for x in line.split("=", 1))
            cfg.setdefault(current, {})[k] = v

    out = []
    for name, body in cfg.items():
        path = body.get("path")
        rid = SUBMODULE_ID.get(path)
        if rid is None:
            # A new submodule nobody taught this script about would otherwise be
            # left out of every contract while everything stayed green.
            out.append((None, path, body.get("branch"), None))
            continue
        m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$",
                      body.get("url", ""))
        slug = "%s/%s" % (m.group(1), m.group(2)) if m else None
        out.append((rid, slug, body.get("branch"), None))

    for rid, entry in extra.items():
        if rid.startswith("_"):
            continue
        out.append((rid, entry.get("slug"), entry.get("branch"), entry.get("pin")))
    return out


def gitlinks():
    """{submodule path: sha} out of the current HEAD tree."""
    out = {}
    for line in git("ls-tree", "-r", "HEAD").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if parts[0] == "160000":
            out[path] = parts[2]
    return out


def resolve(entries, pins, floating, api):
    """({id: sha}, {id: slug}, [failure, ...])."""
    refs, slugs, failures = {}, {}, []
    for rid, slug, branch, declared_pin in entries:
        if rid is None:
            failures.append(
                "the submodule at %r has no entry in SUBMODULE_ID, so the "
                "integration job would not check it out and every contract "
                "would pass without ever seeing it" % (slug,))
            continue
        if not slug:
            failures.append("%s: no github.com slug could be read for it" % rid)
            continue
        slugs[rid] = slug

        if floating:
            if not branch:
                failures.append("%s: declares no branch, so a floating run has "
                                "nothing to follow" % rid)
                continue
            status, body = api("/repos/%s/commits/%s" % (slug, branch))
            if status != 200 or not body:
                failures.append("%s: branch %r does not resolve on %s (HTTP %d)"
                                % (rid, branch, slug, status))
                continue
            sha = body.get("sha", "")
        else:
            sha = declared_pin or pins.get(rid) or ""

        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            failures.append(
                "%s: resolved to %r, which is not a 40-hex sha. Passed to "
                "`actions/checkout` this takes the default branch and the run "
                "goes green against a tree nobody chose." % (rid, sha))
            continue

        status, _ = api("/repos/%s/commits/%s" % (slug, sha))
        if status != 200:
            failures.append("%s: %s@%s does not resolve on the remote (HTTP %d)"
                            % (rid, slug, sha[:10], status))
            continue
        refs[rid] = sha
    return refs, slugs, failures


def self_test():
    """Every way a ref can be wrong, against a stub API. No network."""
    cases = []
    good = "a" * 40
    other = "b" * 40

    gm = "\n".join([
        '[submodule "tools/adk"]',
        "\tpath = tools/adk",
        "\turl = https://github.com/IHP-GmbH/IHP-Open-ADK.git",
        "\tbranch = dev",
    ])
    extra = {"spec": {"slug": "IHP-GmbH/chiplet-spec", "branch": "dev",
                      "pin": other}}

    def stub(reachable=(good, other), head=good, branch_ok=True):
        def api(path):
            m = re.match(r"/repos/[^/]+/[^/]+/commits/(.+)$", path)
            ref = m.group(1)
            if re.fullmatch(r"[0-9a-f]{40}", ref):
                return (200, {"sha": ref}) if ref in reachable else (422, None)
            return (200, {"sha": head}) if branch_ok else (404, None)
        return api

    def expect(name, ok):
        cases.append((name, ok, None))

    def run(pins, api, floating=False, gitmodules=gm, ex=extra):
        return resolve(declared(gitmodules, ex), pins, floating, api)

    refs, slugs, fails = run({"adk": good}, stub())
    expect("a pinned run resolves both a submodule and a json pin",
           not fails and refs == {"adk": good, "spec": other})
    expect("slugs come out alongside the refs",
           slugs.get("adk") == "IHP-GmbH/IHP-Open-ADK")

    refs, _, fails = run({}, stub(), floating=True)
    expect("a floating run takes the branch head, not the pin",
           not fails and refs == {"adk": good, "spec": good})

    _, _, fails = run({}, stub())
    expect("a missing pin fails rather than becoming an empty ref",
           any("not a 40-hex sha" in f for f in fails))

    _, _, fails = run({"adk": "abc123"}, stub())
    expect("a truncated sha fails",
           any("not a 40-hex sha" in f for f in fails))

    _, _, fails = run({"adk": "c" * 40}, stub())
    expect("a sha nobody can fetch fails",
           any("does not resolve on the remote" in f for f in fails))

    _, _, fails = run({}, stub(branch_ok=False), floating=True)
    expect("a branch that does not exist fails a floating run",
           any("does not resolve on" in f for f in fails))

    no_branch = gm.replace("\tbranch = dev\n", "").replace("\tbranch = dev", "")
    _, _, fails = run({}, stub(), floating=True, gitmodules=no_branch)
    expect("a submodule with no declared branch fails a floating run",
           any("nothing to follow" in f for f in fails))

    unknown = "\n".join([
        '[submodule "tools/newtool"]',
        "\tpath = tools/newtool",
        "\turl = https://github.com/IHP-GmbH/newtool.git",
        "\tbranch = dev",
    ])
    _, _, fails = run({"adk": good}, stub(), gitmodules=unknown)
    expect("a submodule this script does not know about fails loudly",
           any("no entry in SUBMODULE_ID" in f for f in fails))

    # The retry, which exists because a hosted runner handed back a self-signed
    # certificate mid-run and both API-using jobs died with a traceback.
    tries = []

    def flaky(fail_first):
        def opener(req, timeout=None):
            tries.append(1)
            if len(tries) <= fail_first:
                raise urllib.error.URLError("SSL: CERTIFICATE_VERIFY_FAILED")
            raise urllib.error.HTTPError(req.full_url, 200, "ok", {}, None)
        return opener

    real_urlopen = urllib.request.urlopen
    try:
        urllib.request.urlopen = flaky(2)
        api = make_api(None, attempts=3, sleep=lambda _: None)
        expect("a transient failure is retried rather than reported",
               api("/x") == (200, None) and len(tries) == 3)

        tries.clear()
        urllib.request.urlopen = flaky(9)
        api = make_api(None, attempts=3, sleep=lambda _: None)
        try:
            api("/x")
            expect("an unreachable API raises rather than looking like a bad pin",
                   False)
        except Unreachable:
            expect("an unreachable API raises rather than looking like a bad pin",
                   len(tries) == 3)
    finally:
        urllib.request.urlopen = real_urlopen

    tries.clear()

    def four_oh_four(req, timeout=None):
        tries.append(1)
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)

    try:
        urllib.request.urlopen = four_oh_four
        api = make_api(None, attempts=3, sleep=lambda _: None)
        expect("a 404 is an answer and is not retried",
               api("/x") == (404, None) and len(tries) == 1)
    finally:
        urllib.request.urlopen = real_urlopen

    ok = True
    for name, passed, _ in cases:
        print("%-62s %s" % (name, "ok" if passed else "FAILED"))
        ok = ok and passed
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pinned", action="store_true")
    mode.add_argument("--floating", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    ap.add_argument("--github-output", action="store_true",
                    help="append refs= and slugs= to $GITHUB_OUTPUT as JSON")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    gitmodules = pathlib.Path(".gitmodules")
    if not gitmodules.is_file():
        print("FAIL: no .gitmodules here. Run this from an adk-tools checkout.",
              file=sys.stderr)
        return 1
    extra = json.loads((HERE / "integration-refs.json").read_text())
    pins = {SUBMODULE_ID[p]: s for p, s in gitlinks().items() if p in SUBMODULE_ID}

    entries = declared(gitmodules.read_text(), extra)
    api = make_api(token())
    try:
        refs, slugs, failures = resolve(entries, pins, args.floating, api)
    except Unreachable as e:
        # Exit 2, not 1: no verdict was reached. Same distinction the local gate
        # makes, and the reason a traceback is not good enough here is that the
        # next person reads "resolve failed" and starts looking at the pins.
        print("NO VERDICT: could not reach the GitHub API (%s). Nothing is "
              "known about the refs; this is not a finding about them."
              % e, file=sys.stderr)
        return 2

    for rid in sorted(slugs):
        print("%-14s %-40s %s" % (rid, slugs[rid], refs.get(rid, "UNRESOLVED")))
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1

    expected = len(SUBMODULE_ID) + len([k for k in extra if not k.startswith("_")])
    if len(refs) != expected:
        print("FAIL: resolved %d refs, expected %d. A short matrix is a run "
              "that checked fewer trees than it reports on."
              % (len(refs), expected), file=sys.stderr)
        return 1

    if args.github_output:
        out = os.environ.get("GITHUB_OUTPUT")
        if not out:
            print("FAIL: --github-output given but GITHUB_OUTPUT is unset",
                  file=sys.stderr)
            return 1
        with open(out, "a") as fh:
            fh.write("refs=%s\n" % json.dumps(refs))
            fh.write("slugs=%s\n" % json.dumps(slugs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
