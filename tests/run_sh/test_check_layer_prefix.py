"""ci/check_layer_prefix.py: an image that does not start with another image's layers is refused."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ci" / "check_layer_prefix.py"


def layers(prefix, n, start=0):
    return [f"sha256:{prefix}{i:062d}" for i in range(start, start + n)]


def run(*args):
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], capture_output=True,
                          text=True)


class LayerPrefixTest(unittest.TestCase):
    def test_an_image_on_every_lower_layer_passes(self):
        tools, ours = layers("aa", 40), layers("bb", 37)
        got = run("tools", json.dumps(tools), "adk-tools:runtime-x", json.dumps(tools + ours))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, "== adk-tools:runtime-x: 40 tools layers, 37 above them\n")

    def test_a_runtime_image_the_verify_image_is_built_on_passes(self):
        runtime = layers("aa", 40) + layers("bb", 37)
        verify = runtime + layers("cc", 41)
        got = run("runtime", json.dumps(runtime), "adk-tools:verify-x", json.dumps(verify))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, "== adk-tools:verify-x: 77 runtime layers, 41 above them\n")

    def test_a_runtime_image_compiled_again_is_refused(self):
        # A runtime build that found none of the verify build's layers: the two share the 40
        # tools layers and the first layer above them, then part (41 of the runtime's 77).
        shared = layers("aa", 41)
        runtime = shared + layers("dd", 36)
        verify = shared + layers("ee", 77)
        got = run("runtime", json.dumps(runtime), "adk-tools:verify-x", json.dumps(verify))
        self.assertEqual(got.returncode, 1)
        self.assertEqual(got.stdout, "")
        self.assertEqual(got.stderr.strip(), "Refusing: adk-tools:verify-x is not built on the "
                                             "runtime image's 77 layers (it shares the first 41)")

    def test_a_shorter_image_is_refused(self):
        lower = layers("aa", 5)
        got = run("tools", json.dumps(lower), "img", json.dumps(lower[:3]))
        self.assertEqual(got.returncode, 1)
        self.assertIn("(it shares the first 3)", got.stderr)

    def test_wrong_arguments_say_the_usage(self):
        got = run("tools", "[]")
        self.assertEqual(got.returncode, 2)
        self.assertIn("Usage: check_layer_prefix.py", got.stderr)

    def test_an_image_docker_could_not_inspect_is_named(self):
        # build.sh passes docker image inspect's output; a failed inspect is an empty argument.
        for bad in ("", "null", '{"a": 1}', "[1, 2]"):
            with self.subTest(bad=bad):
                got = run("runtime", json.dumps(layers("aa", 3)), "adk-tools:verify-x", bad)
                self.assertEqual(got.returncode, 2)
                self.assertIn("adk-tools:verify-x's layers is not a JSON list", got.stderr)
        got = run("tools", "", "img", json.dumps(layers("aa", 3)))
        self.assertEqual(got.returncode, 2)
        self.assertIn("the tools image's layers is not a JSON list", got.stderr)


if __name__ == "__main__":
    unittest.main()
