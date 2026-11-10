#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""sg13g2-models.sha256 is the include closure of its roots, in every section.

The list names the ihp-sg13g2 files the image fetches for a mixed co-simulation
case with a transistor-level sg13g2 die. The case asks ngspice for one section
of a corner library (`.lib cornerMOSlv.lib mos_tt`), but ngspice reads the whole
file and resolves every `.include` in it, in every section, before it looks for
the one asked: a file included only under mos_tt_mismatch must be there for a
case that asks for mos_tt, or ngspice stops on its first line with "Could not
find include file". The first list followed the named sections only, and an
image built from it could not run such a case.

So the list is the closure of every `.include`, `.inc` and `.lib <file>
<section>` reference in every reached file, all sections, recursively, from the
roots its `# root: <path> [<section>]` lines name. A path is resolved against
the directory of the file that names it, as ngspice does, and must stay inside
the PDK checkout. A named section must exist in the file it is named in.

The check recomputes that closure from git objects at the ref the Dockerfile's
SG13G2_REF names (or --ref), read with `git cat-file` from a local repository;
it fetches nothing, and a blob a partial clone lacks is a failed read, not a
lazy fetch (git 2.45 and later). The list's paths and digests must equal the
closure's.
`--walk section-limited` follows only the named section of a root or of a
`.lib` reference, the walk the first list came from; it reads red at the
pinned ref. `--emit` prints the closure in the list's format.

Exit codes: 0 the list is the closure; 1 it is not (each difference printed);
2 usage; 3 no verdict: there is no PDK at the ref to read (not a repository,
the ref is absent, or a blob is not in the object store, as in a partial clone
without its remote). Exit 3 is never a pass.

Usage:
    check_sg13g2_models.py --git DIR (--dockerfile Dockerfile | --ref SHA)
                           [--walk all|section-limited] [--emit] LIST
    check_sg13g2_models.py --self-test
"""

import argparse
import hashlib
import os
import posixpath
import re
import subprocess
import sys
import tempfile

NO_VERDICT = 3
DIRECTIVE = re.compile(r"^\s*\.(include|inc|lib|endl)\b(.*)$", re.IGNORECASE)
TOKEN = re.compile(r"\"([^\"]*)\"|'([^']*)'|(\S+)")
ROOT = re.compile(r"^#\s*root:\s*(\S+)(?:\s+(\S+))?\s*$")
ENTRY = re.compile(r"^([0-9a-f]{64})  (\S+)$")


class NoVerdict(Exception):
    """There is no PDK at the ref to read; the check says nothing about the list."""


class GitTree:
    """Files at one commit of a local repository, read from its object store."""

    def __init__(self, repo, ref):
        self.repo, self.ref = repo, ref
        if not os.path.isdir(repo):
            raise NoVerdict("%s is not a directory" % repo)
        got = self._git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if got.returncode != 0:
            raise NoVerdict("%s has no commit %s" % (repo, ref))
        self.commit = got.stdout.decode().strip()

    def _git(self, *args):
        # A partial clone would fetch a missing blob from its remote on read;
        # GIT_NO_LAZY_FETCH (git 2.45 and later) makes that a failed read.
        env = dict(os.environ, GIT_NO_LAZY_FETCH="1")
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, env=env)

    def read(self, path):
        """The file's bytes, or None when the commit has no file there."""
        listed = self._git("ls-tree", "-z", self.commit, "--", path)
        if listed.returncode != 0:
            raise NoVerdict("git ls-tree %s failed: %s" % (path, listed.stderr.decode().strip()))
        entry = listed.stdout.decode("utf-8", "replace").rstrip("\0")
        if not entry:
            return None
        mode, kind, oid = entry.split("\t", 1)[0].split()
        if kind != "blob":
            return None
        blob = self._git("cat-file", "blob", oid)
        if blob.returncode != 0:
            raise NoVerdict("blob %s (%s) is not in the object store of %s" % (oid, path, self.repo))
        return blob.stdout


