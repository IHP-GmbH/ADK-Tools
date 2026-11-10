#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The resource floor, asserted from a run instead of trusted as a policy.

The shared host leaves 10 GB of RAM and 4 cores free at all times, and every
heavy tool stage is supposed to run inside a thread cap derived from that. A
policy that lives only in prose drifts: a stage that hardcodes a thread count,
or a cap computed against the wrong core count, oversubscribes the box and
nothing notices until someone else's job is starved.

This is the CI twin of that floor. It reads a packaging-flow RunManifest
(<workdir>/manifest.json) and proves, from the recorded argv of every build
stage, that the run actually stayed inside the cap it declared:

  1. every thread value in every build command equals identity.build_thread_cap
  2. identity.build_thread_cap is at most MAX_BUILD_THREADS and at most
     identity.thread_cap
  3. identity.thread_cap is at most the host's core count minus MIN_FREE_CORES
     (the host core count is passed in; the manifest does not carry it)
  4. no build command is missing any of the thread variables it must set
  5. the identity caps and their tools.* duplicates agree

Which argv is "the build command" depends on the manifest version. Up to
version 2 it is the build row's top-level command. From version 3 the top-level
command is the build-die argv, which launches LibreLane rather than being it,
and the LibreLane argv is the row's facts.librelane_command, a JSON-encoded
list of str. Version 4 keeps version 3's build rows and adds what the guard
does not read (the cosim select answer and the signed gate). Version 5, written
for system.chiplet runs, keeps version 4's build rows unchanged. A version this
guard does not know is refused, never guessed at.

The RAM half of the floor is not per-stage data: it is a pre-dispatch probe.
Its CI twin is --ram-probe, a job-level check that MemAvailable is at least
MIN_FREE_GB before the heavy tier starts, so a runner that cannot honour the
floor refuses the tier rather than thrashing.

Exit codes are tri-state, like the other ci checkers: 0 every assertion holds,
1 an assertion failed so this run broke the floor, 2 no verdict was reachable
(manifest absent, malformed or of an unknown version, or the host core count
was not supplied, so the host-cap assertion could not be evaluated). A missing
verdict is never 0.

Usage:
    resource_guard.py <manifest.json> --cpu-count 32
    resource_guard.py <manifest.json>            # reads CHIPLET_FLOW_HOST_CPUS
    resource_guard.py --ram-probe [--floor-gb 10]
    resource_guard.py --self-test
