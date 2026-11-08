#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Checks a directory of die view records against the listing that pins them.

The image carries the EDA views of built dies, their gate-level netlists and
simulation models among them, emitted outside this repository: one directory
per die, named by the layout sha256 its contract binds, holding the record
(eda-views.json) and every file the record names.
The bytes are not in git. die-views.sha256 is: every file of every record, as
sha256sum prints it, under the paths the emitter wrote
(records/<layout sha256>/<file>), sorted by path. staging.pin pins that
listing by its own sha256, so the recipe commit names every byte.

This checks a ROOT that holds the <layout sha256> directories:

  - the listing has that shape, ends in a newline, names each path once and is
    sorted, and with --expect-sha256 hashes to the pin;
  - each listed directory has an eda-views.json line;
  - ROOT holds exactly the listed files and the directories above them, no
    more and no fewer, each with the listed bytes, and nothing in it is a
    symlink: a link makes the bytes depend on whatever it points at, which the
    listing does not name;
  - each directory's eda-views.json names that directory in
    bound_to.layout_sha256, so a record cannot sit under another die's key;
  - with --manifest, the manifest's die_views entry names this listing's
    sha256 and these directories.

With no --root only the listing is checked: CI holds no records.

Usage:
    check_die_views.py --listing FILE [--root DIR] [--expect-sha256 HEX] [--manifest FILE]
    check_die_views.py --self-test
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile

SEGMENT = r"[-+_0-9A-Za-z][-.+_0-9A-Za-z]*"
LINE = re.compile(r"^([0-9a-f]{64})  records/([0-9a-f]{64})/((?:%s/)*%s)$" % (SEGMENT, SEGMENT))
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RECORD = "eda-views.json"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_listing(path, expect=None):
    """(digest, {<layout>/<file>: sha256}, failures)."""
    failures = []
    data = pathlib.Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if expect is not None and digest != expect:
        failures.append("%s hashes to %s, not the pinned %s" % (path, digest, expect))
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        failures.append("%s is not ASCII" % path)
        return digest, {}, failures
    if text and not text.endswith("\n"):
        failures.append("%s does not end in a newline" % path)
    entries, order = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        m = LINE.match(line)
        if not m:
            failures.append("%s:%d is not '<sha256>  records/<layout sha256>/<file>'" % (path, n))
            continue
        rel = "%s/%s" % (m.group(2), m.group(3))
        if rel in entries:
            failures.append("%s:%d names records/%s a second time" % (path, n, rel))
        entries[rel] = m.group(1)
        order.append(rel)
    if not entries and not failures:
        failures.append("%s names no file" % path)
    if order != sorted(order):
        failures.append("%s is not sorted by path" % path)
    for layout in sorted({rel.split("/", 1)[0] for rel in entries}):
        if "%s/%s" % (layout, RECORD) not in entries:
            failures.append("%s lists no %s for records/%s" % (path, RECORD, layout))
    return digest, entries, failures


def walk(root):
    """({rel: path} of regular files, {rel} of directories, failures) under root."""
    root = pathlib.Path(root)
    files, dirs, failures = {}, set(), []
    if root.is_symlink():
        return files, dirs, ["%s is a symlink" % root]
    if not root.is_dir():
        return files, dirs, ["%s is not a directory" % root]
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            p = pathlib.Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                failures.append("%s/%s is a symlink" % (root, rel))
            elif name in dirnames:
                dirs.add(rel)
            elif p.is_file():
                files[rel] = p
            else:
                failures.append("%s/%s is not a regular file" % (root, rel))
    return files, dirs, failures


def check_root(root, entries):
    """Failures of ROOT against the listing's entries."""
    files, dirs, failures = walk(root)
    if failures and not files and not dirs:
        return failures
    wanted_dirs = set()
    for rel in entries:
        parts = rel.split("/")
        wanted_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
    for rel in sorted(set(entries) - set(files)):
        failures.append("%s/%s is listed and missing" % (root, rel))
    for rel in sorted(set(files) - set(entries)):
        failures.append("%s/%s is not listed" % (root, rel))
    for rel in sorted(dirs - wanted_dirs):
        failures.append("%s/%s/ holds no listed file" % (root, rel))
    for rel in sorted(set(entries) & set(files)):
        got = sha256_file(files[rel])
        if got != entries[rel]:
            failures.append("%s/%s is %s, the listing says %s" % (root, rel, got, entries[rel]))
    for layout in sorted({rel.split("/", 1)[0] for rel in entries}):
        record = "%s/%s" % (layout, RECORD)
        if record not in files or sha256_file(files[record]) != entries.get(record):
            continue
        try:
            bound = json.loads(files[record].read_text())["bound_to"]["layout_sha256"]
        except (ValueError, KeyError, TypeError) as e:
            failures.append("%s/%s has no bound_to.layout_sha256 (%s)" % (root, record, e))
            continue
        if bound != layout:
            failures.append("%s/%s/ holds a record bound to %s, not to its own name"
                            % (root, layout, bound))
    return failures


