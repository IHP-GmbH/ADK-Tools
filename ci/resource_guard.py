#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The resource floor, asserted from a run instead of trusted as a policy.

The shared host leaves 10 GB of RAM and 4 cores free at all times, and every
heavy tool stage is supposed to run inside a thread cap derived from that. A
policy that lives only in prose drifts: a stage that hardcodes a thread count,
or a cap computed against the wrong core count, oversubscribes the box and
nothing notices until someone else's job is starved.

This is the CI twin of that floor. It reads a chiplet-flow RunManifest
(<workdir>/manifest.json) and proves, from the recorded argv of every build
stage, that the run actually stayed inside the cap it declared:

  1. every thread value in every build command equals identity.build_thread_cap
  2. identity.build_thread_cap is at most MAX_BUILD_THREADS and at most
     identity.thread_cap
  3. identity.thread_cap is at most the host's core count minus MIN_FREE_CORES
     (the host core count is passed in; the manifest does not carry it)
  4. no build command is missing any of the thread variables it must set
  5. the identity caps and their tools.* duplicates agree

The RAM half of the floor is not per-stage data: it is a pre-dispatch probe.
Its CI twin is --ram-probe, a job-level check that MemAvailable is at least
MIN_FREE_GB before the heavy tier starts, so a runner that cannot honour the
floor refuses the tier rather than thrashing.

Exit codes are tri-state, like the other ci checkers: 0 every assertion holds,
1 an assertion failed so this run broke the floor, 2 no verdict was reachable
(manifest absent or malformed, or the host core count was not supplied, so the
host-cap assertion could not be evaluated). A missing verdict is never 0.

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


def is_build_stage(stage):
    return str(stage.get("id", "")).startswith("build:") and stage.get("command")


def check_manifest(manifest, cpu_count, variables=BUILD_THREAD_VARS):
    """Return [failure, ...]. Raise NoVerdict when a verdict is unreachable."""
    if not isinstance(manifest, dict):
        raise NoVerdict("manifest is not an object")
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
    stages = manifest.get("stages")
    if not isinstance(stages, list):
        raise NoVerdict("manifest has no stages list")
    build_stages = [s for s in stages if is_build_stage(s)]
    for stage in build_stages:
        sid = stage.get("id")
        parsed = thread_values(stage["command"], variables)
        for var in variables:
            if var not in parsed:
                failures.append("%s: build command does not set %s" % (sid, var))
        for var, vals in parsed.items():
            for v in vals:
                if v != btc:
                    failures.append(
                        "%s: %s=%d does not equal build_thread_cap %d" % (sid, var, v, btc))
    return failures


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

    def manifest(tc=28, btc=8, cmd=build_cmd, tools=True):
        m = {"identity": {"thread_cap": tc, "build_thread_cap": btc},
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

    # A manifest with no build stages passes vacuously only in the sense that
    # there is nothing to bound; that is honest, so it must not raise.
    expect("a manifest with no build stages passes",
           check_manifest({"identity": {"thread_cap": 28, "build_thread_cap": 8},
                           "stages": [{"id": "spec", "command": None}]}, 32) == [])

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

    try:
        manifest = json.loads(pathlib.Path(args.manifest).read_text())
        failures = check_manifest(manifest, cpu)
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
    print("thread caps honoured across every build stage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
