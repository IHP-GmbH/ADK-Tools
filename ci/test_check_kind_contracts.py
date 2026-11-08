#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The exec frame of check_kind_contracts.py, on the host.

packaging-flow's key module is stood in for by the few functions the frame
calls, so the frame's own reading of argv[0] is what is under test: a tool's
binary, the venv's python as the python row's tree names it with a script
row's binary after it, and a sh wrapper that runs the venv's python. The
tools are sh scripts that answer as the real ones do.

Usage: python3 -B ci/test_check_kind_contracts.py [-v]
"""
import contextlib
import inspect
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def stub_packaging_flow():
    """packaging_flow.verification.{key, kinds, registry}, as far as the frame reads them."""
    key = types.ModuleType("packaging_flow.verification.key")

    def tool_paths(table, names):
        rows = {entry["name"]: entry for entry in table["tools"]}
        return {name: Path(rows[name]["binary"] or rows[name]["path"]) if name in rows else None
                for name in sorted(set(names))}

    def tool_places(table, names):
        rows = {entry["name"]: entry for entry in table["tools"]}
        return {name: {"path": Path(rows[name]["path"]) if rows[name]["path"] else None,
                       "binary": Path(rows[name]["binary"]) if rows[name]["binary"] else None}
                if name in rows else None for name in sorted(set(names))}

    def builder_keywords(builder, places):
        return ({"tool_places": places}
                if "tool_places" in inspect.signature(builder).parameters else {})

    key.tool_paths = tool_paths
    key.tool_places = tool_places
    key.builder_keywords = builder_keywords
    key.tool_resolved = lambda entry: True
    key.shipped = lambda ref: ref
    key.entry_variables = lambda entry, env: {}
    kinds = types.ModuleType("packaging_flow.verification.kinds")
    registry = types.ModuleType("packaging_flow.verification.registry")
    registry.ORIGINS = ("run_here",)
    verification = types.ModuleType("packaging_flow.verification")
    verification.key, verification.kinds = key, kinds
    sys.modules.update({"packaging_flow": types.ModuleType("packaging_flow"),
                        "packaging_flow.verification": verification,
                        "packaging_flow.verification.key": key,
                        "packaging_flow.verification.kinds": kinds,
                        "packaging_flow.verification.registry": registry})


stub_packaging_flow()
sys.path.insert(0, str(HERE))
import check_kind_contracts as ckc  # noqa: E402

# The venv's python: -V, -c and a script's --help, each answered as python does.
PYTHON = """#!/bin/sh
case "$1" in
  -V) echo "Python 3.12.3" ;;
  -c) [ -n "$FAIL_IMPORT" ] && exit 1; exit 0 ;;
  *) [ "$2" = --help ] && echo "usage: $(basename "$1") [-h]" ;;
