#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for the tracked-tree digest in nix/tool_identity.py, the port of
packaging-flow's verification/tracked_tree.py, whose code it matches.

Each case is a git repository built here with fixed contents, so what the
digest reads (paths and bytes, never the index's stat fields) is the same on
any host. RECORDED holds what packaging-flow's tracked_tree_sha256 gave on each
case: the digest, or its Unresolved.why. The port must give the
same on every host. Where packaging_flow imports (the image's venv, the Nix
lane's), LiveParity also runs packaging-flow's own function on every case and
requires the two to agree. It skips where packaging_flow does not import, so
the image's verify stage and the lane gate each name it in a required-tests
list, where a skip fails.

Usage: python3 nix/test_tracked_tree.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tool_identity as ti  # noqa: E402

# What packaging-flow's tracked_tree.py gave on each case.
RECORDED = {
    "clean": "3e29be67c17e2b0f28cb958f085a2ae72f1313bded254a66077839b8ed2c8ff5",
    "edited": "README.txt differs from its entry in .git/index",
    "untracked": "notes.txt is not tracked",
    "executable_bit": "bin/run.sh differs from its entry in .git/index in its executable bit",
    "missing": "pkg/__init__.py is tracked and missing",
    "link": "link is a link",
    "folder_link": "lib is a link",
    "nested_git": "sub/.git/HEAD is not tracked",
    "least_path": "bin/zz.txt is not tracked",
    "index_before_walk": "pkg/__init__.py is marked skip-worktree or intent-to-add in .git/index",
    "intent_to_add": "new.py is marked skip-worktree or intent-to-add in .git/index",
    "object_format": ".git/config names the object format sha256, not sha1",
    "index_v4": ".git/index is not a version 2 or 3 index",
    "empty_index": ".git/index tracks no file",
    "gitfile": ".git is not a folder",
    "tree_link": ". is not a folder",
}
NON_ASCII = "pkg/donn\u00e9es.txt"


def git(repo, *args):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo),
           "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    return subprocess.run(["git", "-C", str(repo), "-c", "core.fileMode=true",
                           "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", *args],
                          check=True, capture_output=True, text=True, env=env).stdout


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def clean_repo(repo: Path) -> Path:
    """The base case: a plain file, a 100755 script, a package with a non-ASCII
    file name, committed with HEAD detached; and the untracked content the digest
    allows, a pip record and a bytecode file."""
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    write(repo / "README.txt", "a staged checkout\n")
    write(repo / "bin" / "run.sh", "#!/bin/sh\nexit 0\n", 0o755)
    write(repo / "pkg" / "__init__.py", "VALUE = 1\n")
    write(repo / NON_ASCII, "caf\u00e9\n")
    git(repo, "add", "-A")
    git(repo, "update-index", "--index-version", "2")
    git(repo, "commit", "-q", "-m", "fixture")
    git(repo, "checkout", "-q", "--detach")
    write(repo / "x.egg-info" / "PKG-INFO", "Name: x\n")
    write(repo / "pkg" / "__pycache__" / "m.cpython-312.pyc", "not code\n")
    return repo


def build(case: str, root: Path) -> Path:
    """The tree of one case, under root."""
    repo = clean_repo(root / "repo")
    if case == "edited":
        write(repo / "README.txt", "edited after the commit\n")
    elif case == "untracked":
        write(repo / "notes.txt", "not added\n")
    elif case == "executable_bit":
        os.chmod(repo / "bin" / "run.sh", 0o644)
    elif case == "missing":
        (repo / "pkg" / "__init__.py").unlink()
    elif case == "link":
        os.symlink("README.txt", repo / "link")
    elif case == "folder_link":
        os.symlink("pkg", repo / "lib")
    elif case == "nested_git":
        write(repo / "sub" / ".git" / "HEAD", "ref: refs/heads/main\n")
    elif case == "least_path":
        write(repo / "pkg" / "__init__.py", "VALUE = 2\n")
        write(repo / "bin" / "zz.txt", "not added\n")
    elif case == "index_before_walk":
        git(repo, "update-index", "--skip-worktree", "pkg/__init__.py")
        write(repo / "a.txt", "not added, and less than pkg/\n")
    elif case == "intent_to_add":
        write(repo / "new.py", "pass\n")
        git(repo, "add", "-N", "new.py")
    elif case == "object_format":
        with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
            f.write("[extensions]\n\tobjectformat = sha256\n")
        write(repo / "a.txt", "not added\n")
    elif case == "index_v4":
        git(repo, "update-index", "--index-version", "4")
    elif case == "empty_index":
        git(repo, "read-tree", "--empty")
        git(repo, "update-index", "--index-version", "2")
    elif case == "gitfile":
        (repo / ".git").rename(root / "repo.git")
        write(repo / ".git", f"gitdir: {root / 'repo.git'}\n")
    elif case == "tree_link":
        os.symlink("repo", root / "via-link")
        return root / "via-link"
    elif case != "clean":
        raise ValueError(case)
    return repo


