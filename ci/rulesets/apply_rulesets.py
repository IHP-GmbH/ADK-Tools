#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Apply the tracked branch and tag rulesets, and prove GitHub stored them.

Rulesets are the only thing that makes any of the CI in this ecosystem binding.
Everything else is advice: a red check blocks nothing until a ruleset says the
merge cannot happen without it.

They are kept here as tracked JSON, one file per ruleset, and applied through
this script rather than through the web UI. That is not tidiness. The UI, when
you pick a required check from its dropdown, records the app that happened to
post it last as `integration_id` on the context. A context pinned to the Actions
app can then never be satisfied by a status posted with a user token, which is
exactly how the local heavy gate reports its verdict. The failure is invisible
in the UI and permanent, and no payload written here ever contains that field.

Applying is only half of it. GitHub can accept a payload and store something
else: an unknown rule type is dropped rather than refused, and a context can
come back carrying a field nobody sent. So every write is read back and compared
against what was declared, and a difference fails. An apply script that reports
success without looking is a script that cannot tell a ruleset from a typo.

Three things are checked on read-back, per ruleset: every field declared here is
what GitHub holds, no rule type was silently dropped or added, and no required
context grew an `integration_id`.

Usage:
    apply_rulesets.py --validate                 # offline, payloads are sane
    apply_rulesets.py --check                    # read-only, report drift
    apply_rulesets.py --apply                    # create or update, then verify
    apply_rulesets.py --apply --enforcement evaluate --repo OpenIntM4TM2
    apply_rulesets.py --self-test                # prove the comparison can fail

Only `--validate` and `--self-test` run without credentials, which is why they
are the two that run in CI. Everything else needs a token with admin on all ten
repositories, and the workflow token has admin on exactly one.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
OWNER = "IHP-GmbH"
HERE = pathlib.Path(__file__).resolve().parent

# The fields this repository declares. GitHub returns a good deal more (id,
# node_id, created_at, source, _links, current_user_can_bypass); those are its
# business. Comparison is "everything declared here is what is stored", not
# "nothing else exists", because the second one turns every GitHub-side addition
# into a red gate and teaches people to stop reading the output.
DECLARED = ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")


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


def make_api(auth):
    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", "Bearer " + auth)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, None
    return call


def payloads(only=None):
    """[(repo, body)] from the JSON files beside this script.

    The repository is the filename up to the first dot, so a repo can carry
    more than one ruleset: `KiCad-ADK-MOD.json` and
    `KiCad-ADK-MOD.fork-main.json` both apply to KiCad-ADK-MOD.
    """
    out = []
    for path in sorted(HERE.glob("*.json")):
        repo = path.name.split(".")[0]
        if only and repo != only:
            continue
        body = json.loads(path.read_text())
        missing = [k for k in DECLARED if k not in body]
        if missing:
            raise SystemExit("%s: payload is missing %s" % (path.name, missing))
        out.append((repo, body))
    return out


def has_integration_id(node):
    if isinstance(node, dict):
        return ("integration_id" in node
                or any(has_integration_id(v) for v in node.values()))
    if isinstance(node, list):
        return any(has_integration_id(v) for v in node)
    return False


