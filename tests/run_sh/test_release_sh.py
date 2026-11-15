"""release.sh: which version tags it takes, checked before its first git call.

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
REACHED = "REACHED_GIT"
STUBS = {
    "git": f"#!/bin/sh\necho {REACHED} >&2\nexit 1\n",
    "docker": "#!/bin/sh\necho 'docker must not run' >&2\nexit 99\n",
    "gh": "#!/bin/sh\necho 'gh must not run' >&2\nexit 99\n",
}


class ReleaseVersionTagTest(unittest.TestCase):
    def run_script(self, tag):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, text in STUBS.items():
            (tmp / name).write_text(text)
            (tmp / name).chmod(0o755)
        return subprocess.run(["bash", str(SCRIPT), tag], capture_output=True, text=True,
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


if __name__ == "__main__":
    unittest.main()
