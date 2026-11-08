"""ci/check_tools_locks.py: docker/*.lock against the tools key they were
recorded for, on made-up locks and on this tree's own."""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "ci" / "check_tools_locks.py"
KEY = "a" * 64
MOVED = "b" * 64


class CheckToolsLocksTests(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "docker").mkdir()
        for name in ("apt.lock", "pip.lock"):
            (self.dir / "docker" / name).write_text("%s 1\n" % name)

    def run_check(self, *args, key=KEY):
        return subprocess.run(
            [sys.executable, "-B", str(CHECK), "--key", key, *args],
            cwd=self.dir, capture_output=True, text=True, timeout=30)

    def recorded(self):
        res = self.run_check("--write")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_locks_recorded_for_this_key_are_ok(self):
        self.recorded()
        res = self.run_check()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "tools locks: OK, read from the tools image keyed aaaaaaaaaaaa\n")

    def test_a_key_that_moved_without_its_locks_is_refused(self):
        self.recorded()
        res = self.run_check(key=MOVED)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("the locks were read from the tools image keyed aaaaaaaaaaaa, and this "
                      "tree keys bbbbbbbbbbbb", res.stderr)
        self.assertIn("build.sh --tools-only", res.stderr)

    def test_a_lock_that_moved_without_its_record_is_refused(self):
        self.recorded()
        (self.dir / "docker" / "apt.lock").write_text("apt.lock 2\n")
        res = self.run_check()
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("docker/apt.lock is not the file recorded for the tools image keyed "
                      "aaaaaaaaaaaa", res.stderr)
        self.assertNotIn("pip.lock", res.stderr)

    def test_a_missing_record_or_lock_is_unreadable(self):
        res = self.run_check()
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("tools locks: UNREADABLE: docker/tools-locks.txt is missing", res.stderr)
        self.recorded()
        (self.dir / "docker" / "pip.lock").unlink()
        res = self.run_check()
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("docker/pip.lock is missing", res.stderr)

    def test_a_record_naming_no_lock_is_unreadable(self):
        (self.dir / "docker" / "tools-locks.txt").write_text("key %s\n" % KEY)
        res = self.run_check()
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("names no apt.lock, pip.lock", res.stderr)

    def test_a_key_that_is_not_one_is_unreadable(self):
        res = self.run_check(key="bf0b7c3066b0")
        self.assertEqual(res.returncode, 2, res.stderr)

    def test_this_trees_locks_are_the_ones_recorded(self):
        # The key is build.sh's to check; the two files have to be the ones
        # recorded with it.
        record = (ROOT / "docker" / "tools-locks.txt").read_text()
        key = re.search(r"^key ([0-9a-f]{64})$", record, re.M).group(1)
        res = subprocess.run([sys.executable, "-B", str(CHECK), "--key", key], cwd=ROOT,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
