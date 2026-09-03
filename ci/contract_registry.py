#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The one reader of the cross-repository contract registry.

`ci/contract-registry.json` is data, not code. It is data because the
producer-side conformance step fetches it from this repository into four other
repositories' required gates, and a required gate that `exec`s a file it pulled
over the network is a supply-chain smell dressed up as a checker. A `.json` is
parsed by the standard library on a bare runner and nothing in it can run.

The discipline that a Python literal used to carry, then, has to live here
instead:

  * every closed vocabulary is a frozenset in this file, so an unknown literal in
    the data is a `RegistryError` rather than a row that silently means nothing;
  * every row is a fixed-arity array, so a row with a field missing is caught at
    load and not at the moment somebody indexes past the end;
  * every row carries a non-empty `why`, which is the comment the literal used to
    have and which a JSON file would otherwise lose;
  * the layout is canonical (`--fmt`, `--fmt-check`), so a reflow cannot turn a
    one-line change into an unreviewable diff, and `contracts.py --self-test`
    checks the layout before any contract runs.

The reader returns frozen namedtuples. Validation collects every clause it can
find and raises once: a registry with four defects should report four, because
fixing them one per run is how a data file stops being edited at all.

Usage:
    contract_registry.py --self-test
    contract_registry.py --fmt        [FILE]
    contract_registry.py --fmt-check  [FILE]
    contract_registry.py --table      [FILE]
