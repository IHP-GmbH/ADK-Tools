#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The staged repositories' copies of the interposer PDK's KiCad rule templates are the PDK's.

interposer-pnr's export writes a board's .kicad_dru as the PDK's
libs.tech/kicad/interposer_template.kicad_dru, unchanged, followed by its own
per-net rules, and copies the .kicad_pro beside it. Its tests measure that
against a frozen copy of the pair, and packaging-flow's tests read a fixture
copy. Each copy mirrors the PDK's layout under its own root, so a copy root
holds libs.tech/kicad/interposer_template.{kicad_dru,kicad_pro} as the PDK
does. This check holds every copy named to the PDK's bytes: a copy that lags
the PDK would pass its owner's suite against rules the image does not ship.

Only the copies named are compared. A copy that is missing, a symlink or not
the PDK's bytes fails, naming its label, as check_attach_plane.py's --pdk-copy
does for the stack descriptor; a template the PDK lacks fails once and its
copies are not compared. The image build names the staged copies; CI
here has no staged trees and runs the self-test only.

Usage:
    check_kicad_templates.py --pdk /opt/adk-tools/OpenIntM4TM2 \\
        --copy interposer-pnr=DIR --copy packaging-flow=DIR
    check_kicad_templates.py --self-test
"""

import argparse
import hashlib
import pathlib
import sys
import tempfile

TEMPLATES = ("libs.tech/kicad/interposer_template.kicad_dru",
             "libs.tech/kicad/interposer_template.kicad_pro")


def labelled(specs):
    """{label: root} from LABEL=DIR strings; raises ValueError naming the bad one."""
    out = {}
    for spec in specs:
        label, sep, root = spec.partition("=")
        if not sep or not label or not root:
            raise ValueError("--copy takes LABEL=DIR, not %s" % spec)
        if label in out:
            raise ValueError("--copy %s given twice" % label)
        out[label] = root
    return out


def digest(data):
    return hashlib.sha256(data).hexdigest()[:12]


def check(pdk, copies):
    """[failure] for every copy root against the PDK's templates."""
    failures = []
    for rel in TEMPLATES:
        ref = pathlib.Path(pdk) / rel
        if not ref.is_file():
            failures.append("the PDK %s has no %s" % (pdk, rel))
            continue
        want = ref.read_bytes()
        for label, root in sorted(copies.items()):
            copy = pathlib.Path(root) / rel
            if not copy.is_file() or copy.is_symlink():
                failures.append("%s: %s is missing or not a regular file" % (label, copy))
            elif copy.read_bytes() != want:
                failures.append("%s: %s is not the PDK's bytes (sha256 %s, the PDK's %s)"
                                % (label, copy, digest(copy.read_bytes()), digest(want)))
    return failures


def self_test():
    cases = []
    LINK = object()

    def expect(name, ok):
        cases.append((name, ok))

    def run(copy_bytes, pdk_has=True):
        """copy_bytes maps a label to rel -> bytes, None (no files) or LINK."""
        with tempfile.TemporaryDirectory() as tmp:
            t = pathlib.Path(tmp)
            for rel in TEMPLATES:
                if pdk_has:
                    (t / "pdk" / rel).parent.mkdir(parents=True, exist_ok=True)
                    (t / "pdk" / rel).write_bytes(b"pdk " + rel.encode())
                for label, data in copy_bytes.items():
                    if data is None:
                        continue
                    (t / label / rel).parent.mkdir(parents=True, exist_ok=True)
                    if data is LINK:
                        (t / label / rel).symlink_to(t / "pdk" / rel)
                    else:
                        (t / label / rel).write_bytes(data(rel))
            return check(t / "pdk", {label: t / label for label in copy_bytes})

    same = lambda rel: b"pdk " + rel.encode()
    expect("copies equal to the PDK pass", run({"a": same, "b": same}) == [])
    failures = run({"a": same, "b": lambda rel: b"older " + rel.encode()})
    expect("a copy that differs fails, naming its label",
           len(failures) == 2 and all(f.startswith("b: ") for f in failures))
    failures = run({"a": None})
    expect("a copy root without the files fails", len(failures) == 2
           and all("missing or not a regular file" in f for f in failures))
    failures = run({"a": LINK})
    expect("a copy that links to the PDK's file fails", len(failures) == 2
           and all("not a regular file" in f for f in failures))
    failures = run({"a": same}, pdk_has=False)
    expect("a PDK without the templates fails", len(failures) == 2
           and all("has no" in f for f in failures))
    for bad, why in ((["x"], "LABEL=DIR"), (["a=x", "a=y"], "given twice")):
        try:
            labelled(bad)
            ok = False
        except ValueError as e:
            ok = why in str(e)
        expect("--copy %s is refused" % " ".join(bad), ok)

    for name, ok in cases:
        print("%-60s %s" % (name, "ok" if ok else "FAIL"))
    return 0 if all(ok for _, ok in cases) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pdk", help="the interposer PDK tree")
    ap.add_argument("--copy", action="append", default=[], metavar="LABEL=DIR",
                    help="a copy root mirroring the PDK's layout")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.pdk or not args.copy:
        ap.error("--pdk and at least one --copy are required")
    try:
        copies = labelled(args.copy)
    except ValueError as e:
        ap.error(str(e))
    failures = check(args.pdk, copies)
    for failure in failures:
        print("FAIL: %s" % failure, file=sys.stderr)
    if failures:
        return 1
    print("the KiCad rule templates in %s are the PDK's" % ", ".join(sorted(copies)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
