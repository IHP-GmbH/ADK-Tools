#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The gf180mcuD DRC deck resolves below $PDK_ROOT, as chiplet.drc reads it.

chiplet.drc finds a die's deck at $PDK_ROOT/<pdk>/libs.tech/klayout. The image
keeps the GF180 tree at /opt/adk-tools/gf180mcu and makes $PDK_ROOT/gf180mcuD
a link to its gf180mcuD folder, so three things must hold there:

  resolves  chiplet.drc's own deck_files, imported from the staged
            packaging-flow the venv runs, resolves tech/drc/gf180mcu.drc to
            its file set below the tree;
  files     that set holds the number of files given (--files), so a deck
            that loses or gains a file it reads is seen;
  links     nothing below the tree is a link: the kind refuses one there, and
            the link that makes the tree reachable sits above it.

Usage:
    check_gf180_deck.py --files N [--root DIR]   (default: $PDK_ROOT)
    check_gf180_deck.py --self-test
Exit 0 when all three hold, 1 when one does not, 2 on usage.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

PDK = "gf180mcuD"
DECK = "tech/drc/gf180mcu.drc"


def links_below(tree):
    """Every link below tree, as a path relative to it, sorted."""
    found = []
    for folder, dirs, files in os.walk(tree):
        for name in dirs + files:
            path = os.path.join(folder, name)
            if os.path.islink(path):
                found.append(os.path.relpath(path, tree))
    return sorted(found)


def check(root, want, deck_files, deck_tree):
    """The failures, one line each, of the three frames below root."""
    tree = Path(root).joinpath(PDK, *deck_tree)
    if not tree.is_dir():
        return [f"resolves: {tree} is not a folder"]
    failures = []
    files = deck_files(tree, DECK)
    if not isinstance(files, list):
        failures.append(f"resolves: {DECK} below {tree}: {getattr(files, 'why', files)}")
    else:
        print(f"gf180 deck: {DECK} below {tree}: {len(files)} files")
        if len(files) != want:
            failures.append(f"files: {len(files)}, not {want}")
    failures += [f"links: {rel} below {tree} is a link" for rel in links_below(tree)]
    return failures


def self_test():
    deck_tree = ("libs.tech", "klayout")

    def deck_files(tree, deck):
        path = tree / deck
        if not path.is_file():
            return type("Unresolved", (), {"why": f"{deck}: not a regular file"})()
        return sorted(str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file())

    cases = 0
    with tempfile.TemporaryDirectory() as tmp:
        shipped = Path(tmp, "gf180mcu", PDK)
        drc = shipped.joinpath(*deck_tree, "tech", "drc")
        drc.mkdir(parents=True)
        (drc / "gf180mcu.drc").write_text("# deck\n")
        (drc / "rule.drc").write_text("# rule\n")
        root = Path(tmp, "pdk")
        root.mkdir()
        os.symlink(shipped, root / PDK)

        assert check(root, 2, deck_files, deck_tree) == [], "a linked tree above the deck holds"
        cases += 1
        assert check(root, 3, deck_files, deck_tree) == ["files: 2, not 3"], "a count off is red"
        cases += 1
        os.symlink(drc / "rule.drc", drc / "alias.drc")
        got = check(root, 3, deck_files, deck_tree)
        assert got == [f"links: tech/drc/alias.drc below {root / PDK / 'libs.tech/klayout'} "
                       "is a link"], got
        cases += 1
        os.remove(drc / "alias.drc")
        (drc / "gf180mcu.drc").unlink()
        got = check(root, 1, deck_files, deck_tree)
        assert len(got) == 1 and got[0].startswith("resolves: "), got
        cases += 1
        os.remove(root / PDK)
        got = check(root, 1, deck_files, deck_tree)
        assert len(got) == 1 and got[0].endswith("is not a folder"), got
        cases += 1
    print(f"check_gf180_deck self-test: {cases} cases OK")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--files", type=int)
    parser.add_argument("--root", default=os.environ.get("PDK_ROOT"))
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.files is None or not args.root:
        parser.print_usage(sys.stderr)
        return 2
    from packaging_flow.verification.kinds import chiplet_drc
    failures = check(args.root, args.files, chiplet_drc.deck_files, chiplet_drc.DECK_TREE)
    for line in failures:
        print(f"gf180 deck: {line}")
    print(f"gf180 deck: {'PASS' if not failures else 'FAIL'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