"""

import argparse
import collections
import json
import os
import pathlib
import re
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent

#: The registry file name. It appears exactly once in one `.py` under `ci/`,
#: which `contracts.py --self-test` asserts: two readers of one data file is how
#: the second one goes stale.
REGISTRY_FILENAME = "contract-registry.json"
REGISTRY_PATH = HERE / REGISTRY_FILENAME

# --------------------------------------------------------------------------
# closed vocabularies
#
# Anything not in these sets is a failure to load, never a row that is quietly
# ignored. The whole point of moving the hand-lists into data is lost the moment
# the data can say something the reader does not understand.
# --------------------------------------------------------------------------

KINDS = frozenset({
    "schema", "vocabulary", "data_registry", "reader_py", "reader_cpp",
    "tool_pin",
})

#: `bytes`: the copies must be equal byte for byte. `version_policy`: the copies
#: are judged by the version they declare, under `RULES`.
IDENTITY_MODES = frozenset({"bytes", "version_policy"})

RULES = frozenset({"additive_minor", "satisfies"})

LOCATORS = frozenset({"json_pointer", "py_assign", "dockerfile_arg", "regex"})

#: There is no `none`. A repository that is in no checkout is a repository this
#: file must not name: naming one would put a private, unpublished repository
#: into a public file. Confidentiality is a reader invariant here, not a habit.
CHECKOUTS = frozenset({"tree", "api"})

#: Comparison operators, in longest-first order so `>=` is never read as `>`.
#: Moved here from contracts.py with the comparator itself.
OPS = ("===", "==", ">=", "<=", "!=", "~=", ">", "<")

#: Kinds whose directories are scanned for an undeclared twin. A reader is not
#: here: two readers with one basename in two trees is the normal vendored
#: shape, and the declared mirror covers it.
TWIN_KINDS = frozenset({"schema", "vocabulary", "data_registry"})

#: The spelling that means "read the number out of the owner's file at run time
#: rather than declaring it twice". Kept for a tool pin, where the image is the
#: only declaration there is, and for an artifact whose owner releases on its own
#: cadence, where a second declaration here would make somebody else's release
#: red until this repository is edited in the same hour.
FROM_OWNER = "@owner"

ARTIFACT_KEYS = ("id", "kind", "owner", "path", "identity", "version_policy",
                 "mirrors", "producers", "consumers", "declarations",
                 "packages", "why")
VERSION_POLICY_KEYS = ("rule", "current", "floor", "locator")
TOP_KEYS = ("registry_version", "repos", "artifacts", "twin_exempt",
            "pins_exempt")

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Mirror and owner states. Green is exactly {IDENTICAL, VERSION_OK,
#: AHEAD_OF_PIN}; there is deliberately no state that means "the bytes differ
#: and that is fine".
IDENTICAL = "IDENTICAL"
VERSION_OK = "VERSION_OK"
AHEAD_OF_PIN = "AHEAD_OF_PIN"
DRIFTED = "DRIFTED"
UNREADABLE = "UNREADABLE"
MISSING = "MISSING"
STATES = (IDENTICAL, VERSION_OK, AHEAD_OF_PIN, DRIFTED, UNREADABLE, MISSING)
GREEN_STATES = frozenset({IDENTICAL, VERSION_OK, AHEAD_OF_PIN})


class RegistryError(Exception):
    """The registry cannot be trusted, so nothing was judged with it.

    Carries every clause it found, because a data file whose defects arrive one
    per run is a data file nobody finishes fixing.
    """

    def __init__(self, clauses):
        self.clauses = tuple(clauses)
        Exception.__init__(self, "; ".join(self.clauses))


Repo = collections.namedtuple("Repo", "id dir checkout gate_workflow why")
VersionPolicy = collections.namedtuple("VersionPolicy",
                                       "rule current floor locator")
Mirror = collections.namedtuple("Mirror", "repo path why")
Ref = collections.namedtuple("Ref", "repo path why")
Declaration = collections.namedtuple("Declaration", "repo path locator op why")
Artifact = collections.namedtuple(
    "Artifact",
    "id kind owner path identity version_policy mirrors producers consumers "
    "declarations packages why")
Exempt = collections.namedtuple("Exempt", "key why")
PinExempt = collections.namedtuple("PinExempt", "repo path why")
Registry = collections.namedtuple(
    "Registry", "registry_version repos artifacts twin_exempt pins_exempt notes")

Derived = collections.namedtuple(
    "Derived",
    "identical twin_scan twin_exempt layout file_repos ci_gate_workflow "
    "klayout_arg klayout_consumers pip_args requirement_files pins_exempt")


# --------------------------------------------------------------------------
# version handling
#
# Moved here verbatim from contracts.py, with its self-tests. `packaging` is not
# in the standard library and this runs before anything is installed, so
# comparison is done here. Anything not understood is reported as a failure
# rather than passed over: an unparsed constraint that reads as satisfied is the
# same defect as no check at all, arriving with a green tick.
# --------------------------------------------------------------------------


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


def major_minor(text):
    """(MAJOR, MINOR) out of `MAJOR.MINOR` or `MAJOR.MINOR.PATCH`, or None.

    PATCH is ignored throughout, which is the version policy the artifacts are
    governed by; it is not this file inventing a convenience.
    """
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if len(parts) not in (2, 3):
        return None
    try:
        major, minor = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if major < 0 or minor < 0:
        return None
    if len(parts) == 3 and not parts[2].isdigit():
        return None
    return (major, minor)


def additive_minor_verdict(value, supported):
    """"accept" | "warn" | "refuse", the additive-minor policy as a table.

    Deliberately a table and not a call into `chiplet_format_io`: the gate must
    not depend on the artifact it judges, or a reader that broke its own policy
    would judge itself compliant. Parity with
    `cfio.check_contract_version` is asserted by the truth table in the
    self-test, which is the thing that has to be kept honest.
    """
    sup = major_minor(supported)
    if sup is None:
        raise ValueError("supported version %r is not MAJOR.MINOR" % (supported,))
    got = major_minor(value)
    if got is None:
        return "refuse"
    if got[0] != sup[0]:
        return "refuse"
    return "warn" if got[1] > sup[1] else "accept"


# --------------------------------------------------------------------------
# reading a version out of bytes, without importing anything
# --------------------------------------------------------------------------


def read_version(locator, blob):
    """The version a blob declares, as a string, or None.

    Never imports the file it reads. A gate that imports the vendored module it
    is judging has already run the code it was supposed to be suspicious of.
    """
    if blob is None or locator is None:
        return None
    kind, arg = locator[0], locator[1]
    if kind == "json_pointer":
        try:
            doc = json.loads(blob.decode() if isinstance(blob, bytes) else blob)
        except (ValueError, UnicodeDecodeError):
            return None
        node = doc
        for token in [t for t in arg.split("/") if t]:
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or token not in node:
                return None
            node = node[token]
        return node if isinstance(node, str) else None
    try:
        text = blob.decode() if isinstance(blob, bytes) else blob
    except UnicodeDecodeError:
        return None
    if kind == "py_assign":
        m = re.search(r"(?m)^%s\s*=\s*[\"']([^\"']+)[\"']" % re.escape(arg), text)
    elif kind == "dockerfile_arg":
        m = re.search(r"(?m)^ARG %s=([0-9A-Za-z.\-]+)\s*$" % re.escape(arg), text)
    elif kind == "regex":
        m = re.search(arg, text)
    else:
        raise ValueError("unknown locator kind %r" % (kind,))
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# load and validate
# --------------------------------------------------------------------------


def _parse(blob):
    if isinstance(blob, (dict, list)):
        return blob
    if isinstance(blob, bytes):
        blob = blob.decode()
    return json.loads(blob)


def _row(bad, where, value, arity):
    """True if `value` is a list of exactly `arity` strings-or-nulls."""
    if not isinstance(value, list) or len(value) != arity:
        bad.append("R9 %s is not a %d-element row: %r" % (where, arity, value))
        return False
    return True


def _why(bad, where, value):
    if not isinstance(value, str) or not value.strip():
        bad.append("R7 %s has an empty `why`. The reason a row exists is the "
                   "only thing that stops the row from being deleted by "
                   "somebody who cannot see why it is there." % where)


def load_registry(blob, declared_ids=None):
    """Registry, or RegistryError carrying every clause that failed.

    `declared_ids` is the set of repository ids the superproject actually pins
    (`resolve_refs.declared()` plus this repository). When it is given, the
    repos table is required to be exactly that set: a row for a repository
    nothing pins would be a name in a public file with no pinned tree behind it,
    which is how a private repository ends up mentioned here.
    """
    bad = []
    notes = []
    try:
        data = _parse(blob)
    except (ValueError, UnicodeDecodeError) as e:
        raise RegistryError(["the registry does not parse as JSON (%s)" % e])
    if not isinstance(data, dict):
        raise RegistryError(["the registry is not a JSON object"])

    unknown = [k for k in data if k not in TOP_KEYS]
    if unknown:
        bad.append("unknown top-level key(s) %s" % ", ".join(sorted(unknown)))
    for k in TOP_KEYS:
        if k not in data:
            bad.append("the registry has no %r section" % k)
    if bad:
        raise RegistryError(bad)

    # ---- repos -----------------------------------------------------------
    repos = []
    seen_repo = set()
    for i, row in enumerate(data["repos"]):
        where = "repos[%d]" % i
        if not _row(bad, where, row, 5):
            continue
        rid, rdir, checkout, gate, why = row
        if not isinstance(rid, str) or not ID_RE.match(rid or ""):
            bad.append("R1 %s has the id %r, which is not [a-z][a-z0-9_]*"
                       % (where, rid))
        if rid in seen_repo:
            bad.append("R1 the repository id %r is declared twice" % (rid,))
        seen_repo.add(rid)
        if checkout not in CHECKOUTS:
            bad.append("R12 %s declares the checkout %r; the only ones that "
                       "exist are %s. There is no row for a repository that is "
                       "never checked out."
                       % (where, checkout, ", ".join(sorted(CHECKOUTS))))
        if not isinstance(rdir, str) or not rdir:
            bad.append("R9 %s has no directory name" % where)
        if not isinstance(gate, str) or not gate:
            bad.append("R9 %s names no ci-gate workflow" % where)
        _why(bad, where, why)
        repos.append(Repo(rid, rdir, checkout, gate, why))

    repo_ids = {r.id for r in repos}
    repo_by_id = {r.id: r for r in repos}

    if declared_ids is not None:
        declared_ids = set(declared_ids)
        for rid in sorted(repo_ids - declared_ids):
            bad.append("R13 the repos table names %r, which this superproject "
                       "does not pin. Every row here has to be a repository the "
                       "integration run actually checks out." % rid)
        for rid in sorted(declared_ids - repo_ids):
            bad.append("R13 %r is pinned by this superproject and has no row in "
                       "the repos table, so no contract can name it" % rid)

    # ---- artifacts -------------------------------------------------------
    artifacts = []
    seen_ids = set()
    owned = {}                       # (repo, path) -> artifact id
    for i, rec in enumerate(data["artifacts"]):
        where = "artifacts[%d]" % i
        if not isinstance(rec, dict):
            bad.append("R9 %s is not a JSON object" % where)
            continue
        extra = [k for k in rec if k not in ARTIFACT_KEYS]
        if extra:
            bad.append("%s has unknown key(s) %s" % (where, ", ".join(sorted(extra))))
        for k in ARTIFACT_KEYS:
            if k not in rec:
                bad.append("%s has no %r key; every key is present or the "
                           "record is not one" % (where, k))
        if extra or any(k not in rec for k in ARTIFACT_KEYS):
            continue

        aid = rec["id"]
        where = "artifact %r" % aid
        if not isinstance(aid, str) or not ID_RE.match(aid or ""):
            bad.append("R1 %s has an id that is not [a-z][a-z0-9_]*" % (where,))
        if aid in seen_ids:
            bad.append("R1 the artifact id %r is declared twice" % (aid,))
        seen_ids.add(aid)

        kind = rec["kind"]
        if kind not in KINDS:
            bad.append("R1 %s has the kind %r; the kinds that exist are %s"
                       % (where, kind, ", ".join(sorted(KINDS))))
        identity = rec["identity"]
        if identity not in IDENTITY_MODES:
            bad.append("R5 %s has the identity mode %r; the modes that exist "
                       "are %s" % (where, identity,
                                   ", ".join(sorted(IDENTITY_MODES))))

        owner = rec["owner"]
        if owner not in repo_ids:
            bad.append("R2 %s is owned by %r, which has no row in the repos "
                       "table" % (where, owner))
        elif repo_by_id[owner].checkout not in CHECKOUTS:
            bad.append("R3 %s is owned by %r, which is never checked out"
                       % (where, owner))
        path = rec["path"]
        if not isinstance(path, str) or not path:
            bad.append("R9 %s has no path" % where)
        _why(bad, where, rec["why"])

        # R4 is about a file being claimed by two identity relationships. A tool
        # pin is exempt on the owner side and only there: the image Dockerfile
        # legitimately declares several pins, and each one is a separate
        # artifact with its own consumers.
        if kind != "tool_pin":
            key = (owner, path)
            if key in owned:
                bad.append("R4 %s:%s is claimed by both %r and %r"
                           % (owner, path, owned[key], aid))
            owned[key] = aid

        # ---- mirrors / producers / consumers -----------------------------
        mirrors = []
        for j, row in enumerate(rec["mirrors"]):
            w = "%s mirror[%d]" % (where, j)
            if not _row(bad, w, row, 3):
                continue
            mrid, mpath, mwhy = row
            if mrid not in repo_ids:
                bad.append("R2 %s names the repository %r, which has no row in "
                           "the repos table" % (w, mrid))
            if mrid == owner:
                bad.append("R4 %s mirrors into its own owner %r; a mirror is a "
                           "copy in another tree" % (where, owner))
            mkey = (mrid, mpath)
            if mkey in owned:
                bad.append("R4 %s:%s appears twice across owners and mirrors "
                           "(%r and %r)" % (mrid, mpath, owned[mkey], aid))
            owned[mkey] = aid
            _why(bad, w, mwhy)
            mirrors.append(Mirror(mrid, mpath, mwhy))

        refs = {}
        for field in ("producers", "consumers"):
            rows = []
            for j, row in enumerate(rec[field]):
                w = "%s %s[%d]" % (where, field, j)
                if not _row(bad, w, row, 3):
                    continue
                rrid, rpath, rwhy = row
                if rrid not in repo_ids:
                    bad.append("R2 %s names the repository %r, which has no row "
                               "in the repos table" % (w, rrid))
                _why(bad, w, rwhy)
                rows.append(Ref(rrid, rpath, rwhy))
            refs[field] = tuple(rows)

        # ---- declarations -------------------------------------------------
        declarations = []
        for j, row in enumerate(rec["declarations"]):
            w = "%s declaration[%d]" % (where, j)
            if not _row(bad, w, row, 5):
                continue
            drid, dpath, dloc, dop, dwhy = row
            if drid not in repo_ids:
                bad.append("R2 %s names the repository %r, which has no row in "
                           "the repos table" % (w, drid))
            if dloc is not None:
                if not _row(bad, "%s locator" % w, dloc, 2):
                    continue
                if dloc[0] not in LOCATORS:
                    bad.append("R9 %s uses the locator kind %r; the kinds that "
                               "exist are %s"
                               % (w, dloc[0], ", ".join(sorted(LOCATORS))))
                dloc = tuple(dloc)
            if dop is not None and dop not in OPS:
                bad.append("R9 %s uses the operator %r, which is not one this "
                           "checker evaluates" % (w, dop))
            if (dloc is None) != (dop is None):
                bad.append("R9 %s gives a locator without an operator or the "
                           "other way round; a declaration is either read at a "
                           "point with an operator, or the whole file is "
                           "scanned and it has neither" % w)
            _why(bad, w, dwhy)
            declarations.append(Declaration(drid, dpath, dloc, dop, dwhy))

        packages = rec["packages"]
        if not isinstance(packages, dict):
            bad.append("R9 %s has a `packages` that is not an object" % where)
            packages = {}
        for name, arg in packages.items():
            if not isinstance(name, str) or not isinstance(arg, str) or not arg:
                bad.append("R9 %s maps the package %r to %r, which is not an "
                           "ARG name" % (where, name, arg))

        # ---- R8: declarations and packages belong to a tool pin -----------
        if kind == "tool_pin":
            if not declarations:
                bad.append("R8 %s is a tool_pin and declares no consumer. A pin "
                           "nothing is compared against is a number in a file."
                           % where)
        else:
            if declarations:
                bad.append("R8 %s is not a tool_pin but carries declarations"
                           % where)
            if packages:
                bad.append("R8 %s is not a tool_pin but carries packages" % where)

        # ---- R5/R6: identity, rule, locator, floor ------------------------
        vp = rec["version_policy"]
        policy = None
        if vp is not None:
            if not isinstance(vp, dict) or set(vp) != set(VERSION_POLICY_KEYS):
                bad.append("R9 %s has a version_policy whose keys are not "
                           "exactly %s" % (where, ", ".join(VERSION_POLICY_KEYS)))
            else:
                rule, current, floor, loc = (vp[k] for k in VERSION_POLICY_KEYS)
                if rule not in RULES:
                    bad.append("R5 %s uses the rule %r; the rules that exist "
                               "are %s" % (where, rule, ", ".join(sorted(RULES))))
                if loc is not None:
                    if _row(bad, "%s locator" % where, loc, 2):
                        if loc[0] not in LOCATORS:
                            bad.append("R5 %s uses the locator kind %r; the "
                                       "kinds that exist are %s"
                                       % (where, loc[0],
                                          ", ".join(sorted(LOCATORS))))
                        loc = tuple(loc)
                    else:
                        loc = None
                if kind == "tool_pin":
                    if rule != "satisfies":
                        bad.append("R5 %s is a tool_pin, so its rule is "
                                   "`satisfies`, not %r" % (where, rule))
                    if (loc is None) == (not packages):
                        bad.append("R5 %s is a tool_pin and needs exactly one "
                                   "of a locator (one pin) or a packages map "
                                   "(several); it has %s"
                                   % (where, "both" if loc is not None else "neither"))
                else:
                    if rule != "additive_minor":
                        bad.append("R5 %s is judged by version, so its rule is "
                                   "`additive_minor`, not %r" % (where, rule))
                    if loc is None:
                        bad.append("R5 %s declares a version policy with no "
                                   "locator, so nothing can read the version it "
                                   "talks about" % where)
                if current != FROM_OWNER and major_minor(current) is None:
                    bad.append("R6 %s declares current %r, which is neither "
                               "MAJOR.MINOR[.PATCH] nor %s"
                               % (where, current, FROM_OWNER))
                if floor is None:
                    if not (current == FROM_OWNER and kind == "tool_pin"):
                        bad.append("R6 %s declares no floor. Only a tool pin "
                                   "read from the image has no independent "
                                   "floor to declare." % where)
                elif major_minor(floor) is None:
                    bad.append("R6 %s declares the floor %r, which is not "
                               "MAJOR.MINOR[.PATCH]" % (where, floor))
                elif current != FROM_OWNER and major_minor(current) is not None:
                    f, c = major_minor(floor), major_minor(current)
                    if f[0] != c[0]:
                        bad.append("R6 %s has a floor %r and a current %r in "
                                   "different majors" % (where, floor, current))
                    elif f > c:
                        bad.append("R6 %s has a floor %r above its current %r"
                                   % (where, floor, current))
                policy = VersionPolicy(rule, current, floor, loc)
        else:
            if identity == "version_policy":
                bad.append("R5 %s is judged by version and declares no "
                           "version_policy" % where)
            if kind == "tool_pin":
                bad.append("R5 %s is a tool_pin with no version_policy; the pin "
                           "has to say where it is read and how it is compared"
                           % where)

        if identity == "bytes" and kind in ("reader_py", "reader_cpp"):
            notes.append("BYTES-ONLY-READER %s: judged by bytes because the "
                         "reader exports no release constant yet. Visible debt, "
                         "not a policy." % aid)

        artifacts.append(Artifact(
            aid, kind, owner, path, identity, policy, tuple(mirrors),
            refs["producers"], refs["consumers"], tuple(declarations),
            types.MappingProxyType(dict(packages)), rec["why"]))

    # ---- exempt tables ---------------------------------------------------
    twin_exempt = []
    seen = set()
    for i, row in enumerate(data["twin_exempt"]):
        where = "twin_exempt[%d]" % i
        if not _row(bad, where, row, 2):
            continue
        name, why = row
        if name in seen:
            bad.append("R11 the twin exemption %r is declared twice" % (name,))
        seen.add(name)
        _why(bad, where, why)
        twin_exempt.append(Exempt(name, why))

    pins_exempt = []
    seen = set()
    for i, row in enumerate(data["pins_exempt"]):
        where = "pins_exempt[%d]" % i
        if not _row(bad, where, row, 3):
            continue
        prid, ppath, why = row
        if prid not in repo_ids:
            bad.append("R11 %s names the repository %r, which has no row in the "
                       "repos table" % (where, prid))
        if (prid, ppath) in seen:
            bad.append("R11 the pin exemption %s:%s is declared twice"
                       % (prid, ppath))
        seen.add((prid, ppath))
        _why(bad, where, why)
        pins_exempt.append(PinExempt(prid, ppath, why))

    if bad:
        raise RegistryError(bad)

    return Registry(data["registry_version"], tuple(repos), tuple(artifacts),
                    tuple(twin_exempt), tuple(pins_exempt), tuple(notes))


def load_default(declared_ids=None, path=None):
    return load_registry((path or REGISTRY_PATH).read_bytes(), declared_ids)


# --------------------------------------------------------------------------
# derived views
# --------------------------------------------------------------------------


def twin_scan_dirs(registry):
    """The directories an undeclared-twin scan has to cover, derived.

    Parent directory of every owner and mirror path of every artifact whose kind
    is a document rather than code. Derived rather than listed because a hand
    list of directories goes stale the day a mirror lands in a new one, and it
    goes stale silently: the scan keeps passing over a smaller ecosystem.
    """
    out = []
    for art in registry.artifacts:
        if art.kind not in TWIN_KINDS:
            continue
        for rid, path in [(art.owner, art.path)] + [(m.repo, m.path)
                                                    for m in art.mirrors]:
            entry = (rid, os.path.dirname(path))
            if entry not in out:
                out.append(entry)
    return tuple(out)


def derived(registry):
    """The lists contracts.py used to hand-maintain, rebuilt from the registry.

    This exists so the W0 migration can assert that the re-encoding changed
    nothing, and so contracts.py has one place to read rather than nine
    literals.
    """
    identical = []
    for art in registry.artifacts:
        if art.identity != "bytes":
            continue
        for m in art.mirrors:
            identical.append((art.owner, art.path, m.repo, m.path, m.why))

    layout = {r.id: r.dir for r in registry.repos}
    file_repos = tuple(r.id for r in registry.repos if r.checkout == "tree")
    gates = {r.id: r.gate_workflow for r in registry.repos}

    klayout_arg, klayout_consumers = None, ()
    pip_args, requirement_files = {}, ()
    for art in registry.artifacts:
        if art.kind != "tool_pin":
            continue
        loc = art.version_policy.locator if art.version_policy else None
        if loc is not None and loc[0] == "dockerfile_arg" and not art.packages:
            klayout_arg = loc[1]
            klayout_consumers = tuple(
                (d.repo, d.path, d.locator[1], d.op) for d in art.declarations)
        elif art.packages:
            pip_args = dict(art.packages)
            requirement_files = tuple((d.repo, d.path) for d in art.declarations)

    return Derived(
        identical=tuple(identical),
        twin_scan=twin_scan_dirs(registry),
        twin_exempt=frozenset(e.key for e in registry.twin_exempt),
        layout=types.MappingProxyType(layout),
        file_repos=file_repos,
        ci_gate_workflow=types.MappingProxyType(gates),
        klayout_arg=klayout_arg,
        klayout_consumers=klayout_consumers,
        pip_args=types.MappingProxyType(pip_args),
        requirement_files=requirement_files,
        pins_exempt=frozenset((p.repo, p.path) for p in registry.pins_exempt),
    )


# --------------------------------------------------------------------------
# judgements
#
# One state per copy, one list of clauses per state. The states a green run may
# report are GREEN_STATES and nothing else: there is deliberately no state that
# means "the bytes differ under one declared version and that is acceptable",
# because that is exactly the in-place edit a version gate would otherwise wave
# through.
# --------------------------------------------------------------------------


def judge_mirror(artifact, owner_pin, owner_head, mirror):
    """(state, clauses) for one mirror of one artifact.

    `owner_pin`, `owner_head` and `mirror` are bytes or None. `owner_head` may be
    None when the head is not being fetched; the AHEAD_OF_PIN state then simply
    cannot be reached, which is the honest answer rather than a guess.
    """
    if mirror is None:
        return MISSING, ["the copy is not there, so the pair cannot be compared"]
    if owner_pin is None:
        return UNREADABLE, ["the owner's file could not be read at the pin, so "
                            "nothing is known about the copy"]
    if mirror == owner_pin:
        return IDENTICAL, []
    if owner_head is not None and mirror == owner_head:
        return AHEAD_OF_PIN, ["the copy matches the owner's branch head rather "
                              "than the pin, which is a re-sync landing ahead "
                              "of the pin bump"]
    if artifact.identity == "bytes":
        return DRIFTED, ["the copy is declared byte-identical and is not"]

    loc = artifact.version_policy.locator
    floor = artifact.version_policy.floor
    mv = read_version(loc, mirror)
    pv = read_version(loc, owner_pin)
    hv = read_version(loc, owner_head) if owner_head is not None else None
    if mv is None or major_minor(mv) is None:
        return DRIFTED, ["the copy declares no readable version, so the version "
                         "policy has nothing to judge and the bytes differ"]
    if pv is None or major_minor(pv) is None:
        return UNREADABLE, ["the owner's file declares no readable version at "
                            "the pin, so no copy can be judged against it"]
    m, p = major_minor(mv), major_minor(pv)
    h = major_minor(hv) if hv else None
    if m == p or (h is not None and m == h):
        # The crux. A version policy permits a different declared version across
        # the seam; it never permits different bytes under the same declared
        # version. Without this clause an in-place edit that leaves the version
        # alone is invisible to every gate in the ecosystem.
        return DRIFTED, ["the copy declares version %s, the same as the commit "
                         "it does not match byte for byte. A version policy "
                         "allows a different version, never different bytes "
                         "under one version." % mv]
    if m[0] != p[0]:
        return DRIFTED, ["the copy declares version %s and the owner declares "
                         "%s, which are different majors" % (mv, pv)]
    if floor is not None and m < major_minor(floor):
        return DRIFTED, ["the copy declares version %s, below the floor %s this "
                         "registry sets" % (mv, floor)]
    if m > p:
        return DRIFTED, ["the copy declares version %s, ahead of the owner's "
                         "%s at the pin" % (mv, pv)]
    return VERSION_OK, []


def judge_owner(artifact, owner_blob):
    """(state, clauses) for the owner's own file."""
    if owner_blob is None:
        return MISSING, ["the owner's file is not there, so the artifact this "
                         "registry names does not exist where it says it does"]
    policy = artifact.version_policy
    if policy is None or policy.locator is None:
        return IDENTICAL, []
    got = read_version(policy.locator, owner_blob)
    if got is None or major_minor(got) is None:
        return UNREADABLE, ["the owner's file declares no version this checker "
                            "can read at %s" % (policy.locator,)]
    clauses = []
    if policy.current != FROM_OWNER:
        if major_minor(got) != major_minor(policy.current):
            clauses.append("the owner declares version %s and this registry "
                           "declares %s. Two declarations of one number have to "
                           "move in one commit." % (got, policy.current))
    if policy.floor is not None and major_minor(got) < major_minor(policy.floor):
        clauses.append("the owner declares version %s, below the floor %s this "
                       "registry sets" % (got, policy.floor))
    return (DRIFTED if clauses else IDENTICAL), clauses


