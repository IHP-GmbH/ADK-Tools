#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The producer side of the contract registry: one repository, at its own head,
asked whether it still holds up its end.

`ci/contracts.py` runs in the superproject, where every repository exists at
once, and judges the pinned combination. That run is the authority, and it is
also the run that arrives too late: it happens after a pull request has merged
into the repository that broke the agreement, and it goes red in a third
repository nobody involved was looking at. The vendored `.chiplet` reader is the
worked example. Somebody edits the copy in the KiCad plugin, the plugin's own
suite is green because the copy is internally consistent, the pull request
merges, and the integration run turns red the next time somebody bumps a pin.

So the same registry, the same reader and the same judgements run inside the
producing repository, on the pull request that would cause it, against the
combination the superproject pins.

Where each thing comes from, and why:

  the registry, the reader, the pins, the image
      a blobless sparse clone of ADK-Tools at the track branch:

          git clone --depth 1 --filter=blob:none --sparse \\
              -b dev https://github.com/IHP-GmbH/adk-tools .adk-tools-ci
          git -C .adk-tools-ci sparse-checkout set ci Dockerfile

      One clone, one commit, so the registry, the reader that validates it, the
      submodule gitlinks, `ci/integration-refs.json` and the Dockerfile that
      declares every pin are coherent with each other. That commit is printed
      first, before any verdict, because the alternative is a red tick in
      somebody else's repository with nothing on the page saying which version
      of the rules produced it.

  this repository
      the checkout the job is running in, read from disk. On a pull request that
      is the merge head, which is the whole point: the question is whether the
      change under review still agrees, not whether the last release did.

  the other repositories
      the GitHub contents API, at the pinned commit and at the declared branch
      head. Two lookups rather than one, because a copy that matches the owner's
      head but not the pin is a re-sync that landed before the pin bump, and
      calling that drift would make the correct order of two pull requests
      impossible.

Exit codes are three, and the third one is the point:

    0   every registered artifact this repository is named in still agrees
    1   at least one does not
    2   no verdict was reached

A check that could not run exits 2 and never 0. A repository the registry names
in no row at all is exit 2 as well, and this is deliberate: the failure mode of
a producer-side gate is that somebody adds the job to a repository it covers
nothing in, sees a green tick, and believes the repository is under contract.

Usage:
    conformance.py --repo plugin --root . --tools .adk-tools-ci --track dev
    conformance.py --self-test

