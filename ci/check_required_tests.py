#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Named tests must have run and passed, not merely not failed.

Every suite in this ecosystem guards itself with about ninety skips: `pcbnew`
absent, KLayout absent, the interposer `.lyp` absent, a sibling checkout absent.
On a bare runner that is honest, and the per-repo gates are green with a large
part of their suites never executing. This job is the one place where those
capabilities are all present at once, so it is the one place where a skip means
something went wrong rather than something is missing.

"Green" cannot express that. A suite whose tests all skip exits 0. So the job
declares which node ids it is providing the capability for, and this asserts
each of them ran and passed:

  ran      the id is present in the junit XML at all. A module-level
           `importorskip` produces a collect report that per-test hooks never
           see, so the id is simply absent; it has to be an error to be missing,
           not an error to be marked skipped.
  passed   no skipped, failure or error element on it.

A deleted or renamed test also fails, which is the point: the manifest is a
claim about coverage, and a claim that quietly stops being checked when someone
renames a function is not a claim.

Deliberately not done: matching on the skip reason. Module-scope
`importorskip` never reaches a per-test report, so a reason regex would pass
over exactly the cases that matter here.

Usage:
    check_required_tests.py ci/required-tests.txt --junit adk=adk.xml --junit plugin=plugin.xml
    check_required_tests.py --self-test
