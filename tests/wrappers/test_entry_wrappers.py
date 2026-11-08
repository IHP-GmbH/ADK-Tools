"""Installed-layout entry wrappers, exercised only with temporary stubs."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TARGETS = {
    "adk-drc": "adk/klayout/drc/run_drc.py",
    "chiplet-export":
        "chiplet_kicad_plugin/plugins/chiplet_export/chiplet_export_cli.py",
}
PREFIX = "${ADK_TOOLS:-/opt/adk-tools}"
STUB = """#!/usr/bin/python3
import json
import os
import sys
print(json.dumps({"argv": sys.argv, "executable": sys.executable}))
sys.exit(int(os.environ.get("WRAPPER_TEST_EXIT", "0")))
"""


class EntryWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="entry wrappers ")
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name) / "installed layout"
        self.env = os.environ.copy()
        self.env["ADK_TOOLS"] = str(self.prefix)
        self.env["WRAPPER_TEST_EXIT"] = "0"
        # adk-drc executes the prefix interpreter; chiplet-export executes
        # its target under /usr/bin/python3. Neither stub imports ADK code.
        for relative in ("venv/bin/python3", *TARGETS.values()):
            path = self.prefix / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(STUB)
            path.chmod(0o755)

    def invoke(self, name, args=()):
        return subprocess.run(
            [str(ROOT / "bin" / name), *args], env=self.env,
            capture_output=True, text=True, timeout=5, check=False)

    def expected_argv(self, name, args):
        target = str(self.prefix / TARGETS[name])
        if name == "adk-drc":
            return [str(self.prefix / "venv/bin/python3"), target, *args]
        return [target, *args]

    def test_custom_prefix_preserves_arguments(self):
        args = ["", "*", "two words", "a;b", "$(exit 91)", "$HOME",
                'single\' and "double"', "\\", "--flag=value", "line\nbreak"]
        for name in TARGETS:
            with self.subTest(wrapper=name):
                result = self.invoke(name, args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                receipt = json.loads(result.stdout)
                self.assertEqual(receipt["argv"], self.expected_argv(name, args))
                self.assertEqual(receipt["executable"], "/usr/bin/python3")

    def test_nonzero_exit_is_propagated(self):
        self.env["WRAPPER_TEST_EXIT"] = "37"
        for name in TARGETS:
            with self.subTest(wrapper=name):
                result = self.invoke(name, ["argument"])
                self.assertEqual(result.returncode, 37, result.stderr)
                self.assertEqual(json.loads(result.stdout)["argv"],
                                 self.expected_argv(name, ["argument"]))

    def test_unset_and_empty_prefix_fallback_without_execution(self):
        for name, target in TARGETS.items():
            source = (ROOT / "bin" / name).read_text()
            # Extract the real quoted expansions, then only print them. In
            # particular, never execute an interpreter or target under /opt.
            expansions = re.findall(r'"(\$\{ADK_TOOLS:-/opt/adk-tools\}/[^"\n]+)"',
                                    source)
            suffixes = (["venv/bin/python3", target]
                        if name == "adk-drc" else [target])
            self.assertEqual(expansions, [PREFIX + "/" + s for s in suffixes])
            if name == "chiplet-export":
                self.assertIn("exec /usr/bin/python3 \\\n", source)
            for value in (None, ""):
                with self.subTest(wrapper=name, prefix=value):
                    env = self.env.copy()
                    env.pop("ADK_TOOLS", None)
                    if value is not None:
                        env["ADK_TOOLS"] = value
                    command = "printf '%s\\n' " + " ".join(
                        '"' + expansion + '"' for expansion in expansions)
                    result = subprocess.run(
                        ["/bin/sh", "-c", command], env=env,
                        capture_output=True, text=True, timeout=5, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(),
                                     ["/opt/adk-tools/" + s for s in suffixes])


ENV_STUB = """#!/usr/bin/python3
import json
import os
import sys
print(json.dumps({"argv": sys.argv[1:],
                  "env": {k: os.environ.get(k) for k in
                          ("PDK_ROOT", "PDK", "LIBRELANE_CHECKOUT")}}))
