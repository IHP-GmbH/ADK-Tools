#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The interposer PDK the image ships labels its layouts with the version it is pinned at.

The interposer PDK records its version in every layout it makes, through its
own reader (libs.tech/klayout/python/intm4tm2_pycell_lib/ihp/pdk_version.py):
'intm4tm2 <version>', the version its pyproject.toml declares. In a checkout
with tracked local changes the reader appends ' (local changes)'; a tree
without .git, as the image's copy is (.dockerignore drops it), gets no suffix.
The commit is not in the label; the image's manifest records it.

On the host, --print-version reads the version that the pinned commit's
pyproject.toml declares, with git, from the submodule: the commit the image's
manifest records, so the expected value does not come from the tree under
check, and a copy that differs from the pin fails instead of passing on its
own word. In the image, the check
runs the reader on the copied tree and fails unless the label is exactly
'intm4tm2 <that version>': no suffix, not 'unknown', no other version. The
copied tree is the pinned commit because build.sh refuses a submodule that is
uninitialised, dirty or not at its committed gitlink. A tree without the
reader fails too: the label is what a layout made in the image says about its
PDK, and a build that cannot show it is right does not pass. Neither mode
writes anything.

Callers run it with python3 -B, so loading the reader leaves no __pycache__ in
the tree.

Usage:
    check_interposer_label.py --repo tools/OpenIntM4TM2 --pin SHA --print-version
    check_interposer_label.py --tree /opt/adk-tools/OpenIntM4TM2 --expect-version VERSION
    check_interposer_label.py --self-test
"""

import argparse
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile

try:
    import tomllib
except ImportError:  # Python 3.10 and older
    tomllib = None

READER = pathlib.Path("libs.tech/klayout/python/intm4tm2_pycell_lib/ihp/pdk_version.py")
PIN = re.compile(r"[0-9a-f]{40}")
# A released version: digits and dots, nothing after them.
VERSION = re.compile(r"[0-9]+(\.[0-9]+)+")
# The reader's name, written here rather than read from it, so that the
# expected label does not come from the code under check.
NAME = "intm4tm2"


def load_reader(tree):
    """The tree's pdk_version module, or None when the tree has no reader."""
    path = pathlib.Path(tree) / READER
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_shipped_pdk_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_env():
    """This environment without GIT_* variables, so git acts only on the repository named."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def released_version_failure(version, where):
    """None when `version` is a released version, else what is wrong with it."""
    if isinstance(version, str) and VERSION.fullmatch(version):
        return None
    return "%s %r is not a released version (digits and dots)" % (where, version)


def version_at(repo, pin):
    """(failure or None, the [project] version of pyproject.toml at commit `pin` of `repo`)."""
    if not PIN.fullmatch(pin or ""):
        return "the pin %r is not a 40-character lowercase commit id" % pin, None
    if tomllib is None:
        return "reading pyproject.toml needs Python 3.11 or later (tomllib)", None
    proc = subprocess.run(["git", "-C", str(repo), "show", "%s:pyproject.toml" % pin],
                          capture_output=True, check=False, env=_git_env())
    if proc.returncode:
        return ("git could not read pyproject.toml at %s in %s: %s"
                % (pin, repo, proc.stderr.decode(errors="replace").strip())), None
    try:
        version = tomllib.loads(proc.stdout.decode()).get("project", {}).get("version")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return "pyproject.toml at %s is not valid TOML: %s" % (pin, exc), None
    if version is None:
        return "pyproject.toml at %s declares no [project] version" % pin, None
    return released_version_failure(version, "pyproject.toml at %s declares version" % pin), \
        version


def check(tree, version):
    """[failure] for the shipped tree against the pinned version, and the label read."""
    tree = pathlib.Path(tree)
    failure = released_version_failure(
        version, "the expected version (--expect-version; build.sh passes it as the build "
                 "argument INTERPOSER_VERSION)")
    if failure:
        return [failure], None
    reader = load_reader(tree)
    if reader is None:
        return ["%s has no %s, so nothing shows what its layouts will record"
                % (tree, READER)], None
    label = reader.pdk_version(tree)
    want = "%s %s" % (NAME, version)
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
            failures, label = check(tree, version)
            after = sorted(p.relative_to(tree) for p in tree.rglob("*"))
            return failures, label, before == after

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
            git("add", "-A")
            git("commit", "-q", "--allow-empty", "-m", "x")
            return git("rev-parse", "HEAD")

        def git(*args):
            # no global or system config: a user's hooks or signing do not reach the test
            env = dict(_git_env(), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                       GIT_COMMITTER_EMAIL="t@t")
            return subprocess.run(["git", "-C", tmp, *args], check=True, env=env,
                                  capture_output=True, text=True).stdout.strip()

        git("init", "-q")
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
    parser.add_argument("--repo", help="the interposer PDK submodule, on the host")
    parser.add_argument("--pin", help="the commit adk-tools pins it at")
    parser.add_argument("--print-version", action="store_true",
                        help="print the version the pinned commit declares")
    parser.add_argument("--tree", help="the shipped interposer PDK tree")
    parser.add_argument("--expect-version", help="the version the pinned commit declares")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.print_version:
        if not args.repo or args.pin is None:
            parser.error("--print-version needs --repo and --pin")
        failure, version = version_at(args.repo, args.pin)
        if failure:
            print("FAIL: %s" % failure, file=sys.stderr)
            return 1
        print(version)
        return 0
    if not args.tree or args.expect_version is None:
        parser.error("--tree and --expect-version are required")
    failures, label = check(args.tree, args.expect_version)
    for failure in failures:
        print("FAIL: %s" % failure, file=sys.stderr)
    if failures:
        return 1
    print("interposer label: %s" % label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