def validate(entries):
    """Offline sanity of the payloads themselves. No network, no credentials.

    This is the half of the job that can run on a pull request: a hand edit that
    puts a ruleset in an unreachable shape is caught before anyone applies it,
    rather than at the moment somebody is trying to unblock a merge.
    """
    failures = []
    prefixes = {"branch": "refs/heads/", "tag": "refs/tags/"}
    seen = {}

    for repo, body in entries:
        tag = "%s/%s" % (repo, body.get("name"))
        if not repo:
            failures.append("%s: filename does not start with a repository name" % tag)
        key = (repo, body.get("name"))
        if key in seen:
            failures.append(
                "%s: two payloads claim the same ruleset name in the same "
                "repository, so applying them would have the second silently "
                "overwrite the first." % tag)
        seen[key] = True

        if body.get("target") not in prefixes:
            failures.append("%s: target %r is neither branch nor tag"
                            % (tag, body.get("target")))
        if body.get("enforcement") not in ("active", "evaluate", "disabled"):
            failures.append("%s: enforcement %r is not a value GitHub accepts"
                            % (tag, body.get("enforcement")))
        if not body.get("bypass_actors"):
            failures.append(
                "%s: no bypass actor. This repository has needed a force push "
                "after a filter-repo run twice; an empty bypass list means the "
                "next one has to go through deleting the ruleset first." % tag)

        want = prefixes.get(body.get("target"), "refs/")
        for ref in body.get("conditions", {}).get("ref_name", {}).get("include", []):
            if not ref.startswith(want) and not ref.startswith("~"):
                failures.append("%s: %r does not look like a %s ref"
                                % (tag, ref, body.get("target")))

        if has_integration_id(body):
            failures.append(
                "%s: payload sets integration_id. That pins a required check to "
                "one app and locks out any status posted with a user token."
                % tag)

        for rule in body.get("rules", []):
            if rule.get("type") != "required_status_checks":
                continue
            contexts = rule.get("parameters", {}).get("required_status_checks", [])
            if not contexts:
                failures.append(
                    "%s: a required_status_checks rule with no contexts requires "
                    "nothing, which reads as protection and is not." % tag)
            for c in contexts:
                if not c.get("context"):
                    failures.append("%s: a required check has no context name" % tag)
    return failures


def rule_map(body):
    return {r["type"]: r.get("parameters", {}) for r in body.get("rules", [])}


def actors(body):
    return sorted(
        (a.get("actor_id"), a.get("actor_type"), a.get("bypass_mode"))
        for a in body.get("bypass_actors", [])
    )


def refs(body):
    cond = body.get("conditions", {}).get("ref_name", {})
    return sorted(cond.get("include", [])), sorted(cond.get("exclude", []))


def compare(sent, got):
    """Differences between what was declared and what GitHub holds."""
    diffs = []
    if got is None:
        return ["nothing stored at all"]

    for key in ("name", "target", "enforcement"):
        if sent.get(key) != got.get(key):
            diffs.append("%s: declared %r, stored %r"
                         % (key, sent.get(key), got.get(key)))

    if actors(sent) != actors(got):
        diffs.append(
            "bypass_actors: declared %r, stored %r. An emptied bypass list locks "
            "the maintainer out of their own repository on the next force push "
            "that a filter-repo run makes necessary."
            % (actors(sent), actors(got)))

    if refs(sent) != refs(got):
        diffs.append("conditions.ref_name: declared %r, stored %r"
                     % (refs(sent), refs(got)))

    mine, theirs = rule_map(sent), rule_map(got)
    for kind in sorted(set(mine) - set(theirs)):
        diffs.append(
            "rule %r was declared but is not stored. GitHub drops a rule type it "
            "does not recognise instead of refusing the payload, so a typo here "
            "reads as a clean apply." % kind)
    for kind in sorted(set(theirs) - set(mine)):
        diffs.append("rule %r is stored but declared nowhere here. Somebody "
                     "edited this ruleset outside the tracked JSON." % kind)

    for kind in sorted(set(mine) & set(theirs)):
        for param, want in sorted(mine[kind].items()):
            have = theirs[kind].get(param)
            if have != want:
                diffs.append("rule %s parameter %s: declared %r, stored %r"
                             % (kind, param, want, have))

    for context in theirs.get("required_status_checks", {}).get(
            "required_status_checks", []):
        if context.get("integration_id") is not None:
            diffs.append(
                "required context %r carries integration_id %r. That pins the "
                "check to one app, so a status posted with a user token can "
                "never satisfy it. Nothing here sets that field; the web UI "
                "does, which is why rulesets are not edited there."
                % (context.get("context"), context.get("integration_id")))
    return diffs


def existing(repo, api):
    status, body = api("GET", "/repos/%s/%s/rulesets" % (OWNER, repo))
    if status != 200:
        raise SystemExit("%s: cannot list rulesets (HTTP %s)" % (repo, status))
    return body or []