Never writes anything. `GITHUB_TOKEN` or `GH_TOKEN` is read for the contents
API; an unauthenticated run works until the rate limit and then exits 2, which
is the honest answer.
"""

import argparse
import ast
import base64
import collections
import io
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

#: The sentence every consumer of this registry ends on, verbatim in both, so
#: that a green tick from either one says the same thing about what it did not
#: check.
CLOSING = ("contracts green means every registered artifact agrees at the "
           "pinned combination; green is not a completeness claim.")

#: The four checks, named so a failure line says which one produced it. They are
#: the four things a producing repository can be, and a repository is usually
#: several of them at once.
MIRRORS = "mirrors"
OWNER = "owner"
DECLARED_PATHS = "declared-paths"
TOOL_PINS = "tool-pins"
CHECK_ORDER = (MIRRORS, OWNER, DECLARED_PATHS, TOOL_PINS)


class NoVerdict(Exception):
    """The check did not run.

    Kept distinct from a finding all the way to the exit code. "The copy has
    drifted" and "the lookup that would have told us returned a 502" are
    different facts, and reporting the first when the second happened sends
    somebody to re-sync a file that is fine.
    """


# --------------------------------------------------------------------------
# where the bytes come from
# --------------------------------------------------------------------------


class Checkout:
    """This repository, at the head the job is running on. Read-only."""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self._tracked = None

    def read(self, rel):
        try:
            return (self.root / rel).read_bytes()
        except (OSError, ValueError):
            return None

    def exists(self, rel):
        try:
            return (self.root / rel).exists()
        except (OSError, ValueError):
            return False

    def tracked(self):
        """Every path git tracks here.

        Tracked rather than walked, so a virtual environment somebody left in
        the working directory is not reported as an undeclared dependency file.
        A failure to list is a NoVerdict and never an empty list: an empty list
        would make the scan below pass while covering nothing.
        """
        if self._tracked is None:
            try:
                out = subprocess.run(["git", "-C", str(self.root), "ls-files"],
                                     capture_output=True, text=True,
                                     check=True).stdout
            except (OSError, subprocess.CalledProcessError) as e:
                raise NoVerdict("the tracked files of this checkout could not "
                                "be listed (%s), so the scan for dependency "
                                "files nothing reads would have covered nothing "
                                "and passed" % e)
            self._tracked = [line for line in out.splitlines() if line]
        return self._tracked


class Remote:
    """The other repositories, through the contents API, cached per lookup."""

    def __init__(self, call, slugs, pins, branches):
        self.call = call
        self.slugs = slugs
        self.pins = pins
        self.branches = branches
        self._cache = {}

    def at_pin(self, rid, path):
        sha = self.pins.get(rid)
        if not sha:
            raise NoVerdict("%s has no pinned commit in the ADK-Tools clone, so "
                            "there is no combination to judge this repository "
                            "against" % rid)
        return self._contents(rid, path, sha)

    def at_head(self, rid, path):
        branch = self.branches.get(rid)
        if not branch:
            raise NoVerdict("%s declares no branch, so a copy that is ahead of "
                            "the pin cannot be told apart from one that has "
                            "drifted" % rid)
        return self._contents(rid, path, branch)

    def _contents(self, rid, path, ref):
        key = (rid, path, ref)
        if key in self._cache:
            return self._cache[key]
        slug = self.slugs.get(rid)
        if not slug:
            raise NoVerdict("no github.com slug is declared for %s" % rid)
        status, body = self.call("/repos/%s/contents/%s?ref=%s"
                                 % (slug, path, ref))
        if status == 404:
            blob = None
        elif status != 200 or not isinstance(body, dict):
            raise NoVerdict("looking up %s on %s@%s returned HTTP %s"
                            % (path, slug, ref[:7], status))
        elif body.get("encoding") != "base64" or body.get("content") is None:
            # A directory, a symlink, or a file the API declines to inline.
            raise NoVerdict("%s on %s@%s did not come back as file content, so "
                            "nothing was compared against it"
                            % (path, slug, ref[:7]))
        else:
            try:
                blob = base64.b64decode(body["content"])
            except (ValueError, TypeError) as e:
                raise NoVerdict("%s on %s@%s did not decode (%s)"
                                % (path, slug, ref[:7], e))
        self._cache[key] = blob
        return blob


class DictRemote:
    """The same surface over two dicts, for the self-test. No network."""

    def __init__(self, at_pin=None, at_head=None, unreachable=()):
        self.pin_files = dict(at_pin or {})
        self.head_files = dict(at_head or {})
        self.unreachable = set(unreachable)

    def _get(self, files, rid, path):
        if (rid, path) in self.unreachable:
            raise NoVerdict("looking up %s on %s returned HTTP 502" % (path, rid))
        return files.get((rid, path))

    def at_pin(self, rid, path):
        return self._get(self.pin_files, rid, path)

    def at_head(self, rid, path):
        return self._get(self.head_files, rid, path)


class DictCheckout:
    """This repository as a dict, for the self-test."""

    def __init__(self, files, tracked=None):
        self.files = {k: (v.encode() if isinstance(v, str) else v)
                      for k, v in files.items()}
        self._tracked = tracked

    def read(self, rel):
        return self.files.get(rel)

    def exists(self, rel):
        return rel in self.files

    def tracked(self):
        # `False` is the fixture for a checkout git cannot be asked about; an
        # empty list is a checkout that really tracks nothing, and the two must
        # not collapse into each other.
        if self._tracked is False:
            raise NoVerdict("the tracked files of this checkout could not be "
                            "listed (fixture says so)")
        return list(self._tracked)


# --------------------------------------------------------------------------
# the four checks
#
# All clauses are collected. A producing repository that broke three rows should
# be told about three, because fixing them one round trip at a time is how a
# required gate becomes the thing people work around.
# --------------------------------------------------------------------------


def mirror_findings(art, repo, local, remote, states):
    """This repository holds a copy of somebody else's file."""
    out = []
    for m in art.mirrors:
        if m.repo != repo:
            continue
        owner_pin = remote.at_pin(art.owner, art.path)
        owner_head = remote.at_head(art.owner, art.path)
        state, clauses = contract_registry.judge_mirror(
            art, owner_pin, owner_head, local.read(m.path))
        states.append(("%s:%s" % (m.repo, m.path), state))
        if state in contract_registry.GREEN_STATES:
            continue
        for c in clauses:
            out.append("%s: %s is %s against %s:%s at the pin. %s (%s)"
                       % (art.id, m.path, state, art.owner, art.path, c, m.why))
    return out


