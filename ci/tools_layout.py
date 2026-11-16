#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The tools image and its build cache, for a build that runs in a container BuildKit.

The stages below the Dockerfile's thin-layer line start FROM the tools image,
a tag that exists only in the docker daemon's image store. A BuildKit running
in its own container (hostslot gives each slot one, held to the slot's CPUs
and memory) cannot read that store, so build.sh hands it the tools image as an
OCI image layout instead, a named build context whose name is the tag the
Dockerfile names. The layers are the tools image's own tar streams, so the
images built on it still start with the tools image's layers.

The layout lives in <root>/tools-oci/<id>/, <id> being the hex of the tools
image's id (its config digest). It is written from `docker save`, which on
docker 25 and later writes an OCI layout, under a temporary name, checked and
then renamed, so a folder under its final name is always a whole layout. A
layout whose manifest names another config than the image's id is refused,
never reused. Writing one removes every other layout under <root>/tools-oci/.

The tools target's own build cache (it compiles KiCad) is one folder,
<root>/tools-buildcache/current/. A tools build reads it whatever key wrote
it (BuildKit matches cache entries by content) and writes a new one to a
temporary destination, which `cache-commit` puts in its place.

build.sh holds a lock on <root>/lock for the whole build; nothing here locks.
Nothing else reads or prunes the root: `docker system df` does not count it.
It can be removed whenever no build holds the lock; the next build writes the
layout again (a `docker save` of the tools image) and its tools build, if
any, starts without a cache.

Usage:
    tools_layout.py layout --root DIR --ref REF --id sha256:HEX
        write the layout if it is absent, check it, and print the value of
        --build-context: REF=oci-layout://<dir>@sha256:<manifest digest>
    tools_layout.py cache-begin --root DIR
        clear a temporary cache a build left, and print the tools build's cache
        arguments, one per line
    tools_layout.py cache-commit --root DIR
        put the cache the build wrote in place of the one before it
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")


class Refused(Exception):
    pass


def blob(layout: Path, digest: str) -> Path:
    algo, _, hexpart = digest.partition(":")
    if algo != "sha256" or len(hexpart) != 64:
        raise Refused(f"{layout}: {digest!r} is not a sha256 digest")
    return layout / "blobs" / "sha256" / hexpart


def read_blob(layout: Path, desc: dict) -> bytes:
    path = blob(layout, desc["digest"])
    if not path.is_file():
        raise Refused(f"{layout}: blob {desc['digest']} is missing")
    data = path.read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != desc["digest"]:
        raise Refused(f"{layout}: blob {desc['digest']} does not hash to its name")
    return data


def manifest_of(layout: Path, image_id: str) -> str:
    """The digest of the layout's one image manifest, checked against the image's id."""
    try:
        index = json.loads((layout / "index.json").read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"{layout}: no readable index.json ({exc})") from exc
    descs = index.get("manifests") or []
    if len(descs) != 1:
        raise Refused(f"{layout}: index.json names {len(descs)} manifests, not one")
    desc = descs[0]
    if desc.get("mediaType") in INDEX_TYPES:  # one nested index, as a containerd store writes
        nested = json.loads(read_blob(layout, desc)).get("manifests") or []
        if len(nested) != 1:
            raise Refused(f"{layout}: its image index names {len(nested)} manifests, not one")
        desc = nested[0]
    manifest = json.loads(read_blob(layout, desc))
    config = manifest.get("config", {}).get("digest")
    if config != image_id:
        raise Refused(f"{layout}: its manifest's config is {config}, not the tools image {image_id}")
    if not blob(layout, config).is_file():
        raise Refused(f"{layout}: the config blob {config} is missing")
    for layer in manifest.get("layers", []):
        path = blob(layout, layer["digest"])
        if not path.is_file() or path.stat().st_size != layer.get("size"):
            raise Refused(f"{layout}: layer {layer['digest']} is missing or not its size")
    return desc["digest"]


def save(ref: str, dest: Path) -> None:
    """`docker save ref`, unpacked into dest as it streams."""
    proc = subprocess.Popen(["docker", "save", ref], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            tar.extractall(dest, filter="data")
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc:
        raise Refused(f"docker save {ref} exited {rc}")


def layout(root: Path, ref: str, image_id: str) -> str:
    if not image_id.startswith("sha256:"):
        raise Refused(f"{image_id!r} is not an image id")
    parent = root / "tools-oci"
    final = parent / image_id.split(":", 1)[1]
    if not final.is_dir():
        parent.mkdir(parents=True, exist_ok=True)
        tmp = parent / f".tmp-{final.name}-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            save(ref, tmp)
            manifest_of(tmp, image_id)
            tmp.rename(final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        for other in parent.iterdir():
            if other != final:
                shutil.rmtree(other, ignore_errors=True)
    try:
        digest = manifest_of(final, image_id)
    except Refused as exc:
        raise Refused(f"{exc}; remove {final} and build again") from exc
    return f"{ref}=oci-layout://{final}@{digest}"


def cache_paths(root: Path) -> tuple:
    parent = root / "tools-buildcache"
    return parent, parent / "current", parent / ".tmp"


def cache_begin(root: Path) -> list:
    parent, final, tmp = cache_paths(root)
    parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    read = [f"--cache-from=type=local,src={final}"] if (final / "index.json").is_file() else []
    return read + [f"--cache-to=type=local,dest={tmp},mode=max"]


def cache_commit(root: Path) -> None:
    parent, final, tmp = cache_paths(root)
    if not (tmp / "index.json").is_file():
        raise Refused(f"{tmp}: the build wrote no cache")
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("layout")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--ref", required=True)
    p.add_argument("--id", required=True)
    for name in ("cache-begin", "cache-commit"):
        sub.add_parser(name).add_argument("--root", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "layout":
            print(layout(args.root, args.ref, args.id))
        elif args.cmd == "cache-begin":
            print("\n".join(cache_begin(args.root)))
        else:
            cache_commit(args.root)
    except (Refused, OSError, ValueError, tarfile.TarError) as exc:
        print(f"Refusing: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