def judge_declaration(declaration, pin, blob):
    """(state, clauses) for one declared constraint on a tool pin."""
    if blob is None:
        return MISSING, ["%s:%s is not there, and it is one of the places the "
                         "pin %s has to agree with"
                         % (declaration.repo, declaration.path, pin)]
    got = read_version(declaration.locator, blob)
    if got is None:
        return UNREADABLE, ["%s:%s no longer states a version in the form this "
                            "check reads. An unreadable declaration is not an "
                            "agreeing one." % (declaration.repo, declaration.path)]
    try:
        ok = satisfies(pin, declaration.op, got)
    except ValueError as e:
        return UNREADABLE, ["%s:%s: %s" % (declaration.repo, declaration.path, e)]
    if not ok:
        return DRIFTED, ["%s:%s declares %s%s and the pin is %s"
                         % (declaration.repo, declaration.path, declaration.op,
                            got, pin)]
    return IDENTICAL, []


# --------------------------------------------------------------------------
# canonical layout
#
# A data file whose diff is a reflow is a data file nobody reviews. One row per
# line, one fixed key order, artifacts sorted by id; `--fmt-check` runs inside
# contracts.py --self-test so a hand reflow fails before any contract runs.
# --------------------------------------------------------------------------


def _j(value):
    return json.dumps(value, ensure_ascii=False)