def references(text):
    """(section, kind, path, target section) for each reference in a file, and
    the sections it defines. section is None outside any `.lib <name>` block."""
    refs, sections, current = [], set(), None
    for line in text.splitlines():
        m = DIRECTIVE.match(line)
        if not m:
            continue
        word = m.group(1).lower()
        if word == "endl":
            current = None
            continue
        tokens = [next(g for g in t.groups() if g is not None) for t in TOKEN.finditer(m.group(2))]
        if not tokens:
            continue
        if word == "lib" and len(tokens) == 1:
            current = tokens[0].lower()
            sections.add(current)
            continue
        target = tokens[1].lower() if word == "lib" else None
        refs.append((current, word, tokens[0], target))
    return refs, sections


def closure(tree, roots, walk="all"):
    """The files the roots reach, and the failures met on the way. roots is a
    list of (path, section or None); walk "section-limited" follows only the
    named section of a root or of a `.lib` reference."""
    failures, files, done = [], {}, set()
    parsed = {}
    todo = list(roots)
    origin = {path: "a root" for path, _ in roots}
    while todo:
        path, section = todo.pop(0)
        if path not in files:
            data = tree.read(path)
            if data is None:
                failures.append("%s (named by %s) is absent at the ref" % (path, origin[path]))
            else:
                parsed[path] = references(data.decode("latin-1"))
            files[path] = data
        if files[path] is None:
            continue
        refs, sections = parsed[path]
        if section is not None and section not in sections and (path, "?" + section) not in done:
            done.add((path, "?" + section))
            failures.append("%s has no section %s (named by %s)" % (path, section, origin[path]))
        key = (path, section if walk == "section-limited" else None)
        if key in done:
            continue
        done.add(key)
        for where, word, target, target_section in refs:
            if walk == "section-limited" and section is not None and where != section:
                continue
            if posixpath.isabs(target):
                failures.append("%s: .%s %s is an absolute path" % (path, word, target))
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if resolved == ".." or resolved.startswith("../"):
                failures.append("%s: .%s %s resolves outside the PDK checkout" % (path, word, target))
                continue
            origin.setdefault(resolved, path)
            todo.append((resolved, target_section))
    reached = {p: d for p, d in files.items() if d is not None}
    return reached, failures


def read_list(text):
    """(roots, entries, failures) of a list file."""
    roots, entries, failures = [], [], []
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("#"):
            m = ROOT.match(line)
            if m:
                roots.append((m.group(1), m.group(2).lower() if m.group(2) else None))
            elif line.lower().startswith("# root"):
                failures.append("line %d: a root line reads '# root: <path> [<section>]'" % n)
            continue
        if not line.strip():
            continue
        m = ENTRY.match(line)
        if not m:
            failures.append("line %d is not '<sha256>  <path>': %r" % (n, line))
            continue
        entries.append((m.group(2), m.group(1)))
    paths = [p for p, _ in entries]
    if len(set(paths)) != len(paths):
        failures.append("the list names a path twice")
    elif paths != sorted(paths, key=lambda p: p.encode()):
        failures.append("the list is not sorted by path (C order)")
    if not roots:
        failures.append("the list names no root, so the check would pass having walked nothing")
    return roots, entries, failures


def check(tree, text, walk="all"):
    """(failures, summary): the list against the closure its roots reach in tree."""
    roots, entries, failures = read_list(text)
    if not roots:
        return failures, ""
    reached, walk_failures = closure(tree, roots, walk)
    failures += walk_failures
    want = {p: hashlib.sha256(d).hexdigest() for p, d in reached.items()}
    have = dict(entries)
    for p in sorted(set(want) - set(have)):
        failures.append("the closure reaches %s, which the list does not name" % p)
    for p in sorted(set(have) - set(want)):
        failures.append("the list names %s, which the closure does not reach" % p)
    for p in sorted(set(want) & set(have)):
        if want[p] != have[p]:
            failures.append("%s: the list says %s, the ref has %s" % (p, have[p][:12], want[p][:12]))
    return failures, "%d files, the %s closure of %d roots" % (len(want), walk, len(roots))