sys.exit(int(os.environ.get("WRAPPER_TEST_EXIT", "0")))
"""


class ChipletDieFlowWrapperTests(unittest.TestCase):
    """bin/chiplet-die-flow: every build-die reads the sg13g2 tree
    ADK_NIX_SG13G2_PDK names, set by nix/build-env.sh's adk_nix_die_build_env;
    every other call passes through unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chiplet-die-flow wrapper ")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.prefix = base / "installed layout"
        flow = self.prefix / "venv/bin/chiplet-die-flow"
        flow.parent.mkdir(parents=True)
        flow.write_text(ENV_STUB)
        flow.chmod(0o755)
        # The image's copy of build-env.sh, where the wrapper sources it.
        (self.prefix / "nix").mkdir()
        (self.prefix / "nix/build-env.sh").write_bytes((ROOT / "nix/build-env.sh").read_bytes())
        self.pdk = base / "store" / "hash-ihp-open-pdk-22f4335"
        (self.pdk / "ihp-sg13g2/libs.tech/librelane").mkdir(parents=True)
        self.env = {"PATH": "/usr/bin:/bin", "ADK_TOOLS": str(self.prefix),
                    "PDK_ROOT": "/opt/adk-tools/IHP-Open-PDK",
                    "LIBRELANE_CHECKOUT": "/somewhere/librelane",
                    "ADK_NIX_SG13G2_PDK": str(self.pdk)}

    def invoke(self, *args):
        result = subprocess.run([str(ROOT / "bin" / "chiplet-die-flow"), *args], env=self.env,
                                capture_output=True, text=True, timeout=5, check=False)
        return result

    def receipt(self, *args, stderr=""):
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["argv"], list(args))
        return receipt["env"]

    def unchanged(self):
        return {"PDK_ROOT": "/opt/adk-tools/IHP-Open-PDK", "PDK": None,
                "LIBRELANE_CHECKOUT": "/somewhere/librelane"}

    def store_env(self):
        return {"PDK_ROOT": str(self.pdk), "PDK": "ihp-sg13g2", "LIBRELANE_CHECKOUT": None}

    def test_build_die_reads_the_sg13g2_tree(self):
        args = ("build-die", "--instance", "u_a", "--run-dir", "/work/a b",
                "--technology", "sg13g2", "--run-tag", "$(exit 9)")
        self.assertEqual(self.receipt(*args), self.store_env())

    def test_a_root_the_caller_names_reaches_build_die_unchanged(self):
        # The wrapper reads no argument: build-die's own --pdk-root replaces
        # PDK_ROOT for its build (the die flow's
        # test_the_pdk_root_given_replaces_the_environment_for_the_build), so
        # the flag reaching it as given is all this side holds.
        for given in (("--pdk-root", "/pdk"), ("--pdk-root=/pdk",), ("--pd=/pdk",)):
            with self.subTest(given=given):
                self.assertEqual(self.receipt("build-die", "--instance", "u_a", *given),
                                 self.store_env())

    def test_the_environment_is_build_env_sh_s(self):
        # One copy: what the wrapper hands on is what adk_nix_die_build_env
        # exports in the build shell.
        shell = subprocess.run(
            ["bash", "-c", 'ADK_NIX_LIB_ONLY=1 . "$1/nix/build-env.sh"; '
                           'adk_nix_die_build_env "$2"; python3 -c "$3"',
             "_", str(ROOT), str(self.pdk),
             "import json, os; print(json.dumps({k: os.environ.get(k) for k in "
             "('PDK_ROOT', 'PDK', 'LIBRELANE_CHECKOUT')}))"],
            env=self.env, capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(self.receipt("build-die"), json.loads(shell.stdout))
        (self.prefix / "nix/build-env.sh").unlink()
        result = self.invoke("build-die")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_other_calls_pass_through(self):
        for args in (("die-view", "--config", "c.yaml"), ("emit-ready-die",), ("--help",),
                     ("status",), ()):
            with self.subTest(args=args):
                self.assertEqual(self.receipt(*args), self.unchanged())

    def test_without_the_store_tree_nothing_changes(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.env.pop("ADK_NIX_SG13G2_PDK", None)
                if value is not None:
                    self.env["ADK_NIX_SG13G2_PDK"] = value
                self.assertEqual(self.receipt("build-die", "--instance", "u_a"),
                                 self.unchanged())

    def test_a_tree_without_librelane_files_is_a_note(self):
        # Not a refusal: a build-die that names its own --pdk-root reads
        # that one and is not this tree's to stop.
        self.env["ADK_NIX_SG13G2_PDK"] = str(self.pdk.parent)
        note = ("chiplet-die-flow: note: ADK_NIX_SG13G2_PDK (%s) has no "
                "ihp-sg13g2/libs.tech/librelane; build-die reads the root its --pdk-root "
                "names, else the image's PDK_ROOT, which holds no LibreLane files\n"
                % self.pdk.parent)
        self.assertEqual(self.receipt("build-die", "--pdk-root", "/pdk", stderr=note),
                         self.unchanged())

    def test_librelane_without_a_launcher_names_run_sh_s_note(self):
        self.env["ADK_NIX_LIBRELANE"] = "/nix/store/x-python3.13-librelane-3.0.0rc0"
        note = ("chiplet-die-flow: note: ADK_NIX_LIBRELANE is set and no "
                "CHIPLET_FLOW_LIBRELANE_LAUNCHER was passed in; run.sh's start-up notes "
                "say why\n")
        self.assertEqual(self.receipt("build-die", stderr=note), self.store_env())
        self.env["CHIPLET_FLOW_LIBRELANE_LAUNCHER"] = "/opt/adk-tools/librelane-launcher/librelane"
        self.assertEqual(self.receipt("build-die"), self.store_env())
        self.assertEqual(self.receipt("die-view"), self.unchanged())

    def test_a_pdk_root_the_caller_set_is_kept(self):
        self.env["PDK_ROOT"] = "/my/pdk"
        note = ("chiplet-die-flow: note: PDK_ROOT (/my/pdk) is not the image's; build-die reads it, "
                "not ADK_NIX_SG13G2_PDK (%s)\n" % self.pdk)
        self.assertEqual(self.receipt("build-die", stderr=note),
                         {**self.unchanged(), "PDK_ROOT": "/my/pdk"})
        del self.env["PDK_ROOT"]
        self.assertEqual(self.receipt("build-die"), self.store_env())

    def test_nothing_the_caller_exports_runs_first(self):
        # bash -p: no BASH_ENV is read and no exported function is imported.
        marker = Path(self.tmp.name) / "ran"
        sourced = Path(self.tmp.name) / "bash env"
        sourced.write_text("touch '%s'\n" % marker)
        self.env["BASH_ENV"] = str(sourced)
        self.env["BASH_FUNC_adk_nix_die_build_env%%"] = "() { export PDK_ROOT=/elsewhere; }"
        self.assertEqual(self.receipt("build-die"), self.store_env())
        self.assertFalse(marker.exists())
        # A function the script calls before it sources anything: echo, for the
        # note on a PDK_ROOT the caller set. Imported, it would run instead.
        del self.env["BASH_FUNC_adk_nix_die_build_env%%"]
        self.env["BASH_FUNC_echo%%"] = "() { touch '%s'; }" % marker
        self.env["PDK_ROOT"] = "/my/pdk"
        note = ("chiplet-die-flow: note: PDK_ROOT (/my/pdk) is not the image's; build-die reads it, "
                "not ADK_NIX_SG13G2_PDK (%s)\n" % self.pdk)
        self.assertEqual(self.receipt("build-die", stderr=note),
                         {**self.unchanged(), "PDK_ROOT": "/my/pdk"})
        self.assertFalse(marker.exists())

    def test_the_image_s_roots_are_not_a_caller_s(self):
        # The image's root or the store tree, however the path is spelled.
        for root in ("/opt/adk-tools/IHP-Open-PDK/", "/opt//adk-tools/./IHP-Open-PDK",
                     str(self.pdk), str(self.pdk) + "//"):
            with self.subTest(root=root):
                self.env["PDK_ROOT"] = root
                self.assertEqual(self.receipt("build-die"), self.store_env())

    def test_the_root_directory_is_another_root(self):
        self.env["PDK_ROOT"] = "/"
        note = ("chiplet-die-flow: note: PDK_ROOT (/) is not the image's; build-die reads it, "
                "not ADK_NIX_SG13G2_PDK (%s)\n" % self.pdk)
        self.assertEqual(self.receipt("build-die", stderr=note), {**self.unchanged(), "PDK_ROOT": "/"})

    def test_the_exit_is_the_flow_s(self):
        self.env["WRAPPER_TEST_EXIT"] = "7"
        self.assertEqual(self.invoke("build-die", "--instance", "u_a").returncode, 7)


if __name__ == "__main__":
    unittest.main()