def _rows(key, rows, indent, tail):
    if not rows:
        return ['%s"%s": []%s' % (indent, key, tail)]
    out = ['%s"%s": [' % (indent, key)]
    for i, row in enumerate(rows):
        out.append("%s  %s%s" % (indent, _j(row), "," if i + 1 < len(rows) else ""))
    out.append("%s]%s" % (indent, tail))
    return out


def _artifact_lines(rec, last):
    out = ["    {"]
    for k in ARTIFACT_KEYS:
        tail = "" if k == ARTIFACT_KEYS[-1] else ","
        value = rec.get(k)
        if k in ("mirrors", "producers", "consumers", "declarations"):
            out.extend(_rows(k, value or [], "      ", tail))
        elif k == "packages":
            items = list((value or {}).items())
            if not items:
                out.append('      "packages": {}%s' % tail)
            else:
                out.append('      "packages": {')
                for i, (name, arg) in enumerate(items):
                    out.append("        %s: %s%s"
                               % (_j(name), _j(arg),
                                  "," if i + 1 < len(items) else ""))
                out.append("      }%s" % tail)
        elif k == "version_policy":
            if value is None:
                out.append('      "version_policy": null%s' % tail)
            else:
                inner = ", ".join("%s: %s" % (_j(pk), _j(value.get(pk)))
                                  for pk in VERSION_POLICY_KEYS)
                out.append('      "version_policy": {%s}%s' % (inner, tail))
        else:
            out.append('      "%s": %s%s' % (k, _j(value), tail))
    out.append("    }%s" % ("" if last else ","))
    return out