def check_manifest(manifest, digest, layouts):
    try:
        entry = json.loads(pathlib.Path(manifest).read_text())["die_views"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return ["%s has no die_views entry (%s)" % (manifest, e)]
    failures = []
    if entry.get("listing_sha256") != digest:
        failures.append("%s says die_views.listing_sha256 %s, the listing is %s"
                        % (manifest, entry.get("listing_sha256"), digest))
    if entry.get("layouts") != layouts:
        failures.append("%s says die_views.layouts %s, the listing names %s"
                        % (manifest, entry.get("layouts"), layouts))
    return failures


def check(listing, root=None, expect=None, manifest=None):
    """(failures, summary)."""
    if expect is not None and not SHA256.match(expect):
        return ["--expect-sha256 %r is not 64 lowercase hex" % expect], None
    digest, entries, failures = read_listing(listing, expect)
    layouts = sorted({rel.split("/", 1)[0] for rel in entries})
    if failures:
        return failures, None
    if root is not None:
        failures += check_root(root, entries)
    if manifest is not None:
        failures += check_manifest(manifest, digest, layouts)
    where = "the listing" if root is None else str(root)
    summary = ("die views: %s holds %d files in %d records (%s), the bytes listing %s names"
               % (where, len(entries), len(layouts),
                  ", ".join(l[:8] for l in layouts), digest[:12]))
    return failures, summary


# --------------------------------------------------------------------------
# Self-test: a two-record tree, then each named mutation.
# --------------------------------------------------------------------------

def _tree(base, layouts=("a" * 64, "b" * 64)):
    root = base / "records"
    for layout in layouts:
        d = root / layout
        (d / "netlist").mkdir(parents=True)
        (d / RECORD).write_text(json.dumps({"bound_to": {"layout_sha256": layout}}))
        (d / "netlist" / "core.nl.v").write_text("module core; endmodule // %s\n" % layout[:4])
    return root


def _listing(base, root):
    lines = []
    for p in sorted(q for q in root.rglob("*") if q.is_file()):
        lines.append("%s  records/%s" % (sha256_file(p), p.relative_to(root).as_posix()))
    lines.sort(key=lambda l: l.split("  ", 1)[1])
    path = base / "die-views.sha256"
    path.write_text("".join(l + "\n" for l in lines))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def self_test():
    a, b = "a" * 64, "b" * 64
    cases = []

    # pin: the --expect-sha256 given. "after" is the listing as mutated, so a
    # case that edits the listing is held to its own rule and not to the pin;
    # "before" is the listing as written, the pin a stager holds.
    def case(name, mutate, expect_fail, pin="after", use_root=True, manifest=None):
        cases.append((name, mutate, expect_fail, pin, use_root, manifest))

    def nothing(base, root, listing):
        pass

    def extra_file(base, root, listing):
        (root / a / "netlist" / "stray.v").write_text("x\n")

    def extra_dir(base, root, listing):
        (root / a / "empty").mkdir()

    def missing_file(base, root, listing):
        (root / b / "netlist" / "core.nl.v").unlink()

    def listing_byte_off(base, root, listing):
        text = listing.read_text()
        listing.write_text(("0" if text[0] != "0" else "1") + text[1:])

    def file_byte_off(base, root, listing):
        p = root / a / "netlist" / "core.nl.v"
        p.write_text(p.read_text().replace("module", "madule"))

    def wrong_binding(base, root, listing):
        # b's record moved under a's name: the bytes are listed there, so only
        # the binding can tell.
        (root / a / RECORD).write_text((root / b / RECORD).read_text())
        lines = []
        for line in listing.read_text().splitlines():
            sha, path = line.split("  ", 1)
            if path == "records/%s/%s" % (a, RECORD):
                sha = sha256_file(root / a / RECORD)
            lines.append("%s  %s\n" % (sha, path))
        listing.write_text("".join(lines))

    def file_symlink(base, root, listing):
        p = root / a / "netlist" / "core.nl.v"
        keep = base / "outside.v"
        keep.write_bytes(p.read_bytes())
        p.unlink()
        p.symlink_to(keep)

    def dir_symlink(base, root, listing):
        (base / "elsewhere").mkdir()
        (root / a / "link").symlink_to(base / "elsewhere")

    def root_symlink(base, root, listing):
        real = base / "real-records"
        root.rename(real)
        root.symlink_to(real)

    def unsorted(base, root, listing):
        lines = listing.read_text().splitlines(True)
        listing.write_text("".join([lines[1], lines[0]] + lines[2:]))

    def duplicate(base, root, listing):
        lines = listing.read_text().splitlines(True)
        listing.write_text("".join(lines[:1] + lines))

    def dotdot(base, root, listing):
        lines = listing.read_text().splitlines(True)
        lines[0] = lines[0].replace("records/%s/" % a, "records/%s/../" % a)
        listing.write_text("".join(lines))

    def no_newline(base, root, listing):
        listing.write_text(listing.read_text().rstrip("\n"))

    def no_record(base, root, listing):
        (root / b / RECORD).unlink()
        listing.write_text("".join(l for l in listing.read_text().splitlines(True)
                                   if not l.rstrip().endswith("%s/%s" % (b, RECORD))))

    def empty_listing(base, root, listing):
        listing.write_text("")

    case("the tree as listed passes", nothing, None)
    case("the listing alone passes", nothing, None, use_root=False)
    case("one extra file", extra_file, "is not listed")
    case("one extra directory", extra_dir, "holds no listed file")
    case("one missing file", missing_file, "is listed and missing")
    case("a listing one byte off", listing_byte_off, "not the pinned", pin="before")
    case("a listing one byte off, unpinned", listing_byte_off, "the listing says", pin=None)
    case("a file one byte off", file_byte_off, "the listing says")
    case("a directory name that differs from its bound_to.layout_sha256", wrong_binding,
         "holds a record bound to")
    case("a file that is a symlink", file_symlink, "is a symlink")
    case("a directory that is a symlink", dir_symlink, "is a symlink")
    case("a root that is a symlink", root_symlink, "is a symlink")
    case("an unsorted listing", unsorted, "is not sorted")
    case("a path listed twice", duplicate, "a second time")
    case("a path with ..", dotdot, "is not '<sha256>")
    case("a listing with no final newline", no_newline, "does not end in a newline")
    case("a record directory with no eda-views.json", no_record, "lists no eda-views.json")
    case("an empty listing", empty_listing, "names no file")
    case("a manifest that agrees", nothing, None, manifest="good")
    case("a manifest naming another listing", nothing, "die_views.listing_sha256",
         manifest="digest")
    case("a manifest naming other directories", nothing, "die_views.layouts",
         manifest="layouts")
    case("a manifest with no die_views", nothing, "has no die_views entry", manifest="none")

    failed = []
    for name, mutate, expect_fail, pin, use_root, manifest in cases:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            root = _tree(base)
            listing, digest = _listing(base, root)
            mutate(base, root, listing)
            mpath = None
            if manifest is not None:
                entry = {"listing_sha256": hashlib.sha256(listing.read_bytes()).hexdigest(),
                         "layouts": [a, b]}
                if manifest == "digest":
                    entry["listing_sha256"] = "c" * 64
                elif manifest == "layouts":
                    entry["layouts"] = [a]
                mpath = base / "manifest.json"
                mpath.write_text(json.dumps({} if manifest == "none" else {"die_views": entry}))
            after = hashlib.sha256(listing.read_bytes()).hexdigest()
            failures, _ = check(listing, root if use_root else None,
                                {"after": after, "before": digest, None: None}[pin], mpath)
            joined = "\n".join(failures)
            ok = (not failures) if expect_fail is None else (expect_fail in joined)
            if not ok:
                failed.append("%s: expected %s, got %s" % (
                    name, "a pass" if expect_fail is None else repr(expect_fail),
                    failures or "a pass"))
    for f in failed:
        print("FAIL: %s" % f)
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listing")
    ap.add_argument("--root")
    ap.add_argument("--expect-sha256")
    ap.add_argument("--manifest")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.listing:
        ap.error("missing --listing (unless --self-test)")
    failures, summary = check(args.listing, args.root, args.expect_sha256, args.manifest)
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