def owner_findings(art, repo, local, remote, states):
    """This repository is where the artifact lives."""
    if art.owner != repo:
        return []
    out = []
    blob = local.read(art.path)
    state, clauses = contract_registry.judge_owner(art, blob)
    states.append(("%s:%s@head" % (art.owner, art.path), state))
    for c in clauses:
        out.append("%s: %s, which this repository owns, is %s. %s"
                   % (art.id, art.path, state, c))
    if blob is None or art.version_policy is None:
        return out
    locator = art.version_policy.locator
    if locator is None:
        return out

    # Monotonic against the commit the superproject pins. `judge_owner` compares
    # the head with what the registry declares, which is the right question when
    # the registry names a number; when it reads the number out of this file
    # instead (`@owner`), that comparison is vacuously true and the only thing
    # left that can catch a version going backwards is the pinned commit.
    head_version = contract_registry.read_version(locator, blob)
    pin_version = contract_registry.read_version(
        locator, remote.at_pin(repo, art.path))
    head_mm = contract_registry.major_minor(head_version)
    pin_mm = contract_registry.major_minor(pin_version)
    if head_mm is not None and pin_mm is not None and head_mm < pin_mm:
        out.append("%s: %s declares version %s at this head and %s at the commit "
                   "the superproject pins. A published version going backwards "
                   "leaves every consumer that already read the higher one with "
                   "no way to say so." % (art.id, art.path, head_version,
                                          pin_version))
    return out


def declared_path_findings(art, repo, local):
    """This repository is named as a producer or a consumer of the artifact.

    Existence only, which is the whole claim: whether the file truly reads the
    artifact is the discovery job's business, and a row pointing at a path that
    is not there any more is a stale row and a one-line fix.
    """
    out = []
    for field, rows in (("produces", art.producers), ("consumes", art.consumers)):
        for ref in rows:
            if ref.repo != repo or local.exists(ref.path):
                continue
            out.append("%s: the registry says this repository %s it at %s, and "
                       "that path is not in this tree. Either the row is stale "
                       "or the code that read the artifact has gone; both are a "
                       "one-line change and neither is invisible. (%s)"
                       % (art.id, field, ref.path, ref.why))
    return out


def tool_pin_findings(art, repo, local, dockerfile):
    """This repository declares a constraint on a version the image pins."""
    mine = [d for d in art.declarations if d.repo == repo]
    if not mine:
        return []
    if dockerfile is None:
        raise NoVerdict("the ADK-Tools clone has no Dockerfile, and it is where "
                        "every version this repository declares a constraint on "
                        "is actually pinned")

    out = []
    policy = art.version_policy
    if policy is not None and policy.locator is not None:
        # One pin, read at one point, compared with the operator the row states.
        pin = contract_registry.read_version(policy.locator, dockerfile)
        if pin is None:
            raise NoVerdict("%s: the image declares no %s, so the pin this "
                            "repository is compared against is unknown"
                            % (art.id, policy.locator[1]))
        for d in mine:
            state, clauses = contract_registry.judge_declaration(
                d, pin, local.read(d.path))
            if state in contract_registry.GREEN_STATES:
                continue
            for c in clauses:
                out.append("%s: %s. The image pins %s, and the pin is what "
                           "actually runs. (%s)" % (art.id, c, pin, d.why))
        return out

    # Several pins, and the declaration is a whole dependency file rather than a
    # point in one.
    pins = {}
    for package, arg in sorted(art.packages.items()):
        value = contract_registry.read_version(("dockerfile_arg", arg),
                                               dockerfile)
        if value is None:
            raise NoVerdict("%s: the image declares no ARG %s, so the pin for %s "
                            "is unknown" % (art.id, arg, package))
        pins[package] = value

    for d in mine:
        blob = local.read(d.path)
        if blob is None:
            out.append("%s: %s is declared as a place this repository states "
                       "Python constraints and it is not in this tree, so its "
                       "absence silently shrinks what is checked. (%s)"
                       % (art.id, d.path, d.why))
            continue
        for line in contracts.requirement_lines(d.repo, d.path, blob):
            try:
                parsed = contracts.split_requirement(line)
            except ValueError as e:
                out.append("%s: %s: %s" % (art.id, d.path, e))
                continue
            if not parsed:
                continue
            name, specs = parsed
            if name not in pins:
                continue
            for op, bound in specs:
                try:
                    ok = contract_registry.satisfies(pins[name], op, bound)
                except ValueError as e:
                    out.append("%s: %s: %s" % (art.id, d.path, e))
                    continue
                if not ok:
                    out.append("%s: %s asks for %s%s%s and the image pins %s. "
                               "The pin is what actually runs, so this "
                               "repository is exercised against a version it "
                               "declares it does not support."
                               % (art.id, d.path, name, op, bound, pins[name]))
    return out


