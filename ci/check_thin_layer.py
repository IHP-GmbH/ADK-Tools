#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Checks the part of the Dockerfile below the thin-layer line: our code on the tools image, and nothing fetched.

ci/tools_key.py keys the tools image on the part above the line. A change below
it rebuilds only what is below, on the same tools image, so whatever stands
there must not bring in a tool by another way: nothing installs from apt,
nothing downloads, and pip installs only with no index. Below the line:

  - every FROM names ${TOOLS_IMAGE} or a stage defined below the line, and every
    COPY --from such a stage, so a build below the line needs no stage above it;
  - there is no ADD;
  - no RUN installs from apt (apt-get, apt, aptitude, dpkg -i, the snapshot
    script), downloads (curl, wget, git clone/fetch/pull/ls-remote/submodule
    update), or runs pip install/download without --no-index;
  - every RUN of the runtime stage carries --network=none.

studio-builder is the one stage whose RUNs may reach the network: Chiplet
Studio's CMake fetches its pinned dependencies (nlohmann_json, pybind11,
googletest). The rules above but the last still hold for it.

Usage:
    check_thin_layer.py [DOCKERFILE]
    check_thin_layer.py --self-test
"""

import argparse
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tools_key  # noqa: E402

RUNTIME = "runtime"
FETCH = [
    (re.compile(r"(?:^|[\s;&|(])(?:apt-get|apt|aptitude)\s"), "installs from apt"),
    (re.compile(r"\bdpkg\s+(?:-i|--install)\b"), "installs a package with dpkg"),
    (re.compile(r"apt-snapshot"), "runs the apt snapshot script"),
    (re.compile(r"\b(?:curl|wget)\b"), "downloads"),
    (re.compile(r"\bgit\s+(?:clone|fetch|pull|ls-remote|submodule\s+update)\b"),
     "fetches with git"),
]
PIP = re.compile(r"\bpip3?\s+(?:install|download)\b|-m\s+pip\s+(?:install|download)\b")


def stage_of(body):
    m = re.search(r"\s+AS\s+(\S+)\s*$", body, re.IGNORECASE)
    return m.group(1) if m else None


def check(text):
    try:
        _, below = tools_key.split(text)
    except tools_key.ToolsKeyError as exc:
        return [str(exc)]
    failures, stages, stage = [], set(), None
    for inst in tools_key.instructions(below):
        keyword, _, body = inst.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            words = body.split()
            base = tools_key.flags(words)[1][0] if tools_key.flags(words)[1] else ""
            stage = stage_of(body)
            if base != "${TOOLS_IMAGE}" and base not in stages:
                failures.append("FROM %s: below the line a stage starts from ${TOOLS_IMAGE} "
                                "or a stage defined below the line" % base)
            if stage:
                stages.add(stage)
            continue
        if keyword == "ADD":
            failures.append("%s (stage %s): no ADD below the line" % (inst[:80], stage))
            continue
        if keyword == "COPY":
            for f in tools_key.flags(body.split())[0]:
                if f.startswith("--from=") and f[len("--from="):] not in stages:
                    failures.append("%s (stage %s): COPY --from a stage above the line, or an "
                                    "image" % (inst[:80], stage))
            continue
        if keyword != "RUN":
            continue
        fl, rest = tools_key.flags(body.split())
        script = " ".join(rest)
        for pattern, what in FETCH:
            if pattern.search(body):
                failures.append("%s (stage %s): %s below the line" % (inst[:80], stage, what))
        for m in PIP.finditer(script):
            command = re.split(r"&&|\|\||;|\|", script[m.start():])[0]
            if "--no-index" not in command.split():
                failures.append("%s (stage %s): pip %s without --no-index below the line"
                                % (inst[:80], stage, m.group(0).split()[-1]))
        if stage == RUNTIME and "--network=none" not in fl:
            failures.append("%s: a RUN of the %s stage without --network=none"
                            % (inst[:80], RUNTIME))
    if RUNTIME not in stages:
        failures.append("no %s stage below the line" % RUNTIME)
    return failures


def self_test():
    head = ("FROM ubuntu AS deps\n"
            "RUN sh /run/apt-snapshot/apt-snapshot.sh x vim\n"
            "FROM deps AS tools\n"
            "RUN pip install x==1\n"
            "#### thin layer: below\n"
            "FROM ${TOOLS_IMAGE} AS studio-builder\n"
            "RUN cmake .. && make\n"
            "FROM ${TOOLS_IMAGE} AS runtime\n"
            "COPY --from=studio-builder /a /a\n")
    good = head + (
        "RUN --network=none /v/pip install --no-cache-dir --no-index \\\n"
        "        --find-links /w -e /s \\\n"
        "    && /v/pip check\n"
        "RUN --network=none git config --system --add safe.directory /s\n"
        "FROM runtime AS verify\n"
        "RUN pytest\n")
    cases = [
        ("the shape this Dockerfile has", good, None),
        ("apt above the line is fine, apt below is not",
         good.replace("RUN pytest", "RUN apt-get install -y x"), "installs from apt"),
        ("the snapshot script below", good.replace(
            "RUN pytest", "RUN --mount=type=bind,source=docker/apt-snapshot,target=/r sh /r/x"),
         "snapshot script"),
        ("dpkg -i", good.replace("RUN pytest", "RUN dpkg -i x.deb"), "dpkg"),
        ("curl", good.replace("RUN cmake", "RUN curl -O x && cmake"), "downloads"),
        ("git clone", good.replace("RUN pytest", "RUN git clone x"), "fetches with git"),
        ("pip without --no-index", good.replace("--no-cache-dir --no-index", "--no-cache-dir"),
         "without --no-index"),
        ("python -m pip download", good.replace("RUN pytest", "RUN python3 -m pip download x"),
         "without --no-index"),
        ("--no-index on another command does not count", good.replace(
            "RUN pytest", "RUN pip install x && echo --no-index"), "without --no-index"),
        ("a runtime RUN without --network=none", good.replace(
            "RUN --network=none git config", "RUN git config"), "--network=none"),
        ("ADD", good.replace("RUN pytest", "ADD x.tar /"), "no ADD"),
        ("FROM a stage above the line", good.replace(
            "FROM ${TOOLS_IMAGE} AS studio-builder", "FROM deps AS studio-builder"), "FROM deps"),
        ("COPY --from a stage above the line", good.replace(
            "--from=studio-builder", "--from=tools"), "COPY --from"),
        ("COPY --from an image", good.replace(
            "--from=studio-builder", "--from=ubuntu:24.04"), "COPY --from"),
        ("no line", good.replace("#### thin layer: below\n", ""), "exactly one"),
        ("no runtime stage", good.replace("AS runtime", "AS rt").replace(
            "FROM runtime", "FROM rt"), "no runtime stage"),
    ]
    failed = 0
    for name, text, expect in cases:
        got = check(text)
        ok = not got if expect is None else any(expect in f for f in got)
        if not ok:
            failed += 1
            print("FAIL: %s: %s" % (name, got or "no failure"), file=sys.stderr)
    print("%d/%d self-test cases passed" % (len(cases) - failed, len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dockerfile", nargs="?", default=str(HERE.parent / "Dockerfile"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    failures = check(pathlib.Path(a.dockerfile).read_text())
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("%s: below the thin-layer line nothing installs from apt or downloads, pip "
          "installs with no index, and every runtime RUN has no network" % a.dockerfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
