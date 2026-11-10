#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Checks the store closure the runtime image carries against its lock.

nix/host-closure.lock pins each path nix/host-closure.txt lists with its
narHash (hex). In the image (verify stage), this refuses unless the lock lists
exactly the list's paths, in its order, and each path in the store has that
narHash, recomputed from its NAR serialisation with nix/tool_identity.py's
nar_sha256, the one the tool identity table uses. Nothing here runs Nix.

On the build host (--registered), before build.sh packs anything, it refuses
the same way unless each path is valid in the host's store with that narHash as
the store registered it (nix-store --query --hash, a database read; nothing is
hashed).

Usage: check_store_closure.py --lock FILE --list FILE --nar-lib DIR [--store DIR]
       check_store_closure.py --lock FILE --list FILE --registered
       check_store_closure.py --self-test
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def rows_of(lock):
    rows = []
    for n, line in enumerate(Path(lock).read_text().splitlines(), 1):
        if line.startswith("#"):
            continue
        parts = line.split(" ")
        if len(parts) != 2 or len(parts[1]) != 64 or set(parts[1]) - set("0123456789abcdef"):
            raise ValueError(f"{lock}:{n}: not '<store path> <sha256 hex>'")
        rows.append((parts[0], parts[1]))
    return rows


NIX32 = "0123456789abcdfghijklmnpqrsvwxyz"