"""

import argparse
import pathlib
import sys
import xml.etree.ElementTree as ET


def parse_manifest(text):
    """[(repo, nodeid)] from `repo<space>nodeid` lines.

    Split on the first whitespace only, and the rest of the line is the node id
    verbatim. Not colon-separated, because a node id is full of colons; and not
    fully whitespace-separated either, because a parametrised id contains
    spaces of its own, sometimes only spaces:

        plugin tests/test_cmim_devices.py::test_parse_length_um_rejects_junk[   ]

    A `#` only starts a comment at the start of a line, for the same reason.
    """
    entries, bad = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("#") or not raw.strip():
            continue
        parts = raw.strip().split(None, 1)
        if len(parts) != 2 or not parts[1].strip():
            bad.append("line %d: %r is not `repo nodeid`" % (n, raw.strip()))
            continue
        entries.append((parts[0], parts[1]))
    return entries, bad


def node_ids(xml_text):
    """{nodeid: outcome} out of a pytest junit XML.

    pytest writes `file` and `name`, and `classname` as the dotted module plus
    any enclosing classes. The node id is the file, then whatever the classname
    carries beyond the module, then the name.
    """
    root = ET.fromstring(xml_text)
    cases = root.iter("testcase")
    out = {}
    for case in cases:
        path = case.get("file")
        name = case.get("name")
        classname = case.get("classname") or ""
        if not path or not name:
            continue
        module = path[:-3] if path.endswith(".py") else path
        module = module.replace("/", ".")
        parts = [path]
        if classname and classname != module:
            tail = classname[len(module):].lstrip(".") if classname.startswith(module) \
                else classname.rsplit(".", 1)[-1]
            if tail:
                parts.extend(tail.split("."))
        parts.append(name)
        nodeid = "::".join(parts)

        outcome = "passed"
        for kind in ("skipped", "failure", "error"):
            if case.find(kind) is not None:
                outcome = kind
                break
        out[nodeid] = outcome
    return out


def check(entries, reports):
    """reports: {repo: {nodeid: outcome}}. Returns [failure, ...]."""
    failures = []
    wanted = {}
    for repo, nodeid in entries:
        wanted.setdefault(repo, []).append(nodeid)

    for repo in sorted(wanted):
        if repo not in reports:
            failures.append(
                "%s: the manifest requires %d test(s) from it and no junit XML "
                "was given, so nothing was verified for it"
                % (repo, len(wanted[repo])))
            continue
        seen = reports[repo]
        for nodeid in wanted[repo]:
            outcome = seen.get(nodeid)
            if outcome is None:
                failures.append(
                    "%s: %s did not run. Either it was renamed or removed, or "
                    "it was skipped at collection time, which produces no "
                    "per-test report at all." % (repo, nodeid))
            elif outcome == "skipped":
                failures.append(
                    "%s: %s skipped. This job exists to supply the capability "
                    "it is guarded on, so a skip here means the capability was "
                    "not actually supplied." % (repo, nodeid))
            elif outcome != "passed":
                failures.append("%s: %s %s" % (repo, nodeid, outcome))

    for repo in sorted(reports):
        if not reports[repo]:
            failures.append(
                "%s: its junit XML contains no test cases at all, which is what "
                "a collection error looks like from here" % repo)
    return failures


def self_test():
    cases = []

    def xml(*rows):
        body = ""
        for path, classname, name, kind in rows:
            inner = "" if kind is None else "<%s/>" % kind
            body += ('<testcase file="%s" classname="%s" name="%s">%s</testcase>'
                     % (path, classname, name, inner))
        return '<?xml version="1.0"?><testsuites><testsuite>%s</testsuite></testsuites>' % body

    passed = xml(("tests/test_a.py", "tests.test_a", "test_one", None))

    def expect(name, ok):
        cases.append((name, ok))

    ids = node_ids(passed)
    expect("a module-level test gets file::name",
           ids == {"tests/test_a.py::test_one": "passed"})

    ids = node_ids(xml(("tests/test_a.py", "tests.test_a.TestX", "test_one", None)))
    expect("a class-based test gets file::Class::name",
           ids == {"tests/test_a.py::TestX::test_one": "passed"})

    ids = node_ids(xml(("tests/test_a.py", "tests.test_a", "test_one[0.5]", None)))
    expect("a parametrised id keeps its brackets",
           "tests/test_a.py::test_one[0.5]" in ids)

    entries, bad = parse_manifest("# a comment\nadk tests/test_a.py::test_one\n\n")
    expect("the manifest reads `repo nodeid` and drops comments",
           entries == [("adk", "tests/test_a.py::test_one")] and not bad)

    _, bad = parse_manifest("tests/test_a.py::test_one\n")
    expect("a line with no repo is rejected", bool(bad))

    entries, bad = parse_manifest(
        "plugin tests/test_a.py::test_one[  8.11UM -8.11]\n")
    expect("a parametrised id containing spaces survives intact",
           not bad and entries == [
               ("plugin", "tests/test_a.py::test_one[  8.11UM -8.11]")])

    entries, bad = parse_manifest("plugin tests/test_a.py::test_junk[   ]\n")
    expect("an id whose parameter is only spaces survives",
           not bad and entries == [("plugin", "tests/test_a.py::test_junk[   ]")])

    _, bad = parse_manifest("plugin\n")
    expect("a line with a repo and no id is rejected", bool(bad))

    want = [("adk", "tests/test_a.py::test_one")]
    expect("a required test that passed passes",
           not check(want, {"adk": node_ids(passed)}))

    for kind, label in (("skipped", "a skip fails"),
                        ("failure", "a failure fails"),
                        ("error", "an error fails")):
        report = node_ids(xml(("tests/test_a.py", "tests.test_a", "test_one", kind)))
        expect(label, bool(check(want, {"adk": report})))

    expect("a renamed test fails rather than being absent quietly",
           any("did not run" in f for f in check(
               want, {"adk": node_ids(xml(
                   ("tests/test_a.py", "tests.test_a", "test_renamed", None)))})))

    expect("a repo with no junit XML fails",
           any("no junit XML was given" in f for f in check(want, {})))

    expect("an empty junit XML fails",
           any("no test cases at all" in f
               for f in check([], {"adk": node_ids(xml())})))

    ok = True
    for name, passing in cases:
        print("%-62s %s" % (name, "ok" if passing else "FAILED"))
        ok = ok and passing
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", nargs="?")
    ap.add_argument("--junit", action="append", default=[], metavar="REPO=PATH")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.manifest:
        ap.error("a manifest path is required unless --self-test is given")

    entries, bad = parse_manifest(pathlib.Path(args.manifest).read_text())
    for b in bad:
        print("FAIL: %s: %s" % (args.manifest, b), file=sys.stderr)
    if bad:
        return 1
    if not entries:
        print("FAIL: %s lists no tests, so this check would pass having "
              "verified nothing" % args.manifest, file=sys.stderr)
        return 1

    reports = {}
    for item in args.junit:
        if "=" not in item:
            ap.error("--junit wants REPO=PATH, got %r" % item)
        repo, path = item.split("=", 1)
        reports[repo] = node_ids(pathlib.Path(path).read_text())

    failures = check(entries, reports)
    for repo in sorted(reports):
        ran = sum(1 for r, _ in entries if r == repo)
        print("%-12s %4d cases in the report, %d required" % (
            repo, len(reports[repo]), ran))
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("\n%d required tests ran and passed" % len(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
