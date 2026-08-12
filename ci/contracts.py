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

The contracts, and what each one is for:

  identical-files       declared byte-identity pairs. A hand-synced copy is a
                        copy that drifts; the only question is when. The
                        vendored `.chiplet` readers are here too, so an in-place
                        edit to a vendored file fails rather than becoming a
                        third dialect of the format.
  undeclared-twins      a basename living in two trees without a declared pair.
                        Without this, the check above covers exactly the files
                        somebody remembered to list, and a new shared schema
                        escapes on the day it is added.
  gitmodules-track      every pin, in `.gitmodules` and in
                        `ci/integration-refs.json`, names the branch this
                        superproject is on. `git submodule update --remote`
                        follows that declaration, and the plugin's `main` and
                        `dev` have no common ancestor, so a stale declaration
                        moves a pin onto a disjoint history.
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

Usage:
    contracts.py --root DIR [--track dev]
    contracts.py --root DIR --dir kicad=/elsewhere/kicad
    contracts.py --self-test

`--root` holds one checkout per repository, named as in the table below; the
integration workflow lays them out that way and this umbrella working directory
already is that layout. A repository that is missing is a failure and never a
skip: a contract that quietly covers nine repositories reads exactly like one
that covers ten.
"""

import argparse
import base64
import json
import os
import pathlib
import re
import sys

import resolve_refs

# Logical id -> directory name under --root. The ids are what the failure
# messages name, so they are short and stable; the directory names are what
# both this working directory and the workflow use.
LAYOUT = {
    "adk": "adk",
    "spec": "chiplet-spec",
    "interposer": "interposer",
    "interconnect": "interconnect_pdk",
    "plugin": "chiplet_kicad_plugin",
    "gds2kicad": "gds_to_kicad",
    "studio": "chiplet-studio",
    "docs": "adk-docs",
    "kicad": "kicad",
    "tools": "adk-tools",
}

# Byte-identity pairs. Each entry is (left repo, left path, right repo, right
# path, why it matters if they differ).
IDENTICAL = [
    ("spec", "schemas/boundary_manifest.schema.json",
     "adk", "config/schema/boundary_manifest.schema.json",
     "the assembly-DRC boundary manifest is the PDK-agnostic contract between "
     "the exporters and the deck"),
    ("spec", "schemas/interconnect.schema.json",
     "adk", "config/schema/interconnect.schema.json",
     "the interconnect block of a .chiplet document"),
    ("spec", "schemas/layers.schema.json",
     "adk", "config/schema/layers.schema.json",
     "the layer registry every consumer validates against"),
    ("spec", "schemas/rule_params.schema.json",
     "adk", "config/schema/rule_params.schema.json",
     "the JSON-parameterised DRC rule values"),
    ("spec", "schemas/chiplet_pads.json",
     "adk", "config/chiplet_pads.json",
     "the black-box chiplet pad vocabulary. adk has a strict-version gate that "
     "forces its copy forward, so the spec copy goes stale by construction "
     "unless something outside both repositories says otherwise"),
    ("spec", "schemas/interconnect_methods.schema.json",
     "interconnect", "manifest/schema/interconnect_methods.schema.json",
     "the interconnect method registry. The PDK owns the file and the spec "
     "publishes it"),
    ("spec", "reference/python/chiplet_format_io/__init__.py",
     "adk", "vendor/chiplet_format_io/__init__.py",
     "the vendored Python .chiplet reader. adk's own suite cross-checks this "
     "when a chiplet-spec sibling happens to be discoverable, which on a bare "
     "runner it never is"),
]

# Directories scanned for a basename that appears in two trees without a
# declared pair above. Deliberately not recursive and deliberately short: this
# is the guard on the list, not a second contract.
TWIN_SCAN = [
    ("spec", "schemas"),
    ("adk", "config/schema"),
    ("adk", "config"),
    ("interconnect", "manifest/schema"),
]

# Basenames that legitimately exist in two of the scanned directories and are
# not copies of each other. Each needs a reason, because the alternative to a
# reason is a list that grows every time the check is inconvenient.
TWIN_EXEMPT = {
    # adk's `ixn_methods.schema.json` and the spec's `interconnect_methods`
    # one validate different documents, and their basenames differ, so neither
    # is here. Empty on purpose; an entry is a claim that two files with one
    # name are two different things.
}

# The repositories that have to be on disk. chiplet-studio and the KiCad fork
# are absent on purpose: nothing here reads a file of theirs, only their gate
# workflow, which is fetched from the branch, so neither a 349 MB nor a 2.1 GB
# checkout has to happen for a check that reads one file.
FILE_REPOS = ("adk", "spec", "interposer", "interconnect", "plugin",
              "gds2kicad", "docs", "tools")

# The workflow file that publishes `ci-gate`, per repository. Named rather than
# globbed: a repository that moves its gate into a second workflow should have
# to say so here, since two workflows publishing one context is how a gate ends
# up reporting a job nobody meant to be gating.
CI_GATE_WORKFLOW = {
    "adk": ".github/workflows/tests.yml",
    "spec": ".github/workflows/tests.yml",
    "interposer": ".github/workflows/tests.yml",
    "interconnect": ".github/workflows/tests.yml",
    "plugin": ".github/workflows/tests.yml",
    "gds2kicad": ".github/workflows/tests.yml",
    "studio": ".github/workflows/ci.yml",
    "docs": ".github/workflows/docs.yml",
    "kicad": ".github/workflows/ci.yml",
    "tools": ".github/workflows/ci.yml",
}

# KLayout, sourced from `ARG KLAYOUT_PIP` in this repository's Dockerfile. Each
# consumer says where its copy lives and how to read it.
KLAYOUT_CONSUMERS = [
    ("adk", ".github/workflows/tests.yml", r'KLAYOUT_VERSION:\s*"([0-9.]+)"', "=="),
    ("interposer", ".github/workflows/tests.yml", r'KLAYOUT_VERSION:\s*"([0-9.]+)"', "=="),
    ("docs", "docs/requirements.txt", r'(?mi)^klayout==([0-9.]+)\s*$', "=="),
    ("plugin", "plugins/chiplet_export/requirements.txt",
     r'(?mi)^klayout>=([0-9.]+)\s*$', ">="),
]

# Packages the image pins, by the ARG that pins them. The key is the normalised
# distribution name as it appears in a requirements file.
PIP_ARGS = {
    "klayout": "KLAYOUT_PIP",
    "pyyaml": "PYYAML_PIP",
    "pyqt6": "PYQT6_PIP",
    "jinja2": "JINJA2_PIP",
    "jsonschema": "JSONSCHEMA_PIP",
    "psutil": "PSUTIL_PIP",
    "pytest": "PYTEST_PIP",
}

# Where any repository declares a constraint on a Python package.
REQUIREMENT_FILES = [
    ("plugin", "plugins/chiplet_export/requirements.txt"),
    ("plugin", "plugins/resizer_passive_elements/requirements.txt"),
    ("gds2kicad", "requirements.txt"),
    ("docs", "docs/requirements.txt"),
    ("spec", "reference/python/pyproject.toml"),
]


class Trees:
    """Read-only access to a set of checkouts, by logical id."""

    def __init__(self, root, overrides=None):
        self.root = pathlib.Path(root)
        self.paths = {}
        for rid, name in LAYOUT.items():
            self.paths[rid] = self.root / name
        for rid, path in (overrides or {}).items():
            self.paths[rid] = pathlib.Path(path)

    def missing(self):
        return [r for r in sorted(FILE_REPOS) if not self.paths[r].is_dir()]

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


# --------------------------------------------------------------------------
# version handling
#
# `packaging` is not in the standard library and this runs before anything is
# installed, so comparison is done here. Anything not understood is reported as
# a failure rather than passed over: an unparsed constraint that reads as
# satisfied is the same defect as no check at all, arriving with a green tick.
# --------------------------------------------------------------------------

OPS = ("===", "==", ">=", "<=", "!=", "~=", ">", "<")


def parse_version(text):
    parts = text.strip().split(".")
    out = []
    for p in parts:
        if not p.isdigit():
            raise ValueError("version %r has a non-numeric component %r" % (text, p))
        out.append(int(p))
    return tuple(out)


def compare(a, b):
    """-1, 0 or 1, zero-padding the shorter side so 0.30 == 0.30.0."""
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def satisfies(version, op, bound):
    v, b = parse_version(version), parse_version(bound)
    c = compare(v, b)
    if op in ("==", "==="):
        return c == 0
    if op == "!=":
        return c != 0
    if op == ">=":
        return c >= 0
    if op == "<=":
        return c <= 0
    if op == ">":
        return c > 0
    if op == "<":
        return c < 0
    if op == "~=":
        # `~= X.Y.Z` is `>= X.Y.Z, == X.Y.*`.
        if len(b) < 2:
            raise ValueError("~= needs at least two components, got %r" % bound)
        return c >= 0 and compare(v[: len(b) - 1], b[: len(b) - 1]) == 0
    raise ValueError("unsupported operator %r" % op)


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
        for op in OPS:
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
    for lrid, lrel, rrid, rrel, why in IDENTICAL:
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


def contract_undeclared_twins(trees):
    declared = set()
    for lrid, lrel, rrid, rrel, _ in IDENTICAL:
        declared.add(os.path.basename(lrel))
        declared.add(os.path.basename(rrel))

    seen = {}
    for rid, rel in TWIN_SCAN:
        for name in trees.listdir(rid, rel):
            seen.setdefault(name, []).append("%s:%s/%s" % (rid, rel, name))

    out = []
    for name, where in sorted(seen.items()):
        if len(where) < 2 or name in declared or name in TWIN_EXEMPT:
            continue
        out.append("%s exists in more than one tree (%s) and no pair declares "
                   "the relationship. Either they are copies, in which case add "
                   "the pair, or they are different documents that happen to "
                   "share a name, in which case say so in TWIN_EXEMPT."
                   % (name, ", ".join(where)))
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
    return out


def contract_ci_gate(fetch, load_yaml):
    out = []
    for rid, rel in sorted(CI_GATE_WORKFLOW.items()):
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
    m = re.search(r"(?m)^ARG KLAYOUT_PIP=([0-9.]+)\s*$", blob.decode())
    if not m:
        return ["adk-tools Dockerfile has no `ARG KLAYOUT_PIP=`, which is where "
                "the ecosystem's KLayout version is declared"]
    pin = m.group(1)

    out = []
    for rid, rel, pattern, op in KLAYOUT_CONSUMERS:
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


def contract_pip_constraints(trees):
    blob = trees.read("tools", "Dockerfile")
    if blob is None:
        return ["adk-tools has no Dockerfile, so nothing declares the pins"]
    text = blob.decode()

    pins = {}
    for pkg, arg in PIP_ARGS.items():
        m = re.search(r"(?m)^ARG %s=([0-9A-Za-z.\-]+)\s*$" % re.escape(arg), text)
        if not m:
            return ["adk-tools Dockerfile has no `ARG %s=`, so the pin for %s "
                    "cannot be checked against what the repositories ask for"
                    % (arg, pkg)]
        pins[pkg] = m.group(1)

    out = []
    for rid, rel in REQUIREMENT_FILES:
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
    return out


CONTRACTS = [
    ("identical-files", lambda t, ctx: contract_identical_files(t)),
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

GITMODULES_DEV = """
[submodule "tools/adk"]
\tpath = tools/adk
\turl = https://github.com/IHP-GmbH/IHP-Open-ADK.git
\tbranch = dev
[submodule "tools/kicad"]
\tpath = tools/kicad
\turl = https://github.com/IHP-GmbH/KiCad-ADK-MOD.git
\tbranch = dev
"""


def _base_files():
    files = {
        ("tools", "Dockerfile"): DOCKERFILE,
        ("tools", ".gitmodules"): GITMODULES_DEV,
        ("tools", "ci/integration-refs.json"): json.dumps({
            "_comment": ["ignored"],
            "spec": {"slug": "IHP-GmbH/chiplet-spec", "branch": "dev",
                     "pin": "c" * 40},
        }),
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
    for lrid, lrel, rrid, rrel, _ in IDENTICAL:
        files[(lrid, lrel)] = "same\n"
        files[(rrid, rrel)] = "same\n"
    for rid, rel in CI_GATE_WORKFLOW.items():
        files.setdefault((rid, rel), GATE_OK)
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
           mutate(**{"adk|vendor/chiplet_format_io/__init__.py": None}),
           True, "does not exist")

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
               "spec": {"branch": "main", "pin": "c" * 40}})}),
           True, "while this superproject is on")
    expect("a non-submodule pin that is not a sha fails", "gitmodules-track",
           mutate(**{"tools|ci/integration-refs.json": json.dumps({
               "spec": {"branch": "dev", "pin": "dev"}})}),
           True, "not a 40-hex sha")
    expect("a missing integration-refs.json fails", "gitmodules-track",
           mutate(**{"tools|ci/integration-refs.json": None}),
           True, "are unpinned")

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

    # the version comparator itself
    def vcase(name, ok):
        cases.append((name, ok, None))

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

    ok = True
    for name, passed, got in cases:
        print("%-62s %s" % (name, "ok" if passed else "FAILED"))
        if not passed:
            ok = False
            if got is not None:
                print("   contract returned: %r" % (got,))
    return 0 if ok else 1


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
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test(load_yaml_or_die())

    if not args.root:
        ap.error("--root is required unless --self-test is given")

    overrides = {}
    for item in args.dir:
        if "=" not in item:
            ap.error("--dir wants ID=PATH, got %r" % item)
        rid, path = item.split("=", 1)
        if rid not in LAYOUT:
            ap.error("unknown repository id %r; known ids are %s"
                     % (rid, ", ".join(sorted(LAYOUT))))
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