def nix32_to_hex(text):
    """The hex of a Nix base32 string (the last character is the low bits)."""
    out = bytearray(len(text) * 5 // 8)
    for n, char in enumerate(reversed(text)):
        digit = NIX32.index(char)
        i, j = divmod(n * 5, 8)
        out[i] |= (digit << j) & 0xFF
        if i + 1 < len(out):
            out[i + 1] |= digit >> (8 - j)
        elif digit >> (8 - j):
            raise ValueError(f"{text!r} is not a Nix base32 sha256")
    return out.hex()


def hex_of(registered):
    """The hex of nix-store's sha256:<hash> (base32 or hex), None if it is not one."""
    if not registered.startswith("sha256:"):
        return None
    got = registered[len("sha256:"):]
    if len(got) == 64 and not set(got) - set("0123456789abcdef"):
        return got
    return nix32_to_hex(got) if len(got) == 52 and not set(got) - set(NIX32) else None


def registered_sha256s(paths):
    """{path: the narHash the host's store registered (hex), or None if not valid there}.

    One query for all; when it fails (a path not valid there), one per path,
    so each such path is named.
    """
    run = subprocess.run(["nix-store", "--query", "--hash", *paths],
                         capture_output=True, text=True, check=False)
    lines = run.stdout.split()
    if run.returncode == 0 and len(lines) == len(paths):
        return {p: hex_of(h) for p, h in zip(paths, lines)}
    got = {}
    for path in paths:
        one = subprocess.run(["nix-store", "--query", "--hash", path],
                             capture_output=True, text=True, check=False)
        got[path] = hex_of(one.stdout.strip()) if one.returncode == 0 else None
    return got


def check_registered(lock, listing, registered=registered_sha256s):
    """Problems, one line each; none when the host's store registered the lock."""
    rows = rows_of(lock)
    listed = [l for l in Path(listing).read_text().splitlines() if l.startswith("/")]
    if [p for p, _ in rows] != listed:
        return [f"{lock} does not list exactly the paths {listing} lists"]
    hashes = registered([p for p, _ in rows])
    problems = []
    for path, want in rows:
        got = hashes.get(path)
        if got is None:
            problems.append(f"{path} is not valid in this build host's store")
        elif got != want:
            problems.append(f"{path} has narHash {got} on this build host, not the lock's {want}")
    return problems


def check(lock, listing, nar_sha256, store=None):
    """Problems, one line each; none when the store holds the lock."""
    rows = rows_of(lock)
    listed = [l for l in Path(listing).read_text().splitlines() if l.startswith("/")]
    if [p for p, _ in rows] != listed:
        return [f"{lock} does not list exactly the paths {listing} lists"]
    problems = []
    for path, want in rows:
        here = path if store is None else os.path.join(store, os.path.basename(path))
        if not os.path.lexists(here):
            problems.append(f"{path} is not in the store")
            continue
        got = nar_sha256(here)
        if got != want:
            problems.append(f"{path} has narHash {got}, not the lock's {want}")
    return problems


def self_test():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nix"))
    from tool_identity import nar_sha256
    passed = 0
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "store"
        a = store / ("a" * 32 + "-one")
        a.mkdir(parents=True)
        (a / "f").write_text("x")
        b = store / ("b" * 32 + "-two")
        b.write_text("y")
        listing = Path(tmp) / "list.txt"
        listing.write_text(f"# header\n/nix/store/{a.name}\n/nix/store/{b.name}\n")
        lock = Path(tmp) / "list.lock"

        def write(rows):
            lock.write_text("# header\n" + "".join(f"{p} {h}\n" for p, h in rows))

        good = [(f"/nix/store/{a.name}", nar_sha256(a)), (f"/nix/store/{b.name}", nar_sha256(b))]
        cases = [
            ("the store holds the lock", good, None, []),
            ("a path with another narHash", [good[0], (good[1][0], "0" * 64)], None,
             ["has narHash"]),
            ("a lock in another order", [good[1], good[0]], None, ["does not list exactly"]),
            ("a lock with a path less", good[:1], None, ["does not list exactly"]),
            ("a path not in the store", good, b, ["is not in the store"]),
        ]
        for what, rows, remove, want in cases:
            write(rows)
            if remove is not None:
                remove.rename(Path(tmp) / "moved")
            got = check(lock, listing, nar_sha256, store=str(store))
            if remove is not None:
                (Path(tmp) / "moved").rename(remove)
            ok = (not got) if not want else (len(got) == 1 and want[0] in got[0])
            if not ok:
                print(f"FAIL: {what}: {got}")
                return 1
            passed += 1
        (a / "f").write_text("changed")
        write(good)
        if not any("has narHash" in p for p in check(lock, listing, nar_sha256, store=str(store))):
            print("FAIL: a changed file inside a path")
            return 1
        passed += 1
        lock.write_text("# header\n/nix/store/x 12\n")
        try:
            check(lock, listing, nar_sha256, store=str(store))
            print("FAIL: a malformed row")
            return 1
        except ValueError:
            passed += 1
        # A pair the host's store gave: nix-store --query --hash and nix path-info's hex.
        if nix32_to_hex("05hz16anly4ymph08x29rm1q5cb96rd6m7sv4v2z3ycl7svl993c") != \
                "6ca444b73e94f9f1c5265b9f6a5a3669b18243cd497404e0ad9e786a95091f16":
            print("FAIL: Nix base32 to hex")
            return 1
        passed += 1
        write(good)
        hashes = dict(good)
        for what, registered, want in (
                ("the host registered the lock", lambda ps: hashes, None),
                ("a path the host does not hold", lambda ps: {}, "is not valid"),
                ("a path the host registered otherwise",
                 lambda ps: dict.fromkeys(ps, "f" * 64), "on this build host")):
            got = check_registered(lock, listing, registered)
            if bool(got) if want is None else not (len(got) == 2 and want in got[0]):
                print(f"FAIL: {what}: {got}")
                return 1
            passed += 1
        write(good[:1])
        if not check_registered(lock, listing, lambda ps: hashes)[0].endswith(f"{listing} lists"):
            print("FAIL: a lock with a path less, on the build host")
            return 1
        passed += 1
    print(f"check_store_closure self-test: {passed} passed")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--lock")
    ap.add_argument("--list")
    ap.add_argument("--nar-lib")
    ap.add_argument("--store", help="look paths up here by name (tests); default: as listed")
    ap.add_argument("--registered", action="store_true",
                    help="check the host store's registered narHash instead of hashing")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.registered:
        if not (args.lock and args.list) or args.nar_lib or args.store:
            ap.error("--registered takes --lock and --list only")
        problems = check_registered(args.lock, args.list)
        for line in problems:
            print(f"host closure: {line}", file=sys.stderr)
        if problems:
            return 1
        print(f"host closure: {len(rows_of(args.lock))} paths, each registered on this host "
              "with the lock's narHash")
        return 0
    if not (args.lock and args.list and args.nar_lib):
        ap.error("--lock, --list and --nar-lib are required")
    sys.path.insert(0, args.nar_lib)
    from tool_identity import nar_sha256
    problems = check(args.lock, args.list, nar_sha256, store=args.store)
    for line in problems:
        print(f"host closure: {line}", file=sys.stderr)
    if problems:
        return 1
    print(f"host closure: {len(rows_of(args.lock))} paths, each with the lock's narHash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
