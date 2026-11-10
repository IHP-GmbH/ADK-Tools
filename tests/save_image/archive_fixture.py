"""docker save tars for the archive tests, in the order docker 27 writes one."""

import hashlib
import io
import json
import tarfile

CONFIG = b'{"architecture":"amd64","os":"linux","config":{}}'
HEX = hashlib.sha256(CONFIG).hexdigest()
ID = "sha256:" + HEX
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"


def manifest_blob(config_hex):
    """An image manifest whose config is sha256:<config_hex>, as docker save writes it."""
    return json.dumps({"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                       "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                  "digest": "sha256:" + config_hex, "size": len(CONFIG)},
                       "layers": []}, separators=(",", ":")).encode()


BLOB = manifest_blob(HEX)
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(BLOB).hexdigest()
ENTRY = {"mediaType": MANIFEST_TYPE, "digest": MANIFEST_DIGEST, "size": len(BLOB)}
LAYOUT = b'{"imageLayoutVersion": "1.0.0"}'


def blob_name(digest):
    return "blobs/sha256/" + digest.split(":")[1]


def members(configs=(HEX,), index=None, blobs=None, layout=LAYOUT, indent=None):
    """The (name, bytes) members: the blobs (default: BLOB under its digest and the
    config under the ID), a layer, index.json (default: one entry, BLOB's; False: none),
    manifest.json naming one image per config hex, and oci-layout (None: none)."""
    if index is None:
        index = {"schemaVersion": 2, "manifests": [ENTRY]}
    if blobs is None:
        blobs = {MANIFEST_DIGEST: BLOB, ID: CONFIG}
    manifest = json.dumps([{"Config": "blobs/sha256/%s" % c, "RepoTags": None, "Layers": []}
                           for c in configs], indent=indent)
    out = [(blob_name(d), b) for d, b in blobs.items()]
    out.append(("blobs/sha256/" + "0" * 64, b"layer"))
    if index is not False:
        out.append(("index.json", json.dumps(index).encode()))
    out.append(("manifest.json", manifest.encode()))
    if layout is not None:
        out.append(("oci-layout", layout))
    return out


def tar_of(entries, links=(), prefix=""):
    """A tar of (name, bytes); a name in links is written as a symlink instead."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(prefix + name)
            if name in links:
                info.type, info.linkname = tarfile.SYMTYPE, "elsewhere"
                tar.addfile(info)
                continue
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def image_tar(links=(), prefix="", **kwargs):
    return tar_of(members(**kwargs), links, prefix)