esac
"""


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def row(name, binary, path=None):
    return {"name": name, "binary": str(binary) if binary else None,
            "path": str(path) if path else None}


def kind(tools, argv):
    entry = types.SimpleNamespace(tools=tuple(tools),
                                  argv_builder=lambda *args, **kwargs: [str(a) for a in argv])
    return types.SimpleNamespace(ENTRY=entry)


class ExecFrame(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.venv = base / "venv"
        self.python = executable(self.venv / "bin" / "python3", PYTHON)
        # The row's binary is the system interpreter by realpath, not the venv's.
        self.system_python = executable(base / "usr" / "bin" / "python3.12", PYTHON)
        self.drc = base / "pdk" / "drc"
        self.script = self.drc / "run_drc.py"
        self.drc.mkdir(parents=True)
        self.script.write_text("# no #! line, no exec bit\n")
        self.wrapper = executable(base / "bin" / "adk-drc",
                                  f'#!/bin/sh\nexec "{self.python}" run_drc.py "$@"\n')
        self.table = {"tools": [
            row("adk-drc", self.wrapper, base / "adk"),
            row("intm4tm2-run-drc", self.script, self.drc),
            row("klayout-python", base / "dbcore.so", base / "klayout"),
            row("python", self.system_python, self.venv),
        ]}

    def frame(self, shipped, table=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            red = ckc.exec_frame(shipped, table or self.table)
        return red, out.getvalue()

    def test_a_script_under_the_venvs_python_answers_both_flags(self):
        red, out = self.frame({("system.drc", "run_here"): kind(
            ("python", "intm4tm2-run-drc"), [self.python, self.script, "--path", "x"])})
        self.assertEqual(red, 0, out)
        self.assertIn(f"python {self.python} -V: rc 0: Python 3.12.3", out)
        self.assertIn(f"intm4tm2-run-drc {self.python} {self.script} --help: rc 0: usage:", out)

    def test_an_unkeyed_script_under_the_venvs_python_answers_the_interpreters_flag(self):
        red, out = self.frame({("system.terminal_map", "run_here"): kind(
            ("python", "klayout-python"), [self.python, "/x/observe_terminals.py"])})
        self.assertEqual(red, 0, out)
        self.assertIn(f"klayout-python {self.python} -c \"import klayout.db\": rc 0", out)

    def test_the_wrapper_answers_and_its_venv_imports_klayout(self):
        red, out = self.frame({("assembly.drc", "run_here"): kind(
            ("adk-drc", "klayout-python"), [self.wrapper, "--path", "x"])})
        self.assertEqual(red, 0, out)
        self.assertIn(f"adk-drc {self.wrapper} --help: rc 0: usage: run_drc.py", out)
        self.assertIn(f"klayout-python {self.python} -c", out)

    def test_the_venvs_python_is_no_tool_of_a_kind_that_does_not_key_python(self):
        red, out = self.frame({("system.drc", "run_here"): kind(
            ("intm4tm2-run-drc",), [self.python, self.script])})
        self.assertEqual(red, 1, out)
        self.assertIn("is no one tool it keys", out)

    def test_a_script_under_the_rows_binary_answers_both_flags(self):
        """argv[0] as the row's binary is the system interpreter; a keyed
        script after it is still asked its flag, under that interpreter."""
        red, out = self.frame({("interposer.drc", "run_here"): kind(
            ("python", "intm4tm2-run-drc"), [self.system_python, self.script, "--gds", "x"])})
        self.assertEqual(red, 0, out)
        self.assertIn(f"python {self.system_python} -V: rc 0: Python 3.12.3", out)
        self.assertIn(f"intm4tm2-run-drc {self.system_python} {self.script} --help: rc 0", out)

    def test_a_builder_that_declares_tool_places_is_handed_them(self):
        """system.drc names the venv's python from the python row's place."""
        def argv(inputs, outputs, options, tools, *, variables, tool_places):
            return [str(tool_places["python"]["path"] / "bin" / "python3"),
                    str(tools["intm4tm2-run-drc"]), "--path", "x"]
        entry = types.SimpleNamespace(tools=("python", "intm4tm2-run-drc"), argv_builder=argv)
        red, out = self.frame({("system.drc", "run_here"): types.SimpleNamespace(ENTRY=entry)})
        self.assertEqual(red, 0, out)
        self.assertIn(f"intm4tm2-run-drc {self.python} {self.script} --help: rc 0", out)

    def test_a_wrapper_with_no_python_row_is_red(self):
        table = {"tools": [r for r in self.table["tools"] if r["name"] != "python"]}
        red, out = self.frame({("assembly.drc", "run_here"): kind(
            ("adk-drc", "klayout-python"), [self.wrapper])}, table)
        self.assertEqual(red, 1, out)
        self.assertIn("no python row to name the venv by -> red", out)

    def test_a_script_flag_that_fails_is_red(self):
        executable(self.python, PYTHON.replace(
            '*) [ "$2" = --help ] && echo "usage: $(basename "$1") [-h]" ;;', '*) exit 3 ;;'))
        red, out = self.frame({("system.drc", "run_here"): kind(
            ("python", "intm4tm2-run-drc"), [self.python, self.script])})
        self.assertEqual(red, 1, out)
        self.assertIn(f"intm4tm2-run-drc {self.python} {self.script} --help: rc 3", out)


class OlderPackagingFlow(unittest.TestCase):
    def test_a_key_without_builder_keywords_is_refused_by_name(self):
        """An older packaging-flow has no builder_keywords: one FAIL line
        names the missing key, instead of a red per kind."""
        held = ckc.key.builder_keywords
        del ckc.key.builder_keywords
        self.addCleanup(setattr, ckc.key, "builder_keywords", held)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ckc.main(["--table", "/nonexistent"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue().splitlines(), [
            "kind contracts: FAIL, packaging-flow has no key.builder_keywords, which the "
            "exec frame hands argv builders by; pin a packaging-flow ref that carries it"])


class Wrapper(unittest.TestCase):
    def test_every_drc_row_has_a_version_flag(self):
        for tool in ("adk-drc", "adk-verify-carrier-drc", "intm4tm2-run-drc", "python"):
            with self.subTest(tool=tool):
                self.assertIn(tool, ckc.VERSION_FLAGS)

    def test_adk_drc_execs_the_venvs_python(self):
        """VENV_WRAPPERS names adk-drc because bin/adk-drc runs the venv's python."""
        text = (HERE.parent / "bin" / "adk-drc").read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn('exec "${ADK_TOOLS:-/opt/adk-tools}/venv/bin/python3"', text)
        self.assertEqual(ckc.VENV_PYTHON, Path("bin", "python3"))


if __name__ == "__main__":
    unittest.main()
