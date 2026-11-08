"""Check a zstd-compressed docker save tar as the archive of one image.

Usage: python3 -I ci/check_image_archive.py sha256:<64 hex> <archive>

It runs zstd -q -dc on the archive itself and reads the whole stream once,
to its end, through tarfile's stream mode; a stream zstd cannot decompress
whole is a fault of its own. The tar has to hold, as regular files:

  manifest.json  exactly one image, whose Config is blobs/sha256/<the ID's hex>
  the config     that blob, hashing to the ID
  oci-layout     imageLayoutVersion 1.0.0, the OCI layout marker. Without it a
                 daemon on the containerd image store writes a manifest of its
                 own at docker load, so the digest below would not be the one
                 it gives back as the ID.
  index.json     exactly one entry, an OCI image manifest, by digest
  that manifest  under blobs/sha256/, hashing to its name, whose config digest
                 is the ID

On success it prints the manifest's digest and exits 0; otherwise it prints one
line per fault on stderr and exits 1.
"""

import hashlib
import json
import re
import subprocess
import sys
import tarfile

MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
NAMED = ("manifest.json", "oci-layout", "index.json")
LAYOUT_VERSION = "1.0.0"
# The small blobs are kept to find the manifest among them; layers are larger.
# The config blob is kept whatever its size.
SMALL = 1 << 20
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def read(stream, image_id):
    """The named members, the config and the small blobs, by name, and the faults met."""
    config = "blobs/sha256/" + image_id.split(":", 1)[1]
    held, faults = {}, []
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            name = member.name[2:] if member.name.startswith("./") else member.name
            if name not in NAMED and name != config and not (
                    name.startswith("blobs/sha256/") and member.size <= SMALL):
                continue
            if not member.isreg():
                if name in NAMED or name == config:
                    faults.append("%s is not a regular file in the archive" % name)
                continue
            if name in held:
                faults.append("the archive holds %s twice" % name)
                continue
            held[name] = tar.extractfile(member).read()
    return held, faults


def parse(held, name, faults):
    try:
        return json.loads(held[name])
    except (ValueError, UnicodeDecodeError):
        faults.append("%s is not JSON" % name)
        return None


def check(held, image_id):
    """The manifest digest, or None, and the faults."""
    faults = []
    for name in NAMED:
        if name not in held:
            faults.append("the archive holds no %s" % name)
    hex_ = image_id.split(":", 1)[1]
    config = "blobs/sha256/" + hex_
    if "manifest.json" in held:
        images = parse(held, "manifest.json", faults)
        if images is not None:
            if not isinstance(images, list) or not all(isinstance(i, dict) for i in images):
                faults.append("manifest.json is not a list of images")
            elif [i.get("Config") for i in images] != [config]:
                faults.append("manifest.json names %d image(s), not exactly %s"
                              % (len(images), image_id))
    if config not in held:
        faults.append("the archive does not hold the config %s" % image_id)
    elif hashlib.sha256(held[config]).hexdigest() != hex_:
        faults.append("the config %s does not hash to its name" % image_id)
    if "oci-layout" in held:
        layout = parse(held, "oci-layout", faults)
        if layout is not None and (not isinstance(layout, dict)
                                   or layout.get("imageLayoutVersion") != LAYOUT_VERSION):
            faults.append("oci-layout's imageLayoutVersion is not %s" % LAYOUT_VERSION)
    digest = None
    if "index.json" in held:
        index = parse(held, "index.json", faults)
        entries = index.get("manifests") if isinstance(index, dict) else None
        if index is not None:
            if not isinstance(entries, list) or len(entries) != 1:
                faults.append("index.json names %d entries, not one image manifest"
                              % (len(entries) if isinstance(entries, list) else 0))
            elif (not isinstance(entries[0], dict)
                  or entries[0].get("mediaType") != MANIFEST_TYPE
                  or not isinstance(entries[0].get("digest"), str)
                  or not DIGEST.fullmatch(entries[0]["digest"])):
                faults.append("index.json's entry is not an image manifest by sha256 digest")
            else:
                digest = entries[0]["digest"]
    if digest is not None:
        blob = "blobs/sha256/" + digest.split(":", 1)[1]
        if blob not in held:
            faults.append("the archive does not hold the manifest index.json names, %s" % digest)
        elif "sha256:" + hashlib.sha256(held[blob]).hexdigest() != digest:
            faults.append("the manifest %s does not hash to its name" % digest)
        else:
            manifest = parse(held, blob, faults)
            named = manifest.get("config") if isinstance(manifest, dict) else None
            if not isinstance(named, dict) or named.get("digest") != image_id:
                faults.append("the manifest %s is not of the image %s" % (digest, image_id))
    return (None if faults else digest), faults


def main(argv):
    if len(argv) != 3 or not DIGEST.fullmatch(argv[1]):
        print("usage: check_image_archive.py sha256:<64 hex> <archive>", file=sys.stderr)
        return 2
    image_id, archive = argv[1], argv[2]
    try:
        zstd = subprocess.Popen(["zstd", "-q", "-dc", "--", archive], stdout=subprocess.PIPE)
    except OSError as error:
        print("cannot run zstd: %s" % error, file=sys.stderr)
        return 1
    with zstd:
        try:
            held, faults = read(zstd.stdout, image_id)
        except (tarfile.TarError, EOFError) as error:
            held, faults = None, ["the stream is not a whole tar: %s" % error]
        # Read to the end, so zstd's own exit says whether the whole stream was
        # sound, and is never cut short by a closed pipe.
        while zstd.stdout.read(1 << 16):
            pass
    if zstd.returncode != 0:
        print("%s does not decompress whole (zstd exit %d)" % (archive, zstd.returncode),
              file=sys.stderr)
        return 1
    if held is None:
        print(faults[0], file=sys.stderr)
        return 1
    digest, more = check(held, image_id)
    for fault in faults + more:
        print(fault, file=sys.stderr)
    if faults or more:
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
