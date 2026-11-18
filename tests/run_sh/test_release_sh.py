"""release.sh: which version tags and image IDs it takes, checked before its first git call.

The script runs against a git stub that marks the first call and stops it, and against docker
and gh stubs that fail loudly, so no case reaches a registry.
"""

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "release.sh"
REFUSED = "error: VERSION_TAG must look like vYYYY.MM or vYYYY.MM.N"
ID_REFUSED = "error: the image must be given by its ID"
IMAGE_ID = "sha256:" + "0123456789abcdef" * 4
REACHED = "REACHED_GIT"
STUBS = {
    "git": f"#!/bin/sh\necho {REACHED} >&2\nexit 1\n",
    "docker": "#!/bin/sh\necho 'docker must not run' >&2\nexit 99\n",
    "gh": "#!/bin/sh\necho 'gh must not run' >&2\nexit 99\n",
}


class ReleaseVersionTagTest(unittest.TestCase):
    def run_script(self, tag, image_id=IMAGE_ID):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, text in STUBS.items():
            (tmp / name).write_text(text)
            (tmp / name).chmod(0o755)
        args = [tag] if image_id is None else [tag, image_id]
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                              env={"PATH": f"{tmp}:/usr/bin:/bin"}, timeout=60)

    def test_a_release_and_a_point_release_reach_the_provenance_checks(self):
        for tag in ("v2026.11", "v2026.11.1", "v2026.11.2", "v2026.11.10"):
            with self.subTest(tag=tag):
                got = self.run_script(tag)
                self.assertIn(REACHED, got.stderr)
                self.assertNotIn(REFUSED, got.stderr)

    def test_any_other_tag_is_refused_before_git(self):
        for tag in ("2026.11", "v2026.1", "v2026.11.", "v2026.11.0", "v2026.11.02",
                    "v2026.11.2.1", "v2026.11.x", "v2026.11-rc1", "v2026.11.2 "):
            with self.subTest(tag=tag):
                got = self.run_script(tag)
                self.assertEqual(got.returncode, 1, got.stderr)
                self.assertIn(REFUSED, got.stderr)
                self.assertNotIn(REACHED, got.stderr)

    def test_an_image_not_given_by_its_id_is_refused_before_git(self):
        for image_id in (None, "", "adk-tools:dev", "runtime-dd0e32d", IMAGE_ID[:-1],
                         IMAGE_ID.upper(), IMAGE_ID + "0", IMAGE_ID[len("sha256:"):]):
            with self.subTest(image_id=image_id):
                got = self.run_script("v2026.11.3", image_id)
                self.assertEqual(got.returncode, 1, got.stderr)
                self.assertIn(ID_REFUSED, got.stderr)
                self.assertNotIn(REACHED, got.stderr)


if __name__ == "__main__":
    unittest.main()
