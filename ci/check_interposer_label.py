#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The interposer PDK the image ships labels its layouts with the commit it is pinned at.

The interposer PDK records its version in every layout it makes, through its
own reader (libs.tech/klayout/python/intm4tm2_pycell_lib/ihp/pdk_version.py):
'intm4tm2 <version> (<commit>)', the version from its pyproject.toml and the
commit from git, or, in a tree without .git, from the reader's STAMP file at
its top. The image's copy has no .git (.dockerignore drops it), so the build
writes that file.

This writes the pin to the reader's STAMP, then runs the reader on the tree
and fails unless it reports exactly that pin and a declared version. Writing
the pin rather than reading the commit back from the tree is right because
build.sh refuses a submodule that is uninitialised, dirty or not at its
committed gitlink, so the copied tree is that commit. A tree without the
reader fails too: the label is what a layout made in the image says about its
PDK, and a build that cannot show it is right does not pass.

Callers run it with python3 -B, so loading the reader leaves no __pycache__ in
the tree.

Usage:
    check_interposer_label.py --tree /opt/adk-tools/OpenIntM4TM2 --pin SHA
    check_interposer_label.py --self-test
"""

import argparse
import importlib.util
import pathlib
import re
import sys
import tempfile

READER = pathlib.Path("libs.tech/klayout/python/intm4tm2_pycell_lib/ihp/pdk_version.py")
PIN = re.compile(r"[0-9a-f]{40}")


def load_reader(tree):
    """The tree's pdk_version module, or None when the tree has no reader."""
    path = pathlib.Path(tree) / READER
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_shipped_pdk_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(tree, pin):
    """[failure] for the shipped tree against the pinned commit, and the label read."""
    tree = pathlib.Path(tree)
    if not PIN.fullmatch(pin or ""):
        return ["the pin %r is not a 40-character lowercase commit id" % pin], None
    reader = load_reader(tree)
    if reader is None:
        return ["%s has no %s, so nothing shows what its layouts will record"
                % (tree, READER)], None
    version = reader.declared_version(tree)
    if version is None:
        return ["%s/pyproject.toml declares no [project] version" % tree], None
    (tree / reader.STAMP).write_text(pin + "\n", encoding="utf-8")
    label = reader.pdk_version(tree)
    want = "%s %s (%s)" % (reader.NAME, version, pin)
    if label != want:
        return ["the shipped tree labels its layouts '%s', not '%s'" % (label, want)], label
    return [], label


def self_test():
    cases = []

    def expect(name, ok):
        cases.append((name, ok))

    pin = "0123456789abcdef0123456789abcdef01234567"
    # A stub with the reader's names, whose label is the stamp plus a suffix the
    # test sets, so each case reaches one branch of check(); the real reader is
    # checked against a real tree in CI and in the image build.
    reader = (
        "import pathlib\n"
        "NAME = 'intm4tm2'\n"
        "STAMP = '.stamp'\n"
        "VERSION = {version!r}\n"
        "def declared_version(root):\n"
        "    return VERSION\n"
        "def pdk_version(root):\n"
        "    commit = (pathlib.Path(root) / STAMP).read_text().strip()\n"
        "    return '%s %s (%s%s)' % (NAME, VERSION, commit, {suffix!r})\n")

    def run(version="0.2.0", suffix="", with_reader=True, want=pin):
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            if with_reader:
                (tree / READER).parent.mkdir(parents=True)
                (tree / READER).write_text(reader.format(version=version, suffix=suffix))
            return check(tree, want)

    failures, label = run()
    expect("a tree whose reader reads the stamped pin back passes",
           not failures and label == "intm4tm2 0.2.0 (%s)" % pin)

    failures, _ = run(suffix="-dirty")
    expect("a label that is not the pin fails", any("-dirty" in f for f in failures))

    failures, _ = run(with_reader=False)
    expect("a tree without the reader fails", any("has no" in f for f in failures))

    failures, _ = run(version=None)
    expect("a tree declaring no version fails", any("declares no" in f for f in failures))

    for bad in ("", pin[:10], pin.upper(), pin + "-dirty"):
        failures, _ = run(want=bad)
        expect("the pin %r is refused" % bad, any("is not a 40-character" in f for f in failures))

    for name, ok in cases:
        print("%-60s %s" % (name, "ok" if ok else "FAIL"))
    return 0 if all(ok for _, ok in cases) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", help="the shipped interposer PDK tree")
    parser.add_argument("--pin", help="the commit adk-tools pins it at")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.tree or args.pin is None:
        parser.error("--tree and --pin are required")
    failures, label = check(args.tree, args.pin)
    for failure in failures:
        print("FAIL: %s" % failure, file=sys.stderr)
    if failures:
        return 1
    print("interposer label: %s" % label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
