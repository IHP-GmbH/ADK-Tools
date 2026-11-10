#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""docker/apt.lock and docker/pip.lock belong to the tools image this tree keys.

The tools stage writes what it installed to /opt/adk-tools/tools/*.lock, and
the runtime stage's first step holds the tracked files to them. The locks can
only be read from a built tools image, so a commit that moves the tools key
(ci/tools_key.py) and not the locks builds the whole tools image and then
fails at that step. docker/tools-locks.txt records the key the two files
were read from and each file's sha256; this check holds the tree to it before
anything is built:

  - the key recorded has to be the key given (build.sh passes its own), or the
    locks belong to another tools image: build this key's alone
    (build.sh --tools-only), read its two locks into docker/ and record them;
  - each lock's sha256 has to be the one recorded, or a lock moved without the
    image it was read from.

Usage:
    check_tools_locks.py --key KEY [--record docker/tools-locks.txt]
    check_tools_locks.py --key KEY --write

--write records KEY and the two files as they stand, for the commit that
moves them. Exit 0 when the tree holds, 1 when it does not (each gap named), 2
when an input cannot be read.
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

LOCKS = ("apt.lock", "pip.lock")
KEY = re.compile(r"^[0-9a-f]{64}$")
HEADER = """\
# The tools image docker/apt.lock and docker/pip.lock were read from: its key
# (ci/tools_key.py) and each file's sha256. Written by
# ci/check_tools_locks.py --write, in the commit that moves the locks; build.sh
# refuses a tree whose key or locks are not the ones recorded here.
"""


class Unreadable(Exception):
    pass


def sha256(path):
    if not path.is_file():
        raise Unreadable("%s is missing" % path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_record(record):
    if not record.is_file():
        raise Unreadable("%s is missing" % record)
    values = {}
    for line in record.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(key|%s) ([0-9a-f]{64})$" % "|".join(map(re.escape, LOCKS)), line)
        if not m or m.group(1) in values:
            raise Unreadable("%s: unexpected line %r" % (record, line))
        values[m.group(1)] = m.group(2)
    missing = [k for k in ("key",) + LOCKS if k not in values]
    if missing:
        raise Unreadable("%s names no %s" % (record, ", ".join(missing)))
    return values


def check(key, record):
    values = read_record(record)
    gaps = []
    if values["key"] != key:
        gaps.append("the locks were read from the tools image keyed %s, and this tree keys %s: "
                    "build this key's tools image alone (build.sh --tools-only), read its "
                    "two locks into docker/ and record them (ci/check_tools_locks.py --write)"
                    % (values["key"][:12], key[:12]))
    for name in LOCKS:
        if sha256(record.parent / name) != values[name]:
            gaps.append("docker/%s is not the file recorded for the tools image keyed %s"
                        % (name, values["key"][:12]))
    return gaps


def write(key, record):
    lines = ["key %s" % key] + ["%s %s" % (n, sha256(record.parent / n)) for n in LOCKS]
    record.write_text(HEADER + "".join(l + "\n" for l in lines))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key", required=True)
    ap.add_argument("--record", type=Path, default=Path("docker/tools-locks.txt"))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    try:
        if not KEY.match(args.key):
            raise Unreadable("%r is not a tools key" % args.key)
        if args.write:
            write(args.key, args.record)
            print("tools locks: recorded for %s" % args.key[:12])
            return 0
        gaps = check(args.key, args.record)
    except (Unreadable, OSError) as e:
        print("tools locks: UNREADABLE: %s" % e, file=sys.stderr)
        return 2
    if gaps:
        for g in gaps:
            print("tools locks: %s" % g, file=sys.stderr)
        return 1
    print("tools locks: OK, read from the tools image keyed %s" % args.key[:12])
    return 0


if __name__ == "__main__":
    sys.exit(main())
