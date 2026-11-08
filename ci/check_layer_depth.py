#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The images the Dockerfile builds stay under the overlay layer-depth limit.

Docker's overlay2 driver stacks at most 128 layers under a step: a build whose
stage chain is deeper fails at the step that crosses it ("max depth exceeded"),
an hour in. Every COPY, ADD and RUN of a stage adds one layer (WORKDIR is
counted too, to be safe), a stage starts from its FROM's layers, and an image
from outside the file (the pinned ubuntu base) brings --external-layers of its
own. FROM ${ARG} resolves through the ARG's default, as `docker build` with no
--build-arg does: build.sh's TOOLS_IMAGE is the tools stage built for its key,
so the depth is the same.

Prints the depth of every target asked for (every named stage by default) and
fails when one reaches --limit. The default, 120, sits below each real limit:
overlay2 refuses the 129th layer, and `docker load` refuses an image of more
than 125, which matters for the archived runtime image.

Usage:
    check_layer_depth.py [--dockerfile FILE] [--limit N] [--external-layers N]
        [--target NAME ...]
    check_layer_depth.py --self-test
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dockerfile import instructions  # noqa: E402

LAYER_STEPS = ("COPY", "ADD", "RUN", "WORKDIR")
OVERLAY_MAX = 128
DEFAULT_LIMIT = 120


def stages(text):
    """{stage name: (base, layer steps)} and the ARG defaults before the first FROM."""
    args, found, current = {}, {}, None
    for word, rest in instructions(text):
        if word == "ARG" and current is None:
            name, _, default = rest.partition("=")
            args[name.strip()] = default.strip()
        elif word == "FROM":
            parts = [p for p in rest.split() if not p.startswith("--")]
            base = re.sub(r"\$\{(\w+)\}|\$(\w+)",
                          lambda m: args.get(m.group(1) or m.group(2), ""), parts[0])
            current = parts[2] if len(parts) >= 3 and parts[1].upper() == "AS" else None
            if current is None:
                current = "#%d" % len(found)
            found[current] = [base, 0]
        elif word in LAYER_STEPS and current is not None:
            found[current][1] += 1
    return found


def depth(found, target, external, seen=()):
    if target in seen:
        raise ValueError("a FROM cycle through %s" % target)
    if target not in found:
        return external
    base, steps = found[target]
    return depth(found, base, external, seen + (target,)) + steps


def check(text, targets, limit, external):
    found = stages(text)
    lines, failures = [], []
    for target in targets:
        if target not in found:
            failures.append("no stage %s in the Dockerfile" % target)
            continue
        n = depth(found, target, external)
        lines.append("%s: %d layers (limit %d, overlay maximum %d)"
                     % (target, n, limit, OVERLAY_MAX))
        if n >= limit:
            failures.append("%s is %d layers deep, at or over %d; fold COPYs or RUNs before adding"
                            " a step (overlay2 stops at %d)" % (target, n, limit, OVERLAY_MAX))
    return lines, failures


def self_test():
    text = """\
ARG BASE=ubuntu:24.04@sha256:00
ARG TOOLS=tools
FROM ${BASE} AS tools
RUN a \\
    && b
# COPY in a comment is no step
COPY x /x
FROM $TOOLS AS runtime
WORKDIR /w
COPY --from=tools /x /y
ENV A=1
LABEL b=2
FROM runtime AS verify
RUN true
FROM ${BASE}
RUN c
"""
    cases = []
    found = stages(text)
    cases.append(("the stages and their bases", {k: v[0] for k, v in found.items()} ==
                  {"tools": "ubuntu:24.04@sha256:00", "runtime": "tools", "verify": "runtime",
                   "#3": "ubuntu:24.04@sha256:00"}))
    cases.append(("continuations and comments are one step or none",
                  found["tools"][1] == 2))
    cases.append(("ENV and LABEL add no layer", found["runtime"][1] == 2))
    cases.append(("the chain adds up", depth(found, "verify", 1) == 1 + 2 + 2 + 1))
    lines, failures = check(text, ["verify"], 6, 1)
    cases.append(("at the limit fails", len(failures) == 1 and "6 layers deep" in failures[0]))
    lines, failures = check(text, ["verify", "runtime"], 7, 1)
    cases.append(("under it passes", failures == [] and lines[0].startswith("verify: 6 layers")))
    cases.append(("an unknown target fails", check(text, ["nope"], 7, 1)[1] == [
        "no stage nope in the Dockerfile"]))
    for name, ok in cases:
        print("%s: %s" % ("ok" if ok else "FAIL", name))
    failed = [n for n, ok in cases if not ok]
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dockerfile", default="Dockerfile")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--external-layers", type=int, default=1,
                    help="layers of an image the file does not build (the ubuntu base: 1)")
    ap.add_argument("--target", action="append", default=[])
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    with open(args.dockerfile) as f:
        text = f.read()
    targets = args.target or [name for name in stages(text) if not name.startswith("#")]
    lines, failures = check(text, targets, args.limit, args.external_layers)
    for line in lines:
        print(line)
    for failure in failures:
        print("FAIL: %s" % failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
