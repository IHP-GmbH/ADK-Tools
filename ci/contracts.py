#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Facts that have to agree across repositories, asserted from the one place
where more than one repository exists at once.

Every repository in this ecosystem verifies itself, and no per-repo gate can
ever see any of the properties below, because each of them spans two trees. A
schema hand-copied from chiplet-spec into adk is correct in both repositories
separately and wrong together. A KLayout floor raised in one repository is a
sensible change there and silently means the decks are now tested against a
KLayout the image does not ship. The failure mode is uniform: both suites stay
green while the combination stops being the thing anyone tested.

What each contract covers is no longer written here. It is declared once, as
data, in `ci/contract-registry.json`, read by `ci/contract_registry.py`, and
every list below is derived from it. The lists used to be nine Python literals
in this file, which is nine places to forget: a new mirror had to be added to
the pair list, its directory to the twin scan, and the repository it lives in to
three more. The registry states the artifact once and this file derives the rest,
so a mirror that is declared is scanned, and a scan cannot go stale relative to
the pairs it is supposed to be guarding.

The contracts, and what each one is for:

  identical-files       declared byte-identity pairs, derived from the mirrors of
                        every artifact the registry judges by bytes. A hand-synced
                        copy is a copy that drifts; the only question is when.
  version-policy        the copies of every artifact the registry judges by the
                        version it declares rather than by its bytes. A vendored
                        reader is allowed to sit at an older same-major minor
                        while the reference moves on; it is never allowed to
                        carry different bytes under one declared version, which
                        is the in-place edit no per-repository gate can see. The
                        owner's own file is judged too, because a floor and a
                        `current` that nothing reads back are two numbers in a
                        file.
  undeclared-twins      a basename living in two trees without a declared pair.
                        Without this, the check above covers exactly the files
                        somebody remembered to list, and a new shared schema
                        escapes on the day it is added. The directories scanned
                        are derived from the registry, so a mirror in a new
                        directory brings that directory into the scan with it.
  gitmodules-track      every pin, in `.gitmodules` and in
                        `ci/integration-refs.json`, names the branch this
                        superproject is on. `git submodule update --remote`
                        follows that declaration, and the plugin's `main` and
                        `dev` have no common ancestor, so a stale declaration
                        moves a pin onto a disjoint history. Also asserts that
                        the registry's repository table and the set of pins are
                        the same set, in both directions.
  ci-gate               every repository publishes the one required context, its
                        `needs` is non-empty, and the job compares every result
                        it depends on. A `ci-gate` with `if: always()` and no
                        comparison reports success after its dependencies went
                        red, and nothing inside that repository would notice.
                        This one reads each repository's declared branch rather
                        than the commit pinned here: the question is whether the
                        gate that runs today is a real gate, and a pin from
                        before that repository had any CI would answer a
                        different question and report it as a defect.
  klayout               one KLayout across the ecosystem, sourced from the image
                        ARG. Testing a deck against a different KLayout than the
                        one that ships proves the deck works somewhere other
                        than where it runs.
  pip-constraints       every pinned `*_PIP` version satisfies every constraint
                        any repository declares on that package. Catches a floor
                        raised above the pin, which turns an install that
                        resolves into an install that resolves to something else.
                        Also scans every checked-out tree for a tracked
                        dependency file that is in neither the registry nor its
                        exemptions, which closes the class rather than the
                        instance: a repository that grows a second requirements
                        file is otherwise unchecked and looks identical to one
                        that has none.

Usage:
    contracts.py --root DIR [--track dev]
    contracts.py --root DIR --dir kicad=/elsewhere/kicad
    contracts.py --self-test