def apply_one(repo, want, api, write):
    """Return (list of problem strings, action taken)."""
    listing = existing(repo, api)
    match = next((r for r in listing if r["name"] == want["name"]), None)

    if write:
        if match:
            status, got = api("PUT", "/repos/%s/%s/rulesets/%d"
                              % (OWNER, repo, match["id"]), want)
            action = "updated"
        else:
            status, got = api("POST", "/repos/%s/%s/rulesets" % (OWNER, repo), want)
            action = "created"
        if status not in (200, 201):
            return ["%s/%s: %s failed, HTTP %s: %s"
                    % (repo, want["name"], action, status,
                       (got or {}).get("message"))], action
    else:
        action = "checked"
        if not match:
            return ["%s/%s: declared here, absent on the remote"
                    % (repo, want["name"])], action

    # Always re-read. The response body of a write is not proof that a later
    # read returns the same thing, and the read is what a merge is judged by.
    ident = match["id"] if match else got["id"]
    status, stored = api("GET", "/repos/%s/%s/rulesets/%d" % (OWNER, repo, ident))
    if status != 200:
        return ["%s/%s: cannot read back (HTTP %s)" % (repo, want["name"], status)], action

    return ["%s/%s: %s" % (repo, want["name"], d) for d in compare(want, stored)], action


def run(entries, api, write):
    problems, seen = [], {}
    for repo, want in entries:
        found, action = apply_one(repo, want, api, write)
        problems += found
        seen.setdefault(repo, set()).add(want["name"])
        print("%-30s %-16s %-10s %s"
              % (repo, want["name"], action,
                 "%s -> %s" % (want["target"],
                               ",".join(want["conditions"]["ref_name"]["include"]))))

    for repo, names in sorted(seen.items()):
        for r in existing(repo, api):
            if r["name"] not in names:
                print("   note: %s also carries an unmanaged ruleset %r"
                      % (repo, r["name"]))
    return problems