def port_reading(tree: Path) -> str:
    try:
        return ti.tracked_tree_sha256(tree)
    except ti.TreeRefused as err:
        return err.why


class Cases(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def case(self, name: str) -> Path:
        return build(name, self.tmp / name)

    def test_every_case_reads_as_recorded(self):
        for name, want in RECORDED.items():
            with self.subTest(case=name):
                self.assertEqual(port_reading(self.case(name)), want)

    def test_the_digest_is_over_paths_and_bytes(self):
        tree = self.case("clean")
        files = {rel: ti.hashlib.sha256((tree / rel).read_bytes()).hexdigest()
                 for rel in ("README.txt", "bin/run.sh", "pkg/__init__.py", NON_ASCII)}
        document = ti.tracked_document(files)
        self.assertFalse(document.endswith(b"\n"))
        self.assertIn(b"pkg/donn\\u00e9es.txt", document)
        self.assertEqual(ti.tracked_tree_sha256(tree), ti.hashlib.sha256(document).hexdigest())

    def test_two_clones_of_one_commit_read_the_same(self):
        first = self.case("clean")
        second = self.tmp / "second"
        git(self.tmp, "clone", "-q", "--no-hardlinks", str(first), str(second))
        self.assertNotEqual((first / ".git" / "index").read_bytes(),
                            (second / ".git" / "index").read_bytes())
        self.assertEqual(ti.tracked_tree_sha256(first), ti.tracked_tree_sha256(second))

    def test_a_refusal_is_a_value_error(self):
        with self.assertRaises(ValueError):
            ti.tracked_tree_sha256(self.case("untracked"))


class Head(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = clean_repo(self.tmp / "repo")
        self.commit = git(self.repo, "rev-parse", "HEAD").strip()

    def test_a_detached_head_is_its_commit(self):
        self.assertEqual(ti.tracked_head(self.repo), self.commit)

    def test_a_branch_head_resolves_through_its_loose_ref(self):
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(ti.tracked_head(self.repo), self.commit)

    def test_a_branch_head_resolves_through_packed_refs(self):
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "pack-refs", "--all")
        self.assertFalse((self.repo / ".git" / "refs" / "heads" / "main").exists())
        self.assertEqual(ti.tracked_head(self.repo), self.commit)

    def test_a_head_that_resolves_to_no_commit_is_refused(self):
        (self.repo / ".git" / "HEAD").write_text("ref: refs/heads/absent\n")
        with self.assertRaisesRegex(ti.TreeRefused, "resolves to no commit"):
            ti.tracked_head(self.repo)

    def test_a_head_that_is_neither_is_refused(self):
        (self.repo / ".git" / "HEAD").write_text(self.commit.upper() + "\n")
        with self.assertRaisesRegex(ti.TreeRefused, "neither a commit nor a ref"):
            ti.tracked_head(self.repo)

    def test_the_version(self):
        self.assertEqual(ti.tracked_version(self.repo),
                         f"commit {self.commit} tracked {RECORDED['clean']}")


class Entry(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = clean_repo(self.tmp / "repo")

    def test_a_tracked_entry(self):
        script = self.tmp / "venv" / "bin" / "tool"
        write(script, "#!/venv/bin/python\n", 0o755)
        os.symlink(script, self.tmp / "tool-link")
        entry = ti.tracked_entry("tool", str(self.tmp / "tool-link"), str(self.repo))
        self.assertEqual(entry, {
            "name": "tool", "from": "image", "path": None, "nar_sha256": None,
            "closure_sha256": None, "version": ti.tracked_version(self.repo),
            "binary": os.path.realpath(script), "binary_sha256": ti.file_sha256(str(script))})

    def test_a_tree_with_no_digest_refuses_the_entry(self):
        write(self.repo / "notes.txt", "not added\n")
        with self.assertRaisesRegex(ValueError, r"^tool: .*: no tracked digest: notes.txt is not tracked$"):
            ti.tracked_entry("tool", "/bin/sh", str(self.repo))

    def test_a_spec_wants_absolute_paths(self):
        self.assertEqual(ti.tracked_spec("t=/a/b:/c"), ("t", "/a/b", "/c"))
        for spec in ("t=a/b:/c", "t=/a/b:c", "t=/a/b", "=/a:/c"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                ti.tracked_spec(spec)


class LiveParity(unittest.TestCase):
    """packaging-flow's own function on every case. Required where it imports."""

    def test_every_case_as_packaging_flow_reads_it(self):
        try:
            from packaging_flow.verification.key import Unresolved
            from packaging_flow.verification.tracked_tree import tracked_tree_sha256
        except ImportError as err:
            self.skipTest(f"packaging_flow does not import here: {err}")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for name in RECORDED:
            with self.subTest(case=name):
                tree = build(name, tmp / name)
                theirs = tracked_tree_sha256(tree)
                theirs = theirs.why if isinstance(theirs, Unresolved) else theirs
                self.assertEqual(port_reading(tree), theirs)


if __name__ == "__main__":
    unittest.main()