def unregistered_dependency_files(registry, repo, local):
    """Tracked dependency files in this tree that nothing compares against a pin.

    The declarations close the instance and this closes the class. A repository
    that grows a second requirements file is unchecked from the moment it lands,
    and it looks exactly like a repository that has none.
    """
    known = set()
    for art in registry.artifacts:
        for d in art.declarations:
            if d.repo == repo:
                known.add(d.path)
    for p in registry.pins_exempt:
        if p.repo == repo:
            known.add(p.path)

    out = []
    for rel in local.tracked():
        if not contracts.DEPENDENCY_FILE.search(rel) or rel in known:
            continue
        out.append("%s declares Python dependencies and the contract registry "
                   "neither reads it nor exempts it, so nothing compares it "
                   "against the versions the image ships. Either add it to the "
                   "pin declarations or exempt it with a reason." % rel)
    return out


# --------------------------------------------------------------------------
# running them, and saying what happened
# --------------------------------------------------------------------------

Result = collections.namedtuple("Result", "lines counts findings judged")


def evaluate(registry, repo, local, remote, dockerfile):
    """Result, or NoVerdict. One line per artifact, in registry order."""
    lines = []
    findings = collections.OrderedDict((name, []) for name in CHECK_ORDER)
    counts = collections.OrderedDict((s, 0) for s in contract_registry.STATES)
    judged = 0

    for art in registry.artifacts:
        states = []
        findings[MIRRORS].extend(
            mirror_findings(art, repo, local, remote, states))
        findings[OWNER].extend(
            owner_findings(art, repo, local, remote, states))
        findings[DECLARED_PATHS].extend(
            declared_path_findings(art, repo, local))
        findings[TOOL_PINS].extend(
            tool_pin_findings(art, repo, local, dockerfile))

        for _, state in states:
            counts[state] += 1
        stake = bool(states) or any(
            ref.repo == repo
            for ref in tuple(art.producers) + tuple(art.consumers)) or any(
            d.repo == repo for d in art.declarations)
        judged += 1 if stake else 0

        version = "-"
        if art.version_policy is not None:
            version = art.version_policy.current
        detail = "; ".join("%s: %s" % (where, state) for where, state in states)
        if not detail:
            detail = "in scope here" if stake else "not this repository"
        lines.append("%-30s [%s/%s] %s:%s v%s | %s"
                     % (art.id, art.kind, art.identity, art.owner, art.path,
                        version, detail))

    if judged:
        findings[TOOL_PINS].extend(
            unregistered_dependency_files(registry, repo, local))
    return Result(tuple(lines), counts, findings, judged)


def report(registry, repo, local, remote, dockerfile, tools_commit,
           out=None, err=None):
    """Print the run and return its exit code: 0 pass, 1 defect, 2 no verdict."""
    out = out or sys.stdout
    err = err or sys.stderr

    # Printed before anything is judged, so a red tick in this repository is
    # attributable to one ADK-Tools commit without anybody having to guess which
    # revision of the rules was in force.
    print("registry and reader read from adk-tools %s" % tools_commit, file=out)
    print("checking %s at the head of this branch against the combination that "
          "commit pins" % repo, file=out)

    try:
        result = evaluate(registry, repo, local, remote, dockerfile)
    except NoVerdict as e:
        print("NO VERDICT: %s. Nothing was checked; this says nothing about "
              "this repository." % e, file=err)
        return 2

    for line in result.lines:
        print(line, file=out)
    print("states: %s" % ", ".join("%s=%d" % (s, result.counts[s])
                                   for s in contract_registry.STATES), file=out)

    # Before the per-check summary, not after it: four lines reading `ok`
    # followed by an exit code of 2 is a page that says pass and a tick that
    # says otherwise, and the page is what people read.
    if not result.judged:
        print("NO VERDICT: the contract registry names %s in no row, so this "
              "run asserted nothing about it. A green tick here would say this "
              "repository is under contract when it is not." % repo, file=err)
        return 2

    total = 0
    for name in CHECK_ORDER:
        failures = result.findings[name]
        total += len(failures)
        print("%-16s %s" % (name, "ok" if not failures
                            else "%d FAILED" % len(failures)), file=out)

    for name in CHECK_ORDER:
        for f in result.findings[name]:
            print("\nFAIL [%s] %s" % (name, f), file=err)

    print(CLOSING, file=out)
    return 1 if total else 0


