#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Write ci/pin-set.json, the one machine-readable record of every pin this
repository holds, and check that the tracked copy is the one its sources give.

adk-tools pins commits in four places, each read by its own consumer:
staging.pin (the staged repositories, LibreLane and the Nix store paths), the
gitlinks under tools/, ci/integration-refs.json (the repositories the
integration job checks out that are neither) and the Dockerfile (ARG refs, the
pip pins and the base image). This script reads all four and writes one file,
so a consumer outside this repository reads `git show <sha>:ci/pin-set.json`
instead of a fifth parser. The commit the file is read from is its anchor, so
the file does not name it.

One id per repository, its folder name in the usual layout: staging.pin's
STAGE_<X>_PATH value for a staged entry, the submodule name less `tools/` for a
gitlink, and the contract registry's folder for an integration entry. A
sibling resolved as <root>/<id> is then pinned by an export root laid out by
these ids. Two sources naming one id fail here, both values printed: one
repository has one pin, and a second copy is how the two drift.

Each entry records whether its commit can be fetched from a remote
(`reachable`). Nothing is asked over the network; the value is what the sources
declare. A gitlink and a pinned integration entry claim "remote", which the
integration job proves before using it. A staged entry is "local-only", either
for want of a remote (no URL) or because ci/integration-refs.json names its
remote and records the commit as not pushed there (`on_remote: false`).

Each entry also records its repository's root commits (`roots`) and where an
export takes it from (`export_from`): the sibling <root>/<id>, or the module
store of one of this repository's submodules. Both come from
ci/pin-sources.json. The roots are written there by --record-roots from the
source each pin comes from, and export-pins refuses a source whose roots differ,
which is a folder holding another repository under the id's name.

Output is canonical JSON: sorted keys, two-space indent, LF, a final newline.
The pin-set digest is the sha256 of those bytes.

