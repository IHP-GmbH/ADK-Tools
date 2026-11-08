"""save-image.sh, run against a docker stub that serves one image.

The stub answers `docker image inspect --format {{.Id}} <ref>` with
SAVE_TEST_ID and `docker save <ref>` with the bytes of SAVE_TEST_TAR, so the
cases cover what the script adds: the ID rule, the read-back through
ci/check_image_archive.py (its own cases are in test_check_image_archive.py),
the name, the SHA256SUMS line, and that a refusal leaves nothing behind.
Stubs in front of the real zstd and ln can spoil the compressed stream, or
change the directory at the step a race would.
"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from archive_fixture import BLOB, CONFIG, ENTRY, HEX, ID, MANIFEST_DIGEST, image_tar, manifest_blob

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "save-image.sh"
NAME = "adk-tools-runtime-%s.tar.zst" % HEX[:12]
DOCKER = """#!/bin/sh
echo "$*" >> "$SAVE_TEST_CALLS"
case "$1 $2" in
    "image inspect")
        [ -n "$SAVE_TEST_ID" ] || { echo "Error: No such image: $4" >&2; exit 1; }
        echo "$SAVE_TEST_ID" ;;
    "save $ID")
        [ -z "$SAVE_TEST_SAVE_FAILS" ] || { echo "Error: save failed" >&2; exit 1; }
        [ -z "$SAVE_TEST_SLOW" ] || sleep "$SAVE_TEST_SLOW"
        [ -z "$SAVE_TEST_PUT" ] || echo other > "$SAVE_TEST_PUT"
        [ -z "$SAVE_TEST_LIST" ] || printf '%064d  %s\n' 0 "$SAVE_TEST_LIST" >> "$SAVE_TEST_SUMS"
        cat "$SAVE_TEST_TAR" ;;
    *) echo "stub: unexpected docker $*" >&2; exit 2 ;;
esac
""".replace("$ID", ID)
# Compression with SAVE_TEST_TAIL set appends bytes that are no zstd frame.
ZSTD = """#!/bin/sh
case " $* " in
    *" -dc "*) exec "$REAL" "$@" ;;