`--root` holds one checkout per repository, named as in the registry's repos
table; the integration workflow lays them out that way and this umbrella working
directory already is that layout. A repository that is missing is a failure and
never a skip: a contract that quietly covers nine repositories reads exactly like
one that covers ten.
"""

import argparse
import ast
import base64
import json
import os
import pathlib
import re
import subprocess
import sys

import contract_registry
import resolve_refs

HERE = pathlib.Path(__file__).resolve().parent

# The registry is loaded once, here, and every list this file used to carry is a
# view onto it. A registry that does not validate is exit 2 and not exit 1: the
# contracts were not evaluated, which is a different fact from any of them
# failing, and reporting the second when the first happened sends somebody to
# look at the repositories.
try:
    REGISTRY = contract_registry.load_default()
except contract_registry.RegistryError as _e:          # pragma: no cover
    print("NO VERDICT: the contract registry does not validate, so nothing was "
          "checked with it:", file=sys.stderr)
    for _clause in _e.clauses:
        print("  %s" % _clause, file=sys.stderr)
    raise SystemExit(2)

#: Every derived view: the repository layout, the byte-identity pairs, the
#: directories the twin scan covers, the ci-gate workflow per repository, the
#: KLayout consumers, the pinned packages and the files that constrain them.
D = contract_registry.derived(REGISTRY)

# Moved to contract_registry.py with the rest of the version handling, and named
# here so this file reads the same as it did. `packaging` is not in the standard
# library and this runs before anything is installed, so comparison is done by
# hand; anything not understood is reported as a failure rather than passed over.
parse_version = contract_registry.parse_version
compare = contract_registry.compare
satisfies = contract_registry.satisfies


class Trees:
    """Read-only access to a set of checkouts, by logical id."""

    def __init__(self, root, overrides=None):
        self.root = pathlib.Path(root)
        self.paths = {}
        self._tracked = {}
        for rid, name in D.layout.items():
            self.paths[rid] = self.root / name
        for rid, path in (overrides or {}).items():
            self.paths[rid] = pathlib.Path(path)

    def missing(self):
        return [r for r in sorted(D.file_repos) if not self.paths[r].is_dir()]

    def read(self, rid, rel):
        """Bytes, or None if the file is not there."""
        p = self.paths[rid] / rel
        try:
            return p.read_bytes()
        except (OSError, KeyError):
            return None

    def listdir(self, rid, rel):
        p = self.paths[rid] / rel
        try:
            return sorted(x.name for x in p.iterdir() if x.is_file())
        except (OSError, KeyError):
            return []

    def tracked(self, rid):
        """Every path git tracks in that checkout.

        Tracked rather than walked: a walk finds virtual environments, build
        directories and whatever else a developer left behind, and a check that
        reports those is a check people turn off.
        """
        if rid in self._tracked:
            return self._tracked[rid]
        p = self.paths.get(rid)
        try:
            out = subprocess.run(["git", "-C", str(p), "ls-files"],
                                 capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError, TypeError) as e:
            raise RuntimeError("the tracked files of %s could not be listed "
                               "(%s), so the scan for undeclared dependency "
                               "files covers less than it claims" % (rid, e))
        self._tracked[rid] = [line for line in out.splitlines() if line]
        return self._tracked[rid]


class DictTrees:
    """The same surface over a dict, for the self-test. No filesystem."""

    def __init__(self, files):
        # {(rid, relpath): bytes or str}
        self.files = {
            k: (v.encode() if isinstance(v, str) else v) for k, v in files.items()
        }

    def missing(self):
        return []

    def read(self, rid, rel):
        return self.files.get((rid, rel))

    def listdir(self, rid, rel):
        out = set()
        prefix = rel.rstrip("/") + "/"
        for r, path in self.files:
            if r == rid and path.startswith(prefix):
                tail = path[len(prefix):]
                if "/" not in tail:
                    out.add(tail)
        return sorted(out)

    def tracked(self, rid):
        return sorted(path for r, path in self.files if r == rid)


# --------------------------------------------------------------------------
# requirement parsing
# --------------------------------------------------------------------------

#: A tracked file that declares Python dependencies. Anything matching this in a
#: checked-out tree is either a registered declaration or a defect.
DEPENDENCY_FILE = re.compile(r"(?:^|/)(requirements[^/]*\.txt|pyproject\.toml)$")


def split_requirement(line):
    """`klayout>=0.30.5` -> ('klayout', [('>=', '0.30.5')]). None if not one."""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    # Environment markers and extras are not used anywhere here; if one appears
    # it must not be silently dropped.
    if ";" in line or "[" in line:
        raise ValueError("requirement %r uses a marker or extra, which this "
                         "checker does not evaluate" % line)
    m = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", line)
    if not m:
        return None
    name, rest = m.group(1), m.group(2).strip()
    if rest.startswith("@"):
        # A direct URL dependency pins nothing this checker can compare.
        return name.lower().replace("-", "").replace("_", ""), []
    specs = []
    for chunk in filter(None, (c.strip() for c in rest.split(","))):
        for op in contract_registry.OPS:
            if chunk.startswith(op):
                specs.append((op, chunk[len(op):].strip()))
                break
        else:
            raise ValueError("cannot read the constraint %r in %r" % (chunk, line))
    return name.lower().replace("-", "").replace("_", ""), specs


def requirement_lines(rid, rel, blob):
    """Constraint lines out of a requirements.txt or a pyproject.toml."""
    text = blob.decode()
    if rel.endswith(".toml"):
        # One field, read without a TOML parser so this file has no import that
        # a 3.10 interpreter would not have. `dependencies = ["A", "B"]`.
        m = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text)
        if not m:
            return []
        return [x.strip().strip('"').strip("'") for x in m.group(1).split(",") if x.strip()]
    return text.splitlines()


# --------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------


def contract_identical_files(trees):
    out = []
    for lrid, lrel, rrid, rrel, why in D.identical:
        left, right = trees.read(lrid, lrel), trees.read(rrid, rrel)
        if left is None:
            out.append("%s:%s does not exist, so the pair it forms with %s:%s "
                       "cannot be compared (%s)" % (lrid, lrel, rrid, rrel, why))
            continue
        if right is None:
            out.append("%s:%s does not exist, so the pair it forms with %s:%s "
                       "cannot be compared (%s)" % (rrid, rrel, lrid, lrel, why))
            continue
        if left != right:
            out.append("%s:%s and %s:%s have drifted apart. They are declared "
                       "byte-identical: %s" % (lrid, lrel, rrid, rrel, why))
    return out


def version_policy_artifacts():
    """The artifacts whose copies are judged by the version they declare.

    Tool pins are excluded and not forgotten: a pin is judged against the
    declarations the `klayout` and `pip-constraints` contracts already read, and
    running it here as well would report one disagreement twice.
    """
    return [a for a in REGISTRY.artifacts
            if a.identity == "version_policy" and a.kind != "tool_pin"]


def contract_version_policy(trees):
    """Copies judged by the version they declare rather than by their bytes.

    `owner_head` is None throughout: this runs over the pinned checkouts, so the
    owner's branch head is not on disk and `AHEAD_OF_PIN` is simply out of
    reach. That is the honest shape. A copy that has been re-synced ahead of the
    pin is reported here as drifted until the pin bump lands, and the
    producer-side conformance run, which does fetch the head, is where that
    distinction is made.
    """
    out = []
    for art in version_policy_artifacts():
        owner_blob = trees.read(art.owner, art.path)
        state, clauses = contract_registry.judge_owner(art, owner_blob)
        if state not in contract_registry.GREEN_STATES:
            for c in clauses:
                out.append("%s: %s:%s, which owns the artifact, is %s. %s"
                           % (art.id, art.owner, art.path, state, c))
        for m in art.mirrors:
            state, clauses = contract_registry.judge_mirror(
                art, owner_blob, None, trees.read(m.repo, m.path))
            if state in contract_registry.GREEN_STATES:
                continue
            for c in clauses:
                out.append("%s: %s:%s is %s against %s:%s. %s (%s)"
                           % (art.id, m.repo, m.path, state, art.owner,
                              art.path, c, m.why))
    return out


def contract_undeclared_twins(trees):
    # Every declared path, whatever mode the artifact is judged in. Built from
    # the registry rather than from the byte-identity pairs: an artifact that
    # moves to a version policy leaves that list, and a scan that then reported
    # its two copies as an undeclared twin would be reporting the one
    # relationship this file knows most about.
    declared = set()
    for art in REGISTRY.artifacts:
        declared.add(os.path.basename(art.path))
        for m in art.mirrors:
            declared.add(os.path.basename(m.path))

    seen = {}
    for rid, rel in D.twin_scan:
        for name in trees.listdir(rid, rel):
            seen.setdefault(name, []).append("%s:%s/%s" % (rid, rel, name))

    out = []
    for name, where in sorted(seen.items()):
        if len(where) < 2 or name in declared or name in D.twin_exempt:
            continue
        out.append("%s exists in more than one tree (%s) and no pair declares "
                   "the relationship. Either they are copies, in which case add "
                   "the pair, or they are different documents that happen to "
                   "share a name, in which case say so in TWIN_EXEMPT."
                   % (name, ", ".join(where)))
    return out


def repos_vs_declared(entries, repos_ids):
    """The registry's repository table and the set of pins are one set.

    Both directions, because they fail differently. A pin with no row is a tree
    the integration job checks out and no contract looks at. A row with no pin is
    a repository named in a public file that this superproject does not check
    out, which is also the shape a private repository would take if one ever
    reached this table.
    """
    out = []
    declared_ids = set()
    for rid, slug, _branch, _pin in entries:
        if rid is None:
            out.append("the submodule at %r has no logical id, so nothing in the "
                       "contract registry can name the repository it pins"
                       % (slug,))
            continue
        declared_ids.add(rid)
    for rid in sorted(declared_ids - repos_ids):
        out.append("%s is pinned by this superproject and has no row in the "
                   "contract registry, so every contract passes over it while "
                   "the integration job checks it out" % rid)
    for rid in sorted(repos_ids - declared_ids - {"tools"}):
        out.append("%s has a row in the contract registry and nothing here pins "
                   "it. Every row has to be a repository this superproject "
                   "actually checks out." % rid)
    return out


def contract_gitmodules_track(trees, track):
    blob = trees.read("tools", ".gitmodules")
    if blob is None:
        return ["adk-tools has no .gitmodules, which for a lockfile means it "
                "pins nothing"]
    text = blob.decode()
    names = re.findall(r'(?m)^\[submodule "([^"]+)"\]', text)
    if not names:
        return ["adk-tools .gitmodules declares no submodules"]

    out = []
    for name in names:
        body = re.split(r'(?m)^\[submodule "%s"\]' % re.escape(name), text)[1]
        body = re.split(r"(?m)^\[", body)[0]
        m = re.search(r"(?m)^\s*branch\s*=\s*(\S+)", body)
        if not m:
            out.append("%s declares no branch, so `git submodule update "
                       "--remote` follows that repository's default branch "
                       "instead of the track this one is on" % name)
        elif m.group(1) != track:
            out.append("%s declares branch %r while this superproject is on %r. "
                       "`git submodule update --remote` would follow the "
                       "declaration and move the pin off the track."
                       % (name, m.group(1), track))

    # The two repositories the integration job checks out that are not
    # submodules are pinned in their own file, and a release fold has to move
    # them onto the main track exactly like the gitlinks. Checking only
    # .gitmodules would leave half the pins following the other track with
    # nothing red anywhere.
    blob = trees.read("tools", "ci/integration-refs.json")
    if blob is None:
        return out + ["adk-tools has no ci/integration-refs.json, so the "
                      "repositories that are not submodules are unpinned and "
                      "the integration job follows whatever their branch head "
                      "is on the day it runs"]
    try:
        extra = json.loads(blob.decode())
    except ValueError as e:
        return out + ["ci/integration-refs.json does not parse (%s)" % e]
    for rid, entry in sorted(extra.items()):
        if rid.startswith("_"):
            continue
        if entry.get("branch") != track:
            out.append("ci/integration-refs.json pins %s from branch %r while "
                       "this superproject is on %r" % (rid, entry.get("branch"), track))
        if not re.fullmatch(r"[0-9a-f]{40}", str(entry.get("pin", ""))):
            out.append("ci/integration-refs.json gives %s the pin %r, which is "
                       "not a 40-hex sha" % (rid, entry.get("pin")))

    out.extend(repos_vs_declared(resolve_refs.declared(text, extra),
                                 set(D.layout)))
    return out


def contract_ci_gate(fetch, load_yaml):
    out = []
    for rid, rel in sorted(D.ci_gate_workflow.items()):
        try:
            text = fetch(rid, rel)
        except RuntimeError as e:
            # Never reported as "this repository has no gate": an unanswered
            # lookup and a missing file are different facts, and printing the
            # second when the first happened sends somebody to fix a repository
            # that is fine.
            out.append("%s: %s" % (rid, e))
            continue
        if text is None:
            out.append("%s does not publish %s on its declared branch, so its "
                       "ruleset requires a context nothing will ever post and "
                       "every pull request there is unmergeable" % (rid, rel))
            continue
        try:
            doc = load_yaml(text)
        except Exception as e:                     # noqa: BLE001 - reported
            out.append("%s:%s does not parse as YAML (%s)" % (rid, rel, e))
            continue
        jobs = (doc or {}).get("jobs") or {}
        gate = jobs.get("ci-gate")
        if gate is None:
            out.append("%s:%s declares no ci-gate job" % (rid, rel))
            continue

        needs = gate.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        if not needs:
            out.append("%s ci-gate has an empty `needs`, so it is green having "
                       "run nothing. This is the one failure the repository "
                       "itself cannot see: the gate reports success and the "
                       "ruleset is satisfied." % rid)
            continue

        body = "\n".join(
            str(s.get("run", "")) for s in (gate.get("steps") or [])
            if isinstance(s, dict)
        )
        for need in needs:
            if ("needs.%s.result" % need) not in body:
                out.append("%s ci-gate lists %r in `needs` but never compares "
                           "needs.%s.result. With `if: always()` the gate runs "
                           "after that job fails and reports success anyway."
                           % (rid, need, need))
    return out


def contract_klayout(trees):
    blob = trees.read("tools", "Dockerfile")
    if blob is None:
        return ["adk-tools has no Dockerfile, so the KLayout version has no "
                "single source of truth to compare against"]
    m = re.search(r"(?m)^ARG %s=([0-9.]+)\s*$" % re.escape(D.klayout_arg),
                  blob.decode())
    if not m:
        return ["adk-tools Dockerfile has no `ARG %s=`, which is where "
                "the ecosystem's KLayout version is declared" % D.klayout_arg]
    pin = m.group(1)

    out = []
    for rid, rel, pattern, op in D.klayout_consumers:
        cblob = trees.read(rid, rel)
        if cblob is None:
            out.append("%s:%s is missing, and it is one of the places the "
                       "KLayout version has to agree with the image pin %s"
                       % (rid, rel, pin))
            continue
        cm = re.search(pattern, cblob.decode())
        if not cm:
            out.append("%s:%s no longer states a KLayout version in the form "
                       "this check reads (%s). An unreadable declaration is not "
                       "an agreeing one." % (rid, rel, pattern))
            continue
        try:
            ok = satisfies(pin, op, cm.group(1))
        except ValueError as e:
            out.append("%s:%s: %s" % (rid, rel, e))
            continue
        if not ok:
            out.append("%s:%s declares klayout%s%s, which the image pin %s does "
                       "not satisfy. A deck tested against a KLayout the image "
                       "does not ship is a deck proven to work somewhere else."
                       % (rid, rel, op, cm.group(1), pin))
    return out


def undeclared_dependency_files(trees):
    """Tracked dependency files nothing compares against the image pins.

    The registered files close the instance; this closes the class. A repository
    that grows a second requirements file is unchecked from the moment it lands
    and looks exactly like a repository that has none.
    """
    out = []
    known = set(D.requirement_files) | set(D.pins_exempt)
    for rid in D.file_repos:
        try:
            tracked = trees.tracked(rid)
        except RuntimeError as e:
            out.append(str(e))
            continue
        for rel in tracked:
            if not DEPENDENCY_FILE.search(rel):
                continue
            if (rid, rel) in known:
                continue
            out.append("%s:%s declares Python dependencies and the contract "
                       "registry neither reads it nor exempts it, so nothing "
                       "compares it against the versions the image ships. "
                       "Either add it to the pin declarations or exempt it with "
                       "a reason." % (rid, rel))
    return out


def contract_pip_constraints(trees):
    blob = trees.read("tools", "Dockerfile")
    if blob is None:
        return ["adk-tools has no Dockerfile, so nothing declares the pins"]
    text = blob.decode()

    pins = {}
    for pkg, arg in D.pip_args.items():
        m = re.search(r"(?m)^ARG %s=([0-9A-Za-z.\-]+)\s*$" % re.escape(arg), text)
        if not m:
            return ["adk-tools Dockerfile has no `ARG %s=`, so the pin for %s "
                    "cannot be checked against what the repositories ask for"
                    % (arg, pkg)]
        pins[pkg] = m.group(1)

    out = []
    for rid, rel in D.requirement_files:
        rblob = trees.read(rid, rel)
        if rblob is None:
            out.append("%s:%s is missing; it is listed as a place this "
                       "ecosystem declares Python constraints, so its absence "
                       "silently shrinks this check" % (rid, rel))
            continue
        for line in requirement_lines(rid, rel, rblob):
            try:
                parsed = split_requirement(line)
            except ValueError as e:
                out.append("%s:%s: %s" % (rid, rel, e))
                continue
            if not parsed:
                continue
            name, specs = parsed
            if name not in pins:
                continue
            for op, bound in specs:
                try:
                    ok = satisfies(pins[name], op, bound)
                except ValueError as e:
                    out.append("%s:%s: %s" % (rid, rel, e))
                    continue
                if not ok:
                    out.append("%s:%s asks for %s%s%s and the image pins %s. "
                               "The pin is what actually runs, so this "
                               "repository is exercised against a version it "
                               "declares it does not support."
                               % (rid, rel, name, op, bound, pins[name]))
    out.extend(undeclared_dependency_files(trees))
    return out


CONTRACTS = [
    ("identical-files", lambda t, ctx: contract_identical_files(t)),
    ("version-policy", lambda t, ctx: contract_version_policy(t)),
    ("undeclared-twins", lambda t, ctx: contract_undeclared_twins(t)),
    ("gitmodules-track", lambda t, ctx: contract_gitmodules_track(t, ctx["track"])),
    ("ci-gate", lambda t, ctx: contract_ci_gate(ctx["fetch"], ctx["load_yaml"])),
    ("klayout", lambda t, ctx: contract_klayout(t)),
    ("pip-constraints", lambda t, ctx: contract_pip_constraints(t)),
]


def run(trees, ctx, only=None):
    """[(name, [failure, ...])], in declaration order."""
    return [
        (name, fn(trees, ctx))
        for name, fn in CONTRACTS
        if only is None or name in only
    ]


# --------------------------------------------------------------------------
# the tripwire
#
# The registry only helps while it is the only copy. These four checks are about
# this directory rather than about the ecosystem: they fail when a second reader
# of the data appears, when one of the hand-lists grows back, when the version
# comparator is copied somewhere it can diverge, or when a workflow file starts
# carrying its own copy of what the registry already says.
# --------------------------------------------------------------------------

#: The names the hand-lists had. A module-level assignment of one of these to
#: anything other than a plain constant is one of them growing back.
FORMER_HAND_LISTS = ("IDENTICAL", "TWIN_SCAN", "TWIN_EXEMPT", "CI_GATE_WORKFLOW",
                     "KLAYOUT_CONSUMERS", "PIP_ARGS", "REQUIREMENT_FILES",
                     "LAYOUT", "FILE_REPOS")

#: Names that may exist in exactly one module, because two implementations of a
#: comparison rule is two rules.
SINGLE_HOME = ("parse_version", "compare", "satisfies")


def _code_strings(tree):
    """Every string literal that is not a docstring.

    A docstring naming the registry file is documentation; a string literal in
    code that names it is a second parser waiting to happen, and only the second
    one is a finding.
    """
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def _module_level_names(tree):
    """{name: value node} for every module-level assignment, and every def."""
    assigns, defs = {}, set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigns[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigns[node.target.id] = node.value
    return assigns, defs


def tripwire(ci_dir=None, workflow_dir=None):
    """[failure, ...]; empty when this directory still has one of everything."""
    ci_dir = pathlib.Path(ci_dir or HERE)
    workflow_dir = pathlib.Path(workflow_dir or (ci_dir.parent / ".github" / "workflows"))
    out = []

    sources = {}
    for path in sorted(ci_dir.glob("*.py")):
        try:
            sources[path.name] = path.read_text()
        except OSError as e:
            out.append("%s could not be read (%s), so the tripwire covers less "
                       "than it claims" % (path.name, e))

    trees = {}
    for name, text in sorted(sources.items()):
        try:
            trees[name] = ast.parse(text)
        except SyntaxError as e:
            out.append("%s does not parse (%s)" % (name, e))

    # (a) one reader of the registry file, and it is the reader.
    needle = contract_registry.REGISTRY_FILENAME
    holders = sorted(name for name, tree in trees.items()
                     if any(needle in s for s in _code_strings(tree)))
    if holders != ["contract_registry.py"]:
        out.append("the registry file is named in %s. It is data with exactly "
                   "one reader; a second one is a second parser, and the second "
                   "parser is the one that goes stale."
                   % (", ".join(holders) if holders else "no module at all"))

    # (b) none of the hand-lists has grown back.
    for name, tree in sorted(trees.items()):
        assigns, defs = _module_level_names(tree)
        for former in FORMER_HAND_LISTS:
            value = assigns.get(former)
            if value is None or isinstance(value, ast.Constant):
                continue
            out.append("%s assigns a top-level %s. That list is derived from the "
                       "registry now; a second copy of it is the thing this "
                       "migration removed." % (name, former))
        # (c) one home for the comparator and for the judgements.
        for fname in sorted(defs):
            if name == "contract_registry.py":
                continue
            if fname in SINGLE_HOME or fname.startswith("judge_"):
                out.append("%s defines %s, which lives in contract_registry.py. "
                           "Two implementations of one comparison rule is two "
                           "rules." % (name, fname))

    # (d) the workflows do not carry their own copy of what the registry says.
    want_paths = {D.layout[rid] for rid in D.file_repos}
    for wf in ("integration.yml", "integration-floating.yml"):
        path = workflow_dir / wf
        if not path.is_file():
            out.append("%s is not there, and it is one of the two workflows that "
                       "have to lay out exactly the trees the registry names" % wf)
            continue
        text = path.read_text()
        got = set(re.findall(r"(?m)^\s+path:\s*(\S+)\s*$", text))
        if got != want_paths:
            out.append("%s checks out %s and the registry names %s as the trees "
                       "that are read from disk. A workflow that lays out a "
                       "different set is a run that checks something else."
                       % (wf, ", ".join(sorted(got)) or "nothing",
                          ", ".join(sorted(want_paths))))
        for name, arg in _pins_heredoc(text):
            key = name.lower().replace("-", "").replace("_", "")
            if key not in D.pip_args:
                out.append("%s installs %s from ARG %s and the registry pins no "
                           "such package. The list of pins in a workflow is the "
                           "sixth copy of this table; it has to be a subset of "
                           "the one in the registry." % (wf, name, arg))
            elif D.pip_args[key] != arg:
                out.append("%s installs %s from ARG %s while the registry reads "
                           "it from ARG %s" % (wf, name, arg, D.pip_args[key]))
    return out


def _pins_heredoc(text):
    """[(package, ARG), ...] out of the `PINS` heredoc, or [] if there is none."""
    m = re.search(r"(?ms)<<'PINS'\n(.*?)\n\s*PINS\s*$", text)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        parts = line.split()
        if len(parts) == 2:
            out.append((parts[0], parts[1]))
    return out


# --------------------------------------------------------------------------
# self-test
#
# Every contract gets a tree that makes it fail. A checker whose negative case
# has never been run is indistinguishable from `exit 0` until the day it is
# supposed to catch something.
# --------------------------------------------------------------------------

GATE_OK = """
jobs:
  tests:
    runs-on: ubuntu-24.04
  ci-gate:
    needs: [tests]
    if: always()
    steps:
      - run: test "${{ needs.tests.result }}" = "success"
