#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The tool identity table: each tool of an image named by its content.

The table is format adk-tools-tool-identity/v1 (formats/, where the schema
says what each key means and the byte form). A check keyed on it names a tool
by what it is, not by where it sits: a store path is input-addressed and moves
when nothing in it changed, and an image ID moves with every layer.

A store path is digested as Nix digests it: the sha256 of its NAR
serialisation, which equals the narHash `nix path-info` reports, so the host
predicts what the image computes and the two can be compared. The image has no
Nix database, which is why the serialiser is here and needs only the standard
library.

The table holds no image ID: the ID is a digest over the image's layers, this
file included, so no build can write it. Whoever keeps a copy pairs it with
the ID the launcher gave (run.sh sets ADK_TOOLS_IMAGE in every container).

The image build writes the table in two steps. On the build host, `facts`
records what the host's store says: each staging.pin Nix pin, whether the image
carries it (a pin the closure lists name) or run.sh mounts it from the host,
and the narHash of every path it needs. In the image, `image` serialises every
path the image carries, refuses one that is not the host's narHash, keeps the
host's values for a pin from the host, and adds the tools no pin names, which
are exactly IMAGE_TOOLS. `verify` rebuilds the table the same way and holds
the written file to it byte for byte, and to the schema.

A pin whose name an image tool takes is the Nix lane's, not the image's: the
image's `python` is its venv, the lane's is the pinned interpreter env. Such a
pin is exactly IMAGE_DROP. `facts` leaves it out, so the build host never asks
its store for it, and `image` passes over it if a facts file has it. Host mode
keeps every pin.

Usage:
  python3 nix/tool_identity.py nar PATH
      the NAR sha256 of PATH, hex
  python3 nix/tool_identity.py host [--pin FILE] [--check-nar]
          [--tool NAME=VERSION:BINARY[:TREE] ...]
          [--tracked-tool NAME=BINARY:TREE ...] --out FILE
      the table for staging.pin's Nix pins, from this host's store: each
      pin's NAR sha256 and closure from `nix path-info`; --check-nar also
      serialises every path of the image's own store (the pins the closure
      lists name) and refuses on a difference; --tool adds a tool outside Nix;
      --tracked-tool adds one run from a staged checkout, its version
      "commit <HEAD> tracked <digest>" computed here, never passed in
  python3 nix/tool_identity.py lane --closure-info DIR [--pin FILE]
          [--tool ...] [--tracked-tool ...] --out FILE
      the table for the Nix lane's shell, from the closureInfo it names and
      nothing else: no store query, no nix on PATH. Each pin the closure
      lists, with the narHash and references its registration records; a pin
      outside it is no tool of that shell and is left out. The table's
      closure digest is the whole closure's
  python3 nix/tool_identity.py facts [--pin FILE] --out FILE
      the build host's prediction the image build reads
  python3 nix/tool_identity.py image --facts FILE --tool ... [--tracked-tool ...]
          --out FILE
      the table, in the image (one --tool or --tracked-tool per name in
      IMAGE_TOOLS)
  python3 nix/tool_identity.py verify --table FILE --facts FILE --tool ...
          [--tracked-tool ...] [--schema FILE]
      refuses a written table other than the one `image` gives now

A --tool's paths are absolute, and its version holds no ':'.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
from pathlib import Path, PurePosixPath

FORMAT = "adk-tools-tool-identity/v1"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# The closure lists of the Nix store the image carries; a pin one of them
# lists is in the image's store, any other comes from the host's at run time.
IMAGE_CLOSURES = (HERE / "cosim-closure.txt", HERE / "mixsim-closure.txt",
                  HERE / "viewer-closure.txt", HERE / "vams-closure.txt",
                  HERE / "host-closure.txt")
# The tools no pin names that an image's table names, each by --tool or
# --tracked-tool. A table that leaves one out would make every check that needs
# it unresolved, so `image` refuses a set other than this one.
IMAGE_TOOLS = ("adk-drc", "adk-verify-carrier-drc", "assembly-lvs", "chiplet-cosim",
               "chiplet-studio", "git", "intm4tm2-run-drc", "kicad-cli", "klayout",
               "klayout-python", "python")
# The pins an image tool's name takes, which the image leaves out. Written out
# rather than derived, so a pin that comes to share a name with an image tool
# is refused at `facts` instead of quietly dropped.
IMAGE_DROP = frozenset({"python"})
PIN = re.compile(r'^NIX_([A-Z0-9_]+)_OUTPATH="(/[^"]+)"$')
CHUNK = 1 << 20