Usage:
    pin_set.py                     write ci/pin-set.json from the working tree
    pin_set.py --check [--rev R]   the tracked file equals a regeneration, from
                                   the working tree or from commit R; exit 1
                                   and a diff when it does not
    pin_set.py --stdout [--rev R]  print the pin set
    pin_set.py --digest [--rev R]  print its sha256
    pin_set.py [--source-root DIR] [--superproject DIR]
                                   on a write, every sha that moved must descend
                                   from the one the tracked file held, read in
                                   its source; a sha that does not resolve
                                   there fails too
    pin_set.py --rerooted ID=SHA[,SHA...] [...]
                                   on that write, ID's history was started
                                   again: each SHA is the last commit of a
                                   history it left, oldest first (the rule is
                                   ancestry's)
    pin_set.py --record-roots --source-root DIR [--superproject DIR]
                                   add the roots of every id ci/pin-sources.json
                                   has none for, read from its source; a
                                   recorded root that differs fails
    pin_set.py --self-test
"""

import argparse
import difflib
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract_registry  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = "ci/pin-set.json"
SOURCES = "ci/pin-sources.json"
SCHEMA = "adk-pin-set/1"
SHA = re.compile(r"[0-9a-f]{40}")
STAGED = re.compile(r"STAGE_([A-Z0-9_]+)_PATH")
# The interpreter each base image's python3 is, by base tag. The venv is made
# with that python3 (`python3 -m venv`), and verify's tool table names it as
# the python row. A base this table does not know fails rather than guesses.
PYTHON_BY_BASE = {"ubuntu:24.04": "3.12"}
# Dockerfile ARGs that are not pins: the build's parallelism, the manifest
# placeholder build.sh overrides, and the tools image the part below the
# thin-layer line builds on, which build.sh names by its key.
NOT_PINS = {"JOBS", "MANIFEST_B64", "TOOLS_IMAGE"}
BASE_ARG = "UBUNTU_IMAGE"
PIP_SPEC = re.compile(r'"([A-Za-z0-9][A-Za-z0-9._-]*)==\$\{([A-Z0-9_]+_PIP)\}"')


class PinSetError(Exception):
    pass


def git(root, *args, binary=False):
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          env=env, text=not binary)
    if done.returncode:
        err = done.stderr if not binary else done.stderr.decode(errors="replace")
        raise PinSetError("git %s: %s" % (" ".join(args), err.strip()))
    return done.stdout


class Sources:
    """The four sources, from the working tree (gitlinks from the index) or
    from one commit."""

    def __init__(self, root, rev=None):
        self.root, self.rev = Path(root), rev

    def read(self, rel):
        if self.rev is None:
            path = self.root / rel
            if not path.is_file():
                raise PinSetError("%s not found in %s" % (rel, self.root))
            return path.read_bytes()
        try:
            return git(self.root, "show", "%s:%s" % (self.rev, rel), binary=True)
        except PinSetError:
            raise PinSetError("%s not found at %s" % (rel, self.rev)) from None

    def gitlinks(self):
        if self.rev is None:
            lines = git(self.root, "ls-files", "--stage").splitlines()
            rows = [(line.split()[0], line.split()[1], line.split("\t", 1)[1])
                    for line in lines]
        else:
            lines = git(self.root, "ls-tree", "-r", self.rev).splitlines()
            rows = [(line.split()[0], line.split()[2], line.split("\t", 1)[1])
                    for line in lines]
        return {path: sha for mode, sha, path in rows if mode == "160000"}

    def gitmodules(self):
        """{name: {key: value}} as git itself parses .gitmodules."""
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(self.read(".gitmodules"))
            tmp.flush()
            text = git(self.root, "config", "--file", tmp.name, "--list")
        out = {}
        for line in text.splitlines():
            key, _, value = line.partition("=")
            m = re.fullmatch(r"submodule\.(.+)\.([a-z]+)", key)
            if m:
                out.setdefault(m.group(1), {})[m.group(2)] = value
        return out


def source_pin(text):
    """staging.pin's assignments, read by sourcing it in a fresh bash under
    set -euo pipefail, as stage-sources.sh and ci.yml do. Never a regex: the
    value is what a shell reads."""
    script = 'set -a; [ "$1" = - ] || . "$1" >/dev/null; exec env -0'
    base = {"PATH": "/usr/bin:/bin"}
    with tempfile.NamedTemporaryFile(suffix=".pin") as tmp:
        tmp.write(text)
        tmp.flush()
        runs = []
        for arg in ("-", tmp.name):
            done = subprocess.run(
                ["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", script, "_", arg],
                capture_output=True, env=base, cwd="/")
            if done.returncode:
                last = done.stderr.decode(errors="replace").strip().splitlines()
                raise PinSetError("staging.pin does not source cleanly under set -euo "
                                  "pipefail: %s" % (last[-1] if last else "rc %d" % done.returncode))
            runs.append(dict(item.split(b"=", 1) for item in done.stdout.split(b"\0") if item))
    before, after = runs
    return {k.decode(): v.decode() for k, v in after.items()
            if k != b"_" and before.get(k) != v}


def full_sha(value, what):
    if not SHA.fullmatch(value or ""):
        raise PinSetError("%s is %r, not a full 40-hex sha" % (what, value))
    return value


def staged_entries(pins):
    """({id: entry}, {variable: value} for every assignment no entry takes)."""
    repos, used = {}, set()
    for var in sorted(pins):
        m = STAGED.fullmatch(var)
        if not m:
            continue
        x = m.group(1)
        rid = pins[var]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", rid):
            raise PinSetError("staging.pin's %s is %r, not one folder name" % (var, rid))
        ref_var, extra_var = "STAGE_%s_REF" % x, "STAGE_%s_EXTRA_REFS" % x
        if ref_var not in pins:
            raise PinSetError("staging.pin sets %s but not %s" % (var, ref_var))
        extra = [full_sha(s, "a commit in staging.pin's %s" % extra_var)
                 for s in pins.get(extra_var, "").split()]
        if rid in repos:
            raise PinSetError("staging.pin stages %s twice" % rid)
        repos[rid] = {"sha": full_sha(pins[ref_var], "staging.pin's %s" % ref_var),
                      "extra": extra, "source": "staged",
                      "stage_name": x.lower().replace("_", "-"),
                      "remote": None, "reachable": "local-only"}
        used.update((var, ref_var, extra_var))
    return repos, {k: v for k, v in pins.items() if k not in used}


def gitlink_entries(src):
    modules = src.gitmodules()
    links = {path: sha for path, sha in src.gitlinks().items()}
    repos = {}
    for name, body in sorted(modules.items()):
        path = body.get("path")
        if not name.startswith("tools/"):
            raise PinSetError("submodule %r is not named tools/<id>, so it has no id" % name)
        if path not in links:
            raise PinSetError("submodule %s declares path %r, which holds no gitlink" % (name, path))
        repos[name[len("tools/"):]] = {
            "sha": full_sha(links.pop(path), "the gitlink at %s" % path), "extra": [],
            "source": "gitlink", "stage_name": None, "remote": body.get("url") or None,
            "reachable": "remote", "submodule": name}
    if links:
        raise PinSetError("gitlink %s has no .gitmodules entry" % ", ".join(sorted(links)))
    return repos


def integration_entries(src, staged):
    """Entries from ci/integration-refs.json, keyed by the registry's folder for
    the integration id. An entry carries its own pin, or takes the staged one
    (`pin_from: staging.pin`) and says whether that commit is on its remote."""
    refs = json.loads(src.read("ci/integration-refs.json"))
    try:
        folder = contract_registry.derived(contract_registry.load_registry(
            src.read("ci/" + contract_registry.REGISTRY_FILENAME))).layout
    except contract_registry.RegistryError as exc:
        raise PinSetError("the contract registry does not load: %s" % exc) from None
    repos, reach = {}, {}
    for key, entry in sorted(refs.items()):
        if key.startswith("_"):
            continue
        rid = folder.get(key)
        if rid is None:
            raise PinSetError("ci/integration-refs.json names %r, which has no row in "
                              "the contract registry" % key)
        slug = entry.get("slug")
        if not slug:
            raise PinSetError("ci/integration-refs.json gives %s no slug" % key)
        remote = "https://github.com/%s.git" % slug
        if "pin_from" in entry:
            if entry["pin_from"] != "staging.pin":
                raise PinSetError("ci/integration-refs.json %s: pin_from %r; only "
                                  "staging.pin is a source" % (key, entry["pin_from"]))
            if rid not in staged:
                raise PinSetError("ci/integration-refs.json %s takes its pin from "
                                  "staging.pin, which stages no %s" % (key, rid))
            if not isinstance(entry.get("on_remote"), bool):
                raise PinSetError("ci/integration-refs.json %s: on_remote must say "
                                  "true or false" % key)
            reach[rid] = (remote, "remote" if entry["on_remote"] else "local-only")
        if "pin" in entry:
            repos[rid] = {"sha": full_sha(entry["pin"], "ci/integration-refs.json %s pin" % key),
                          "extra": [], "source": "integration", "stage_name": None,
                          "remote": remote,
                          "reachable": "remote" if entry.get("on_remote", True) else "local-only"}
        elif "pin_from" not in entry:
            raise PinSetError("ci/integration-refs.json %s has neither pin nor pin_from" % key)
    return repos, reach


def dockerfile(text):
    """({ARG: default}, {distribution: version}, base image)."""
    args = {}
    for line in text.splitlines():
        m = re.match(r"\s*ARG\s+([A-Za-z0-9_]+)=(\S+)\s*$", line)
        if not m:
            continue
        name, value = m.groups()
        if args.get(name, value) != value:
            raise PinSetError("Dockerfile ARG %s has two defaults, %s and %s"
                              % (name, args[name], value))
        args[name] = value
    pip, named = {}, {}
    for dist, arg in PIP_SPEC.findall(text):
        if arg not in args:
            raise PinSetError("the Dockerfile installs %s at ${%s}, which has no default"
                              % (dist, arg))
        if arg in named and named[arg] != dist:
            raise PinSetError("%s pins both %s and %s" % (arg, named[arg], dist))
        named[arg] = dist
        pip[re.sub(r"[-_.]+", "-", dist).lower()] = args[arg]
    for arg in sorted(a for a in args if a.endswith("_PIP")):
        if arg not in named:
            raise PinSetError("Dockerfile ARG %s names no distribution: no pip "
                              "install line reads \"<name>==${%s}\"" % (arg, arg))
    base = args.get(BASE_ARG)
    if not base or "@sha256:" not in base:
        raise PinSetError("Dockerfile ARG %s is %r, not a tag pinned by digest"
                          % (BASE_ARG, base))
    rest = {k: v for k, v in args.items()
            if k not in NOT_PINS and k != BASE_ARG and k not in named}
    return rest, pip, base


def pin_sources(src):
    """({id: export kind}, {id: [root sha]}) from ci/pin-sources.json."""
    data = json.loads(src.read(SOURCES))
    unknown = sorted(set(data) - {"_comment", "export_from", "roots"})
    if unknown:
        raise PinSetError("%s has keys %s; it holds only export_from and roots"
                          % (SOURCES, ", ".join(unknown)))
    kinds = data.get("export_from", {})
    for rid, kind in kinds.items():
        if kind not in ("sibling", "submodule"):
            raise PinSetError("%s export_from %s is %r, not sibling or submodule"
                              % (SOURCES, rid, kind))
    roots = data.get("roots", {})
    for rid, shas in roots.items():
        if not isinstance(shas, list) or not shas:
            raise PinSetError("%s roots %s is not a list of commits" % (SOURCES, rid))
        for sha in shas:
            full_sha(sha, "a root of %s in %s" % (rid, SOURCES))
        if shas != sorted(set(shas)):
            raise PinSetError("%s roots %s is not sorted and unique" % (SOURCES, rid))
    return kinds, roots


def generate(src, need_roots=True):
    staged, pin_rest = staged_entries(source_pin(src.read("staging.pin")))
    links = gitlink_entries(src)
    integ, reach = integration_entries(src, staged)
    repos, origin = {}, {}
    for name, part in (("staging.pin", staged), ("a gitlink", links),
                       ("ci/integration-refs.json", integ)):
        for rid, entry in part.items():
            if rid in repos:
                raise PinSetError(
                    "%s is pinned twice: %s by %s and %s by %s. One repository has one "
                    "pin; drop one of them" % (rid, repos[rid]["sha"], origin[rid],
                                               entry["sha"], name))
            repos[rid], origin[rid] = entry, name
    for rid, (remote, reachable) in reach.items():
        repos[rid]["remote"], repos[rid]["reachable"] = remote, reachable
    kinds, roots = pin_sources(src)
    stray = sorted((set(kinds) | set(roots)) - set(repos))
    if stray:
        raise PinSetError("%s names %s, which nothing pins" % (SOURCES, ", ".join(stray)))
    for rid, entry in repos.items():
        module = entry.pop("submodule", None)
        kind = kinds.get(rid, "sibling")
        if kind == "submodule" and module is None:
            raise PinSetError("%s exports %s from a submodule, but %s is not a gitlink"
                              % (SOURCES, rid, rid))
        entry["export_from"] = ({"kind": "submodule", "name": module} if kind == "submodule"
                                else {"kind": "sibling"})
        if rid in roots:
            entry["roots"] = roots[rid]
        elif need_roots:
            raise PinSetError("%s records no roots for %s; add them with python3 "
                              "ci/pin_set.py --record-roots --source-root DIR" % (SOURCES, rid))
    args, pip, base = dockerfile(src.read("Dockerfile").decode())
    tag = base.split("@", 1)[0]
    if tag not in PYTHON_BY_BASE:
        raise PinSetError("no python minor known for base image %s; add it to "
                          "PYTHON_BY_BASE" % tag)
    return {"schema": SCHEMA, "repos": repos, "pip": pip,
            "python": PYTHON_BY_BASE[tag], "base_image": base,
            "image": {"dockerfile": args, "staging_pin": pin_rest}}


def export_pins():
    """ci/export-pins as a module: it owns where a source is and what its roots
    are, so the two cannot disagree."""
    loader = importlib.machinery.SourceFileLoader("export_pins", str(HERE / "export-pins"))
    spec = importlib.util.spec_from_loader("export_pins", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def record_roots(src, source_root, superproject, report=print):
    """Add to ci/pin-sources.json the roots of every id it has none for, read
    in the source each pin comes from. A recorded root that differs fails: the
    id names another repository now. Returns the ids added."""
    ep = export_pins()
    pin_set = generate(src, need_roots=False)
    data = json.loads(src.read(SOURCES))
    roots = data.setdefault("roots", {})
    added, bad = [], []
    for rid, entry in sorted(pin_set["repos"].items()):
        try:
            repo = ep.source_path(rid, entry, Path(source_root), Path(superproject))
            got = ep.roots(repo, entry["sha"])
        except ep.ExportError as exc:
            bad.append("%s: %s" % (rid, exc))
            continue
        if rid not in roots:
            roots[rid] = got
            added.append(rid)
            report("  %-26s %s  from %s" % (rid, " ".join(r[:12] for r in got), repo))
        elif roots[rid] != got:
            bad.append("%s: %s has roots %s, %s records %s" % (
                rid, repo, " ".join(got), SOURCES, " ".join(roots[rid])))
    if bad:
        raise PinSetError("\n".join(bad))
    (src.root / SOURCES).write_bytes(canonical(data))
    return added


def git_rc(root, *args):
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          env=env).returncode


def ancestry(old, new, source_root, superproject, rerooted=None):
    """A pin only moves forward: every id whose sha moved from the old pin set to
    the new one must have the new sha descend from the old, read in the source
    the new entry exports from. A sha that does not resolve there fails too;
    there is no history to read. Returns the ids that moved.

    rerooted maps an id to the last commits of the histories it left, oldest
    first, for a repository whose history was started again (a new root, or a
    release squash), once per history. The first of them has to descend from
    the old pin. Then, on the first-parent line of the next one (of the new
    sha, for the last), a commit has to start a history from it: one that
    holds its tree, is not in its history, and has every parent in it (none,
    for a root; one of its commits, for a squash) or among the entry's extra
    refs. A root reached only through a merge's second parent is not on that
    line, and a squash onto a commit outside the old history starts nothing
    unless the entry pins that commit itself, so the export carries it and the
    pin file says why it is there. The new sha's roots have to be the
    ones the entry records, as an export reads them; a pin that only moves
    forward leaves its roots to the export. An id named there that did not
    move fails."""
    rerooted = dict(rerooted or {})
    ep = export_pins()
    moved, bad = [], []
    for rid, entry in sorted(new["repos"].items()):
        before = old.get("repos", {}).get(rid, {}).get("sha")
        if before is None or before == entry["sha"]:
            continue
        moved.append(rid)
        tips = tuple(rerooted.pop(rid, ()))
        try:
            repo = ep.source_path(rid, entry, Path(source_root), Path(superproject))
        except ep.ExportError as exc:
            bad.append("%s: %s" % (rid, exc))
            continue
        missing = [sha for sha in (before, entry["sha"]) + tips
                   if git_rc(repo, "cat-file", "-e", "%s^{commit}" % sha)]
        if missing:
            bad.append("%s: %s does not resolve in %s" % (rid, " ".join(missing), repo))
        else:
            try:
                why = backward(repo, before, tips, entry["sha"], entry["extra"])
                got = ep.roots(repo, entry["sha"]) if tips and not why else None
                if got is not None and got != entry.get("roots", []):
                    why = ("the roots of %s, %s, are not the roots %s records, %s"
                           % (entry["sha"], " ".join(got), SOURCES,
                              " ".join(entry.get("roots", []))))
            except ep.ExportError as exc:
                why = str(exc)
            if why:
                bad.append("%s: %s (read in %s)" % (rid, why, repo))
    for rid in sorted(rerooted):
        bad.append("%s: named as rerooted, but its pin did not move" % rid)
    if bad:
        raise PinSetError("\n".join(bad))
    return moved


def backward(repo, before, tips, new, extra=()):
    """Why the move from before to new, through the histories tips end, does
    not go forward (see ancestry), or None. Only the first step is a descent:
    each later one sits on the first-parent line of the next, so it is in the
    next one's history. A commit that starts a history may also have a parent
    among extra, the commits the entry pins beside its sha."""
    def descends(rev, old):
        return not git_rc(repo, "merge-base", "--is-ancestor", old, rev)
    first = (tips + (new,))[0]
    if not descends(first, before):
        return "%s%s does not descend from %s, the pin before it" % (
            "the old history's tip " if tips else "", first, before)
    for tip, onto in zip(tips, tips[1:] + (new,)):
        tree = git(repo, "rev-parse", "%s^{tree}" % tip).strip()
        lines = git(repo, "log", "--no-show-signature", "--first-parent",
                    "--format=%H %T %P", onto).splitlines()
        if not any(held == tree and not descends(tip, sha)
                   and all(descends(tip, parent) or parent in extra for parent in parents)
                   for sha, held, *parents in map(str.split, lines)):
            return ("no commit on the first-parent line of %s holds %s's tree %s and "
                    "starts a history from it" % (onto, tip, tree))
    return None


def canonical(pin_set):
    return (json.dumps(pin_set, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def check(src):
    """None when the tracked file equals a regeneration, else why not."""
    want = canonical(generate(src))
    try:
        got = src.read(OUT)
    except PinSetError:
        return "%s is missing; write it with python3 ci/pin_set.py" % OUT
    if got == want:
        return None
    diff = difflib.unified_diff(got.decode(errors="replace").splitlines(),
                                want.decode().splitlines(), "tracked", "regenerated",
                                lineterm="", n=1)
    return ("%s is stale%s: regenerate it with python3 ci/pin_set.py and commit it with "
            "the pin it follows\n%s" % (OUT, " at %s" % src.rev if src.rev else "",
                                        "\n".join(list(diff)[:40])))


def self_test():
    cases = []

    def expect(name, ok):
        cases.append((name, bool(ok)))

    def fails(fn, needle):
        try:
            fn()
        except PinSetError as exc:
            return needle in str(exc)
        return False

    def gives(fn, want):
        try:
            return fn() == want
        except PinSetError:
            return False

    a, b, c, d = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    pin = ("# comment\nSTAGE_SPEC_THING_PATH=chiplet-spec\nSTAGE_SPEC_THING_REF=%s\n"
           'STAGE_SPEC_THING_EXTRA_REFS="%s"\nSTAGE_PNR_PATH=interposer-pnr\n'
           "STAGE_PNR_REF=%s\nNIX_X_OUTPATH=\"/nix/store/x-y\"\n" % (a, b, c))
    refs = {"_comment": ["x"],
            "spec": {"slug": "o/chiplet-spec", "branch": "dev", "pin_from": "staging.pin",
                     "on_remote": False},
            "docs": {"slug": "o/docs", "branch": "dev", "pin": d}}
    # The real registry: it is validated on load, and it maps spec and docs.
    registry = (ROOT / "ci" / contract_registry.REGISTRY_FILENAME).read_text()
    docker = ("ARG JOBS=8\nARG UBUNTU_IMAGE=ubuntu:24.04@sha256:%s\nFROM ${UBUNTU_IMAGE}\n"
              "ARG SG13G2_REF=%s\nARG KLAYOUT_PIP=0.30.5\nARG PYYAML_PIP=6.0.3\n"
              'RUN pip install "klayout==${KLAYOUT_PIP}" \\\n    "PyYAML==${PYYAML_PIP}"\n'
              % ("e" * 64, a))
    modules = '[submodule "tools/adk"]\n\tpath = tools/adk\n\turl = https://x/adk.git\n'
    r1, r2 = "1" * 40, "2" * 40
    sources = {"_comment": ["x"], "export_from": {"adk": "submodule"},
               "roots": {"adk": [r1], "adk-docs": [r1], "chiplet-spec": [r1, r2],
                         "interposer-pnr": [r2]}}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {"staging.pin": pin, "ci/integration-refs.json": json.dumps(refs),
                 "ci/" + contract_registry.REGISTRY_FILENAME: registry, "Dockerfile": docker,
                 ".gitmodules": modules, SOURCES: json.dumps(sources)}

        def lay(**over):
            for rel, text in {**files, **over}.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)

        git(root, "init", "-q")
        # An uninitialised submodule is an empty directory; without one, `add -A`
        # would stage the gitlink's removal.
        (root / "tools" / "adk").mkdir(parents=True)
        (root / "tools" / "stray").mkdir()
        lay()
        git(root, "add", "-A")
        git(root, "update-index", "--add", "--cacheinfo", "160000,%s,tools/adk" % a)
        src = Sources(root)
        got = generate(src)
        expect("the staged id is the PATH value, the stage name the variable's",
               got["repos"]["chiplet-spec"]["stage_name"] == "spec-thing"
               and got["repos"]["interposer-pnr"]["sha"] == c)
        expect("extra refs ride with their staged entry",
               got["repos"]["chiplet-spec"]["extra"] == [b])
        expect("a staged entry the integration file names takes its remote and stays "
               "local-only when on_remote is false",
               got["repos"]["chiplet-spec"]["remote"] == "https://github.com/o/chiplet-spec.git"
               and got["repos"]["chiplet-spec"]["reachable"] == "local-only")
        expect("a staged entry with no remote is local-only with remote null",
               got["repos"]["interposer-pnr"]["remote"] is None
               and got["repos"]["interposer-pnr"]["reachable"] == "local-only")
        expect("a gitlink is keyed by its submodule name less tools/",
               got["repos"]["adk"] == {"sha": a, "extra": [], "source": "gitlink",
                                       "stage_name": None, "remote": "https://x/adk.git",
                                       "reachable": "remote", "roots": [r1],
                                       "export_from": {"kind": "submodule",
                                                       "name": "tools/adk"}})
        expect("every other entry exports from its sibling and carries its roots",
               got["repos"]["chiplet-spec"]["export_from"] == {"kind": "sibling"}
               and got["repos"]["chiplet-spec"]["roots"] == [r1, r2])
        expect("an integration entry is keyed by the registry's folder",
               got["repos"]["adk-docs"]["sha"] == d
               and got["repos"]["adk-docs"]["source"] == "integration")
        expect("pip pins come out by normalised distribution name",
               got["pip"] == {"klayout": "0.30.5", "pyyaml": "6.0.3"})
        expect("python, base image and the image pins come out",
               got["python"] == "3.12" and got["base_image"].endswith("e" * 64)
               and got["image"]["dockerfile"] == {"SG13G2_REF": a}
               and got["image"]["staging_pin"] == {"NIX_X_OUTPATH": "/nix/store/x-y"})
        out = canonical(got)
        expect("the bytes are canonical: sorted keys, two-space indent, final newline",
               out.endswith(b"}\n") and b"\r" not in out and b'\n  "base_image"' in out
               and out == canonical(json.loads(out)))

        (root / OUT).write_bytes(out)
        git(root, "add", "-A")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
        expect("a fresh pin set checks at HEAD", check(Sources(root, "HEAD")) is None)

        # Named mutation: a staging.pin edit committed without regenerating.
        lay(**{"staging.pin": pin.replace(c, d)})
        git(root, "add", "staging.pin")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two")
        why = check(Sources(root, "HEAD"))
        expect("a stale tracked pin set after a staging.pin edit fails, with the diff",
               why and "is stale at HEAD" in why and '"sha": "%s"' % d in why)
        expect("the working tree check sees the same staleness",
               check(Sources(root)) is not None)
        lay()

        # Named mutation: a second chiplet-spec sha in integration-refs.json.
        second = dict(refs, spec=dict(refs["spec"], pin=c))
        lay(**{"ci/integration-refs.json": json.dumps(second)})
        expect("two disagreeing chiplet-spec pins fail, naming both",
               fails(lambda: generate(src), "chiplet-spec is pinned twice: %s by "
                     "staging.pin and %s by ci/integration-refs.json" % (a, c)))
        lay()

        expect("a staging.pin that does not source fails",
               fails(lambda: (lay(**{"staging.pin": pin + "X=$UNSET\n"}), generate(src)),
                     "does not source cleanly"))
        expect("a short staged sha fails",
               fails(lambda: (lay(**{"staging.pin": pin.replace(a, "abc123")}), generate(src)),
                     "not a full 40-hex sha"))
        expect("a PATH with no REF fails",
               fails(lambda: (lay(**{"staging.pin": pin.replace("STAGE_PNR_REF", "Z")}),
                              generate(src)), "but not STAGE_PNR_REF"))
        lay()
        expect("an integration key with no registry row fails",
               fails(lambda: (lay(**{"ci/integration-refs.json": json.dumps(
                   dict(refs, new={"slug": "o/n", "pin": d}))}), generate(src)),
                   "has no row"))
        expect("pin_from with no on_remote fails",
               fails(lambda: (lay(**{"ci/integration-refs.json": json.dumps(
                   dict(refs, spec={"slug": "o/s", "pin_from": "staging.pin"}))}),
                   generate(src)), "on_remote must say"))
        lay()
        expect("a _PIP arg with no install line fails",
               fails(lambda: (lay(Dockerfile=docker + "ARG PSUTIL_PIP=7\n"), generate(src)),
                     "PSUTIL_PIP names no distribution"))
        expect("an ARG with two defaults fails",
               fails(lambda: (lay(Dockerfile=docker + "ARG SG13G2_REF=%s\n" % b),
                              generate(src)), "two defaults"))
        expect("a base image without a digest fails",
               fails(lambda: (lay(Dockerfile=docker.replace("@sha256:" + "e" * 64, "")),
                              generate(src)), "not a tag pinned by digest"))
        lay()

        def with_sources(**over):
            return {SOURCES: json.dumps(dict(sources, **over))}
        expect("an id with no recorded roots fails, saying how to record them",
               fails(lambda: (lay(**with_sources(roots={"adk": [r1]})), print(generate(src))),
                     "records no roots for interposer-pnr; add them with"))
        expect("roots for an id nothing pins fail",
               fails(lambda: (lay(**with_sources(roots=dict(sources["roots"], gone=[r1]))),
                              generate(src)), "names gone, which nothing pins"))
        expect("unsorted roots fail",
               fails(lambda: (lay(**with_sources(roots=dict(sources["roots"],
                                                            adk=[r2, r1]))), generate(src)),
                     "not sorted and unique"))
        expect("a submodule source for an entry that is not a gitlink fails",
               fails(lambda: (lay(**with_sources(export_from={"adk-docs": "submodule"})),
                              generate(src)), "adk-docs is not a gitlink"))
        expect("an export source that is neither fails",
               fails(lambda: (lay(**with_sources(export_from={"adk": "umbrella"})),
                              generate(src)), "not sibling or submodule"))
        lay()
        git(root, "update-index", "--add", "--cacheinfo", "160000,%s,tools/stray" % b)
        expect("a gitlink with no .gitmodules entry fails",
               fails(lambda: generate(src), "tools/stray has no .gitmodules entry"))

    with tempfile.TemporaryDirectory() as tmp:
        # --record-roots on real repositories: one staged sibling, no gitlinks.
        tmp = Path(tmp)
        sib = tmp / "sibs" / "interposer-pnr"
        sib.mkdir(parents=True)
        commit = ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q"]
        git(sib, "init", "-q")
        git(sib, *commit, "--allow-empty", "-m", "one")
        first = git(sib, "rev-parse", "HEAD").strip()
        git(sib, *commit, "--allow-empty", "-m", "two")
        head = git(sib, "rev-parse", "HEAD").strip()
        root = tmp / "repo"
        root.mkdir()
        git(root, "init", "-q")
        quiet = lambda *_: None  # noqa: E731
        lay_files = {"staging.pin": "STAGE_PNR_PATH=interposer-pnr\n"
                                    "STAGE_PNR_REF=%s\n" % head,
                     "ci/integration-refs.json": "{}",
                     "ci/" + contract_registry.REGISTRY_FILENAME: registry,
                     "Dockerfile": docker, ".gitmodules": "",
                     SOURCES: json.dumps({"_comment": ["x"]})}
        for rel, text in lay_files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        added = record_roots(Sources(root), tmp / "sibs", root, report=quiet)
        recorded = json.loads((root / SOURCES).read_text())
        expect("--record-roots writes the root read in the source, keeping the comment",
               added == ["interposer-pnr"] and recorded["_comment"] == ["x"]
               and recorded["roots"] == {"interposer-pnr": [first]}
               and generate(Sources(root))["repos"]["interposer-pnr"]["roots"]
               == [first])
        expect("a second run adds nothing",
               record_roots(Sources(root), tmp / "sibs", root, report=quiet) == [])
        (root / SOURCES).write_text(json.dumps({"roots": {"interposer-pnr": [r1]}}))
        expect("a recorded root the source contradicts fails, naming both",
               fails(lambda: record_roots(Sources(root), tmp / "sibs", root, report=quiet),
                     "has roots %s, %s records %s" % (first, SOURCES, r1)))
        (root / SOURCES).write_text("{}")
        expect("an id whose source is missing fails, naming it",
               fails(lambda: record_roots(Sources(root), tmp / "none", root, report=quiet),
                     "interposer-pnr: "))

        (root / SOURCES).write_text(json.dumps({"roots": {"interposer-pnr": [first]}}))
        at = lambda sha: {"repos": {"interposer-pnr": dict(  # noqa: E731
            generate(Sources(root))["repos"]["interposer-pnr"], sha=sha)}}
        sibs = tmp / "sibs"
        expect("a pin that moves forward passes",
               gives(lambda: ancestry(at(first), at(head), sibs, root), ["interposer-pnr"]))
        expect("a pin that does not move is not read",
               gives(lambda: ancestry(at(head), at(head), tmp / "none", root), []))
        # Named mutation: drop the merge-base check and this case reads FAILED.
        expect("a pin moved back to an older commit fails, naming both",
               fails(lambda: ancestry(at(head), at(first), sibs, root),
                     "interposer-pnr: %s does not descend from %s" % (first, head)))
        git(sib, "checkout", "-q", "--orphan", "other")
        git(sib, *commit, "--allow-empty", "-m", "unrelated")
        other = git(sib, "rev-parse", "HEAD").strip()
        expect("a pin moved to an unrelated history fails",
               fails(lambda: ancestry(at(head), at(other), sibs, root),
                     "%s does not descend from %s" % (other, head)))
        expect("a sha the source does not hold fails, naming it",
               fails(lambda: ancestry(at(head), at(d), sibs, root),
                     "interposer-pnr: %s does not resolve in" % d))

        # A history started again from one commit: the old line goes on to tip, a
        # parentless commit holds tip's tree, and the new pin sits on top of it.
        git(sib, "checkout", "-q", "-b", "pre", head)
        (sib / "f").write_text("x\n")
        git(sib, "add", "f")
        git(sib, *commit, "-m", "three")
        tip = git(sib, "rev-parse", "HEAD").strip()
        tree = git(sib, "rev-parse", "%s^{tree}" % tip).strip()
        cat = ["-c", "user.name=t", "-c", "user.email=t@t", "commit-tree"]
        start = git(sib, *cat, tree, "-m", "release").strip()
        new = git(sib, *cat, tree, "-p", start, "-m", "after").strip()
        stray = git(sib, *cat, git(sib, "rev-parse", "%s^{tree}" % head).strip(),
                    "-m", "release of another tree").strip()
        on_stray = git(sib, *cat, tree, "-p", stray, "-m", "after").strip()

        def rooted(sha, roots, extra=()):
            return {"repos": {"interposer-pnr": dict(at(sha)["repos"]["interposer-pnr"],
                                                     roots=roots, extra=list(extra))}}
        name = "interposer-pnr"
        expect("a rerooted pin whose root holds the old tip's tree passes",
               gives(lambda: ancestry(at(head), rooted(new, [start]), sibs, root,
                                      {name: [tip]}), [name]))
        expect("the same move without --rerooted fails as unrelated",
               fails(lambda: ancestry(at(head), rooted(new, [start]), sibs, root),
                     "%s does not descend from %s" % (new, head)))
        # Named mutation: drop the tree comparison and this case reads FAILED.
        expect("a rerooted pin whose root holds another tree fails, naming the tip",
               fails(lambda: ancestry(at(head), rooted(on_stray, [stray]), sibs, root,
                                      {name: [tip]}),
                     "no commit on the first-parent line of %s holds %s's tree"
                     % (on_stray, tip)))
        # The same pin, its parent outside the old history but pinned beside it:
        # the export carries that commit, so it may start the new line.
        # Named mutation: delete `or parent in extra` in backward() and this case
        # reads FAILED.
        expect("a pin on a parent outside the old history passes when the entry pins it",
               gives(lambda: ancestry(at(head), rooted(on_stray, [stray], [stray]), sibs,
                                      root, {name: [tip]}), [name]))
        expect("an extra ref that is not the parent lets nothing past",
               fails(lambda: ancestry(at(head), rooted(on_stray, [stray], [first]), sibs,
                                      root, {name: [tip]}),
                     "no commit on the first-parent line of %s holds %s's tree"
                     % (on_stray, tip)))
        expect("an old tip that does not descend from the old pin fails",
               fails(lambda: ancestry(at(head), rooted(new, [start]), sibs, root,
                                      {name: [other]}),
                     "the old history's tip %s does not descend from %s" % (other, head)))
        expect("a matching root that the sources do not record fails, naming both",
               fails(lambda: ancestry(at(head), rooted(new, [first]), sibs, root,
                                      {name: [tip]}),
                     "the roots of %s, %s, are not the roots %s records, %s"
                     % (new, start, SOURCES, first)))
        # Named mutation: compare the roots as sets that meet, not as equal lists,
        # and this case reads FAILED.
        both = sorted([first, start])
        expect("recording one more root than the pin has fails",
               fails(lambda: ancestry(at(head), rooted(new, both), sibs, root, {name: [tip]}),
                     "the roots of %s, %s, are not the roots %s records, %s"
                     % (new, start, SOURCES, " ".join(both))))
        # Named mutation: read the roots on every move, and this case reads FAILED.
        expect("a pin that moves forward leaves its roots to the export",
               gives(lambda: ancestry(at(first), rooted(head, [stray]), sibs, root), [name]))
        expect("a rerooted id whose pin did not move fails",
               fails(lambda: ancestry(at(head), at(head), sibs, root, {name: [tip]}),
                     "named as rerooted, but its pin did not move"))
        expect("an old tip the source does not hold fails, naming it",
               fails(lambda: ancestry(at(head), rooted(new, [start]), sibs, root,
                                      {name: [d]}),
                     "interposer-pnr: %s does not resolve in" % d))

        # Started again twice: commits land on the first new history, whose tip
        # a second root then holds.
        head_tree = git(sib, "rev-parse", "%s^{tree}" % head).strip()
        tip2 = git(sib, *cat, head_tree, "-p", new, "-m", "on the first restart").strip()
        start2 = git(sib, *cat, head_tree, "-m", "release again").strip()
        new2 = git(sib, *cat, head_tree, "-p", start2, "-m", "after").strip()
        expect("a pin started again twice passes along both old tips",
               gives(lambda: ancestry(at(head), rooted(new2, [start2]), sibs, root,
                                      {name: [tip, tip2]}), [name]))
        expect("the same move naming only the last old tip fails at the pin before it",
               fails(lambda: ancestry(at(head), rooted(new2, [start2]), sibs, root,
                                      {name: [tip2]}),
                     "%s does not descend from %s, the pin before it" % (tip2, head)))
        astray = git(sib, *cat, head_tree, "-p", stray, "-m", "on another release").strip()
        expect("a second old tip whose history no commit of the first one's tree starts fails",
               fails(lambda: ancestry(at(head), rooted(new2, [start2]), sibs, root,
                                      {name: [tip, astray]}),
                     "no commit on the first-parent line of %s holds %s's tree"
                     % (astray, tip)))
        # A squash on top of the published line: not a root, its parent is older.
        squash = git(sib, *cat, tree, "-p", first, "-m", "release").strip()
        expect("a squash holding the old tip's tree on an older parent passes",
               gives(lambda: ancestry(at(head), rooted(squash, [first]), sibs, root,
                                      {name: [tip]}), [name]))
        # Named mutation: check the parents against the pin before rather than
        # the tip, and this case reads FAILED.
        on_published = git(sib, *cat, head_tree, "-p", new, "-m", "release again").strip()
        expect("a second restart squashed onto a published commit of the first passes",
               gives(lambda: ancestry(at(head), rooted(on_published, [start]), sibs, root,
                                      {name: [tip, tip2]}), [name]))
        expect("the same move with a root the sources do not record fails, naming both",
               fails(lambda: ancestry(at(head), rooted(on_published, [first]), sibs, root,
                                      {name: [tip, tip2]}),
                     "the roots of %s, %s, are not the roots %s records, %s"
                     % (on_published, start, SOURCES, first)))
        first_tree = git(sib, "rev-parse", "%s^{tree}" % first).strip()
        undo = git(sib, *cat, first_tree, "-p", head, "-m", "revert to the first").strip()
        off_first = git(sib, *cat, tree, "-p", first, "-m", "a branch").strip()
        # Named mutation: drop the check that the commit is not in the old tip's
        # history, and this case reads FAILED.
        expect("an old commit that repeats the old tip's tree starts no history",
               fails(lambda: ancestry(at(head), rooted(off_first, [first]), sibs, root,
                                      {name: [undo]}),
                     "no commit on the first-parent line of %s holds %s's tree"
                     % (off_first, undo)))
        # A side line holding the tip's tree, merged into the old history: the new
        # pin branches off it without the old pin, so nothing starts a history.
        # Named mutation: ask whether the commit is in the old pin's history
        # rather than the tip's, and this case reads FAILED.
        side = git(sib, *cat, tree, "-p", first, "-m", "a side line").strip()
        tip_m = git(sib, *cat, tree, "-p", tip, "-p", side, "-m", "merge the side line").strip()
        off_side = git(sib, *cat, head_tree, "-p", side, "-m", "a branch off it").strip()
        expect("a commit of the old history off its first-parent line starts no history",
               fails(lambda: ancestry(at(head), rooted(off_side, [first]), sibs, root,
                                      {name: [tip_m]}),
                     "no commit on the first-parent line of %s holds %s's tree"
                     % (off_side, tip_m)))
        # Named mutation: drop --first-parent and this case reads FAILED.
        merged = git(sib, *cat, tree, "-p", stray, "-p", squash, "-m", "merge").strip()
        expect("a squash reached only through a merge's second parent starts nothing",
               fails(lambda: ancestry(at(head), rooted(merged, sorted([first, stray])), sibs,
                                      root, {name: [tip]}),
                     "no commit on the first-parent line of %s holds %s's tree"
                     % (merged, tip)))

    width = max(len(name) for name, _ in cases)
    for name, ok in cases:
        print("%-*s %s" % (width, name, "ok" if ok else "FAILED"))
    return 0 if all(ok for _, ok in cases) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--stdout", action="store_true")
    mode.add_argument("--digest", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--record-roots", action="store_true")
    ap.add_argument("--rev", help="read every source from this commit, not the working tree")
    ap.add_argument("--source-root", type=Path, help="where the siblings sit, as <root>/<id>")
    ap.add_argument("--superproject", type=Path, default=ROOT,
                    help="the checkout whose submodule module stores hold the gitlinked "
                         "sources; default this one")
    ap.add_argument("--rerooted", action="append", default=[], metavar="ID=SHA[,SHA...]",
                    help="on a write, ID's history was started again: each SHA is the "
                         "last commit of a history it left, oldest first")
    args = ap.parse_args(argv)
    rerooted = {}
    for item in args.rerooted:
        rid, _, shas = item.partition("=")
        tips = shas.split(",")
        if (not rid or not all(SHA.fullmatch(sha) for sha in tips)
                or len(set(tips)) != len(tips) or rid in rerooted):
            ap.error("--rerooted takes ID=SHA[,SHA...], distinct full shas, once per id: %r"
                     % item)
        rerooted[rid] = tips
    if rerooted and (args.check or args.stdout or args.digest or args.record_roots
                     or args.self_test):
        ap.error("--rerooted goes with a write")
    if args.self_test:
        return self_test()
    if args.record_roots:
        if args.rev or args.source_root is None:
            ap.error("--record-roots takes --source-root and reads the working tree")
        try:
            added = record_roots(Sources(ROOT), args.source_root.absolute(),
                                 args.superproject.absolute())
        except PinSetError as exc:
            for line in str(exc).splitlines():
                print("pin set: FAIL, %s" % line, file=sys.stderr)
            return 1
        print("pin set: recorded the roots of %d ids in %s; now regenerate %s"
              % (len(added), SOURCES, OUT))
        return 0
    if args.source_root is not None and not args.source_root.is_absolute():
        ap.error("--source-root must be absolute")
    if args.rev and not (args.check or args.stdout or args.digest):
        ap.error("--rev goes with --check, --stdout or --digest; the file is written "
                 "from the working tree")
    src = Sources(ROOT, args.rev)
    try:
        if args.check:
            why = check(src)
            if why:
                print("pin set: FAIL, %s" % why, file=sys.stderr)
                return 1
            print("pin set: %s matches its sources, sha256 %s"
                  % (OUT, hashlib.sha256(src.read(OUT)).hexdigest()))
            return 0
        data = canonical(generate(src))
    except PinSetError as exc:
        print("pin set: FAIL, %s" % exc, file=sys.stderr)
        return 1
    if args.stdout:
        sys.stdout.buffer.write(data)
    elif args.digest:
        print(hashlib.sha256(data).hexdigest())
    else:
        try:
            if (ROOT / OUT).is_file():
                ancestry(json.loads((ROOT / OUT).read_bytes()), json.loads(data),
                         args.source_root or ROOT.parent, args.superproject.absolute(),
                         rerooted)
        except PinSetError as exc:
            for line in str(exc).splitlines():
                print("pin set: FAIL, %s" % line, file=sys.stderr)
            print("pin set: %s left as it was" % OUT, file=sys.stderr)
            return 1
        (ROOT / OUT).write_bytes(data)
        print("pin set: wrote %s, sha256 %s" % (OUT, hashlib.sha256(data).hexdigest()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
