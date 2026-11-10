"""The image writes into /work only when asked, and it carries no examples.

adk-entrypoint runs on every start and must leave /work alone. adk-example
copies nothing: the examples live in an adk-examples checkout that run.sh
mounts at /examples, so it always refuses, and its message gives the host line
that starts the image with one.
"""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ADK_EXAMPLE = ROOT / "bin" / "adk-example"
ENTRYPOINT = ROOT / "bin" / "adk-entrypoint"
RUN_LINE = ('"${ADK_TOOLS_ROOT:-$HOME/adk-tools}/run.sh" '
            '--examples "${ADK_EXAMPLES_ROOT:-$HOME/adk-examples}"')


class AdkExample(unittest.TestCase):

    def run_it(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            r = subprocess.run([str(ADK_EXAMPLE), *args], cwd=tmp,
                               env=dict(os.environ, HOME=str(home)),
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["home"])
            self.assertEqual(list(home.iterdir()), [])
        return r

    def test_it_always_refuses_and_writes_nothing(self):
        for args in ((), ("--refresh",), ("--force",)):
            with self.subTest(args=args):
                r = self.run_it(*args)
                self.assertEqual(r.returncode, 1)
                self.assertEqual(r.stdout, "")
                self.assertIn("carries no examples", r.stderr)

    def test_the_message_gives_the_host_line(self):
        r = self.run_it()
        self.assertIn(RUN_LINE, r.stderr)
        self.assertIn("/examples", r.stderr)

    def test_help_prints_the_same_message(self):
        for flag in ("-h", "--help"):
            r = self.run_it(flag)
            self.assertEqual(r.returncode, 0)
            self.assertIn(RUN_LINE, r.stderr)


class Entrypoint(unittest.TestCase):

    def test_its_code_never_names_work(self):
        # Comments may say where the user's folder is; no command may touch it.
        code = [line for line in ENTRYPOINT.read_text().splitlines()
                if not line.lstrip().startswith("#")]
        self.assertEqual([line for line in code if re.search(r"/work\b", line)], [])

    def test_it_still_runs_the_command(self):
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([str(ENTRYPOINT), "echo", "ran"],
                               env=dict(os.environ, HOME=home),
                               capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, "ran\n"), r.stderr)



class AdkSmoke(unittest.TestCase):
    """adk-smoke has no default board: the only demo board under /opt/adk-tools
    is the verify stage's copy, so a default would name a path the runtime
    image does not have."""

    SMOKE = ROOT / "bin" / "adk-smoke"

    def run_it(self, *args):
        return subprocess.run([str(self.SMOKE), *args], capture_output=True,
                              text=True, timeout=30)

    def test_no_board_is_refused_in_one_line(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(r.stderr.splitlines()), 1)
        self.assertIn("adk-smoke /work/example/kicad/two_die_interposer.kicad_pcb", r.stderr)

    def test_a_missing_board_is_named(self):
        r = self.run_it("/nonexistent/board.kicad_pcb")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, "adk-smoke: no board at /nonexistent/board.kicad_pcb\n")

    def test_the_script_names_no_image_path_as_a_default(self):
        code = [line for line in self.SMOKE.read_text().splitlines()
                if not line.lstrip().startswith("#")]
        self.assertFalse([line for line in code if "/opt/adk-tools/examples" in line])

    def test_verify_passes_the_board_it_copies(self):
        text = (ROOT / "Dockerfile").read_text()
        board = "/opt/adk-tools/examples/two_die_interposer/kicad/two_die_interposer.kicad_pcb"
        self.assertIn("\nCOPY examples /opt/adk-tools/examples\n", text)
        self.assertIn("\nRUN adk-smoke %s\n" % board, text)
        self.assertTrue((ROOT / board.replace("/opt/adk-tools/", "")).is_file())

if __name__ == "__main__":
    unittest.main()