def emit(tree, text, walk="all"):
    roots, _, _ = read_list(text)
    reached, failures = closure(tree, roots, walk)
    for p in sorted(reached, key=lambda p: p.encode()):
        print("%s  %s" % (hashlib.sha256(reached[p]).hexdigest(), p))
    return failures


def dockerfile_ref(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^ARG SG13G2_REF=([0-9a-f]{40})\s*$", line)
            if m:
                return m.group(1)
    return None


class MemoryTree:
    def __init__(self, files):
        self.files = {p: (d.encode() if isinstance(d, str) else d) for p, d in files.items()}

    def read(self, path):
        return self.files.get(path)


def listing(files, roots, drop=(), extra=None):
    """A list text for the self-test: roots, then each file but drop, sorted."""
    head = "".join("# root: %s%s\n" % (p, " " + s if s else "") for p, s in roots)
    body = dict((p, hashlib.sha256(d.encode()).hexdigest()) for p, d in files.items() if p not in drop)
    body.update(extra or {})
    return head + "".join("%s  %s\n" % (body[p], p) for p in sorted(body, key=lambda p: p.encode()))


def self_test():
    cases = []

    def expect(name, ok, got=None):
        cases.append((name, bool(ok), got))

    m = "pdk/models/"
    corner = (".lib mos_tt\n.include mod.lib\n.endl mos_tt\n"
              ".LIB mos_tt_mismatch\n  .include \"mismatch.lib\"\n.ENDL mos_tt_mismatch\n")
    files = {m + "corner.lib": corner, m + "mod.lib": "* model\n.include parm.lib\n",
             m + "parm.lib": "* parameters\n", m + "mismatch.lib": "* mismatch\n"}
    roots = [(m + "corner.lib", "mos_tt")]

    def run(fs, text, walk="all"):
        return check(MemoryTree(fs), text, walk)

    failures, summary = run(files, listing(files, roots))
    expect("a list equal to the all-sections closure passes", failures == [] and "4 files" in summary,
           failures)
    failures, _ = run(files, listing(files, roots), walk="section-limited")
    expect("mutation: the section-limited closure reads red on that list",
           any("names %smismatch.lib, which the closure does not reach" % m in f for f in failures),
           failures)
    failures, _ = run(files, listing(files, roots, drop=[m + "mismatch.lib"]))
    expect("a file included only under another section of a root is in the closure",
           any("reaches %smismatch.lib, which the list does not name" % m in f for f in failures),
           failures)
    failures, _ = run(files, listing(files, roots, drop=[m + "parm.lib"]))
    expect("an include of an included file (second level) is in the closure",
           any("reaches %sparm.lib" % m in f for f in failures), failures)

    # Each directive form, alone in a root, must reach its target.
    forms = {
        ".include unquoted": ".include t.lib\n",
        ".include double-quoted": '.include "t.lib"\n',
        ".include single-quoted": ".include 't.lib'\n",
        ".inc": ".inc t.lib\n",
        ".INCLUDE upper case": ".INCLUDE t.lib\n",
        ".Inc mixed case, quoted": '  .Inc "t.lib"   \n',
        ".lib <file> <section>": ".lib t.lib sec\n",
        ".LIB \"<file>\" <section>, upper case, quoted": '.LIB "t.lib" SEC\n',
    }
    for name, line in forms.items():
        fs = {m + "root.lib": "* root\n" + line, m + "t.lib": ".lib sec\n.endl sec\n"}
        failures, _ = run(fs, listing(fs, [(m + "root.lib", None)], drop=[m + "t.lib"]))
        expect("form %s is read" % name,
               failures == ["the closure reaches %st.lib, which the list does not name" % m], failures)
    fs = {m + "root.lib": "* .include not.lib\n*.include not.lib\n"}
    failures, _ = run(fs, listing(fs, [(m + "root.lib", None)]))
    expect("a commented-out include is not a reference", failures == [], failures)

    # .lib <file> <section>: the whole file is read, its other sections too.
    fs = {m + "root.lib": ".lib lib.lib b\n",
          m + "lib.lib": ".lib a\n.include a.lib\n.endl a\n.lib b\n.include b.lib\n.endl b\n",
          m + "a.lib": "", m + "b.lib": ""}
    failures, _ = run(fs, listing(fs, [(m + "root.lib", None)]))
    expect("a .lib reference reaches every section of its file", failures == [], failures)
    failures, _ = run(fs, listing(fs, [(m + "root.lib", None)]), walk="section-limited")
    expect("the section-limited walk reads only the referenced section",
           failures == ["the list names %sa.lib, which the closure does not reach" % m], failures)
    fs2 = dict(fs, **{m + "root.lib": ".lib lib.lib c\n"})
    failures, _ = run(fs2, listing(fs2, [(m + "root.lib", None)]))
    expect("a .lib reference to a section its file lacks fails",
           any("lib.lib has no section c" in f for f in failures), failures)
    failures, _ = run(files, listing(files, [(m + "corner.lib", "mos_ff")]))
    expect("a root section its file lacks fails",
           any("corner.lib has no section mos_ff" in f for f in failures), failures)

    fs = {"pdk/a/x.lib": ".include ../b/y.lib\n", "pdk/b/y.lib": ".include z.lib\n", "pdk/b/z.lib": ""}
    failures, _ = run(fs, listing(fs, [("pdk/a/x.lib", None)]))
    expect("a path resolves against the directory of the file naming it", failures == [], failures)
    fs = {m + "x.lib": ".include y.lib\n", m + "y.lib": ".include x.lib\n"}
    failures, summary = run(fs, listing(fs, [(m + "x.lib", None)]))
    expect("an include cycle ends", failures == [] and "2 files" in summary, failures)
    fs = {m + "x.lib": ".include ../../../etc.lib\n"}
    failures, _ = run(fs, listing(fs, [(m + "x.lib", None)]))
    expect("a path outside the checkout fails", any("resolves outside" in f for f in failures), failures)
    fs = {m + "x.lib": ".include /abs/y.lib\n"}
    failures, _ = run(fs, listing(fs, [(m + "x.lib", None)]))
    expect("an absolute path fails", any("absolute path" in f for f in failures), failures)
    fs = {m + "x.lib": ".include gone.lib\n"}
    failures, _ = run(fs, listing(fs, [(m + "x.lib", None)]))
    expect("an included file absent at the ref fails",
           any("%sgone.lib (named by %sx.lib) is absent" % (m, m) in f for f in failures), failures)
    failures, _ = run(files, listing(files, roots, extra={m + "other.lib": "0" * 64}))
    expect("a listed file the closure does not reach fails",
           any("names %sother.lib" % m in f for f in failures), failures)
    failures, _ = run(files, listing(files, roots, extra={m + "parm.lib": "0" * 64}))
    expect("a digest that differs fails", any("parm.lib: the list says 000000000000" in f
                                              for f in failures), failures)
    text = listing(files, roots)
    lines = text.splitlines(True)
    failures, _ = run(files, lines[0] + "".join(reversed(lines[1:])))
    expect("a list out of order fails", any("not sorted" in f for f in failures), failures)
    failures, _ = run(files, text + lines[-1])
    expect("a path listed twice fails", any("twice" in f for f in failures), failures)
    failures, _ = run(files, "".join(lines[1:]))
    expect("a list with no root fails", any("names no root" in f for f in failures), failures)
    failures, _ = run(files, "# root:\n" + text)
    expect("a malformed root line fails", any("a root line reads" in f for f in failures), failures)

    # The git reader: blobs at the ref, never the work tree; no PDK at the ref is no verdict.
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "pdk")

        def git(*args):
            return subprocess.run(["git", "-C", repo, *args], env=env, check=True,
                                  capture_output=True).stdout.decode().strip()
        os.makedirs(os.path.join(repo, "pdk", "models"))
        for p, d in files.items():
            with open(os.path.join(repo, p), "w") as f:
                f.write(d)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "models")
        sha = git("rev-parse", "HEAD")
        listfile = os.path.join(tmp, "list")
        with open(listfile, "w") as f:
            f.write(listing(files, roots))
        with open(os.path.join(repo, m + "parm.lib"), "w") as f:
            f.write("* edited in the work tree\n")

        def main_rc(*args):
            return subprocess.run([sys.executable, "-B", os.path.abspath(__file__), *args],
                                  capture_output=True, env=env).returncode
        expect("the git reader reads the ref, not the work tree: exit 0",
               main_rc("--git", repo, "--ref", sha, listfile) == 0)
        expect("section-limited at the ref: exit 1",
               main_rc("--git", repo, "--ref", sha, "--walk", "section-limited", listfile) == 1)
        expect("a ref the repository lacks: exit 3, no verdict",
               main_rc("--git", repo, "--ref", "1" * 40, listfile) == NO_VERDICT)
        expect("a directory that is not a repository: exit 3, no verdict",
               main_rc("--git", os.path.join(tmp, "nowhere"), "--ref", sha, listfile) == NO_VERDICT)
        dockerfile = os.path.join(tmp, "Dockerfile")
        with open(dockerfile, "w") as f:
            f.write("FROM x\nARG SG13G2_REF=%s\n" % sha)
        expect("the ref read from the Dockerfile's SG13G2_REF: exit 0",
               main_rc("--git", repo, "--dockerfile", dockerfile, listfile) == 0)
        with open(dockerfile, "w") as f:
            f.write("FROM x\n")
        expect("a Dockerfile without SG13G2_REF: exit 2",
               main_rc("--git", repo, "--dockerfile", dockerfile, listfile) == 2)
        oid = git("rev-parse", "%s:%sparm.lib" % (sha, m))
        os.remove(os.path.join(repo, ".git", "objects", oid[:2], oid[2:]))
        expect("a blob not in the object store: exit 3, no verdict",
               main_rc("--git", repo, "--ref", sha, listfile) == NO_VERDICT)

    ok = True
    for name, passed, got in cases:
        print("%-70s %s" % (name, "ok" if passed else "FAILED"))
        if not passed:
            ok = False
            print("   checker returned: %r" % (got,))
    print("%d/%d self-test cases passed" % (sum(1 for c in cases if c[1]), len(cases)))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--git", help="a local repository holding the PDK commit")
    ref = ap.add_mutually_exclusive_group()
    ref.add_argument("--ref", help="the PDK commit")
    ref.add_argument("--dockerfile", help="read the commit from its ARG SG13G2_REF")
    ap.add_argument("--walk", choices=("all", "section-limited"), default="all")
    ap.add_argument("--emit", action="store_true", help="print the closure in the list's format")
    ap.add_argument("list", nargs="?")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not (args.git and args.list and (args.ref or args.dockerfile)):
        ap.error("--git, --ref or --dockerfile, and LIST are required")
    sha = args.ref or dockerfile_ref(args.dockerfile)
    if not sha:
        ap.error("%s has no 'ARG SG13G2_REF=<40 hex>' line" % args.dockerfile)
    with open(args.list, encoding="utf-8") as f:
        text = f.read()
    try:
        tree = GitTree(args.git, sha)
        if args.emit:
            failures = emit(tree, text, args.walk)
            for f in failures:
                print("FAIL: " + f, file=sys.stderr)
            return 1 if failures else 0
        failures, summary = check(tree, text, args.walk)
    except NoVerdict as e:
        print("NO VERDICT: no PDK at %s to read (%s). The list was not checked; this says "
              "nothing about it." % (sha, e), file=sys.stderr)
        return NO_VERDICT
    for f in failures:
        print("FAIL: " + f)
    if failures:
        print("%d failure(s) at %s" % (len(failures), sha))
        return 1
    print("%s at %s, equal to the list (paths and digests)" % (summary, sha))
    return 0


if __name__ == "__main__":
    sys.exit(main())