"""

import argparse
import json
import os
import pathlib
import re
import sys

MIN_FREE_CORES = 4
MIN_FREE_GB = 10
MAX_BUILD_THREADS = 8

# Every thread-count variable a build:* command is required to set, each to the
# build cap. The three environment assignments LibreLane reads plus the four it
# is told on the command line. A build command that omits any of these has left
# a tool free to pick its own parallelism.
BUILD_THREAD_VARS = (
    "_OPENLANE_MAX_CORES",
    "OMP_NUM_THREADS",
    "OPENROAD_NUM_THREADS",
    "DRT_THREADS",
    "KLAYOUT_DRC_THREADS",
    "KLAYOUT_DENSITY_THREADS",
    "STA_THREADS",
)


class NoVerdict(Exception):
    """The check could not be evaluated. Maps to exit 2, never to 1 or 0."""


def thread_values(command, variables=BUILD_THREAD_VARS):
    """{var: [int, ...]} for each variable that appears in a command's argv.

    The argv is joined into one string first: a build command is
    ["nix-shell", "--run", "<the whole shell line>"], so the assignments live
    inside one element, and `-c DRT_THREADS=8` and `OMP_NUM_THREADS=8` are the
    same shape once joined. The lookbehind keeps FOO_DRT_THREADS from matching
    DRT_THREADS.
    """
    text = " ".join(command)
    out = {}
    for var in variables:
        found = re.findall(r"(?<![A-Za-z0-9_])%s=(\d+)" % re.escape(var), text)
        if found:
            out[var] = [int(v) for v in found]
    return out


STATE_COMPLETED = "completed"   # chiplet_flow.manifest.STATE_COMPLETED

# chiplet_flow.manifest.MANIFEST_FORMAT, and the versions split by where a build
# row keeps its LibreLane argv: the top-level command, or a fact.
MANIFEST_FORMAT = "chiplet_flow.manifest"
COMMAND_VERSIONS = (1, 2)
FACT_VERSIONS = (3, 4, 5)
LIBRELANE_COMMAND_FACT = "librelane_command"


def manifest_version(manifest):
    """The manifest's version, or None for a legacy one. NoVerdict if unknown.

    The same rule as chiplet_flow.manifest._format_problem: a manifest with
    neither key is legacy and is read as version 1 always was, one carrying
    either key must carry both, and a format or version this guard does not
    know is refused rather than read by the nearest rule it does know. Reading
    a version-3 row the version-2 way would find no build command at all, so a
    guess here is not a small error.
    """
    if "format" not in manifest and "version" not in manifest:
        return None
    fmt, version = manifest.get("format"), manifest.get("version")
    if (fmt == MANIFEST_FORMAT and type(version) is int
            and version in COMMAND_VERSIONS + FACT_VERSIONS):
        return version
    raise NoVerdict(
        "format %r version %r; this guard reads %r version %s, or a manifest "
        "with neither key"
        % (fmt, version, MANIFEST_FORMAT,
           " or ".join(map(str, COMMAND_VERSIONS + FACT_VERSIONS))))


def is_build_stage(stage, version=None):
    """A build:* row. Up to version 2 only one that recorded a command.

    From version 3 the row is selected by its id alone. Its LibreLane argv is
    no longer in the top-level command, so selecting on that would drop every
    row whose command went unrecorded without a word, and the guard would then
    either pass having checked nothing or, held to the die plan, fail with the
    false reason that the row was never written.
    """
    if not str(stage.get("id", "")).startswith("build:"):
        return False
    return version in FACT_VERSIONS or bool(stage.get("command"))


def failed_before_argv(stage, version=None):
    """A version-3 build row that failed and recorded no LibreLane argv.

    Such a row is skipped, not read: there is no argv to hold to the cap, and
    no argv means LibreLane never started, so there was nothing to bound. A
    row written after LibreLane ran carries the fact whatever its exit code,
    and is read like any other. The failure must be a recorded integer exit
    code other than 0. A row skipped by its tier carries no exit code at all,
    so it does not land here and stays a NoVerdict: it did not fail, it did
    not run, and a run that did not build cannot prove the floor.
    """
    if version not in FACT_VERSIONS:
        return False
    facts = stage.get("facts")
    if isinstance(facts, dict) and facts.get(LIBRELANE_COMMAND_FACT) is not None:
        return False
    code = stage.get("exit_code")
    return type(code) is int and code != 0


def build_command(stage, version=None):
    """The LibreLane argv a build row dispatched. NoVerdict if it is unreadable.

    From version 3 that is facts.librelane_command, decoded from its JSON
    string (facts stay strings); the row's own top-level command is the
    build-die argv and is not read. A row without the fact, or with one that
    is not a non-empty list of non-empty strings, recorded no LibreLane argv
    this guard can hold to the cap, which is a NoVerdict, not a pass: a
    tier that skipped the build lands here too, as it should. A row that
    failed before LibreLane is set aside by failed_before_argv first.
    """
    if version not in FACT_VERSIONS:
        return stage["command"]
    sid = stage.get("id")
    facts = stage.get("facts")
    raw = facts.get(LIBRELANE_COMMAND_FACT) if isinstance(facts, dict) else None
    if raw is None:
        raise NoVerdict(
            "%s: a version-%d build row without facts.%s, so its LibreLane argv "
            "is not recorded" % (sid, version, LIBRELANE_COMMAND_FACT))
    argv = None
    if isinstance(raw, str):
        try:
            argv = json.loads(raw)
        except ValueError:
            pass
    if not (isinstance(argv, list) and argv
            and all(isinstance(a, str) and a for a in argv)):
        raise NoVerdict(
            "%s: facts.%s is not a JSON-encoded non-empty list of non-empty "
            "strings" % (sid, LIBRELANE_COMMAND_FACT))
    return argv


def check_manifest(manifest, cpu_count, variables=BUILD_THREAD_VARS, summary=None):
    """Return [failure, ...]. Raise NoVerdict when a verdict is unreachable."""
    if not isinstance(manifest, dict):
        raise NoVerdict("manifest is not an object")
    version = manifest_version(manifest)
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise NoVerdict("manifest has no identity object")
    try:
        tc = int(identity["thread_cap"])
        btc = int(identity["build_thread_cap"])
    except (KeyError, TypeError, ValueError):
        raise NoVerdict("identity.thread_cap / build_thread_cap absent or non-integer")
    if cpu_count is None:
        raise NoVerdict(
            "host core count not supplied (pass --cpu-count or set "
            "CHIPLET_FLOW_HOST_CPUS); the host-cap assertion cannot run")

    failures = []

    # (2) the build cap sits under the absolute cap and under the runner cap.
    if btc > MAX_BUILD_THREADS:
        failures.append(
            "build_thread_cap %d exceeds MAX_BUILD_THREADS %d" % (btc, MAX_BUILD_THREADS))
    if btc > tc:
        failures.append("build_thread_cap %d exceeds thread_cap %d" % (btc, tc))

    # (3) the runner cap leaves the floor of cores free on the host that ran it.
    if tc > cpu_count - MIN_FREE_CORES:
        failures.append(
            "thread_cap %d exceeds host cpu_count %d minus MIN_FREE_CORES %d (= %d)"
            % (tc, cpu_count, MIN_FREE_CORES, cpu_count - MIN_FREE_CORES))

    # (5) the tools.* duplicates agree with identity.*, so a reader of either is safe.
    tools = manifest.get("tools")
    if isinstance(tools, dict):
        for key, want in (("thread_cap", tc), ("build_thread_cap", btc)):
            if key in tools and tools[key] != want:
                failures.append(
                    "tools.%s %r disagrees with identity.%s %d"
                    % (key, tools[key], key, want))

    # (1) + (4) every build command sets every variable, each to the build cap.
    #
    # An empty build_stages is a PASS here, and it is a reachable normal case
    # rather than a degenerate input: a FlowSpec with
    # zero builds and only ready dies produces a manifest with no build:* stage
    # at all. Passing is honest, because this guard bounds resource use and a
    # run with no builds uses none.
    #
    # What an empty list alone cannot do is tell that apart from a regression
    # that DROPPED the build rows. So the rows are held to `die_plan.build`
    # below (written at S0 from the FlowSpec, never rewritten) instead of being
    # compared against zero. Where the plan is absent, that separation is simply
    # not available and the pass says so rather than implying it was made.
    #
    # DELIBERATE DIVERGENCE, recorded on both sides: packaging-flow's verify
    # REFUSES a completed manifest with no die_plan, because it speaks only for
    # runs its own code produced, where the absence is a defect. This guard is
    # handed whatever it is pointed at, archived runs included, so absence here
    # means no intent was recorded and stays a pass. Neither reader's silence is
    # the other's verdict. packaging-flow's README verify paragraph says the same
    # thing from its end; if one side changes, change both.
    stages = manifest.get("stages")
    if not isinstance(stages, list):
        raise NoVerdict("manifest has no stages list")
    build_stages = [s for s in stages if is_build_stage(s, version)]
    unread = []
    for stage in build_stages:
        sid = stage.get("id")
        if failed_before_argv(stage, version):
            unread.append(str(sid or "").partition(":")[2])
            continue
        parsed = thread_values(build_command(stage, version), variables)
        for var in variables:
            if var not in parsed:
                failures.append("%s: build command does not set %s" % (sid, var))
        for var, vals in parsed.items():
            for v in vals:
                if v != btc:
                    failures.append(
                        "%s: %s=%d does not equal build_thread_cap %d" % (sid, var, v, btc))

    # (5) the build rows are the rows the run planned to build.
    #
    # packaging-flow writes die_plan at S0 from the FlowSpec and never rewrites
    # it, so it is the run's INTENT rather than a restatement of what happened,
    # which is the whole reason it can catch a dropped stage. Checking only the
    # `build` kind is deliberate: `admit` rows place no build command and cost
    # this guard's caps nothing, and packaging-flow's own verify leg (h) already
    # holds both kinds.
    # A RUNNING manifest has not finished adding its rows, so a planned build
    # with no row means "not yet", not "dropped". Holding it to the plan anyway
    # would fail a healthy run mid-flight with a confident and false reason,
    # which is worse than not checking. packaging-flow's leg (h) draws the same
    # line (`if m.state() != STATE_COMPLETED: return []`) and this guard has to
    # draw it in the same place or the two readers disagree about a manifest
    # neither of them should be judging yet.
    #
    # Found by running this against a manifest their writer produced rather than
    # one written here: the fixture version had no state field and passed.
    rows = []
    for stage in build_stages:
        _, _, instance = str(stage.get("id") or "").partition(":")
        rows.append(instance)
    completed = manifest.get("state") == STATE_COMPLETED
    planned = _planned_builds(manifest)
    if planned is not None and completed:
        for instance in planned:
            if instance not in rows:
                failures.append(
                    "the die plan names %s as a build and no build:%s row records it"
                    % (instance, instance))
        seen = []
        for instance in rows:
            if instance not in planned:
                failures.append("row build:%s is not in the die plan" % instance)
            elif instance in seen:
                failures.append("row build:%s is recorded twice" % instance)
            seen.append(instance)

    if summary is not None:
        summary["build_rows"] = list(rows)
        summary["unread_rows"] = list(unread)
        summary["planned_builds"] = None if planned is None else list(planned)
        summary["held_to_plan"] = planned is not None and completed
    return failures


def _planned_builds(manifest):
    """The instances the run planned to build, or None if it recorded no plan.

    Absent is NOT a failure and NOT a NoVerdict: it means no intent was
    recorded, which is every manifest written before chiplet-flow recorded a plan and
    anything this guard is handed out of an archive. Present but malformed IS a
    NoVerdict, because then an intent was recorded and this guard cannot read
    it, which is a different thing from there being none to read.
    """
    plan = manifest.get("die_plan")
    if plan is None:
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("build"), list) \
            or not all(isinstance(i, str) for i in plan["build"]):
        raise NoVerdict(
            "die_plan is present but its build entry is not a list of instance "
            "names, so the rows cannot be held to it")
    return plan["build"]


def mem_available_kb(meminfo_text):
    """MemAvailable in KiB out of /proc/meminfo text. None if the line is absent."""
    for line in meminfo_text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def ram_probe(meminfo_text, floor_gb=MIN_FREE_GB):
    """Return [failure, ...] for the RAM floor. Raise NoVerdict if unreadable."""
    kb = mem_available_kb(meminfo_text)
    if kb is None:
        raise NoVerdict("MemAvailable not present in the meminfo text")
    floor_kb = floor_gb * 1024 * 1024
    if kb < floor_kb:
        return ["MemAvailable %.1f GB is below the floor of %d GB"
                % (kb / 1024.0 / 1024.0, floor_gb)]
    return []


def self_test():
    cases = []

    def expect(name, ok):
        cases.append((name, bool(ok)))

    build_cmd = [
        "nix-shell", "--run",
        "cd /w && _OPENLANE_MAX_CORES=8 OMP_NUM_THREADS=8 OPENROAD_NUM_THREADS=8 "
        "librelane -c DRT_THREADS=8 -c KLAYOUT_DRC_THREADS=8 -c KLAYOUT_DENSITY_THREADS=8 "
        "-c STA_THREADS=8 --run-tag RUN_x --to Yosys.Synthesis config.yaml"]

    def manifest(tc=28, btc=8, cmd=build_cmd, tools=True, state="completed"):
        m = {"identity": {"thread_cap": tc, "build_thread_cap": btc},
             "state": state,
             "stages": [{"id": "elaborate", "command": None},
                        {"id": "build:U1", "command": cmd},
                        {"id": "route", "command": ["python3", "-m", "x"]}]}
        if tools:
            m["tools"] = {"thread_cap": tc, "build_thread_cap": btc}
        return m

    # Parsing.
    vals = thread_values(build_cmd)
    expect("every one of the seven thread vars is parsed",
           set(vals) == set(BUILD_THREAD_VARS))
    expect("each parsed value is the cap", all(v == [8] for v in vals.values()))
    expect("a longer-named variable does not shadow a shorter one",
           thread_values(["FOO_STA_THREADS=3 STA_THREADS=8"]).get("STA_THREADS") == [8])

    # The happy path, on a host with room.
    expect("a compliant run at cpu 32 passes",
           check_manifest(manifest(), 32) == [])

    # (1) a value above the cap.
    over = ("cd /w && _OPENLANE_MAX_CORES=8 OMP_NUM_THREADS=8 OPENROAD_NUM_THREADS=8 "
            "librelane -c DRT_THREADS=16 -c KLAYOUT_DRC_THREADS=8 "
            "-c KLAYOUT_DENSITY_THREADS=8 -c STA_THREADS=8 config.yaml")
    fails = check_manifest(manifest(cmd=["nix-shell", "--run", over]), 32)
    expect("a DRT_THREADS above the cap fails",
           any("DRT_THREADS=16" in f for f in fails))

    # (4) a missing variable.
    miss = ("cd /w && OMP_NUM_THREADS=8 OPENROAD_NUM_THREADS=8 librelane "
            "-c DRT_THREADS=8 -c KLAYOUT_DRC_THREADS=8 -c KLAYOUT_DENSITY_THREADS=8 "
            "-c STA_THREADS=8 config.yaml")
    fails = check_manifest(manifest(cmd=["nix-shell", "--run", miss]), 32)
    expect("a build command missing _OPENLANE_MAX_CORES fails",
           any("does not set _OPENLANE_MAX_CORES" in f for f in fails))

    # (3) the cap above what the host can give.
    fails = check_manifest(manifest(tc=28, btc=8), 30)
    expect("thread_cap 28 on a 30-core host fails (needs <= 26)",
           any("exceeds host cpu_count" in f for f in fails))

    # (2) build cap above the absolute ceiling, and above the runner cap.
    expect("build_thread_cap above MAX_BUILD_THREADS fails",
           any("MAX_BUILD_THREADS" in f for f in check_manifest(manifest(tc=28, btc=12), 32)))
    expect("build_thread_cap above thread_cap fails",
           any("exceeds thread_cap" in f
               for f in check_manifest(manifest(tc=4, btc=8), 32)))

    # (5) the duplicate disagreeing.
    m = manifest()
    m["tools"]["thread_cap"] = 99
    expect("a tools.thread_cap that disagrees with identity fails",
           any("disagrees" in f for f in check_manifest(m, 32)))

    # NoVerdict, not a pass, when the host count is unknown.
    try:
        check_manifest(manifest(), None)
        expect("a missing host core count is NoVerdict, not a pass", False)
    except NoVerdict:
        expect("a missing host core count is NoVerdict, not a pass", True)

    # NoVerdict on a malformed manifest.
    try:
        check_manifest({"stages": []}, 32)
        expect("a manifest with no identity is NoVerdict", False)
    except NoVerdict:
        expect("a manifest with no identity is NoVerdict", True)

    # A manifest with no build stages passes: there is nothing to bound and
    # saying so is honest. This is a real shape, not only a degenerate one, so
    # it must not raise.
    def plain(stages, plan=None, state="completed"):
        m = {"identity": {"thread_cap": 28, "build_thread_cap": 8},
             "stages": stages, "state": state}
        if plan is not None:
            m["die_plan"] = plan
        return m

    no_builds = [{"id": "spec", "command": None}, {"id": "admit:U1", "command": None}]
    expect("a manifest with no build stages passes",
           check_manifest(plain(no_builds), 32) == [])

    # (5) the rows against the plan. THE PAIR BELOW IS THE POINT OF THE FIELD:
    # the same build-less manifest passes or fails on what the run intended,
    # and without the plan both of them pass identically.
    expect("no build rows, and the plan wanted none, passes",
           check_manifest(plain(no_builds, {"build": [], "admit": ["U1"]}), 32) == [])
    expect("no build rows, but the plan wanted one, FAILS",
           check_manifest(plain(no_builds, {"build": ["U1"], "admit": []}), 32)
           == ["the die plan names U1 as a build and no build:U1 row records it"])

    expect("a build row matching the plan passes",
           check_manifest(dict(manifest(), die_plan={"build": ["U1"], "admit": []}), 32) == [])
    expect("a build row absent from the plan fails",
           "row build:U1 is not in the die plan"
           in check_manifest(dict(manifest(), die_plan={"build": [], "admit": []}), 32))
    twice = manifest()
    twice["stages"] = twice["stages"] + [{"id": "build:U1", "command": build_cmd}]
    expect("the same build row recorded twice fails",
           "row build:U1 is recorded twice"
           in check_manifest(dict(twice, die_plan={"build": ["U1"], "admit": []}), 32))

    # Absent is no intent recorded, and stays a pass: every manifest written
    # before chiplet-flow recorded a plan lacks the field, and this guard is handed
    # archived runs. packaging-flow's verify refuses the same absence on purpose.
    expect("an absent die_plan is still a pass",
           check_manifest(manifest(), 32) == [])
    # Present but unreadable is a different thing from absent: an intent WAS
    # recorded and cannot be held to, which is the definition of NoVerdict.
    for bad in ({"build": "U1"}, {"build": [1]}, "nope"):
        try:
            check_manifest(dict(manifest(), die_plan=bad), 32)
            expect("a malformed die_plan %r is NoVerdict" % (bad,), False)
        except NoVerdict:
            expect("a malformed die_plan %r is NoVerdict" % (bad,), True)

    # A RUNNING manifest is not held to the plan at all: its missing rows are
    # rows it has not reached. Without this the guard fails a healthy run
    # mid-flight, which is a confident false reason, the worst of the three
    # answers. Found on a manifest chiplet-flow's own writer produced; the
    # fixture here had no state field and passed either way.
    expect("a running manifest is NOT held to the plan",
           check_manifest(plain(no_builds, {"build": ["U1"], "admit": []},
                                state="running"), 32) == [])
    expect("the same manifest completed IS held to the plan",
           check_manifest(plain(no_builds, {"build": ["U1"], "admit": []},
                                state="completed"), 32) != [])

    # The summary is what lets the caller say what was bounded rather than only
    # that nothing failed.
    seen = {}
    check_manifest(plain(no_builds, {"build": [], "admit": ["U1"]}), 32, summary=seen)
    expect("the summary reports zero build rows against an empty plan",
           seen == {"build_rows": [], "unread_rows": [], "planned_builds": [],
                    "held_to_plan": True})
    seen = {}
    check_manifest(manifest(), 32, summary=seen)
    expect("the summary distinguishes no plan from an empty plan",
           seen == {"build_rows": ["U1"], "unread_rows": [], "planned_builds": None,
                    "held_to_plan": False})

    # Versions. The fixtures above carry neither key, so they are legacy
    # manifests and read the top-level command; every one of them still does.
    def versioned(version, row, plan=None, btc=8):
        m = {"format": MANIFEST_FORMAT, "version": version,
             "identity": {"thread_cap": 28, "build_thread_cap": btc},
             "state": "completed",
             "stages": [{"id": "elaborate", "command": None}, row]}
        if plan is not None:
            m["die_plan"] = plan
        return m

    for v in COMMAND_VERSIONS:
        expect("a version-%d build row is read from its command" % v,
               check_manifest(versioned(v, {"id": "build:U1", "command": build_cmd}), 32)
               == [])
    lying_fact = {"id": "build:U1", "command": build_cmd,
                  "facts": {LIBRELANE_COMMAND_FACT: json.dumps(
                      ["nix-shell", "--run", over])}}
    expect("a version-2 row does not read the librelane_command fact",
           check_manifest(versioned(2, lying_fact), 32) == [])

    # Version 3: the fact is the LibreLane argv and the top-level command is
    # the build-die argv. The door's argv below sets a thread value over the cap
    # on purpose; it passes because it is not the argv that is held to the cap.
    door = ["chiplet-flow", "build-die", "--instance", "U1", "DRT_THREADS=99"]

    def v3_row(fact, command=door):
        row = {"id": "build:U1", "command": command, "facts": {}}
        if fact is not None:
            row["facts"][LIBRELANE_COMMAND_FACT] = fact
        return row

    expect("a version-3 row is read from facts.librelane_command, not its command",
           check_manifest(versioned(3, v3_row(json.dumps(build_cmd))), 32) == [])
    fails = check_manifest(versioned(3, v3_row(json.dumps(["nix-shell", "--run", over]))), 32)
    expect("a version-3 fact with DRT_THREADS above the cap fails",
           any("DRT_THREADS=16" in f for f in fails))
    fails = check_manifest(versioned(3, v3_row(json.dumps(["nix-shell", "--run", miss]))), 32)
    expect("a version-3 fact missing _OPENLANE_MAX_CORES fails",
           any("does not set _OPENLANE_MAX_CORES" in f for f in fails))

    # The hazard the dispatch exists for: a version-3 row with no top-level
    # command is still a build row. Held to the plan it must neither vanish
    # (a false "no build:U1 row") nor, without the fact, pass unread.
    expect("a version-3 row with no command is selected by its id",
           check_manifest(versioned(3, v3_row(json.dumps(build_cmd), command=None),
                                    plan={"build": ["U1"], "admit": []}), 32) == [])
    for name, fact, command in (
            ("without the fact", None, door),
            ("without the fact or a command", None, None),
            ("whose fact is not JSON", "nix-shell --run x", door),
            ("whose fact is a JSON string", json.dumps("nix-shell"), door),
            ("whose fact is an empty list", "[]", door),
            ("whose fact holds a non-string", json.dumps(["nix-shell", 8]), door),
            ("whose fact holds an empty string", json.dumps(["nix-shell", ""]), door),
            ("whose fact is a list, not its JSON", list(build_cmd), door)):
        try:
            check_manifest(versioned(3, v3_row(fact, command=command),
                                     plan={"build": ["U1"], "admit": []}), 32)
            expect("a version-3 row %s is NoVerdict" % name, False)
        except NoVerdict:
            expect("a version-3 row %s is NoVerdict" % name, True)

    # A version-3 row that failed before LibreLane recorded an argv is set
    # aside, not a NoVerdict for the whole run: nothing ran that the cap could
    # bound. It is still a row, so the plan still sees it, and the summary
    # names it so the pass does not claim it was checked.
    def failed(code, fact=None):
        row = v3_row(fact)
        row["exit_code"] = code
        return row

    plan_u1 = {"build": ["U1"], "admit": []}
    seen = {}
    expect("a failed version-3 row without the fact is skipped",
           check_manifest(versioned(3, failed(1), plan=plan_u1), 32, summary=seen) == [])
    expect("the skipped row is still a row, and is named as unread",
           seen.get("build_rows") == ["U1"] and seen.get("unread_rows") == ["U1"])
    fails = check_manifest(versioned(3, failed(1, json.dumps(["nix-shell", "--run", over]))), 32)
    expect("a failed version-3 row with the fact is read, and over the cap fails",
           any("DRT_THREADS=16" in f for f in fails))
    for name, row in (
            ("exit 0", failed(0)),
            ("an exit code of true", failed(True)),
            ("a string exit code", failed("1")),
            ("exit 1 and an unreadable fact", failed(1, "nix-shell --run x"))):
        try:
            check_manifest(versioned(3, row, plan=plan_u1), 32)
            expect("a version-3 row with %s and no readable fact is NoVerdict" % name,
                   False)
        except NoVerdict:
            expect("a version-3 row with %s and no readable fact is NoVerdict" % name,
                   True)

    # A row skipped by its tier, in the shape packaging-flow writes it: the
    # door's argv, no exit code, empty facts, and the skip reason. It did not
    # fail and did not run, so it is not set aside and stays a NoVerdict.
    tier_skipped = {"id": "build:U1", "command": door, "facts": {},
                    "exit_code": None, "skipped_by_tier": "hermetic tier builds no die"}
    try:
        check_manifest(versioned(3, tier_skipped, plan=plan_u1), 32)
        expect("a version-3 row skipped by its tier is NoVerdict", False)
    except NoVerdict:
        expect("a version-3 row skipped by its tier is NoVerdict", True)

    # The hermetic twin: the fake plan reports build_thread_cap null, so a
    # version-3 hermetic run stays NoVerdict before any row is read.
    try:
        check_manifest(versioned(3, v3_row(json.dumps(build_cmd)), btc=None), 32)
        expect("a version-3 manifest with build_thread_cap null is NoVerdict", False)
    except NoVerdict:
        expect("a version-3 manifest with build_thread_cap null is NoVerdict", True)

    # Version 4 builds as version 3 does; what it adds (the select answer, the
    # gate's signature) is not read here, so a v4 manifest carrying them is read
    # by its fact exactly like a v3 one.
    def v4(row):
        m = versioned(4, row)
        m["cosim_select"] = None
        m["gate"] = {"status_sha256": "0" * 64, "ack_sha256": "1" * 64}
        return m

    def read4(row):
        """check_manifest's failures, or ["NoVerdict: ..."] when it gives none."""
        try:
            return check_manifest(v4(row), 32)
        except NoVerdict as exc:
            return ["NoVerdict: %s" % exc]

    expect("a version-4 row is read from facts.librelane_command, not its command",
           read4(v3_row(json.dumps(build_cmd))) == [])
    # Named mutation: a version-4 fact over the cap. Read the version-2 way it
    # would pass on the door's command alone.
    fails = read4(v3_row(json.dumps(["nix-shell", "--run", over])))
    expect("a version-4 fact with DRT_THREADS above the cap fails",
           any("DRT_THREADS=16" in f for f in fails))
    expect("a version-4 row with no command is selected by its id",
           read4(v3_row(json.dumps(build_cmd), command=None)) == [])
    expect("a version-4 row with no fact and no exit code is NoVerdict",
           "librelane_command" in "".join(read4(v3_row(None))))

    # Version 5 keeps version 4's build rows, so each row reads exactly as it
    # does in a version-4 manifest: the same failures, or the same NoVerdict,
    # which names the manifest's own version.
    def read5(row):
        m = v4(row)
        m["version"] = 5
        try:
            return check_manifest(m, 32)
        except NoVerdict as exc:
            return ["NoVerdict: %s" % exc]

    for name, row in (
            ("a fact under the cap", v3_row(json.dumps(build_cmd))),
            ("a fact over the cap", v3_row(json.dumps(["nix-shell", "--run", over]))),
            ("no command", v3_row(json.dumps(build_cmd), command=None)),
            ("no fact and no exit code", v3_row(None))):
        expect("a version-5 row with %s reads as version 4" % name,
               read5(row) == [f.replace("version-4", "version-5") for f in read4(row)])
    expect("a version-5 fact with DRT_THREADS above the cap fails",
           any("DRT_THREADS=16" in f
               for f in read5(v3_row(json.dumps(["nix-shell", "--run", over])))))

    # A format or version this guard does not know is refused, never guessed.
    for name, keys in (
            ("version 6", {"format": MANIFEST_FORMAT, "version": 6}),
            ("version 100", {"format": MANIFEST_FORMAT, "version": 100}),
            ("version \"3\" as a string", {"format": MANIFEST_FORMAT, "version": "3"}),
            ("version true", {"format": MANIFEST_FORMAT, "version": True}),
            ("another format", {"format": "other.manifest", "version": 2}),
            ("a format with no version", {"format": MANIFEST_FORMAT}),
            ("a version with no format", {"version": 2})):
        try:
            check_manifest(dict(manifest(), **keys), 32)
            expect("a manifest with %s is NoVerdict" % name, False)
        except NoVerdict:
            expect("a manifest with %s is NoVerdict" % name, True)

    # RAM probe.
    expect("MemAvailable above the floor passes",
           ram_probe("MemAvailable: %d kB\n" % (12 * 1024 * 1024)) == [])
    expect("MemAvailable below the floor fails",
           bool(ram_probe("MemAvailable: %d kB\n" % (8 * 1024 * 1024))))
    try:
        ram_probe("MemFree: 100 kB\n")
        expect("meminfo without MemAvailable is NoVerdict", False)
    except NoVerdict:
        expect("meminfo without MemAvailable is NoVerdict", True)

    ok = True
    for name, passing in cases:
        print("%-62s %s" % (name, "ok" if passing else "FAILED"))
        ok = ok and passing
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("manifest", nargs="?")
    ap.add_argument("--cpu-count", type=int, default=None)
    ap.add_argument("--ram-probe", action="store_true")
    ap.add_argument("--floor-gb", type=int, default=MIN_FREE_GB)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.ram_probe:
        try:
            failures = ram_probe(pathlib.Path("/proc/meminfo").read_text(), args.floor_gb)
        except (OSError, NoVerdict) as e:
            print("NO VERDICT: %s" % e, file=sys.stderr)
            return 2
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        if failures:
            return 1
        print("MemAvailable is at or above the %d GB floor" % args.floor_gb)
        return 0

    if not args.manifest:
        ap.error("a manifest path is required unless --self-test or --ram-probe is given")

    cpu = args.cpu_count
    if cpu is None and os.environ.get("CHIPLET_FLOW_HOST_CPUS", "").isdigit():
        cpu = int(os.environ["CHIPLET_FLOW_HOST_CPUS"])

    summary = {}
    try:
        manifest = json.loads(pathlib.Path(args.manifest).read_text())
        failures = check_manifest(manifest, cpu, summary=summary)
    except (OSError, json.JSONDecodeError) as e:
        print("NO VERDICT: %s: %s" % (args.manifest, e), file=sys.stderr)
        return 2
    except NoVerdict as e:
        print("NO VERDICT: %s" % e, file=sys.stderr)
        return 2

    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    # Say what was bounded, not just that nothing failed. "thread caps honoured
    # across every build stage" is true of a manifest with no build stages, and
    # a reader cannot tell that sentence from a completed check.
    rows = summary.get("build_rows", [])
    unread = summary.get("unread_rows", [])
    read = [r for r in rows if r not in unread]
    planned = summary.get("planned_builds")
    print("thread caps honoured across %d build stage(s)%s"
          % (len(read), (": " + ", ".join(read)) if read else ""))
    if unread:
        print("not held to the cap, because they failed before recording a "
              "LibreLane argv: %s" % ", ".join(unread))
    if planned is None:
        print("this manifest records no die plan, so the rows were not held to "
              "an intent; packaging-flow's verify refuses that absence, this "
              "guard does not (see check_manifest)")
    elif not summary.get("held_to_plan"):
        print("the run is not completed, so its rows were NOT held to the die "
              "plan: a build it has not reached yet is not a build it dropped")
    else:
        print("and they are the %d build(s) the run planned" % len(planned))
    return 0


if __name__ == "__main__":
    sys.exit(main())
