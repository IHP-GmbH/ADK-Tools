#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Every vendored chiplet_format_io the image holds is the staged reader, byte for byte.

The image installs one chiplet_format_io, editable off the staged chiplet-spec, and
a staged tree may carry a copy of it under a private name: the system description
does, in description/src/chiplet_system_description/_vendor/chiplet_format_io. Each tool decides which reader it
imports, so a copy is not redundant; what it must not be is different. A
copy that lags the reference reads the same document with another release's
rules, and nothing in the image would say so.

The rule compared by: a copy may leave out files the reference has, never add
one. Every regular file under a copy, at any depth, must be at the same relative
path under the reference with the same bytes. The description vendors
__init__.py alone, which is why this is a subset and not an equality of trees.
`__pycache__` directories are skipped on both sides (Python's bytecode cache,
not vendored source), and anything under a copy that is neither a regular file
nor a directory fails. The package wrapper above a copy
(chiplet_system_description/_vendor/__init__.py) is not part of the reader and is not
compared.

The declared list is checked for completeness too: every directory named
chiplet_format_io that holds an __init__.py anywhere under the staging root, or
under a --root, must be the reference or a declared copy, so a copy added to a
tree later fails here until it is declared. Copies outside staging are declared
by absolute path and must lie under a --root. The C++ reader's include
directory has no __init__.py and is not a Python copy.

Usage:
    check_vendored_reader.py STAGING --reference chiplet-spec/reference/python/chiplet_format_io \\
        --copy chiplet-spec/description/src/chiplet_system_description/_vendor/chiplet_format_io \\
        [--root /opt/adk-tools/gds_to_kicad \\
         --copy /opt/adk-tools/gds_to_kicad/_vendor/chiplet_format_io]
    check_vendored_reader.py --self-test