# --------------------------------------------------------------------------
# assembling the run from a sparse ADK-Tools clone
# --------------------------------------------------------------------------


def tools_commit(tools):
    try:
        return subprocess.run(["git", "-C", str(tools), "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise NoVerdict("the ADK-Tools clone at %s has no commit this run could "
                        "name (%s), so a verdict from it would not be "
                        "attributable to any version of the rules" % (tools, e))


def tools_gitlinks(tools):
    """{logical id: sha} out of the sparse clone's own tree."""
    try:
        out = subprocess.run(["git", "-C", str(tools), "ls-tree", "-r", "HEAD"],
                             capture_output=True, text=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise NoVerdict("the submodule pins could not be read out of the "
                        "ADK-Tools clone (%s)" % e)
    pins = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[0] == "160000":
            rid = resolve_refs.SUBMODULE_ID.get(path)
            if rid:
                pins[rid] = parts[2]
    return pins


def load_from(tools, track):
    """(registry, slugs, pins, branches, dockerfile), or NoVerdict."""
    tools = pathlib.Path(tools)
    if not tools.is_dir():
        raise NoVerdict("%s is not a directory, so there is no registry, no "
                        "reader and no pins to judge anything against" % tools)

    gitmodules = tools / ".gitmodules"
    refs = tools / "ci" / "integration-refs.json"
    for path in (gitmodules, refs):
        if not path.is_file():
            raise NoVerdict("%s is not in the ADK-Tools clone; the sparse "
                            "checkout has to include `ci` and the repository "
                            "root" % path.name)
    try:
        extra = json.loads(refs.read_text())
    except (OSError, ValueError) as e:
        raise NoVerdict("the non-submodule pins do not parse (%s)" % e)

    entries = resolve_refs.declared(gitmodules.read_text(), extra)
    slugs, branches, pins = {}, {}, tools_gitlinks(tools)
    declared_ids = set()
    for rid, slug, branch, pin in entries:
        if rid is None:
            raise NoVerdict("the ADK-Tools clone pins a submodule at %r that "
                            "nothing can name, so the set of repositories this "
                            "run covers is not the set it pins" % (slug,))
        declared_ids.add(rid)
        slugs[rid] = slug
        branches[rid] = branch or track
        if pin:
            pins[rid] = pin
    declared_ids.add("tools")

    try:
        registry = contract_registry.load_default(declared_ids=declared_ids)
    except contract_registry.RegistryError as e:
        raise NoVerdict("the contract registry at that commit does not validate "
                        "(%s)" % "; ".join(e.clauses))

    dockerfile = None
    docker_path = tools / "Dockerfile"
    if docker_path.is_file():
        dockerfile = docker_path.read_bytes()
    return registry, slugs, pins, branches, dockerfile


# --------------------------------------------------------------------------
# self-test
#
# A fake tree and a fake API, with a conforming input, a non-conforming one and
# an unreachable one, because a CI helper whose negative case has never run is
# indistinguishable from `exit 0` until the day it was supposed to catch
# something.
# --------------------------------------------------------------------------

_FIXTURE = {
    "registry_version": 1,
    "repos": [
        ["adk", "adk", "tree", ".github/workflows/tests.yml", "the ADK"],
        ["spec", "chiplet-spec", "tree", ".github/workflows/tests.yml",
         "the spec"],
        ["plugin", "chiplet_kicad_plugin", "tree",
         ".github/workflows/tests.yml", "the plugin"],
        ["docs", "adk-docs", "tree", ".github/workflows/docs.yml",
         "the documentation build, which this fixture deliberately gives no "
         "artifact row, because a repository under no contract must not be able "
         "to report a green one"],
        ["tools", "adk-tools", "tree", ".github/workflows/ci.yml", "this one"],
    ],
    "artifacts": [
        {
            "id": "a_reader",
            "kind": "reader_py",
            "owner": "spec",
            "path": "reference/python/r/__init__.py",
            "identity": "version_policy",
            "version_policy": {"rule": "additive_minor", "current": "1.1.0",
                               "floor": "1.1",
                               "locator": ["py_assign", "__version__"]},
            "mirrors": [["plugin", "vendor/r/__init__.py", "the vendored copy"]],
            "producers": [],
            "consumers": [["plugin", "export/reader_use.py",
                           "the exporter reads through it"]],
            "declarations": [],
            "packages": {},
            "why": "the reference reader and the copy in the plugin",
        },
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
            "id": "pip_pins",
            "kind": "tool_pin",
            "owner": "tools",
            "path": "Dockerfile",
            "identity": "version_policy",
            "version_policy": {"rule": "satisfies", "current": "@owner",
                               "floor": None, "locator": None},
            "mirrors": [],
            "producers": [],
            "consumers": [],
            "declarations": [
                ["plugin", "requirements.txt", None, None,
                 "the plugin's own dependencies"]],
            "packages": {"pyyaml": "PYYAML_PIP"},
            "why": "every Python package the image pins",
        },
    ],
    "twin_exempt": [],
    "pins_exempt": [["plugin", "docs/requirements.txt",
                     "the documentation build's own pins, out of scope here"]],
}

_DOCKERFILE = b"ARG PYYAML_PIP=6.0.3\n"
_READER_PIN = b'"""the reference."""\n__version__ = "1.1.0"\nbody = 1\n'
_READER_HEAD = b'"""the reference."""\n__version__ = "1.2.0"\nbody = 2\n'


#: Everything in the standard library this module would have to reach for in
#: order to change anything on disk, plus `open` in a mode that is not a read.
WRITING = frozenset({
    "write_text", "write_bytes", "writelines", "mkdir", "makedirs", "remove",
    "unlink", "rmdir", "rmtree", "rename", "replace_file", "chmod", "symlink_to",
    "touch", "copy", "copy2", "copytree", "move", "NamedTemporaryFile",
})


def writing_calls(path, source=None):
    """The names in `WRITING` this source calls, sorted. [] when it writes none.

    `shutil.move` and `pathlib.Path.replace` are called through an attribute and
    `open` through a name, so both shapes are looked at. A call whose mode is
    computed rather than written out is reported, because a mode this cannot
    read is a mode nobody reviewing the diff can either.
    """
    tree = ast.parse(source if source is not None
                     else pathlib.Path(path).read_text())
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(
            func, "id", None)
        if name in WRITING:
            found.add(name)
        if name != "open":
            continue
        mode = None
        if len(node.args) > 1:
            mode = node.args[1]
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if mode is None:
            continue                      # `open(p)` is a read
        if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
            found.add("open")
        elif set(mode.value) & set("wax+"):
            found.add("open")
    return sorted(found)


def _fixture_registry(mutate=None):
    blob = json.loads(json.dumps(_FIXTURE))
    if mutate:
        mutate(blob)
    return contract_registry.load_registry(blob)


COPY = "vendor/r/__init__.py"
USE = "export/reader_use.py"
REQS = "requirements.txt"


def _plugin_files(changes=None):
    files = {
        COPY: _READER_PIN,
        USE: "import r\n",
        REQS: "PyYAML>=6.0.0\n",
        "docs/requirements.txt": "sphinx==7.4.7\n",
    }
    for rel, value in (changes or {}).items():
        if value is None:
            files.pop(rel, None)
        else:
            files[rel] = value
    return files


def _plugin_tracked(files):
    return sorted(files)


def _remote(**kwargs):
    return DictRemote(
        at_pin={("spec", "reference/python/r/__init__.py"): _READER_PIN,
                ("spec", "schemas/a.schema.json"): b"same\n"},
        at_head={("spec", "reference/python/r/__init__.py"): _READER_HEAD,
                 ("spec", "schemas/a.schema.json"): b"same\n"},
        **kwargs)


def _run(repo, files, remote=None, registry=None, dockerfile=_DOCKERFILE,
         tracked=None):
    """The exit code, with the printed run thrown away."""
    sink = io.StringIO()
    return report(registry or _fixture_registry(), repo,
                  DictCheckout(files, _plugin_tracked(files)
                               if tracked is None else tracked),
                  remote or _remote(), dockerfile, "0" * 40,
                  out=sink, err=sink)


def _findings(repo, files, remote=None, registry=None, dockerfile=_DOCKERFILE,
              tracked=None):
    result = evaluate(registry or _fixture_registry(), repo,
                      DictCheckout(files, _plugin_tracked(files)
                                   if tracked is None else tracked),
                      remote or _remote(), dockerfile)
    return [f for name in CHECK_ORDER for f in result.findings[name]]


def self_test():
    cases = []

    def expect(name, ok, detail=None):
        cases.append((name, bool(ok), detail))

    def code(name, want, *args, **kwargs):
        try:
            got = _run(*args, **kwargs)
        except NoVerdict as e:                       # pragma: no cover - a bug
            cases.append((name, False, "NoVerdict escaped report(): %s" % e))
            return
        cases.append((name, got == want, "exit %d, wanted %d" % (got, want)))

    def has(name, files, needle, **kwargs):
        got = _findings("plugin", files, **kwargs)
        cases.append((name, any(needle in g for g in got), got))

    # ---- exit 0: a conforming repository --------------------------------
    code("a repository that still holds up its end exits 0", 0,
         "plugin", _plugin_files())
    expect("and it does so having actually judged something",
           _findings("plugin", _plugin_files()) == [])

    # A copy re-synced from the owner's head before the pin bump lands is the
    # correct order of two pull requests, not drift.
    code("a copy matching the owner's branch head is green, not drift", 0,
         "plugin", _plugin_files({COPY: _READER_HEAD}))

    # ---- exit 1: every way a producer can be non-conforming --------------
    code("a copy that matches neither the pin nor the head exits 1", 1,
         "plugin", _plugin_files({COPY: b'__version__ = "1.1.0"\nbody = 99\n'}))
    has("an in-place edit under the same declared version is named as such",
        _plugin_files({COPY: b'__version__ = "1.1.0"\nb = 9\n'}),
        "never different bytes")
    has("a copy carrying no version at all is red",
        _plugin_files({COPY: b"body = 1\n"}), "no readable version")
    has("a deleted copy is red rather than passing vacuously",
        _plugin_files({COPY: None}), "not there")
    has("a consumer row pointing at a path that has gone is a stale row",
        _plugin_files({USE: None}), "stale")
    has("a raised floor above the image pin is red",
        _plugin_files({REQS: "PyYAML>=6.9.0\n"}), "does not support")
    has("a dependency file nothing reads is red",
        _plugin_files({"requirements-dev.txt": "pytest>=9\n"}),
        "neither reads it nor exempts it")
    expect("an exempted dependency file is not",
           not any("docs/requirements.txt" in f
                   for f in _findings("plugin", _plugin_files())))

    # The bytes-identity side, in the repository that holds that copy.
    code("a byte-identical copy that is not exits 1", 1,
         "adk", {"config/schema/a.schema.json": b"different\n"},
         tracked=["config/schema/a.schema.json"])
    code("a byte-identical copy that is exits 0", 0,
         "adk", {"config/schema/a.schema.json": b"same\n"},
         tracked=["config/schema/a.schema.json"])

    # The owner side, in the repository the artifact lives in.
    code("an owner whose head agrees with the registry exits 0", 0,
         "spec", {"reference/python/r/__init__.py": _READER_PIN,
                  "schemas/a.schema.json": b"same\n"},
         tracked=["reference/python/r/__init__.py", "schemas/a.schema.json"])
    code("an owner that has deleted the artifact exits 1", 1,
         "spec", {"schemas/a.schema.json": b"same\n"},
         tracked=["schemas/a.schema.json"])
    got = _findings("spec",
                    {"reference/python/r/__init__.py":
                     b'__version__ = "1.0.0"\nbody = 1\n',
                     "schemas/a.schema.json": b"same\n"},
                    tracked=["reference/python/r/__init__.py"])
    expect("an owner lowering the published version is told so twice: the "
           "registry declares another number and the pin is already higher",
           any("move in one commit" in g for g in got)
           and any("going backwards" in g for g in got), got)

    # ---- exit 2: no verdict ---------------------------------------------
    code("an API that cannot be reached is no verdict, never green", 2,
         "plugin", _plugin_files(),
         remote=_remote(unreachable={("spec",
                                      "reference/python/r/__init__.py")}))
    code("a checkout whose tracked files cannot be listed is no verdict", 2,
         "plugin", _plugin_files(), tracked=False)
    code("no Dockerfile in the ADK-Tools clone is no verdict", 2,
         "plugin", _plugin_files(), dockerfile=None)
    code("an image that declares no ARG for a pinned package is no verdict", 2,
         "plugin", _plugin_files(), dockerfile=b"ARG SOMETHING_ELSE=1\n")
    code("a repository the registry names in no row is no verdict, not a pass",
         2, "docs", {}, tracked=[])

    # The trap this exit code exists for: the no-row case must not be reachable
    # by deleting rows until a run goes green.
    expect("the no-row case is what would otherwise be the greenest run there "
           "is", _findings("docs", {}, tracked=[]) == [])

    # ---- the shape of the emission ---------------------------------------
    result = evaluate(_fixture_registry(), "plugin",
                      DictCheckout(_plugin_files(),
                                   _plugin_tracked(_plugin_files())),
                      _remote(), _DOCKERFILE)
    expect("one line per artifact, in registry order",
           len(result.lines) == 3
           and [line.split()[0] for line in result.lines]
           == ["a_reader", "a_schema", "pip_pins"], result.lines)
    expect("every state is counted, zeros printed",
           list(result.counts) == list(contract_registry.STATES)
           and result.counts[contract_registry.IDENTICAL] == 1
           and result.counts[contract_registry.DRIFTED] == 0, result.counts)
    expect("an artifact this repository is not named in says so",
           any("not this repository" in line for line in result.lines),
           result.lines)

    # ---- what the run does not do ---------------------------------------
    #
    # This module runs inside four other repositories' required gates, off a
    # clone it made itself. "Read-only" is worth asserting rather than
    # intending, and the assertion is about the source rather than about a run,
    # because the run that writes something is the one nobody thought to try.
    expect("it never writes: nothing in this module can create or modify a "
           "file", not writing_calls(HERE / "conformance.py"),
           writing_calls(HERE / "conformance.py"))
    expect("and that guard is not vacuous", writing_calls(
        HERE / "conformance.py",
        source="import pathlib\npathlib.Path('x').write_text('y')\n")
        == ["write_text"])

    ok = True
    for name, passed, detail in cases:
        print("%-72s %s" % (name, "ok" if passed else "FAILED"))
        if not passed:
            ok = False
            if detail is not None:
                print("   detail: %r" % (detail,))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def _import_from(tools):
    """Put the ADK-Tools clone's own `ci` on the path and import the reader.

    The registry, the reader that validates it and the judgements all have to
    come from the same commit as each other, or a producing repository is judged
    by one version of the rules against a data file written for another.
    """
    global contract_registry, resolve_refs, contracts
    ci = pathlib.Path(tools).resolve() / "ci"
    sys.path.insert(0, str(ci))
    import contract_registry as _cr
    import resolve_refs as _rr
    import contracts as _c
    for module in (_cr, _rr, _c):
        where = pathlib.Path(module.__file__).resolve().parent
        if where != ci:
            raise NoVerdict("%s was imported from %s rather than from the "
                            "ADK-Tools clone at %s, so the rules and the data "
                            "would not be from one commit"
                            % (module.__name__, where, ci))
    contract_registry, resolve_refs, contracts = _cr, _rr, _c


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="this repository's id in the registry")
    ap.add_argument("--root", default=".",
                    help="this repository's checkout (default: the directory "
                         "the job is running in)")
    ap.add_argument("--tools", default=".adk-tools-ci",
                    help="the blobless sparse ADK-Tools clone")
    ap.add_argument("--track", default="dev",
                    help="the branch the ecosystem is on (default: dev)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        _import_from(HERE.parent)
        return self_test()

    if not args.repo:
        ap.error("--repo is required unless --self-test is given")

    try:
        _import_from(args.tools)
        commit = tools_commit(args.tools)
        registry, slugs, pins, branches, dockerfile = load_from(args.tools,
                                                                args.track)
    except NoVerdict as e:
        print("NO VERDICT: %s." % e, file=sys.stderr)
        return 2

    if args.repo not in {r.id for r in registry.repos}:
        print("NO VERDICT: %r is not a repository id in the registry at that "
              "commit. The ids are %s."
              % (args.repo, ", ".join(r.id for r in registry.repos)),
              file=sys.stderr)
        return 2
    if not pathlib.Path(args.root).is_dir():
        print("NO VERDICT: %s is not a directory, so this repository was never "
              "read." % args.root, file=sys.stderr)
        return 2

    remote = Remote(resolve_refs.make_api(resolve_refs.token()),
                    slugs, pins, branches)
    try:
        return report(registry, args.repo, Checkout(args.root), remote,
                      dockerfile, commit)
    except resolve_refs.Unreachable as e:
        print("NO VERDICT: could not reach the GitHub API (%s). Nothing was "
              "checked; this says nothing about this repository." % e,
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