def canonical_dump(blob):
    """The registry, laid out the one way this repository writes it."""
    data = _parse(blob)
    out = ["{", '  "registry_version": %s,' % _j(data.get("registry_version"))]
    out.extend(_rows("repos", data.get("repos") or [], "  ", ","))
    arts = sorted(data.get("artifacts") or [], key=lambda a: a.get("id") or "")
    if not arts:
        out.append('  "artifacts": [],')
    else:
        out.append('  "artifacts": [')
        for i, rec in enumerate(arts):
            out.extend(_artifact_lines(rec, last=(i + 1 == len(arts))))
        out.append("  ],")
    out.extend(_rows("twin_exempt", data.get("twin_exempt") or [], "  ", ","))
    out.extend(_rows("pins_exempt", data.get("pins_exempt") or [], "  ", ""))
    out.append("}")
    return "\n".join(out) + "\n"


def fmt_check(path):
    """[] when the file is already canonical, else one clause saying so."""
    text = path.read_text()
    want = canonical_dump(text)
    if text == want:
        return []
    return ["%s is not in canonical layout. Run `contract_registry.py --fmt %s`; "
            "a reflowed data file turns a one-line change into a diff nobody "
            "reads." % (path.name, path.name)]


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

_MINIMAL = {
    "registry_version": 1,
    "repos": [
        ["adk", "adk", "tree", ".github/workflows/tests.yml", "the ADK"],
        ["spec", "chiplet-spec", "tree", ".github/workflows/tests.yml", "the spec"],
        ["tools", "adk-tools", "tree", ".github/workflows/ci.yml", "this one"],
    ],
    "artifacts": [
        {
            "id": "a_schema",
            "kind": "schema",
            "owner": "spec",
            "path": "schemas/a.schema.json",
            "identity": "bytes",
            "version_policy": None,
            "mirrors": [["adk", "config/schema/a.schema.json", "a copy"]],
            "producers": [],
            "consumers": [],
            "declarations": [],
            "packages": {},
            "why": "a schema in two trees",
        },
        {
            "id": "a_reader",
            "kind": "reader_py",
            "owner": "spec",
            "path": "reference/python/r/__init__.py",
            "identity": "version_policy",
            "version_policy": {"rule": "additive_minor", "current": "1.1.0",
                               "floor": "1.1",
                               "locator": ["py_assign", "__version__"]},
            "mirrors": [["adk", "vendor/r/__init__.py", "a vendored reader"]],
            "producers": [],
            "consumers": [],
            "declarations": [],
            "packages": {},
            "why": "the vendored reader",
        },
        {
            "id": "a_pin",
            "kind": "tool_pin",
            "owner": "tools",
            "path": "Dockerfile",
            "identity": "version_policy",
            "version_policy": {"rule": "satisfies", "current": "@owner",
                               "floor": None,
                               "locator": ["dockerfile_arg", "TOOL_PIP"]},
            "mirrors": [],
            "producers": [],
            "consumers": [],
            "declarations": [["adk", "req.txt", ["regex", r"(?m)^t==([0-9.]+)$"],
                              "==", "the ADK pins it too"]],
            "packages": {},
            "why": "one tool across the ecosystem",
        },
    ],
    "twin_exempt": [],
    "pins_exempt": [],
}


