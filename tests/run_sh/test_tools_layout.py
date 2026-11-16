"""ci/tools_layout.py against a docker stub whose `save` streams a small OCI layout."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ci" / "tools_layout.py"

DOCKER = """#!/bin/sh
echo "docker $*" >> "$LOG"
[ "$1" = save ] && [ -f "$SAVE_TAR" ] && exec cat "$SAVE_TAR"
exit 1
"""


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def oci_tar(path, config=b'{"config":{}}', drop_layer=False, nested=False):
    """A one-image OCI layout as `docker save` writes it (or, nested, as a containerd store
    does: the index names an image index that names the manifest); returns the config and
    manifest digests."""
    layer = b"layer bytes"
    manifest = json.dumps({"schemaVersion": 2, "config": {"digest": digest(config), "size": len(config)},
                           "layers": [{"digest": digest(layer), "size": len(layer)}]}).encode()
    entry = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": digest(manifest),
             "size": len(manifest)}
    blobs = (config, manifest) + (() if drop_layer else (layer,))
    if nested:
        inner = json.dumps({"schemaVersion": 2, "manifests": [entry]}).encode()
        entry = {"mediaType": "application/vnd.oci.image.index.v1+json", "digest": digest(inner),
                 "size": len(inner)}
        blobs += (inner,)
    index = json.dumps({"schemaVersion": 2, "manifests": [entry]}).encode()
    files = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}', "index.json": index}
    for data in blobs:
        files["blobs/sha256/" + digest(data)[7:]] = data
    with tarfile.open(path, "w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return digest(config), digest(manifest)


class ToolsLayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "docker").write_text(DOCKER)
        (self.tmp / "bin" / "docker").chmod(0o755)
        self.root = self.tmp / "cache"
        self.log = self.tmp / "log"
        self.tar = self.tmp / "save.tar"

    def run_it(self, *args):
        env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "LOG": str(self.log),
               "SAVE_TAR": str(self.tar)}
        return subprocess.run([sys.executable, "-B", str(SCRIPT), *args, "--root", str(self.root)],
                              capture_output=True, text=True, env=env)

    def docker_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_the_layout_is_written_once_and_named_as_the_tag_it_replaces(self):
        image_id, manifest = oci_tar(self.tar)
        got = self.run_it("layout", "--ref", "adk-tools:tools-abc", "--id", image_id)
        self.assertEqual(got.returncode, 0, got.stderr)
        final = self.root / "tools-oci" / image_id[7:]
        self.assertEqual(got.stdout.strip(), f"adk-tools:tools-abc=oci-layout://{final}@{manifest}")
        again = self.run_it("layout", "--ref", "adk-tools:tools-abc", "--id", image_id)
        self.assertEqual(again.stdout, got.stdout)
        self.assertEqual(self.docker_calls(), ["docker save adk-tools:tools-abc"])  # not saved again

    def test_a_nested_index_names_its_one_manifest(self):
        image_id, manifest = oci_tar(self.tar, nested=True)
        got = self.run_it("layout", "--ref", "t", "--id", image_id)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(got.stdout.strip().endswith("@" + manifest), got.stdout)

    def test_a_failed_save_leaves_no_layout(self):
        got = self.run_it("layout", "--ref", "t", "--id", digest(b"x"))  # no tar: save exits 1
        self.assertEqual(got.returncode, 1)
        self.assertIn("Refusing:", got.stderr)
        self.assertEqual(list((self.root / "tools-oci").iterdir()), [])

    def test_a_new_tools_image_replaces_the_old_layout(self):
        old_id, _ = oci_tar(self.tar, config=b'{"old":1}')
        self.assertEqual(self.run_it("layout", "--ref", "t", "--id", old_id).returncode, 0)
        new_id, _ = oci_tar(self.tar, config=b'{"new":1}')
        self.assertEqual(self.run_it("layout", "--ref", "t", "--id", new_id).returncode, 0)
        self.assertEqual(sorted(p.name for p in (self.root / "tools-oci").iterdir()), [new_id[7:]])

    def test_a_layout_of_another_image_or_with_a_hole_is_refused_and_left_nowhere(self):
        oci_tar(self.tar)
        other = digest(b"another image")
        got = self.run_it("layout", "--ref", "t", "--id", other)
        self.assertEqual(got.returncode, 1)
        self.assertIn("not the tools image", got.stderr)
        self.assertEqual(list((self.root / "tools-oci").iterdir()), [])
        image_id, _ = oci_tar(self.tar, drop_layer=True)
        got = self.run_it("layout", "--ref", "t", "--id", image_id)
        self.assertIn("missing or not its size", got.stderr)
        self.assertEqual(list((self.root / "tools-oci").iterdir()), [])

    def test_a_layout_that_changed_after_it_was_written_is_refused(self):
        image_id, _ = oci_tar(self.tar)
        self.assertEqual(self.run_it("layout", "--ref", "t", "--id", image_id).returncode, 0)
        index = self.root / "tools-oci" / image_id[7:] / "index.json"
        index.write_text('{"manifests": []}')
        got = self.run_it("layout", "--ref", "t", "--id", image_id)
        self.assertEqual(got.returncode, 1)
        self.assertIn("names 0 manifests", got.stderr)
        self.assertIn("remove ", got.stderr)

    def test_one_cache_is_read_and_replaced_by_the_one_the_build_wrote(self):
        parent = self.root / "tools-buildcache"
        tmp, current = parent / ".tmp", parent / "current"
        got = self.run_it("cache-begin")
        self.assertEqual(got.stdout.split(), [f"--cache-to=type=local,dest={tmp},mode=max"])
        self.assertIn("wrote no cache", self.run_it("cache-commit").stderr)
        for round_ in (1, 2):
            tmp.mkdir()
            (tmp / "index.json").write_text(json.dumps({"round": round_}))
            self.assertEqual(self.run_it("cache-commit").returncode, 0)
            self.assertEqual(sorted(p.name for p in parent.iterdir()), ["current"])
            self.assertEqual(json.loads((current / "index.json").read_text()), {"round": round_})
            got = self.run_it("cache-begin")
            self.assertEqual(got.stdout.split(), [f"--cache-from=type=local,src={current}",
                                                  f"--cache-to=type=local,dest={tmp},mode=max"])
        tmp.mkdir()  # a build killed after writing part of its cache
        (tmp / "partial").write_text("x")
        self.run_it("cache-begin")
        self.assertFalse(tmp.exists())


class RequireSlotTest(unittest.TestCase):
    def test_build_sh_refuses_outside_a_slot_when_asked_and_runs_no_docker(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "docker").write_text(DOCKER)
        (tmp / "docker").chmod(0o755)
        env = {"PATH": f"{tmp}:/usr/bin:/bin", "LOG": str(tmp / "log"), "HOME": str(tmp),
               "ADK_TOOLS_REQUIRE_SLOT": "1"}
        got = subprocess.run(["bash", str(ROOT / "build.sh"), "x"], capture_output=True,
                             text=True, env=env)
        self.assertEqual(got.returncode, 1)
        self.assertIn("ADK_TOOLS_REQUIRE_SLOT=1 and this is not a hostslot slot", got.stderr)
        self.assertFalse((tmp / "log").exists())

if __name__ == "__main__":
    unittest.main()