"""

import argparse
import os
import pathlib
import sys
import tempfile

PACKAGE = "chiplet_format_io"
SKIP_DIRS = {".git", "__pycache__"}


def files_under(root):
    """({relative path: Path} of regular files, [relative paths that are neither])."""
    found, odd = {}, []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        base = pathlib.Path(dirpath)
        for d in dirnames:
            if (base / d).is_symlink():
                odd.append((base / d).relative_to(root).as_posix())
        dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                odd.append(rel)
            else:
                found[rel] = path
    return found, odd


def python_copies(staging):
    """Relative paths of every chiplet_format_io directory holding an __init__.py."""
    staging = pathlib.Path(staging)
    hits = []
    for dirpath, dirnames, _ in os.walk(staging):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        base = pathlib.Path(dirpath)
        if base.name == PACKAGE and (base / "__init__.py").is_file():
            hits.append(base.relative_to(staging).as_posix())
    return hits


def _absolute(path):
    """An absolute, normalized path; symlinks are kept as written."""
    return pathlib.Path(os.path.abspath(path))


def check(staging, reference, copies, roots=()):
    """[failure] and [(copy, files compared)] for the declared layout.

    A copy is relative to staging, or absolute. Every path is made absolute and
    normalized before it is compared, so a '..' cannot carry a copy out of the
    trees the completeness scan walks; symlinks are not resolved, so paths are
    compared as written.
    """
    staging = _absolute(staging)
    failures, compared = [], []

    def show(path):
        return (path.relative_to(staging).as_posix()
                if path.is_relative_to(staging) else str(path))

    scanned = [staging]
    for root in map(_absolute, roots):
        if not root.is_dir():
            failures.append("%s: a --root that is not a directory" % root)
        elif not any(root.is_relative_to(base) for base in scanned):
            scanned.append(root)

    ref_root = _absolute(staging / reference)
    if not (ref_root / "__init__.py").is_file():
        return failures + ["the reference %s holds no __init__.py" % reference], compared
    ref_files, _ = files_under(ref_root)

    declared = {ref_root}
    for copy in copies:
        root = _absolute(staging / copy)  # an absolute copy replaces staging
        declared.add(root)
        if not any(root.is_relative_to(base) for base in scanned):
            failures.append("%s: under neither %s nor a --root, so no scan would "
                            "find a copy beside it" % (copy, staging))
            continue
        if not root.is_dir():
            failures.append("%s: not a directory" % copy)
            continue
        files, odd = files_under(root)
        for rel in odd:
            failures.append("%s/%s: not a regular file" % (copy, rel))
        if "__init__.py" not in files:
            failures.append("%s: holds no __init__.py, so there is no copy to "
                            "compare" % copy)
        for rel, path in sorted(files.items()):
            ref = ref_files.get(rel)
            if ref is None:
                failures.append("%s/%s: the reference %s has no such file"
                                % (copy, rel, reference))
            elif path.read_bytes() != ref.read_bytes():
                failures.append("%s/%s: differs from %s/%s"
                                % (copy, rel, reference, rel))
        compared.append((copy, len(files)))

    for base in scanned:
        for hit in python_copies(base):
            if base / hit not in declared:
                failures.append("%s: a chiplet_format_io package this check was not "
                                "told about, so it would not be compared" % show(base / hit))
    return failures, compared


def self_test():
    cases = []

    def expect(name, ok):
        cases.append((name, ok))

    def tree(root, files):
        for rel, data in files.items():
            path = pathlib.Path(root) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    # Staging is <tmp>/staging; <tmp>/outside is a tree beside it, not under it.
    ref = "spec/reference/python/chiplet_format_io"
    full = "tool/vendor/chiplet_format_io"
    init_only = "dsl/_vendor/chiplet_format_io"
    outside = "outside/_vendor/chiplet_format_io"
    reader = {"__init__.py": b"__version__ = '1.10.0'\n", "pins.py": b"PINS = 1\n"}

    def run(extra=None, copies=(full, init_only), roots=(), outside_copy=False):
        """check() on a fresh tree; <top> in a copy, root or message is the temp dir."""
        with tempfile.TemporaryDirectory() as tmp:
            top = pathlib.Path(tmp)
            stg = top / "staging"
            tree(stg, {"%s/%s" % (ref, k): v for k, v in reader.items()})
            tree(stg, {"%s/%s" % (full, k): v for k, v in reader.items()})
            tree(stg, {"%s/__init__.py" % init_only: reader["__init__.py"]})
            tree(stg, {"spec/reference/cpp/include/chiplet_format_io/reader.hpp": b"//\n"})
            if outside_copy:
                tree(top, {"%s/__init__.py" % outside: reader["__init__.py"]})
            if extra:
                extra(top, stg)
            failures, compared = check(
                stg, ref, [c.replace("<top>", tmp) for c in copies],
                [r.replace("<top>", tmp) for r in roots])
            return [f.replace(tmp, "<top>") for f in failures], compared

    failures, compared = run()
    expect("identical copies pass, the C++ include dir is not a copy",
           not failures and compared == [(full, 2), (init_only, 1)])

    failures, _ = run(lambda t, s: (s / full / "pins.py").write_bytes(b"PINS = 2\n"))
    expect("a copy whose bytes lag the reference fails",
           any("pins.py: differs" in f for f in failures))

    failures, _ = run(lambda t, s: (s / full / "local.py").write_bytes(b"x = 1\n"))
    expect("a file the reference does not have fails",
           any("local.py: the reference" in f for f in failures))

    def pycache(t, s):
        (s / full / "__pycache__").mkdir()
        (s / full / "__pycache__" / "pins.cpython-312.pyc").write_bytes(b"\0")
    failures, _ = run(pycache)
    expect("a __pycache__ directory is not vendored source", not failures)

    failures, _ = run(copies=(full,))
    expect("an undeclared copy under the staging root fails",
           any(f.startswith(init_only) and "not told about" in f for f in failures))

    failures, _ = run(copies=(full, init_only, "gone/chiplet_format_io"))
    expect("a declared copy that is missing fails",
           any(f.startswith("gone/") for f in failures))

    def link(t, s):
        (s / full / "alias.py").symlink_to(s / ref / "pins.py")
    failures, _ = run(link)
    expect("a symlink under a copy fails", any("alias.py: not a regular" in f for f in failures))

    def no_init(t, s):
        (s / "empty/chiplet_format_io").mkdir(parents=True)
    failures, _ = run(no_init, copies=(full, init_only, "empty/chiplet_format_io"))
    expect("an empty copy fails rather than passing vacuously",
           any("empty/chiplet_format_io: holds no __init__.py" in f for f in failures))

    rooted = (full, init_only, "<top>/" + outside)
    failures, compared = run(copies=rooted, roots=("<top>/outside",), outside_copy=True)
    expect("a declared copy under a second root passes",
           not failures and len(compared) == 3 and compared[-1][1] == 1)

    failures, _ = run(lambda t, s: (t / outside / "pins.py").write_bytes(b"PINS = 2\n"),
                      copies=rooted, roots=("<top>/outside",), outside_copy=True)
    expect("a copy under a second root that lags the reference fails",
           any("pins.py: differs" in f for f in failures))

    failures, _ = run(roots=("<top>/outside",), outside_copy=True)
    expect("an undeclared copy under a second root fails",
           any(f.startswith("<top>/" + outside) and "not told about" in f
               for f in failures))

    failures, _ = run(copies=rooted, outside_copy=True)
    expect("a declared copy outside staging and every root fails",
           any("under neither" in f for f in failures))

    failures, _ = run(copies=(full, init_only, "../" + outside), outside_copy=True)
    expect("a '..' does not carry a copy out of the scanned trees",
           any("under neither" in f for f in failures))

    failures, _ = run(roots=("<top>/missing",))
    expect("a --root that is not a directory fails",
           any("<top>/missing: a --root that is not" in f for f in failures))

    failures, _ = run(copies=(full,), roots=("<top>/staging/dsl",))
    expect("a root inside staging reports an undeclared copy once",
           sum("not told about" in f for f in failures) == 1)

    ok = True
    for name, passing in cases:
        print("%-66s %s" % (name, "ok" if passing else "FAILED"))
        ok = ok and passing
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("staging", nargs="?")
    ap.add_argument("--reference", metavar="PATH")
    ap.add_argument("--copy", action="append", default=[], metavar="PATH")
    ap.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="another tree to scan for undeclared copies")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.staging or not args.reference or not args.copy:
        ap.error("STAGING, --reference and at least one --copy are required "
                 "unless --self-test is given")

    failures, compared = check(args.staging, args.reference, args.copy, args.root)
    for copy, count in compared:
        print("%s: %d file%s compared with %s" % (
            copy, count, "" if count == 1 else "s", args.reference))
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("every vendored %s is byte-equal to the reference" % PACKAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