def _nar_str(h, data: bytes) -> None:
    h.update(len(data).to_bytes(8, "little"))
    h.update(data)
    h.update(b"\0" * (-len(data) % 8))


def _nar_node(h, path: bytes) -> None:
    st = os.lstat(path)
    _nar_str(h, b"(")
    _nar_str(h, b"type")
    if stat.S_ISREG(st.st_mode):
        _nar_str(h, b"regular")
        if st.st_mode & stat.S_IXUSR:
            _nar_str(h, b"executable")
            _nar_str(h, b"")
        _nar_str(h, b"contents")
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            h.update(size.to_bytes(8, "little"))
            left = size
            while left:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    raise OSError(f"{os.fsdecode(path)}: shorter than its size while read")
                h.update(chunk)
                left -= len(chunk)
            h.update(b"\0" * (-size % 8))
    elif stat.S_ISLNK(st.st_mode):
        _nar_str(h, b"symlink")
        _nar_str(h, b"target")
        _nar_str(h, os.readlink(path))
    elif stat.S_ISDIR(st.st_mode):
        _nar_str(h, b"directory")
        for name in sorted(os.listdir(path)):
            _nar_str(h, b"entry")
            _nar_str(h, b"(")
            _nar_str(h, b"name")
            _nar_str(h, name)
            _nar_str(h, b"node")
            _nar_node(h, os.path.join(path, name))
            _nar_str(h, b")")
    else:
        raise OSError(f"{os.fsdecode(path)}: not a file, directory or symlink; NAR has no form for it")
    _nar_str(h, b")")


def nar_sha256(path: str | os.PathLike) -> str:
    """The sha256 of path's NAR serialisation, hex: Nix's narHash, decoded.

    A regular file is executable when its owner may execute it, as Nix reads
    it; directory entries go in byte order of their names.
    """
    h = hashlib.sha256()
    _nar_str(h, b"nix-archive-1")
    _nar_node(h, os.fsencode(path))
    return h.hexdigest()


def sri_to_hex(nar_hash: str) -> str:
    """A narHash in `nix path-info --json`'s form, sha256-<base64>, as hex."""
    m = re.fullmatch(r"sha256-([A-Za-z0-9+/]{43}=)", nar_hash)
    if not m:
        raise ValueError(f"not a sha256 narHash in SRI form: {nar_hash!r}")
    return base64.b64decode(m[1]).hex()


def closure_sha256(nar_hexes) -> str:
    """The closure digest: sha256 over one '<nar sha256>\\n' line per path, sorted.

    Content only: two closures with the same contents under other store paths
    digest the same; a path holding another's store path in its bytes (a
    reference) changes with it.
    """
    return hashlib.sha256("".join(f"{x}\n" for x in sorted(nar_hexes)).encode()).hexdigest()


