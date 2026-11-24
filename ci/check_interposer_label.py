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
import subprocess
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

    # A stub with the reader's one function, whose label the test sets, so each
    # case reaches one branch of check(); the real reader is checked against a
    # real tree in CI and in the image build.
    reader = "def pdk_version(root):\n    return %r\n"

    def run(label="intm4tm2 0.8.0", with_reader=True, version="0.8.0"):
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            if with_reader:
                (tree / READER).parent.mkdir(parents=True)
                (tree / READER).write_text(reader % label)
            before = sorted(p.relative_to(tree) for p in tree.rglob("*"))
            out = check(tree, version)
            after = sorted(p.relative_to(tree) for p in tree.rglob("*"))
            return out + (before == after,)

    failures, label, kept = run()
    expect("a tree labelled with the pinned version passes",
           not failures and label == "intm4tm2 0.8.0")
    expect("the check writes nothing in the tree", kept)

    for bad in ("intm4tm2 0.8.0 (local changes)", "intm4tm2 unknown",
                "intm4tm2 0.8.0 (0123456789abcdef0123456789abcdef01234567)",
                "intm4tm2 0.7.0"):
        failures, _, _ = run(label=bad)
        expect("the label %r fails" % bad, any(bad in f for f in failures))

    failures, _, _ = run(with_reader=False)
    expect("a tree without the reader fails", any("has no" in f for f in failures))

    for bad in ("", "unknown", "0.8.0 (local changes)", "0.8.0rc1", "v0.8.0", None):
        failures, _, _ = run(version=bad)
        expect("the version %r is refused" % bad,
               any("not a released version" in f for f in failures))

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)

        def commit(text):
            if text is None:
                (repo / "pyproject.toml").unlink(missing_ok=True)
            else:
                (repo / "pyproject.toml").write_text(text)
            env = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            subprocess.run(["git", "-C", tmp, *env, "add", "-A"], check=True)
            subprocess.run(["git", "-C", tmp, *env, "commit", "-q", "--allow-empty", "-m", "x"],
                           check=True)
            return subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()

        subprocess.run(["git", "init", "-q", tmp], check=True)
        good = commit('[project]\nname = "x"\nversion = "0.8.0"\n')
        newer = commit('[project]\nname = "x"\nversion = "0.9.0"\n')
        failure, version = version_at(repo, good)
        expect("the version is read at the pinned commit, not at HEAD",
               failure is None and version == "0.8.0")
        failure, version = version_at(repo, newer)
        expect("a newer pin reads its own version", failure is None and version == "0.9.0")
        for text, words in (('[project]\nname = "x"\n', "declares no [project] version"),
                            ('[project]\nversion = "0.8.0.dev1"\n', "not a released version"),
                            ('[project\n', "not valid TOML"),
                            (None, "git could not read")):
            failure, _ = version_at(repo, commit(text))
            expect("a pin whose pyproject.toml gives %r fails" % words,
                   failure is not None and words in failure)
        for bad in ("", good[:10], good.upper(), good + "-dirty"):
            failure, _ = version_at(repo, bad)
            expect("the pin %r is refused" % bad,
                   failure is not None and "is not a 40-character" in failure)

    for name, ok in cases:
        print("%-72s %s" % (name, "ok" if ok else "FAIL"))
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