def _mutated(fn):
    blob = json.loads(json.dumps(_MINIMAL))
    fn(blob)
    return blob


def self_test():
    cases = []

    def expect(name, ok, detail=None):
        cases.append((name, bool(ok), detail))

    def refuses(name, fn, needle):
        try:
            load_registry(_mutated(fn))
        except RegistryError as e:
            expect(name, any(needle in c for c in e.clauses), e.clauses)
            return
        expect(name, False, "loaded without complaint")

    # ---- the comparator, moved here with its cases ----------------------
    expect("0.30 equals 0.30.0", satisfies("0.30", "==", "0.30.0"))
    expect("0.30.10 is above 0.30.9", satisfies("0.30.10", ">=", "0.30.9"))
    expect("0.9 is below 0.10", satisfies("0.9", "<", "0.10"))
    expect("~= holds the middle component",
           satisfies("6.0.5", "~=", "6.0.3") and not satisfies("6.1.0", "~=", "6.0.3"))
    try:
        satisfies("1.0", "==", "1.0rc1")
        expect("a non-numeric bound is refused, not guessed at", False)
    except ValueError:
        expect("a non-numeric bound is refused, not guessed at", True)

    # ---- the additive-minor truth table, parity with the reference ------
    table = [
        ("1.0", "1.0", "accept"),
        ("1.0.7", "1.0", "accept"),          # PATCH ignored
        ("1.0", "1.2", "accept"),            # at or below
        ("1.3", "1.2", "warn"),              # same major, higher minor
        ("2.0", "1.0", "refuse"),            # other major
        ("0.9", "1.0", "refuse"),            # other major, downward
        ("1", "1.0", "refuse"),              # bare major
        ("1.0.0.1", "1.0", "refuse"),        # four components
        ("v1.0", "1.0", "refuse"),           # malformed
        ("", "1.0", "refuse"),
        (None, "1.0", "refuse"),             # missing
        ("1.-1", "1.0", "refuse"),
    ]
    wrong = [(v, s, additive_minor_verdict(v, s), want)
             for v, s, want in table
             if additive_minor_verdict(v, s) != want]
    expect("the additive-minor truth table holds (%d rows)" % len(table),
           not wrong, wrong)

    # ---- loading the minimal registry -----------------------------------
    reg = load_registry(_MINIMAL)
    expect("a well-formed registry loads", len(reg.artifacts) == 3)
    expect("a reader judged by bytes would be flagged as debt, and this one is "
           "not", reg.notes == ())
    expect("repos come back frozen", isinstance(reg.repos, tuple))

    d = derived(reg)
    expect("derived identical carries only the bytes artifacts",
           d.identical == (("spec", "schemas/a.schema.json",
                            "adk", "config/schema/a.schema.json", "a copy"),))
    expect("derived twin_scan skips readers and tool pins",
           d.twin_scan == (("spec", "schemas"), ("adk", "config/schema")))
    expect("derived layout and file_repos come out of the repos table",
           d.layout == {"adk": "adk", "spec": "chiplet-spec", "tools": "adk-tools"}
           and d.file_repos == ("adk", "spec", "tools"))
    expect("derived klayout-shaped consumers come from the single-pin tool_pin",
           d.klayout_arg == "TOOL_PIP" and len(d.klayout_consumers) == 1)

    # ---- negative cases, one per clause ---------------------------------
    def set_kind(b, kind):
        b["artifacts"][0]["kind"] = kind

    refuses("an unknown kind is refused, not ignored",
            lambda b: set_kind(b, "spreadsheet"), "R1")
    refuses("an unknown identity mode is refused",
            lambda b: b["artifacts"][0].__setitem__("identity", "vibes"), "R5")
    refuses("an unknown checkout is refused, and `none` is one of them",
            lambda b: b["repos"][0].__setitem__(2, "none"), "R12")
    refuses("an unknown locator kind is refused",
            lambda b: b["artifacts"][1]["version_policy"].__setitem__(
                "locator", ["divination", "x"]), "R5")
    refuses("an unknown operator is refused",
            lambda b: b["artifacts"][2]["declarations"][0].__setitem__(3, "=~"),
            "R9")
    refuses("a duplicate artifact id is refused",
            lambda b: b["artifacts"][1].__setitem__("id", "a_schema"), "R1")
    refuses("an id that is not the declared shape is refused",
            lambda b: b["artifacts"][0].__setitem__("id", "A-Schema"), "R1")
    refuses("an owner with no row in the repos table is refused",
            lambda b: b["artifacts"][0].__setitem__("owner", "studio"), "R2")
    refuses("a mirror repository with no row in the repos table is refused",
            lambda b: b["artifacts"][0]["mirrors"][0].__setitem__(0, "studio"),
            "R2")
    refuses("a mirror in the owner's own tree is refused",
            lambda b: b["artifacts"][0]["mirrors"][0].__setitem__(0, "spec"),
            "R4")
    refuses("one (repo, path) claimed by two artifacts is refused",
            lambda b: b["artifacts"][1]["mirrors"][0].__setitem__(
                1, "config/schema/a.schema.json"), "R4")
    refuses("a version policy with no locator is refused",
            lambda b: b["artifacts"][1]["version_policy"].__setitem__(
                "locator", None), "R5")
    refuses("a version-judged artifact on the wrong rule is refused",
            lambda b: b["artifacts"][1]["version_policy"].__setitem__(
                "rule", "satisfies"), "R5")
    refuses("a tool pin on the wrong rule is refused",
            lambda b: b["artifacts"][2]["version_policy"].__setitem__(
                "rule", "additive_minor"), "R5")
    refuses("a tool pin with both a locator and packages is refused",
            lambda b: b["artifacts"][2].__setitem__("packages", {"t": "TOOL_PIP"}),
            "R5")
    refuses("a floor above its current is refused",
            lambda b: b["artifacts"][1]["version_policy"].__setitem__(
                "floor", "1.2"), "R6")
    refuses("a floor in another major is refused",
            lambda b: b["artifacts"][1]["version_policy"].__setitem__(
                "floor", "0.9"), "R6")
    refuses("a current that parses as nothing is refused",
            lambda b: b["artifacts"][1]["version_policy"].__setitem__(
                "current", "latest"), "R6")
    refuses("a declared version with no floor is refused",
            lambda b: b["artifacts"][1]["version_policy"].__setitem__(
                "floor", None), "R6")
    refuses("an empty why is refused",
            lambda b: b["artifacts"][0]["mirrors"][0].__setitem__(2, "  "), "R7")
    refuses("a tool pin nothing is compared against is refused",
            lambda b: b["artifacts"][2].__setitem__("declarations", []), "R8")
    refuses("declarations on something that is not a tool pin are refused",
            lambda b: b["artifacts"][0].__setitem__(
                "declarations", [["adk", "x", None, None, "why"]]), "R8")
    refuses("a short row is refused rather than indexed past",
            lambda b: b["artifacts"][0]["mirrors"].__setitem__(
                0, ["adk", "config/schema/a.schema.json"]), "R9")
    refuses("a repository row with a missing field is refused",
            lambda b: b["repos"].__setitem__(0, ["adk", "adk", "tree"]), "R9")
    refuses("a locator without an operator is refused",
            lambda b: b["artifacts"][2]["declarations"][0].__setitem__(3, None),
            "R9")
    refuses("a duplicate twin exemption is refused",
            lambda b: b.__setitem__("twin_exempt",
                                    [["x.json", "why"], ["x.json", "why"]]),
            "R11")
    refuses("a pin exemption naming an unknown repository is refused",
            lambda b: b.__setitem__("pins_exempt",
                                    [["studio", "req.txt", "why"]]), "R11")
    refuses("an unknown top-level key is refused",
            lambda b: b.__setitem__("extras", []), "unknown top-level key")
    refuses("an artifact with an unknown key is refused",
            lambda b: b["artifacts"][0].__setitem__("notes", []), "unknown key")
    refuses("an artifact with a key missing is refused",
            lambda b: b["artifacts"][0].pop("packages"), "has no 'packages'")

    try:
        load_registry("{not json")
        expect("a file that is not JSON is refused", False)
    except RegistryError as e:
        expect("a file that is not JSON is refused",
               any("does not parse" in c for c in e.clauses))

    # every clause at once, not the first one
    try:
        load_registry(_mutated(lambda b: (
            b["artifacts"][0].__setitem__("kind", "spreadsheet"),
            b["artifacts"][0]["mirrors"][0].__setitem__(2, ""),
            b["repos"][0].__setitem__(2, "none"))))
        expect("every clause is collected, not just the first", False)
    except RegistryError as e:
        expect("every clause is collected, not just the first",
               len(e.clauses) >= 3, e.clauses)

    # ---- BYTES-ONLY-READER is a note, not a failure ---------------------
    reg2 = load_registry(_mutated(lambda b: (
        b["artifacts"][1].__setitem__("identity", "bytes"),
        b["artifacts"][1].__setitem__("version_policy", None))))
    expect("a reader judged by bytes loads and is printed as visible debt",
           any("BYTES-ONLY-READER" in n for n in reg2.notes), reg2.notes)

    # ---- confidentiality: the repos table is the pinned set -------------
    try:
        load_registry(_MINIMAL, declared_ids={"adk", "spec"})
        expect("a repos row nothing pins is refused", False)
    except RegistryError as e:
        expect("a repos row nothing pins is refused",
               any("R13" in c for c in e.clauses), e.clauses)
    try:
        load_registry(_MINIMAL, declared_ids={"adk", "spec", "tools", "studio"})
        expect("a pinned repository with no row is refused", False)
    except RegistryError as e:
        expect("a pinned repository with no row is refused",
               any("no row in the repos table" in c for c in e.clauses), e.clauses)
    expect("the pinned set and the repos table agreeing loads",
           load_registry(_MINIMAL,
                         declared_ids={"adk", "spec", "tools"}) is not None)

    # ---- read_version ---------------------------------------------------
    expect("a version comes out of a JSON pointer",
           read_version(("json_pointer", "/version"), b'{"version": "0.2.0"}')
           == "0.2.0")
    expect("a missing JSON pointer is None, not a guess",
           read_version(("json_pointer", "/version"), b'{"v": "0.2.0"}') is None)
    expect("a version comes out of a python assignment",
           read_version(("py_assign", "__version__"),
                        b'x = 1\n__version__ = "1.1.0"\n') == "1.1.0")
    expect("a version comes out of a Dockerfile ARG",
           read_version(("dockerfile_arg", "KLAYOUT_PIP"),
                        b"ARG KLAYOUT_PIP=0.30.5\n") == "0.30.5")
    expect("a version comes out of a plain regex",
           read_version(("regex", r'(?mi)^klayout==([0-9.]+)\s*$'),
                        b"klayout==0.30.5\n") == "0.30.5")
    expect("bytes that are not text are None rather than an exception",
           read_version(("py_assign", "__version__"), b"\xff\xfe\x00") is None)

    # ---- judge_mirror ---------------------------------------------------
    by_id = {a.id: a for a in reg.artifacts}
    bytes_art, ver_art = by_id["a_schema"], by_id["a_reader"]
    pin = b'__version__ = "1.1.0"\nbody = 1\n'
    head = b'__version__ = "1.2.0"\nbody = 2\n'

    expect("a matching copy is IDENTICAL",
           judge_mirror(bytes_art, b"same", b"same", b"same")[0] == IDENTICAL)
    expect("a copy matching the head is AHEAD_OF_PIN",
           judge_mirror(bytes_art, b"pin", b"head", b"head")[0] == AHEAD_OF_PIN)
    expect("a copy matching neither is DRIFTED",
           judge_mirror(bytes_art, b"pin", b"head", b"other")[0] == DRIFTED)
    expect("an absent copy is MISSING",
           judge_mirror(bytes_art, b"pin", b"head", None)[0] == MISSING)
    expect("an unreadable owner is UNREADABLE, not a finding about the copy",
           judge_mirror(bytes_art, None, None, b"x")[0] == UNREADABLE)
    # (A): same declared version, different bytes is red, with no bytes fallback
    state, clauses = judge_mirror(ver_art, pin, head,
                                  b'__version__ = "1.1.0"\nbody = 99\n')
    expect("an in-place edit that keeps the version is DRIFTED, never green",
           state == DRIFTED and any("never different bytes" in c for c in clauses),
           clauses)
    state, _ = judge_mirror(ver_art, pin, head,
                            b'__version__ = "1.2.0"\nbody = 99\n')
    expect("a copy declaring the head's version but not its bytes is DRIFTED",
           state == DRIFTED)
    state, _ = judge_mirror(ver_art, pin, head, b"nothing = 1\n")
    expect("a copy declaring no version at all is DRIFTED", state == DRIFTED)
    state, _ = judge_mirror(ver_art, b'__version__ = "1.4.0"\nb = 1\n', head,
                            b'__version__ = "1.3.0"\nb = 2\n')
    expect("a same-major older copy inside the floor is VERSION_OK",
           state == VERSION_OK)
    state, _ = judge_mirror(ver_art, b'__version__ = "1.4.0"\nb = 1\n', head,
                            b'__version__ = "1.0.0"\nb = 2\n')
    expect("a copy below the declared floor is DRIFTED", state == DRIFTED)
    state, _ = judge_mirror(ver_art, b'__version__ = "1.4.0"\nb = 1\n', head,
                            b'__version__ = "2.0.0"\nb = 2\n')
    expect("a copy in another major is DRIFTED", state == DRIFTED)
    expect("there is no green state that means the bytes differ under one "
           "version", "MIRROR_DIFFERS" not in globals()
           and set(STATES) - GREEN_STATES == {DRIFTED, UNREADABLE, MISSING})

    # ---- judge_owner and judge_declaration ------------------------------
    state, clauses = judge_owner(ver_art, b'__version__ = "1.0.0"\n')
    expect("an owner below the registry's declaration is DRIFTED",
           state == DRIFTED and len(clauses) == 2, clauses)
    expect("an owner at the declared version is clean",
           judge_owner(ver_art, pin)[0] == IDENTICAL)
    expect("an absent owner file is MISSING",
           judge_owner(ver_art, None)[0] == MISSING)
    decl = by_id["a_pin"].declarations[0]
    expect("a declaration the pin satisfies is clean",
           judge_declaration(decl, "1.2", b"t==1.2\n")[0] == IDENTICAL)
    expect("a declaration the pin does not satisfy is DRIFTED",
           judge_declaration(decl, "1.3", b"t==1.2\n")[0] == DRIFTED)
    expect("a declaration this check can no longer read is UNREADABLE",
           judge_declaration(decl, "1.2", b"t = 1.2\n")[0] == UNREADABLE)

    # ---- canonical layout ------------------------------------------------
    once = canonical_dump(_MINIMAL)
    expect("canonical_dump is idempotent", canonical_dump(once) == once)
    expect("canonical_dump sorts artifacts by id",
           once.index('"a_pin"') < once.index('"a_reader"') < once.index('"a_schema"'))
    expect("canonical_dump puts one row on one line",
           '["adk", "config/schema/a.schema.json", "a copy"]' in once)
    expect("a reflowed file is not canonical",
           canonical_dump(json.dumps(_MINIMAL, indent=8)) == once)

    # ---- the registry this repository actually ships ---------------------
    if REGISTRY_PATH.is_file():
        expect("the shipped registry is in canonical layout",
               not fmt_check(REGISTRY_PATH), fmt_check(REGISTRY_PATH))
        try:
            real = load_default()
            expect("the shipped registry loads", True)
            expect("every shipped artifact has a reason",
                   all(a.why.strip() for a in real.artifacts))
        except RegistryError as e:
            expect("the shipped registry loads", False, e.clauses)

    ok = True
    for name, passed, detail in cases:
        print("%-72s %s" % (name, "ok" if passed else "FAILED"))
        if not passed:
            ok = False
            if detail is not None:
                print("   detail: %r" % (detail,))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--fmt", action="store_true",
                      help="rewrite the registry in canonical layout")
    mode.add_argument("--fmt-check", action="store_true",
                      help="fail if the registry is not in canonical layout")
    mode.add_argument("--table", action="store_true",
                      help="print the registry as one line per artifact")
    ap.add_argument("file", nargs="?", default=None)
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    path = pathlib.Path(args.file) if args.file else REGISTRY_PATH
    if not path.is_file():
        print("FAIL: %s is not there" % path, file=sys.stderr)
        return 2

    if args.fmt:
        text = canonical_dump(path.read_text())
        path.write_text(text)
        print("%s: canonical" % path)
        return 0

    if args.fmt_check:
        clauses = fmt_check(path)
        for c in clauses:
            print("FAIL: %s" % c, file=sys.stderr)
        return 1 if clauses else 0

    try:
        reg = load_registry(path.read_bytes())
    except RegistryError as e:
        for c in e.clauses:
            print("FAIL: %s" % c, file=sys.stderr)
        return 2
    for art in reg.artifacts:
        version = "-"
        if art.version_policy:
            version = art.version_policy.current
        print("%-30s [%s/%s] %s:%s v%s  mirrors=%d"
              % (art.id, art.kind, art.identity, art.owner, art.path, version,
                 len(art.mirrors)))
    for note in reg.notes:
        print("NOTE %s" % note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