def canonical(record) -> bytes:
    """Compact JSON, keys sorted, one newline: the table's byte form."""
    return (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode()


def table_bytes(tools: list[dict], closure: str) -> bytes:
    """The table in its byte form."""
    names = [t["name"] for t in tools]
    if len(set(names)) != len(names):
        raise ValueError(f"tool names repeat: {names}")
    return canonical({"format": FORMAT, "closure_sha256": closure,
                      "tools": sorted(tools, key=lambda t: t["name"])})


def pins(pin_file: Path) -> dict[str, str]:
    """staging.pin's Nix pins, NIX_<X>_OUTPATH, by tool name: <x> in lower case, '-' for '_'."""
    found = {}
    for line in pin_file.read_text().splitlines():
        m = PIN.match(line)
        if m:
            found[m[1].lower().replace("_", "-")] = m[2]
    if not found:
        raise ValueError(f"{pin_file}: no NIX_*_OUTPATH pin")
    return found


def image_store_paths() -> set[str]:
    paths = set()
    for listing in IMAGE_CLOSURES:
        for line in listing.read_text().splitlines():
            if line.startswith("/"):
                paths.add(line.strip())
    return paths


def path_info(path: str) -> dict[str, str]:
    """Every requisite of path (itself included) and its narHash as hex, from this host's store."""
    out = subprocess.run(["nix", "path-info", "--json", "--recursive", path],
                         check=True, capture_output=True, text=True).stdout
    info = json.loads(out)
    if isinstance(info, list):
        info = {e["path"]: e for e in info}
    if path not in info:
        raise ValueError(f"nix path-info gave no entry for {path}")
    return {p: sri_to_hex(e["narHash"]) for p, e in info.items()}


def nix_entry(name: str, path: str, where: str, requisites: dict[str, str]) -> dict:
    return {"name": name, "from": where, "path": path, "nar_sha256": requisites[path],
            "closure_sha256": closure_sha256(requisites.values()),
            "version": None, "binary": None, "binary_sha256": None}


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def image_entry(name: str, version: str, binary: str, tree: str | None) -> dict:
    """A tool outside Nix: its version, its binary's sha256 and its tree's NAR, if it has one."""
    real = os.path.realpath(binary)
    return {"name": name, "from": "image", "path": tree,
            "nar_sha256": nar_sha256(tree) if tree else None, "closure_sha256": None,
            "version": version, "binary": real, "binary_sha256": file_sha256(real)}


# The digest of a staged checkout as git tracks it, read without git: a port of
# packaging-flow's verification/tracked_tree.py, whose code it matches, held to it by
# nix/test_tracked_tree.py. It refuses what that refuses, in the same words, and
# gives the same digest: the sha256 of the canonical JSON (keys sorted, no
# spaces, ASCII, no newline) of the sorted [path, sha256] rows of every file the
# tree's .git/index tracks, each by its bytes on disk. A clone's .git differs from
# another's of the same commit (the index holds stat times), so a NAR over the
# tree never repeats; this does, for one commit and clean bytes.
GIT = ".git"
GIT_INDEX = f"{GIT}/index"
GIT_CONFIG = f"{GIT}/config"
GIT_MODES = (0o100644, 0o100755)
# One index entry's header: ten 32-bit stat and mode fields, the object id, the flags.
_ENTRY = struct.Struct(">10I20sH")
_ENTRY_MODE = 6
_EXTENDED = 0x4000
_STAGE_SHIFT = 12
_NAME_LENGTH = 0x0FFF
# The extended flags of a version 3 entry.
_SKIP_WORKTREE = 0x4000
_INTENT_TO_ADD = 0x2000
_OBJECT_FORMAT = re.compile(r"^\s*objectformat\s*=\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class TreeRefused(ValueError):
    """A tree with no tracked digest; ``why`` is packaging-flow's Unresolved.why."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def _index_entries(data: bytes) -> dict[str, tuple[int, bytes]]:
    """Each stage-0 entry of an index, path to (mode, object id)."""
    if len(data) < 32 or hashlib.sha1(data[:-20]).digest() != data[-20:]:
        raise TreeRefused(f"{GIT_INDEX} fails its trailing checksum")
    signature, version, count = struct.unpack_from(">4sII", data)
    if signature != b"DIRC" or version not in (2, 3):
        raise TreeRefused(f"{GIT_INDEX} is not a version 2 or 3 index")
    end, offset = len(data) - 20, 12
    entries: dict[str, tuple[int, bytes]] = {}
    bad: dict[str, str] = {}
    malformed = False
    for _ in range(count):
        if offset + _ENTRY.size > end:
            raise TreeRefused(f"{GIT_INDEX} does not parse")
        fields = _ENTRY.unpack_from(data, offset)
        mode, oid, flags = fields[_ENTRY_MODE], fields[10], fields[11]
        start, extended = offset + _ENTRY.size, 0
        if flags & _EXTENDED:
            if version < 3 or start + 2 > end:
                raise TreeRefused(f"{GIT_INDEX} does not parse")
            extended = struct.unpack_from(">H", data, start)[0]
            start += 2
        nul = data.find(b"\0", start, end)
        if nul < 0 or (flags & _NAME_LENGTH) != min(nul - start, _NAME_LENGTH):
            raise TreeRefused(f"{GIT_INDEX} does not parse")
        try:
            name = data[start:nul].decode("utf-8")
        except UnicodeDecodeError:
            raise TreeRefused(f"{GIT_INDEX} names a path that is not UTF-8") from None
        # Each entry is padded with one to eight NULs to a multiple of eight.
        offset += (nul - offset + 8) & ~7
        path = PurePosixPath(name)
        if name != str(path) or path.is_absolute() or ".." in path.parts:
            # Not a tracked path; a split index writes a nameless one, and an
            # extension may yet say why, so the entries are read on.
            malformed = True
            continue
        stage = flags >> _STAGE_SHIFT & 3
        if stage:
            bad.setdefault(name, f"is at stage {stage} in {GIT_INDEX}")
            continue
        if name in entries or name in bad:
            raise TreeRefused(f"{GIT_INDEX} does not parse")
        if mode not in GIT_MODES:
            bad[name] = f"has mode {mode:o} in {GIT_INDEX}"
        elif extended & (_SKIP_WORKTREE | _INTENT_TO_ADD):
            bad[name] = f"is marked skip-worktree or intent-to-add in {GIT_INDEX}"
        else:
            entries[name] = (mode, oid)
    while offset < end:
        if offset + 8 > end:
            raise TreeRefused(f"{GIT_INDEX} does not parse")
        signature, size = struct.unpack_from(">4sI", data, offset)
        if not b"A" <= signature[:1] <= b"Z":
            raise TreeRefused(f"{GIT_INDEX} carries the {signature.decode('latin-1')!r} "
                              f"extension, which a reader must understand")
        offset += 8 + size
    if offset != end or malformed:
        raise TreeRefused(f"{GIT_INDEX} does not parse")
    if bad:
        first = min(bad)
        raise TreeRefused(f"{first} {bad[first]}")
    if not entries:
        raise TreeRefused(f"{GIT_INDEX} tracks no file")
    return entries


def _untracked_allowed(rel: str) -> bool:
    """pip's ``*.egg-info/`` records and the ``.pyc`` files in ``__pycache__/``."""
    parts = rel.split("/")
    return any(part.endswith(".egg-info") for part in parts[:-1]) or (
        len(parts) > 1 and parts[-2] == "__pycache__" and parts[-1].endswith(".pyc"))


def tracked_document(files: dict[str, str]) -> bytes:
    """The canonical JSON of the sorted [path, sha256] rows: ASCII, no newline."""
    rows = []
    for rel, digest in files.items():
        parts = PurePosixPath(rel).parts
        if not rel or PurePosixPath(rel).is_absolute() or ".." in parts or "." in parts \
                or rel != str(PurePosixPath(rel)):
            raise ValueError(f"tree entry {rel!r} is not a relative path inside the tree")
        if not (isinstance(digest, str) and _DIGEST.fullmatch(digest)):
            raise ValueError(f"tree entry {rel!r} digest {digest!r} is not a lowercase "
                             f"64-hex sha256")
        rows.append([rel, digest])
    return json.dumps(sorted(rows), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def tracked_tree_sha256(tree: str | os.PathLike) -> str:
    """The digest of the files tree's index tracks; TreeRefused says why there is none.

    The index is read whole before the tree is walked; each step names the least
    offending path, relative to the tree.
    """
    tree = Path(tree)
    if tree.is_symlink() or not tree.is_dir():
        raise TreeRefused(". is not a folder")
    if (tree / GIT).is_symlink() or not (tree / GIT).is_dir():
        raise TreeRefused(f"{GIT} is not a folder")
    try:
        config = (tree / GIT_CONFIG).read_text(encoding="utf-8")
        data = (tree / GIT_INDEX).read_bytes()
    except (OSError, UnicodeDecodeError):
        raise TreeRefused(f"{GIT_CONFIG} or {GIT_INDEX} cannot be read") from None
    found = _OBJECT_FORMAT.search(config)
    if found and found.group(1).lower() != "sha1":
        raise TreeRefused(f"{GIT_CONFIG} names the object format {found.group(1)}, not sha1")
    entries = _index_entries(data)
    bad: dict[str, str] = {}
    files: dict[str, str] = {}

    def unreadable(exc: OSError) -> None:
        bad[Path(exc.filename).relative_to(tree).as_posix()] = "cannot be read"

    for folder, dirs, names in os.walk(tree, onerror=unreadable):
        here = Path(folder)
        if here == tree and GIT in dirs:
            dirs.remove(GIT)
        for name in sorted(dirs):
            if (here / name).is_symlink():
                bad[(here / name).relative_to(tree).as_posix()] = "is a link"
                dirs.remove(name)
        for name in names:
            path = here / name
            rel = path.relative_to(tree).as_posix()
            try:
                mode = path.lstat().st_mode
                data = path.read_bytes() if stat.S_ISREG(mode) and rel in entries else b""
            except OSError:
                bad[rel] = "cannot be read"
                continue
            if stat.S_ISLNK(mode):
                bad[rel] = "is a link"
            elif not stat.S_ISREG(mode):
                bad[rel] = "is not a regular file"
            elif rel in entries:
                index_mode, oid = entries[rel]
                if hashlib.sha1(b"blob %d\0" % len(data) + data).digest() != oid:
                    bad[rel] = f"differs from its entry in {GIT_INDEX}"
                elif bool(mode & stat.S_IXUSR) != (index_mode == 0o100755):
                    bad[rel] = f"differs from its entry in {GIT_INDEX} in its executable bit"
                else:
                    files[rel] = hashlib.sha256(data).hexdigest()
            elif not _untracked_allowed(rel):
                bad[rel] = "is not tracked"
    for rel in entries:
        if rel not in files and rel not in bad:
            bad[rel] = "is tracked and missing"
    if bad:
        first = min(bad)
        raise TreeRefused(f"{first} {bad[first]}")
    return hashlib.sha256(tracked_document(files)).hexdigest()


def tracked_head(tree: str | os.PathLike) -> str:
    """The commit tree's HEAD names: 40 hex as written (a detached HEAD), or the
    ref it names, read from the loose ref or packed-refs. Anything else refuses."""
    git = Path(tree) / GIT
    try:
        head = (git / "HEAD").read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        raise TreeRefused(f"{GIT}/HEAD cannot be read") from None
    if _COMMIT.fullmatch(head):
        return head
    ref = head[len("ref: "):] if head.startswith("ref: ") else None
    if not ref or not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", ref) or ".." in ref.split("/"):
        raise TreeRefused(f"{GIT}/HEAD is neither a commit nor a ref: {head!r}")
    try:
        loose = (git / ref).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        loose = None
    if loose is not None:
        if _COMMIT.fullmatch(loose):
            return loose
        raise TreeRefused(f"{GIT}/{ref} is not a commit")
    try:
        packed = (git / "packed-refs").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        packed = []
    for line in packed:
        sha, _, name = line.partition(" ")
        if name == ref and _COMMIT.fullmatch(sha):
            return sha
    raise TreeRefused(f"{GIT}/HEAD names {ref}, which resolves to no commit")


def tracked_version(tree: str | os.PathLike) -> str:
    """A staged checkout's version: "commit <HEAD> tracked <tracked digest>"."""
    digest = tracked_tree_sha256(tree)
    return f"commit {tracked_head(tree)} tracked {digest}"


def tracked_entry(name: str, binary: str, tree: str) -> dict:
    """A tool run from a staged checkout: its file by sha256, the checkout by its
    commit and tracked digest. No path and no NAR: a NAR over a clone's .git
    would key the clone, not the code."""
    try:
        version = tracked_version(tree)
    except TreeRefused as err:
        raise ValueError(f"{name}: {tree}: no tracked digest: {err.why}") from None
    real = os.path.realpath(binary)
    return {"name": name, "from": "image", "path": None, "nar_sha256": None,
            "closure_sha256": None, "version": version, "binary": real,
            "binary_sha256": file_sha256(real)}


def tracked_spec(spec: str) -> tuple[str, str, str]:
    """NAME=BINARY:TREE as (name, binary, tree)."""
    name, _, rest = spec.partition("=")
    binary, _, tree = rest.partition(":")
    if not (name and binary.startswith("/") and tree.startswith("/")):
        raise ValueError(f"--tracked-tool wants NAME=BINARY:TREE with absolute paths, "
                         f"got {spec!r}")
    return name, binary, tree


def tool_spec(spec: str) -> tuple[str, str, str, str | None]:
    """NAME=VERSION:BINARY[:TREE] as (name, version, binary, tree or None)."""
    name, _, rest = spec.partition("=")
    version, _, rest = rest.partition(":")
    binary, _, tree = rest.partition(":")
    if not (name and version and binary.startswith("/") and (not tree or tree.startswith("/"))):
        raise ValueError(f"--tool wants NAME=VERSION:BINARY[:TREE], absolute paths and no ':' "
                         f"in the version, got {spec!r}")
    return name, version, binary, tree or None


def image_drop(pin_names) -> frozenset:
    """IMAGE_DROP, once the image tools' names that pins take are exactly it."""
    taken = sorted(set(IMAGE_TOOLS) & set(pin_names))
    if taken != sorted(IMAGE_DROP):
        raise ValueError(f"the pins named like an image tool are {taken}; the image leaves "
                         f"out exactly {sorted(IMAGE_DROP)}")
    return IMAGE_DROP


def predict(pin_file: Path, drop: frozenset) -> dict[str, dict]:
    """Each Nix pin but those in drop: its path, where the image gets it, and its
    requisites' narHash from this host."""
    in_image = image_store_paths()
    found = {}
    for name, path in sorted(pins(pin_file).items()):
        if name in drop:
            continue
        requisites = path_info(path)
        where = "image-store" if path in in_image else "host-store"
        if where == "image-store":
            outside = sorted(set(requisites) - in_image)
            if outside:
                raise ValueError(f"{name}: {path} is in the image's store, but it needs "
                                 f"{outside[0]}, which no closure list names")
        found[name] = {"path": path, "from": where, "requisites": requisites}
    return found


def host_table(pin_file: Path, check_nar: bool, extra: list[str],
               tracked: list[str] = ()) -> tuple[bytes, list[str]]:
    """The table from this host's store, and one line per path --check-nar serialised."""
    tools, every, checked = [], {}, []
    for name, pin in predict(pin_file, frozenset()).items():
        every.update(pin["requisites"])
        tools.append(nix_entry(name, pin["path"], pin["from"], pin["requisites"]))
    if check_nar:
        for path in sorted(image_store_paths()):
            want = every.get(path) or path_info(path)[path]
            got = nar_sha256(path)
            if got != want:
                raise ValueError(f"{path}: serialised NAR sha256 {got}, nix narHash {want}")
            checked.append(f"{got}  {path}")
    tools += [image_entry(*tool_spec(spec)) for spec in extra]
    tools += [tracked_entry(*tracked_spec(spec)) for spec in tracked]
    return table_bytes(tools, closure_sha256(every.values())), checked


# A closureInfo's registration is `nix-store --load-db` input: per path, the
# path, its narHash, its NAR size, its deriver (empty there), a reference count
# and that many references. Nix writes the hash in its own base32.
NIX32 = "0123456789abcdfghijklmnpqrsvwxyz"
STORE_PATH = re.compile(r"/nix/store/[0-9a-z]{32}-[^/]+")


def nix32_to_hex(text: str) -> str:
    """A SHA-256 in Nix's base32 as hex. Nix writes the digest's last bits first."""
    if len(text) != 52 or any(ch not in NIX32 for ch in text):
        raise ValueError(f"not a SHA-256 in Nix's base32: {text!r}")
    out = bytearray(32)
    for n, ch in enumerate(reversed(text)):
        digit = NIX32.index(ch)
        i, j = divmod(n * 5, 8)
        out[i] |= (digit << j) & 0xFF
        carry = digit >> (8 - j)
        if i + 1 < len(out):
            out[i + 1] |= carry
        elif carry:
            raise ValueError(f"not a SHA-256 in Nix's base32: {text!r}")
    return out.hex()


def registration_hash(text: str) -> str:
    """A registration narHash, sha256:<base32> or sha256:<hex>, as hex."""
    m = re.fullmatch(r"sha256:([0-9a-z]{52}|[0-9a-f]{64})", text)
    if not m:
        raise ValueError(f"not a sha256 narHash: {text!r}")
    return m[1] if len(m[1]) == 64 else nix32_to_hex(m[1])


def closure_info(info_dir: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Each path of a closureInfo: its NAR sha256 as hex and its references.
    Read by the file's structure, and refused whole on a record that does not
    fit it, a reference outside it, or a store-paths list naming other paths."""
    reg = info_dir / "registration"
    lines = reg.read_text().splitlines()
    info: dict[str, tuple[str, tuple[str, ...]]] = {}
    i = 0
    while i < len(lines) and any(lines[i:]):
        if i + 5 > len(lines):
            raise ValueError(f"{reg} line {i + 1}: truncated record")
        path, nar, size, _deriver, count = lines[i:i + 5]
        if not STORE_PATH.fullmatch(path) or path in info:
            raise ValueError(f"{reg} line {i + 1}: {path!r} is not a new store path")
        if not (size.isdigit() and count.isdigit()):
            raise ValueError(f"{reg} line {i + 1}: bad size or reference count")
        refs = tuple(lines[i + 5:i + 5 + int(count)])
        if len(refs) != int(count):
            raise ValueError(f"{reg} line {i + 1}: truncated references")
        info[path] = (registration_hash(nar), refs)
        i += 5 + int(count)
    outside = sorted({r for _, refs in info.values() for r in refs} - set(info))
    if outside:
        raise ValueError(f"{reg}: {outside[0]} is referenced and not registered")
    listed = {p for p in (info_dir / "store-paths").read_text().splitlines() if p}
    if listed != set(info):
        first = min(listed ^ set(info))
        raise ValueError(f"{info_dir}: store-paths and the registration differ at {first}")
    return info


def requisites_in(info: dict[str, tuple[str, tuple[str, ...]]], path: str) -> dict[str, str]:
    """Every requisite of path (itself included) and its NAR sha256, from a closureInfo."""
    found: dict[str, str] = {}
    todo = [path]
    while todo:
        p = todo.pop()
        if p not in found:
            found[p] = info[p][0]
            todo.extend(info[p][1])
    return found


def lane_table(info_dir: Path, pin_file: Path, extra: list[str],
               tracked: list[str] = ()) -> bytes:
    """The table for the Nix lane's shell, from its closureInfo alone."""
    info = closure_info(info_dir)
    tools = [nix_entry(name, path, "host-store", requisites_in(info, path))
             for name, path in sorted(pins(pin_file).items()) if path in info]
    tools += [image_entry(*tool_spec(spec)) for spec in extra]
    tools += [tracked_entry(*tracked_spec(spec)) for spec in tracked]
    return table_bytes(tools, closure_sha256(nar for nar, _ in info.values()))


def read_facts(path: Path) -> dict[str, dict]:
    pins_ = json.loads(Path(path).read_text())["pins"]
    for name, pin in pins_.items():
        if pin.get("from") not in ("image-store", "host-store") or pin.get("path") not in \
                pin.get("requisites", {}):
            raise ValueError(f"{path}: {name} is not a pin as `facts` writes one")
    return pins_


def image_table(facts: dict[str, dict], extra: list[str],
                tracked: list[str] = ()) -> tuple[bytes, int]:
    """The table in the image, and how many store paths it serialised.

    A pin from the image's store: each path it needs is serialised here and has
    to be the build host's narHash. A pin from the host's store is not in the
    image, so the host's values stand, and one the image does hold is refused.
    """
    names = sorted([tool_spec(spec)[0] for spec in extra] +
                   [tracked_spec(spec)[0] for spec in tracked])
    if names != sorted(IMAGE_TOOLS):
        raise ValueError(f"the tools outside Nix are {names}; an image names each of "
                         f"{', '.join(IMAGE_TOOLS)} once")
    serialised: dict[str, str] = {}
    tools, every = [], {}
    for name, pin in sorted(facts.items()):
        if name in IMAGE_DROP:
            continue
        path, where, requisites = pin["path"], pin["from"], pin["requisites"]
        if where == "image-store":
            for req, want in sorted(requisites.items()):
                if not os.path.lexists(req):
                    raise ValueError(f"{name}: {req} is not in this image's store")
                if req not in serialised:
                    serialised[req] = nar_sha256(req)
                if serialised[req] != want:
                    raise ValueError(f"{name}: {req} serialises to NAR sha256 {serialised[req]}, "
                                     f"the build host's narHash is {want}")
        elif os.path.lexists(path):
            raise ValueError(f"{name}: {path} is in this image's store, but the build host "
                             f"found it in no closure list, so run.sh mounts it")
        every.update(requisites)
        tools.append(nix_entry(name, path, where, requisites))
    tools += [image_entry(*tool_spec(spec)) for spec in extra]
    tools += [tracked_entry(*tracked_spec(spec)) for spec in tracked]
    return table_bytes(tools, closure_sha256(every.values())), len(serialised)


def differences(got: bytes, want: bytes) -> list[str]:
    """What a written table has other than the one rebuilt, entry by entry."""
    try:
        have = json.loads(got)
        tools = {t["name"]: t for t in have["tools"]}
    except (ValueError, KeyError, TypeError):
        return ["it is not a table"]
    need = json.loads(want)
    out = [f"{key}: {have.get(key)!r}, rebuilt {need[key]!r}"
           for key in ("format", "closure_sha256") if have.get(key) != need[key]]
    rebuilt = {t["name"]: t for t in need["tools"]}
    for name in sorted(set(tools) | set(rebuilt)):
        a, b = tools.get(name), rebuilt.get(name)
        if a != b:
            keys = sorted(k for k in set(a or {}) | set(b or {}) if (a or {}).get(k) != (b or {}).get(k))
            out.append(f"{name}: " + ("not written" if a is None else "not rebuilt" if b is None
                                      else ", ".join(f"{k} {a.get(k)!r}, rebuilt {b.get(k)!r}"
                                                     for k in keys)))
    return out or ["the same entries in other bytes"]


def schema_errors(record, schema_path: Path) -> list[str]:
    import jsonschema  # the image's venv has it; the rest of this file needs none
    validator = jsonschema.Draft202012Validator(json.loads(Path(schema_path).read_text()))
    return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
            for e in validator.iter_errors(record)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    nar = sub.add_parser("nar", help="print a path's NAR sha256, hex")
    nar.add_argument("path")
    host = sub.add_parser("host", help="write the table from this host's store")
    host.add_argument("--pin", type=Path, default=ROOT / "staging.pin")
    host.add_argument("--check-nar", action="store_true")
    host.add_argument("--tool", action="append", default=[], metavar="NAME=VERSION:BINARY[:TREE]")
    host.add_argument("--tracked-tool", action="append", default=[], metavar="NAME=BINARY:TREE")
    host.add_argument("--out", type=Path, required=True)
    lane = sub.add_parser("lane", help="write the table for the Nix lane's shell")
    lane.add_argument("--closure-info", type=Path, required=True)
    lane.add_argument("--pin", type=Path, default=ROOT / "staging.pin")
    lane.add_argument("--tool", action="append", default=[], metavar="NAME=VERSION:BINARY[:TREE]")
    lane.add_argument("--tracked-tool", action="append", default=[], metavar="NAME=BINARY:TREE")
    lane.add_argument("--out", type=Path, required=True)
    facts = sub.add_parser("facts", help="write the build host's prediction for the image")
    facts.add_argument("--pin", type=Path, default=ROOT / "staging.pin")
    facts.add_argument("--out", type=Path, required=True)
    image = sub.add_parser("image", help="write the table in the image")
    verify = sub.add_parser("verify", help="refuse a written table other than the image's")
    for p in (image, verify):
        p.add_argument("--facts", type=Path, required=True)
        p.add_argument("--tool", action="append", default=[],
                       metavar="NAME=VERSION:BINARY[:TREE]")
        p.add_argument("--tracked-tool", action="append", default=[], metavar="NAME=BINARY:TREE")
    image.add_argument("--out", type=Path, required=True)
    verify.add_argument("--table", type=Path, required=True)
    verify.add_argument("--schema", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "nar":
            print(nar_sha256(args.path))
            return 0
        if args.cmd == "facts":
            drop = image_drop(pins(args.pin))
            args.out.write_bytes(canonical({"pins": predict(args.pin, drop)}))
            print(f"{hashlib.sha256(args.out.read_bytes()).hexdigest()}  {args.out}")
            return 0
        if args.cmd in ("image", "verify"):
            data, count = image_table(read_facts(args.facts), args.tool, args.tracked_tool)
            if args.cmd == "verify":
                return verify_table(args.table, data, count, args.schema)
            checked = [f"{count} paths of this image's store serialised, each the build "
                       f"host's narHash"]
        elif args.cmd == "lane":
            data, checked = lane_table(args.closure_info, args.pin, args.tool,
                                       args.tracked_tool), []
        else:
            data, checked = host_table(args.pin, args.check_nar, args.tool, args.tracked_tool)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as err:
        print(f"tool_identity: {err}", file=sys.stderr)
        return 1
    args.out.write_bytes(data)
    for line in checked:
        print(line)
    print(f"{hashlib.sha256(data).hexdigest()}  {args.out}")
    return 0


def verify_table(table: Path, want: bytes, count: int, schema: Path | None) -> int:
    got = table.read_bytes()
    problems = [] if got == want else [f"{table} is not the table this image gives: {d}"
                                       for d in differences(got, want)]
    if schema and not problems:
        problems = [f"{table}: {e}" for e in schema_errors(json.loads(got), schema)]
    for line in problems:
        print(f"tool_identity: {line}", file=sys.stderr)
    if problems:
        return 1
    print(f"{hashlib.sha256(got).hexdigest()}  {table}: the table this image gives, "
          f"{len(json.loads(got)['tools'])} tools; {count} paths of its store serialised, "
          f"each the build host's narHash" + ("; valid against its schema" if schema else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