esac
"$REAL" "$@" || exit
[ -z "$SAVE_TEST_TAIL" ] || printf 'not a frame'
"""
# The link into place: SAVE_TEST_TAKE takes the name just before it, as another
# run would; SAVE_TEST_LIST_AFTER lists the name just after it.
LN = """#!/bin/sh
for last; do :; done
[ -z "$SAVE_TEST_TAKE" ] || echo other > "$last"
"$REAL" "$@" || exit
[ -z "$SAVE_TEST_LIST_AFTER" ] || printf '%064d  %s\n' 0 "$SAVE_TEST_LIST_AFTER" >> "$SAVE_TEST_SUMS"
"""


class SaveImageTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        stub = base / "bin"
        stub.mkdir()
        for name, text in (("docker", DOCKER), ("zstd", ZSTD), ("ln", LN)):
            (stub / name).write_text(text.replace('"$REAL"', '"%s"' % shutil.which(name)))
            (stub / name).chmod(0o755)
        self.out = base / "out dir"
        self.out.mkdir()
        self.tar = base / "image.tar"
        self.tar.write_bytes(image_tar())
        self.calls = base / "calls"
        self.env = {"PATH": "%s:/usr/bin:/bin" % stub, "SAVE_TEST_ID": ID,
                    "SAVE_TEST_TAR": str(self.tar), "SAVE_TEST_CALLS": str(self.calls)}
        self.sums = self.out / "SHA256SUMS"
        self.env["SAVE_TEST_SUMS"] = str(self.sums)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, *args, **env):
        return subprocess.run([str(SCRIPT), *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=30, check=False)

    def files(self):
        return sorted(p.name for p in self.out.iterdir())

    def assert_refused(self, result, line, files=()):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "error: %s\n" % line)
        self.assertEqual(self.files(), sorted(files))

    def assert_not_kept(self, faults, tar=None):
        """The archive of tar is refused with the checker's fault lines, and not kept."""
        if tar is not None:
            self.tar.write_bytes(tar)
        result = self.save(ID, str(self.out))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "".join(f + "\n" for f in faults) +
                         "error: %s is not kept: it does not read back as the image %s (above)\n"
                         % (NAME, ID))
        self.assertEqual(self.files(), [])

    def test_the_archive_its_line_and_its_bytes(self):
        result = self.save(ID, str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.out / NAME
        data = archive.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(result.stdout,
                         "save-image: %s, %d bytes, sha256 %s, config %s, manifest %s\n"
                         % (archive, len(data), digest, ID, MANIFEST_DIGEST))
        self.assertEqual(self.sums.read_text(), "%s  %s\n" % (digest, NAME))
        back = subprocess.run(["zstd", "-q", "-dc", str(archive)], capture_output=True,
                              check=True).stdout
        self.assertEqual(back, self.tar.read_bytes())
        self.assertEqual(self.files(), ["SHA256SUMS", NAME])
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o666 & ~mask)

    def test_manifest_json_is_read_as_json(self):
        self.tar.write_bytes(image_tar(indent=2))
        self.assertEqual(self.save(ID, str(self.out)).returncode, 0)

    def test_a_second_archive_adds_its_line(self):
        self.sums.write_text("%s  other.tar.zst\n" % ("0" * 64))
        self.assertEqual(self.save(ID, str(self.out)).returncode, 0)
        lines = self.sums.read_text().splitlines()
        self.assertEqual([l.split("  ")[1] for l in lines], ["other.tar.zst", NAME])

    def test_a_tag_is_refused(self):
        self.assert_refused(self.save("adk-tools:runtime-x", str(self.out)),
                            "the image must be an ID, sha256:<64 hex> (got 'adk-tools:runtime-x')")
        self.assertFalse(self.calls.exists())

    def test_an_image_the_daemon_does_not_hold(self):
        self.assert_refused(self.save(ID, str(self.out), SAVE_TEST_ID=""),
                            "no image %s on this docker daemon" % ID)

    def test_the_daemon_gives_another_id(self):
        other = "sha256:" + "cd" * 32
        self.assert_refused(self.save(ID, str(self.out), SAVE_TEST_ID=other),
                            "docker image inspect %s gave another ID: '%s'" % (ID, other))

    def test_an_archive_of_another_config_is_not_kept(self):
        for configs in (["cd" * 32], [HEX, "cd" * 32], []):
            with self.subTest(configs=configs):
                self.assert_not_kept(["manifest.json names %d image(s), not exactly %s"
                                      % (len(configs), ID)], image_tar(configs=configs))

    def test_an_archive_without_oci_layout_or_index_json_is_not_kept(self):
        self.assert_not_kept(["the archive holds no oci-layout"], image_tar(layout=None))
        self.assert_not_kept(["the archive holds no index.json"], image_tar(index=False))

    def test_a_file_the_check_reads_has_to_be_a_regular_file(self):
        self.assert_not_kept(["oci-layout is not a regular file in the archive",
                              "the archive holds no oci-layout"],
                             image_tar(links=("oci-layout",)))

    def test_an_index_of_other_than_one_image_manifest_is_not_kept(self):
        nested = dict(ENTRY, mediaType="application/vnd.oci.image.index.v1+json")
        for index, fault in (
                ({"manifests": []}, "index.json names 0 entries, not one image manifest"),
                ({"manifests": [ENTRY, ENTRY]}, "index.json names 2 entries, not one image manifest"),
                ([ENTRY], "index.json names 0 entries, not one image manifest"),
                ({"manifests": [nested]},
                 "index.json's entry is not an image manifest by sha256 digest"),
                ({"manifests": [dict(ENTRY, digest="sha256:12")]},
                 "index.json's entry is not an image manifest by sha256 digest")):
            with self.subTest(index=index):
                self.assert_not_kept([fault], image_tar(index=index))

    def test_the_manifest_the_index_names_has_to_be_held_and_be_the_image_s(self):
        self.assert_not_kept(["the archive does not hold the manifest index.json names, %s"
                              % MANIFEST_DIGEST], image_tar(blobs={ID: CONFIG}))
        self.assert_not_kept(["the manifest %s does not hash to its name" % MANIFEST_DIGEST],
                             image_tar(blobs={MANIFEST_DIGEST: BLOB + b" ", ID: CONFIG}))
        other = manifest_blob("cd" * 32)
        other_digest = "sha256:" + hashlib.sha256(other).hexdigest()
        self.assert_not_kept(["the manifest %s is not of the image %s" % (other_digest, ID)],
                             image_tar(index={"manifests": [dict(ENTRY, digest=other_digest)]},
                                       blobs={other_digest: other, ID: CONFIG}))

    def test_a_stream_that_does_not_decompress_whole_is_not_kept(self):
        result = self.save(ID, str(self.out), SAVE_TEST_TAIL="1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertRegex(result.stderr, r"\.%s\.\w+\.part does not decompress whole "
                         r"\(zstd exit 1\)\nerror: %s is not kept: " % (NAME, NAME))
        self.assertEqual(self.files(), [])

    def test_a_failed_save_leaves_nothing(self):
        result = self.save(ID, str(self.out), SAVE_TEST_SAVE_FAILS="1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(result.stderr.endswith(
            "error: docker save %s or its compression failed\n" % ID), result.stderr)
        self.assertEqual(self.files(), [])

    def test_nothing_is_overwritten(self):
        (self.out / NAME).write_bytes(b"old")
        self.assert_refused(self.save(ID, str(self.out)),
                            "%s is already there" % (self.out / NAME), [NAME])
        self.assertEqual((self.out / NAME).read_bytes(), b"old")

    def test_a_line_for_the_name_in_any_form_sha256sum_reads_is_refused(self):
        for line in ("%s  %s\n" % ("0" * 64, NAME), "%s *%s\n" % ("0" * 64, NAME),
                     "%s  %s\n" % ("AB" * 32, NAME), "%s  %s\r\n" % ("0" * 64, NAME)):
            with self.subTest(line=line):
                self.sums.write_text(line, newline="")
                self.assert_refused(self.save(ID, str(self.out)),
                                    "%s already lists %s" % (self.sums, NAME), ["SHA256SUMS"])
        self.assertFalse(self.calls.exists())

    def test_a_sha256sums_without_a_final_newline_is_refused(self):
        self.sums.write_text("%s  other.tar.zst" % ("0" * 64))
        self.assert_refused(self.save(ID, str(self.out)),
                            "%s does not end in a newline; the archive's line would join its last"
                            % self.sums, ["SHA256SUMS"])

    def test_an_archive_put_in_place_meanwhile_is_kept_and_this_one_is_not(self):
        result = self.save(ID, str(self.out), SAVE_TEST_PUT=str(self.out / NAME))
        self.assert_refused(result, "%s is already there" % (self.out / NAME), [NAME])
        self.assertEqual((self.out / NAME).read_text(), "other\n")

    def test_a_name_taken_just_before_the_link_is_kept_and_this_one_is_not(self):
        result = self.save(ID, str(self.out), SAVE_TEST_TAKE="1")
        self.assert_refused(result, "%s is already there" % (self.out / NAME), [NAME])
        self.assertEqual((self.out / NAME).read_text(), "other\n")

    def test_a_line_added_during_the_save_is_seen_before_the_link(self):
        result = self.save(ID, str(self.out), SAVE_TEST_LIST=NAME)
        self.assert_refused(result, "%s already lists %s" % (self.sums, NAME), ["SHA256SUMS"])

    def test_a_line_added_after_the_link_removes_this_archive(self):
        result = self.save(ID, str(self.out), SAVE_TEST_LIST_AFTER=NAME)
        self.assert_refused(result, "%s changed while %s was saved" % (self.sums, NAME),
                            ["SHA256SUMS"])
        self.assertEqual(len(self.sums.read_text().splitlines()), 1)

    def test_two_runs_at_once_keep_one_archive_and_one_line(self):
        env = dict(self.env, SAVE_TEST_SLOW="0.5")
        runs = [subprocess.Popen([str(SCRIPT), ID, str(self.out)], env=env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(2)]
        results = [(r.wait(timeout=30), r.communicate()) for r in runs]
        self.assertEqual(sorted(rc for rc, _ in results), [0, 1], results)
        refused = [err for rc, (_, err) in results if rc == 1][0]
        self.assertIn(refused, ("error: %s is already there\n" % (self.out / NAME),
                                "error: %s already lists %s\n" % (self.sums, NAME)))
        self.assertEqual(self.files(), ["SHA256SUMS", NAME])
        self.assertEqual(len(self.sums.read_text().splitlines()), 1)

    def test_a_stale_partial_file_is_left_as_it_is(self):
        stale = self.out / (".%s.deadbeef.part" % NAME)
        stale.write_bytes(b"killed run")
        self.assertEqual(self.save(ID, str(self.out)).returncode, 0)
        self.assertEqual(self.files(), sorted(["SHA256SUMS", NAME, stale.name]))
        self.assertEqual(stale.read_bytes(), b"killed run")

    def test_an_archive_whose_line_cannot_be_added_is_removed(self):
        self.sums.write_text("")
        self.sums.chmod(0o444)
        if os.access(self.sums, os.W_OK):
            self.skipTest("this user writes a read-only file")
        result = self.save(ID, str(self.out))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(result.stderr.endswith(
            "error: could not add %s's line to %s\n" % (NAME, self.sums)), result.stderr)
        self.assertEqual(self.files(), ["SHA256SUMS"])
        self.assertEqual(self.sums.read_text(), "")

    def test_a_directory_that_is_not_there(self):
        missing = self.out / "nope"
        self.assert_refused(self.save(ID, str(missing)), "%s is not a directory" % missing)


if __name__ == "__main__":
    unittest.main()
