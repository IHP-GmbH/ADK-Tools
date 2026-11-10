"""ci/check_image_archive.py on its own: read() and check() on tars built here,
and main() on archives compressed with the real zstd."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from archive_fixture import (BLOB, CONFIG, HEX, ID, LAYOUT, MANIFEST_DIGEST, blob_name,
                             image_tar, members, tar_of)

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_image_archive",
                                              ROOT / "ci" / "check_image_archive.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def held(**kwargs):
    return dict(members(**kwargs))


class ReadTests(unittest.TestCase):

    def read(self, data):
        return checker.read(io.BytesIO(data), ID)

    def test_the_files_it_checks_and_the_small_blobs(self):
        got, faults = self.read(image_tar())
        self.assertEqual(faults, [])
        self.assertEqual(sorted(got), sorted(dict(members())))

    def test_names_written_with_a_leading_dot_slash(self):
        got, faults = self.read(image_tar(prefix="./"))
        self.assertEqual(faults, [])
        self.assertEqual(checker.check(got, ID), (MANIFEST_DIGEST, []))

    def test_a_member_held_twice(self):
        got, faults = self.read(tar_of(members() + [("index.json", b"{}")]))
        self.assertEqual(faults, ["the archive holds index.json twice"])

    def test_a_link_where_a_checked_file_should_be(self):
        for name in ("oci-layout", blob_name(ID)):
            with self.subTest(name=name):
                got, faults = self.read(image_tar(links=(name,)))
                self.assertEqual(faults, ["%s is not a regular file in the archive" % name])
                self.assertNotIn(name, got)

    def test_the_config_is_kept_whatever_its_size(self):
        large = CONFIG + b" " * (checker.SMALL + 1)
        got, _ = self.read(image_tar(blobs={MANIFEST_DIGEST: BLOB, ID: large}))
        self.assertEqual(got[blob_name(ID)], large)


class CheckTests(unittest.TestCase):

    def faults(self, files):
        digest, faults = checker.check(files, ID)
        self.assertIsNone(digest)
        return faults

    def test_a_sound_archive_gives_the_manifest_digest(self):
        self.assertEqual(checker.check(held(), ID), (MANIFEST_DIGEST, []))

    def test_no_manifest_json(self):
        files = held()
        del files["manifest.json"]
        self.assertEqual(self.faults(files), ["the archive holds no manifest.json"])

    def test_a_file_that_is_not_json(self):
        for name in ("manifest.json", "oci-layout", "index.json"):
            with self.subTest(name=name):
                self.assertEqual(self.faults(dict(held(), **{name: b"{"})),
                                 ["%s is not JSON" % name])

    def test_manifest_json_entries_that_are_not_images(self):
        for value in ({}, [HEX], [{"Config": blob_name(ID)}, "x"]):
            with self.subTest(value=value):
                self.assertEqual(
                    self.faults(dict(held(), **{"manifest.json": json.dumps(value).encode()})),
                    ["manifest.json is not a list of images"])

    def test_oci_layout_has_to_be_version_1_0_0(self):
        for layout in (b"{}", b'{"imageLayoutVersion": "1.1.0"}', b"[]"):
            with self.subTest(layout=layout):
                self.assertEqual(self.faults(held(layout=layout)),
                                 ["oci-layout's imageLayoutVersion is not 1.0.0"])

    def test_the_config_has_to_be_held_and_hash_to_the_id(self):
        self.assertEqual(self.faults(held(blobs={MANIFEST_DIGEST: BLOB})),
                         ["the archive does not hold the config %s" % ID])
        self.assertEqual(self.faults(held(blobs={MANIFEST_DIGEST: BLOB, ID: CONFIG + b" "})),
                         ["the config %s does not hash to its name" % ID])


class MainTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self.tmp.name) / "a.tar.zst"

    def tearDown(self):
        self.tmp.cleanup()

    def run_on(self, data, compress=True):
        if compress:
            data = subprocess.run(["zstd", "-q", "-c"], input=data, capture_output=True,
                                  check=True).stdout
        self.archive.write_bytes(data)
        return subprocess.run([sys.executable, "-I", str(ROOT / "ci" / "check_image_archive.py"),
                               ID, str(self.archive)], capture_output=True, text=True,
                              timeout=30, check=False)

    def test_a_sound_archive(self):
        result = self.run_on(image_tar())
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, MANIFEST_DIGEST + "\n", ""))

    def test_a_stream_that_is_not_a_whole_tar(self):
        result = self.run_on(image_tar()[:700])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(result.stderr.startswith("the stream is not a whole tar: "),
                        result.stderr)

    def test_a_file_that_is_not_zstd(self):
        result = self.run_on(image_tar(), compress=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(result.stderr.endswith(
            "%s does not decompress whole (zstd exit 1)\n" % self.archive), result.stderr)

    def test_a_tag_is_refused(self):
        result = subprocess.run([sys.executable, "-I", str(ROOT / "ci" / "check_image_archive.py"),
                                 "adk-tools:x", "a"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