"""

GATE_NO_NEEDS = """
jobs:
  ci-gate:
    needs: []
    if: always()
    steps:
      - run: echo fine
"""

GATE_UNCOMPARED = """
jobs:
  tests:
    runs-on: ubuntu-24.04
  ci-gate:
    needs: [tests]
    if: always()
    steps:
      - run: echo "tests ran"
"""

DOCKERFILE = """
ARG KLAYOUT_PIP=0.30.5
ARG PYYAML_PIP=6.0.3
ARG PYQT6_PIP=6.11.0
ARG JINJA2_PIP=3.1.6
ARG JSONSCHEMA_PIP=4.26.0
ARG PSUTIL_PIP=7.2.2
ARG PYTEST_PIP=9.1.0
"""

# Every submodule, not two of them: the repository table and the set of pins are
# now asserted to be the same set, so a fixture that pins a subset would be a
# fixture that fails for a reason the case is not about.
GITMODULES_DEV = """
[submodule "tools/kicad"]
\tpath = tools/kicad
\turl = https://github.com/IHP-GmbH/KiCad-ADK-MOD.git
\tbranch = dev
[submodule "tools/chiplet-studio"]
\tpath = tools/chiplet-studio
\turl = https://github.com/IHP-GmbH/chiplet-studio.git
\tbranch = dev
[submodule "tools/chiplet_kicad_plugin"]
\tpath = tools/chiplet_kicad_plugin
\turl = https://github.com/IHP-GmbH/Chiplets-KiCad-Plugin.git
\tbranch = dev
[submodule "tools/gds_to_kicad"]
\tpath = tools/gds_to_kicad
\turl = https://github.com/IHP-GmbH/gds2kicad.git
\tbranch = dev
[submodule "tools/adk"]
\tpath = tools/adk
\turl = https://github.com/IHP-GmbH/IHP-Open-ADK.git
\tbranch = dev
[submodule "tools/interposer"]
\tpath = tools/OpenIntM4TM2
\turl = https://github.com/IHP-GmbH/OpenIntM4TM2.git
\tbranch = dev
[submodule "tools/interconnect_pdk"]
\tpath = tools/IHP-Interconnect-IntM4TM2
\turl = https://github.com/IHP-GmbH/IHP-Interconnect-IntM4TM2.git
\tbranch = dev
"""

INTEGRATION_REFS = json.dumps({
    "_comment": ["ignored"],
    "spec": {"slug": "IHP-GmbH/chiplet-spec", "branch": "dev", "pin": "c" * 40},
    "docs": {"slug": "IHP-GmbH/IHP-Open-ADK-docs", "branch": "dev",
             "pin": "d" * 40},
})


def _declaring_blob(locator, version):
    """A file that declares `version` where `locator` says to look for it."""
    kind, arg = locator
    if kind == "py_assign":
        return '"""a reference reader."""\n%s = "%s"\nbody = 1\n' % (arg, version)
    if kind == "json_pointer":
        tokens = [t for t in arg.split("/") if t]
        doc = version
        for token in reversed(tokens):
            doc = {token.replace("~1", "/").replace("~0", "~"): doc}
        return json.dumps(doc, indent=2) + "\n"
    if kind == "dockerfile_arg":
        return "ARG %s=%s\n" % (arg, version)
    raise ValueError("the self-test cannot synthesise a %r declaration" % (kind,))


def _base_files():
    files = {
        ("tools", "Dockerfile"): DOCKERFILE,
        ("tools", ".gitmodules"): GITMODULES_DEV,
        ("tools", "ci/integration-refs.json"): INTEGRATION_REFS,
        ("adk", ".github/workflows/tests.yml"):
            'env:\n  KLAYOUT_VERSION: "0.30.5"\n' + GATE_OK,
        ("interposer", ".github/workflows/tests.yml"):
            'env:\n  KLAYOUT_VERSION: "0.30.5"\n' + GATE_OK,
        ("docs", "docs/requirements.txt"): "sphinx==7.4.7\nklayout==0.30.5\n",
        ("plugin", "plugins/chiplet_export/requirements.txt"):
            "klayout>=0.30.5\nPyYAML>=6.0.3\n",
        ("plugin", "plugins/resizer_passive_elements/requirements.txt"):
            "PyYAML>=6.0.3\n",
        ("gds2kicad", "requirements.txt"): "PyQt6>=6.6.0\n",
        ("spec", "reference/python/pyproject.toml"):
            'requires-python = ">=3.8"\ndependencies = ["PyYAML>=5.1"]\n',
    }
    for lrid, lrel, rrid, rrel, _ in D.identical:
        files[(lrid, lrel)] = "same\n"
        files[(rrid, rrel)] = "same\n"
    # The version-judged artifacts start out at the state the ecosystem is
    # working towards: the reference and every copy carrying one reader release,
    # byte for byte. Every negative case below moves one of them off it.
    for art in version_policy_artifacts():
        current = art.version_policy.current
        if current == contract_registry.FROM_OWNER:
            current = art.version_policy.floor
        blob = _declaring_blob(art.version_policy.locator, current)
        files[(art.owner, art.path)] = blob
        for m in art.mirrors:
            files[(m.repo, m.path)] = blob
    for rid, rel in D.ci_gate_workflow.items():
        files.setdefault((rid, rel), GATE_OK)
    # conformance/requirements.txt is a declared pin file now that the spec
    # pin carries it, so the base tree has to include it or pip-constraints
    # reports it missing.
    files[("spec", "conformance/requirements.txt")] = "jsonschema>=4.18\nPyYAML>=5.1\n"
    return files


def self_test(load_yaml):
    cases = []

    def expect(name, contract, files, want_fail, substring=None):
        trees = DictTrees(files)

        def fetch(rid, rel):
            if files.get(("__api__", rid)) == "error":
                raise RuntimeError("looking it up returned HTTP 502")
            blob = trees.read(rid, rel)
            return None if blob is None else blob.decode()

        ctx = {"track": "dev", "load_yaml": load_yaml, "fetch": fetch}
        got = dict(run(trees, ctx, only={contract}))[contract]
        ok = bool(got) == want_fail
        if ok and substring:
            ok = any(substring in g for g in got)
        cases.append((name, ok, got))

    def mutate(**changes):
        files = _base_files()
        for key, value in changes.items():
            rid, rel = key.split("|", 1)
            if value is None:
                files.pop((rid, rel), None)
            else:
                files[(rid, rel)] = value
        return files

    base = _base_files()

    # identical-files
    expect("matching copies pass", "identical-files", base, False)
    expect("a drifted copy fails", "identical-files",
           mutate(**{"adk|config/schema/layers.schema.json": "different\n"}),
           True, "drifted apart")
    expect("a deleted copy fails rather than passing vacuously",
           "identical-files",
           mutate(**{"adk|config/schema/rule_params.schema.json": None}),
           True, "does not exist")

    # version-policy. The artifact behind every case below is the vendored
    # `.chiplet` reader, which is the only one judged this way today; the
    # fixtures are built from the registry rather than typed out, so a case
    # cannot keep passing after the row it is about has gone.
    cfio = {a.id: a for a in REGISTRY.artifacts}.get("cfio_python_reader")

    def reader(version=None, body="body = 1\n"):
        head = "" if version is None else '__version__ = "%s"\n' % version
        return '"""the .chiplet reader."""\n%s%s' % (head, body)

    def cfio_files(owner_blob, mirror_blob):
        files = _base_files()
        if owner_blob is None:
            files.pop((cfio.owner, cfio.path), None)
        else:
            files[(cfio.owner, cfio.path)] = owner_blob
        for m in cfio.mirrors:
            if mirror_blob is None:
                files.pop((m.repo, m.path), None)
            else:
                files[(m.repo, m.path)] = mirror_blob
        return files

    def cfio_result(owner_blob, mirror_blob):
        trees = DictTrees(cfio_files(owner_blob, mirror_blob))
        ctx = {"track": "dev", "load_yaml": load_yaml,
               "fetch": lambda rid, rel: None}
        return dict(run(trees, ctx, only={"version-policy"}))["version-policy"]

    def vpcase(name, ok, detail=None):
        cases.append((name, ok, detail))

    expect("copies carrying the reference's reader release pass",
           "version-policy", base, False)

    vpcase("the vendored .chiplet reader is judged by the version it declares",
           cfio is not None and cfio.identity == "version_policy"
           and cfio.version_policy is not None
           and cfio.version_policy.rule == "additive_minor"
           and cfio.version_policy.current == "1.1.0"
           and cfio.version_policy.floor == "1.1"
           and cfio.version_policy.locator == ("py_assign", "__version__")
           and len(cfio.mirrors) == 2,
           cfio and (cfio.identity, cfio.version_policy, len(cfio.mirrors)))

    # The state the ecosystem is actually in: the reference at the pinned commit
    # and both hosted copies are the reader that has no release constant at all.
    got = cfio_result(reader(None, body="reference = 1\n"),
                      reader(None, body="vendored = 1\n"))
    vpcase("when the reference and both copies declare no version the reader is "
           "red on three clauses",
           len(got) == 3 and all(g.startswith("cfio_python_reader:") for g in got)
           and any("owns the artifact" in g for g in got), got)

    got = cfio_result(reader("1.1.0"), reader(None))
    vpcase("publishing the reference alone does not turn it green; both copies "
           "have to be re-synced from it", len(got) == 2, got)

    got = cfio_result(reader("1.1.0"), reader("1.1.0"))
    vpcase("it goes green only once the reference and both copies carry one "
           "reader release, byte for byte", got == [], got)

    got = cfio_result(reader("1.1.0"), reader("1.1.0", body="body = 99\n"))
    vpcase("a copy edited in place under the same declared version stays red",
           len(got) == 2 and all("never different bytes" in g for g in got), got)

    got = cfio_result(reader("1.1.0"), reader("1.0.0"))
    vpcase("a copy below the declared floor is red, so 1.0 is not grandfathered",
           len(got) == 2 and all("below the floor" in g for g in got), got)

    got = cfio_result(reader("1.1.0"), reader("1.2.0"))
    vpcase("a copy ahead of the reference is red rather than tolerated",
           len(got) == 2 and all("ahead of the owner" in g for g in got), got)

    got = cfio_result(reader("1.1.0"), reader("2.0.0"))
    vpcase("a copy in another major is red", len(got) == 2
           and all("different majors" in g for g in got), got)

    got = cfio_result(reader("1.1.0"), None)
    vpcase("a deleted copy is red rather than passing vacuously",
           len(got) == 2 and all("not there" in g for g in got), got)

    got = cfio_result(None, reader("1.1.0"))
    vpcase("a missing reference is reported about the reference, and no copy is "
           "declared fine on the strength of it",
           len(got) == 3 and any("owns the artifact" in g for g in got)
           and sum("nothing is known about the copy" in g for g in got) == 2, got)

    got = cfio_result(reader("1.0.0"), reader("1.0.0"))
    vpcase("a reference that disagrees with the version this registry declares "
           "is red, because two declarations of one number move together",
           any("move in one commit" in g for g in got), got)

    # undeclared-twins
    expect("no undeclared twin passes", "undeclared-twins", base, False)
    expect("a new shared basename fails until declared", "undeclared-twins",
           mutate(**{"spec|schemas/stackup.schema.json": "a\n",
                     "adk|config/schema/stackup.schema.json": "a\n"}),
           True, "no pair declares")
    expect("two files with one name in one tree only pass",
           "undeclared-twins",
           mutate(**{"spec|schemas/stackup.schema.json": "a\n"}), False)

    # gitmodules-track
    expect("every submodule on the track passes", "gitmodules-track", base, False)
    expect("a submodule declaring the other track fails", "gitmodules-track",
           mutate(**{"tools|.gitmodules":
                     GITMODULES_DEV.replace("branch = dev", "branch = main", 1)}),
           True, "would follow the declaration")
    expect("a submodule declaring no branch fails", "gitmodules-track",
           mutate(**{"tools|.gitmodules":
                     GITMODULES_DEV.replace("\tbranch = dev\n", "", 1)}),
           True, "declares no branch")
    expect("a missing .gitmodules fails", "gitmodules-track",
           mutate(**{"tools|.gitmodules": None}), True, "pins nothing")
    expect("a non-submodule pin left on the other track fails",
           "gitmodules-track",
           mutate(**{"tools|ci/integration-refs.json": json.dumps({
               "spec": {"branch": "main", "pin": "c" * 40},
               "docs": {"branch": "dev", "pin": "d" * 40}})}),
           True, "while this superproject is on")
    expect("a non-submodule pin that is not a sha fails", "gitmodules-track",
           mutate(**{"tools|ci/integration-refs.json": json.dumps({
               "spec": {"branch": "dev", "pin": "dev"},
               "docs": {"branch": "dev", "pin": "d" * 40}})}),
           True, "not a 40-hex sha")
    expect("a missing integration-refs.json fails", "gitmodules-track",
           mutate(**{"tools|ci/integration-refs.json": None}),
           True, "are unpinned")
    expect("a repository in the registry that nothing pins fails",
           "gitmodules-track",
           mutate(**{"tools|.gitmodules": GITMODULES_DEV.split(
               '[submodule "tools/adk"]')[0]}),
           True, "nothing here pins it")
    expect("a pin with no row in the registry fails", "gitmodules-track",
           mutate(**{"tools|ci/integration-refs.json": json.dumps({
               "spec": {"branch": "dev", "pin": "c" * 40},
               "docs": {"branch": "dev", "pin": "d" * 40},
               "newthing": {"branch": "dev", "pin": "e" * 40}})}),
           True, "has no row in the contract registry")

    # ci-gate
    expect("a gate that compares its needs passes", "ci-gate", base, False)
    expect("an emptied needs fails", "ci-gate",
           mutate(**{"adk|.github/workflows/tests.yml": GATE_NO_NEEDS}),
           True, "green having run nothing")
    expect("a gate that never compares a result fails", "ci-gate",
           mutate(**{"adk|.github/workflows/tests.yml": GATE_UNCOMPARED}),
           True, "reports success anyway")
    expect("a repository with no gate workflow fails", "ci-gate",
           mutate(**{"kicad|.github/workflows/ci.yml": None}),
           True, "requires a context nothing will ever post")
    expect("a lookup that could not be answered is not reported as a missing "
           "gate", "ci-gate",
           mutate(**{"__api__|kicad": "error"}), True, "returned HTTP 502")

    # klayout
    expect("one KLayout everywhere passes", "klayout", base, False)
    expect("a consumer pinned to another version fails", "klayout",
           mutate(**{"docs|docs/requirements.txt": "klayout==0.30.9\n"}),
           True, "does not satisfy")
    expect("a floor above the image pin fails", "klayout",
           mutate(**{"plugin|plugins/chiplet_export/requirements.txt":
                     "klayout>=0.31.0\n"}),
           True, "does not satisfy")
    expect("a floor below the image pin passes", "klayout",
           mutate(**{"plugin|plugins/chiplet_export/requirements.txt":
                     "klayout>=0.29.0\n"}), False)
    expect("a declaration this check can no longer read fails", "klayout",
           mutate(**{"adk|.github/workflows/tests.yml":
                     "env:\n  KLAYOUT: 0.30.5\n" + GATE_OK}),
           True, "unreadable declaration")

    # pip-constraints
    expect("every pin satisfies every declared constraint", "pip-constraints",
           base, False)
    expect("a raised floor fails", "pip-constraints",
           mutate(**{"gds2kicad|requirements.txt": "PyQt6>=6.12.0\n"}),
           True, "exercised against a version it declares")
    expect("a constraint this checker cannot evaluate fails rather than "
           "passing", "pip-constraints",
           mutate(**{"gds2kicad|requirements.txt": "PyQt6 ~ 6.6\n"}),
           True, "cannot read the constraint")
    expect("an environment marker fails rather than being dropped",
           "pip-constraints",
           mutate(**{"gds2kicad|requirements.txt":
                     'PyQt6>=6.6.0 ; python_version < "3.13"\n'}),
           True, "marker or extra")
    expect("a missing requirements file fails", "pip-constraints",
           mutate(**{"gds2kicad|requirements.txt": None}),
           True, "silently shrinks this check")
    expect("a dependency file nobody registered fails", "pip-constraints",
           mutate(**{"adk|requirements-dev.txt": "pytest>=9.1.0\n"}),
           True, "neither reads it nor exempts it")

    # the version comparator itself
    def vcase(name, ok, detail=None):
        cases.append((name, ok, detail))

    vcase("0.30 equals 0.30.0", satisfies("0.30", "==", "0.30.0"))
    vcase("0.30.10 is above 0.30.9", satisfies("0.30.10", ">=", "0.30.9"))
    vcase("0.9 is below 0.10", satisfies("0.9", "<", "0.10"))
    vcase("~= holds the middle component",
          satisfies("6.0.5", "~=", "6.0.3") and not satisfies("6.1.0", "~=", "6.0.3"))
    try:
        satisfies("1.0", "==", "1.0rc1")
        vcase("a non-numeric bound is refused, not guessed at", False)
    except ValueError:
        vcase("a non-numeric bound is refused, not guessed at", True)

    # the registry itself: layout, re-encoding, and the tripwire
    fmt = contract_registry.fmt_check(contract_registry.REGISTRY_PATH)
    vcase("the registry is in canonical layout", not fmt, fmt)
    vcase("the interconnect method schema is owned by the PDK that authors it",
          {a.id: a.owner for a in REGISTRY.artifacts}.get(
              "interconnect_methods_schema") == "interconnect")
    tw = tripwire()
    vcase("nothing has grown a second copy of the registry", not tw, tw)
    vcase("the tripwire notices a hand-list growing back",
          any("IDENTICAL" in f for f in _tripwire_on(
              "IDENTICAL = [('spec', 'a', 'adk', 'b', 'why')]\n")))
    vcase("the tripwire notices a second comparator",
          any("satisfies" in f for f in _tripwire_on(
              "def satisfies(v, op, b):\n    return True\n")))
    vcase("the tripwire notices a second reader of the registry file",
          any("one reader" in f for f in _tripwire_on(
              "PATH = 'ci/%s'\n" % contract_registry.REGISTRY_FILENAME)))

    ok = True
    for name, passed, got in cases:
        print("%-62s %s" % (name, "ok" if passed else "FAILED"))
        if not passed:
            ok = False
            if got is not None:
                print("   contract returned: %r" % (got,))
    return 0 if ok else 1


def _tripwire_on(source):
    """Run the tripwire over a scratch copy of ci/ with one extra module.

    The negative case for a check about a directory has to be a directory, or
    the check is asserted against its own author's memory of it.
    """
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        ci = root / "ci"
        ci.mkdir()
        for path in sorted(HERE.glob("*.py")):
            shutil.copy2(path, ci / path.name)
        shutil.copy2(contract_registry.REGISTRY_PATH,
                     ci / contract_registry.REGISTRY_FILENAME)
        (ci / "scratch_module.py").write_text(source)
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        for name in ("integration.yml", "integration-floating.yml"):
            src = HERE.parent / ".github" / "workflows" / name
            if src.is_file():
                shutil.copy2(src, wf / name)
        return tripwire(ci_dir=ci, workflow_dir=wf)


def make_fetch(trees, targets, api):
    """Read a workflow file: from disk for this repository, from the branch for
    the others.

    This repository is read from the checkout because on a pull request the
    change under review is the head, not the branch; the other nine are read
    from the branch head because what is being asserted is that their live gate
    is a real gate.
    """
    def fetch(rid, rel):
        if rid == "tools":
            blob = trees.read("tools", rel)
            return None if blob is None else blob.decode()
        target = targets.get(rid)
        if target is None:
            raise RuntimeError("no slug and branch are declared for it, so its "
                               "gate cannot be looked up")
        slug, branch = target
        if not branch:
            raise RuntimeError("%s declares no branch to look the gate up on" % slug)
        status, body = api("/repos/%s/contents/%s?ref=%s" % (slug, rel, branch))
        if status == 404:
            return None
        if status != 200 or not body:
            raise RuntimeError("looking up %s on %s@%s returned HTTP %d"
                               % (rel, slug, branch, status))
        return base64.b64decode(body.get("content", "")).decode()
    return fetch


def load_yaml_or_die():
    try:
        import yaml
    except ImportError:
        print("FAIL: PyYAML is not installed. The ci-gate contract reads "
              "workflow files, and guessing at their structure with a regular "
              "expression is how a gate check stops noticing a restructured "
              "workflow.", file=sys.stderr)
        raise SystemExit(1)
    return yaml.safe_load


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", help="directory holding one checkout per repository")
    ap.add_argument("--track", default="dev",
                    help="the branch this superproject is on; .gitmodules must "
                         "declare it (default: dev)")
    ap.add_argument("--dir", action="append", default=[], metavar="ID=PATH",
                    help="override the directory for one repository id")
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="run only the named contract; repeatable")
    ap.add_argument("--json", action="store_true",
                    help="emit the result as JSON for the floating run's issue body")
    ap.add_argument("--table", action="store_true",
                    help="print the registry, one line per artifact, and stop")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test(load_yaml_or_die())

    if args.table:
        for art in REGISTRY.artifacts:
            version = art.version_policy.current if art.version_policy else "-"
            print("%-30s [%s/%s] %s:%s v%s  mirrors=%d"
                  % (art.id, art.kind, art.identity, art.owner, art.path,
                     version, len(art.mirrors)))
        for note in REGISTRY.notes:
            print("NOTE %s" % note)
        return 0

    if not args.root:
        ap.error("--root is required unless --self-test is given")

    overrides = {}
    for item in args.dir:
        if "=" not in item:
            ap.error("--dir wants ID=PATH, got %r" % item)
        rid, path = item.split("=", 1)
        if rid not in D.layout:
            ap.error("unknown repository id %r; known ids are %s"
                     % (rid, ", ".join(sorted(D.layout))))
        overrides[rid] = path

    trees = Trees(args.root, overrides)
    missing = trees.missing()
    if missing:
        print("FAIL: not checked out: %s. Every contract spans two trees, so a "
              "missing one turns assertions into silence."
              % ", ".join(missing), file=sys.stderr)
        return 1

    only = set(args.only) or None
    if only:
        unknown = only - {n for n, _ in CONTRACTS}
        if unknown:
            ap.error("unknown contract(s): %s" % ", ".join(sorted(unknown)))

    extra = json.loads((trees.paths["tools"] / "ci/integration-refs.json").read_text())
    targets = {}
    for rid, slug, branch, _ in resolve_refs.declared(
            (trees.paths["tools"] / ".gitmodules").read_text(), extra):
        if rid:
            targets[rid] = (slug, branch)

    ctx = {
        "track": args.track,
        "load_yaml": load_yaml_or_die(),
        "fetch": make_fetch(trees, targets,
                            resolve_refs.make_api(resolve_refs.token())),
    }
    try:
        results = run(trees, ctx, only)
    except resolve_refs.Unreachable as e:
        print("NO VERDICT: could not reach the GitHub API (%s). The contracts "
              "were not evaluated; this says nothing about them." % e,
              file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([{"contract": n, "failures": f} for n, f in results],
                         indent=2))
    else:
        for name, failures in results:
            print("%-20s %s" % (name, "ok" if not failures else
                                "%d FAILED" % len(failures)))
        for name, failures in results:
            for f in failures:
                print("\nFAIL [%s] %s" % (name, f), file=sys.stderr)

    return 1 if any(f for _, f in results) else 0


if __name__ == "__main__":
    sys.exit(main())