def self_test():
    """Every difference this script is supposed to catch, made to happen."""
    want = {
        "name": "protected-lines",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
        "conditions": {"ref_name": {"include": ["refs/heads/dev"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": "ci-gate"}]}},
        ],
    }

    def mutated(**kw):
        got = json.loads(json.dumps(want))
        got.update(kw)
        return got

    cases = []

    def expect(name, got, want_fail, substring=None):
        diffs = compare(want, got)
        ok = bool(diffs) == want_fail
        if ok and substring:
            ok = any(substring in d for d in diffs)
        cases.append((name, ok, diffs))

    expect("an identical read-back passes", json.loads(json.dumps(want)), False)
    expect("a different enforcement fails", mutated(enforcement="evaluate"), True,
           "enforcement")
    expect("an emptied bypass list fails", mutated(bypass_actors=[]), True,
           "bypass_actors")
    expect("a changed ref condition fails",
           mutated(conditions={"ref_name": {"include": ["refs/heads/main"],
                                            "exclude": []}}), True, "ref_name")
    expect("a silently dropped rule fails",
           mutated(rules=[r for r in want["rules"] if r["type"] != "deletion"]),
           True, "not stored")
    expect("a rule added outside the tracked JSON fails",
           mutated(rules=want["rules"] + [{"type": "non_fast_forward"}]),
           True, "declared nowhere")
    expect("a changed rule parameter fails",
           mutated(rules=[{"type": "deletion"},
                          {"type": "required_status_checks", "parameters": {
                              "strict_required_status_checks_policy": True,
                              "required_status_checks": [{"context": "ci-gate"}]}}]),
           True, "strict_required_status_checks_policy")
    expect("a dropped required context fails",
           mutated(rules=[{"type": "deletion"},
                          {"type": "required_status_checks", "parameters": {
                              "strict_required_status_checks_policy": False,
                              "required_status_checks": []}}]),
           True, "required_status_checks")
    expect("a context pinned to an app fails",
           mutated(rules=[{"type": "deletion"},
                          {"type": "required_status_checks", "parameters": {
                              "strict_required_status_checks_policy": False,
                              "required_status_checks": [
                                  {"context": "ci-gate", "integration_id": 15368}]}}]),
           True, "integration_id")
    expect("nothing stored at all fails", None, True)

    def expect_v(name, entries, want_fail, substring=None):
        got = validate(entries)
        ok = bool(got) == want_fail
        if ok and substring:
            ok = any(substring in g for g in got)
        cases.append((name, ok, got))

    expect_v("a sane payload validates", [("r", want)], False)
    expect_v("two payloads with one name in one repo fail",
             [("r", want), ("r", mutated())], True, "silently overwrite")
    expect_v("an unknown target fails", [("r", mutated(target="commit"))],
             True, "neither branch nor tag")
    expect_v("an unknown enforcement fails",
             [("r", mutated(enforcement="on"))], True, "not a value GitHub accepts")
    expect_v("an empty bypass list fails validation",
             [("r", mutated(bypass_actors=[]))], True, "no bypass actor")
    expect_v("a tag ref under a branch target fails",
             [("r", mutated(conditions={"ref_name": {"include": ["refs/tags/v*"],
                                                     "exclude": []}}))],
             True, "does not look like a branch ref")
    expect_v("integration_id anywhere in a payload fails",
             [("r", mutated(rules=[{"type": "required_status_checks", "parameters": {
                 "required_status_checks": [
                     {"context": "ci-gate", "integration_id": 15368}]}}]))],
             True, "integration_id")
    expect_v("a required_status_checks rule with no contexts fails",
             [("r", mutated(rules=[{"type": "required_status_checks", "parameters": {
                 "required_status_checks": []}}]))],
             True, "requires\nnothing".replace("\n", " "))

    # The two write paths, against a stub that records what it was asked to do.
    calls = []

    def stub(method, path, body=None):
        calls.append((method, path))
        if method == "GET" and path.endswith("/rulesets"):
            return 200, ([{"id": 7, "name": "protected-lines"}] if stub.exists else [])
        if method == "GET":
            return 200, json.loads(json.dumps(want))
        return (200 if method == "PUT" else 201), {"id": 7}

    stub.exists = False
    calls.clear()
    apply_one("r", want, stub, write=True)
    cases.append(("an absent ruleset is created",
                  any(m == "POST" for m, _ in calls), calls))

    stub.exists = True
    calls.clear()
    apply_one("r", want, stub, write=True)
    cases.append(("an existing ruleset is updated in place",
                  any(m == "PUT" and p.endswith("/rulesets/7") for m, p in calls),
                  calls))

    stub.exists = False
    calls.clear()
    found, _ = apply_one("r", want, stub, write=False)
    cases.append(("--check never writes, and reports what is missing",
                  not any(m in ("PUT", "POST") for m, _ in calls) and bool(found),
                  calls))

    ok = True
    for name, passed, detail in cases:
        print("%-52s %s" % (name, "ok" if passed else "FAILED"))
        if not passed:
            ok = False
            print("   got: %r" % (detail,))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true",
                      help="offline: the payloads themselves are well formed")
    mode.add_argument("--check", action="store_true",
                      help="read-only: report any drift from the tracked JSON")
    mode.add_argument("--apply", action="store_true",
                      help="create or update, then read back and verify")
    mode.add_argument("--self-test", action="store_true")
    ap.add_argument("--enforcement", choices=("active", "evaluate", "disabled"),
                    help="override the enforcement in the JSON, for a staged rollout")
    ap.add_argument("--repo", help="limit to one repository")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    entries = payloads(args.repo)
    if not entries:
        print("FAIL: no payloads matched, so this would pass having done nothing",
              file=sys.stderr)
        return 1
    if args.enforcement:
        for _, body in entries:
            body["enforcement"] = args.enforcement

    if args.validate:
        failures = validate(entries)
        for repo, body in entries:
            print("%-30s %-16s %s -> %s"
                  % (repo, body["name"], body["target"],
                     ",".join(body["conditions"]["ref_name"]["include"])))
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        if failures:
            return 1
        print("\n%d payloads are well formed" % len(entries))
        return 0

    api = make_api(token())
    problems = run(entries, api, write=args.apply)

    for p in problems:
        print("FAIL: %s" % p, file=sys.stderr)
    if problems:
        return 1
    print("\n%d rulesets across %d repositories match the tracked JSON"
          % (len(entries), len({r for r, _ in entries})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
