#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for nix-tail/capture_tail_context.py, without Nix.

The export probe is run for real: a venv of this interpreter made the way
packages.tail-export-python makes one (--without-pip, and a .pth file whose
one plain line is the fork's site-packages), with a stand-in fork holding a
pcbnew.py that imports _pcbnew. That shows the fork lands last on the import
path and pcbnew loads from it, and each break of that layout is refused. The
realised closure is given as its paths, with this host's /usr standing in for
the store Python's. The worker probe needs klayout.db and is not run here; its
reader is tested on replies in the shape the probe prints. The plugin and
gds_to_kicad records run against real git checkouts, and the variables the
tail hook clears are read from nix-tail/tail-env.sh itself.

Usage: python3 nix-tail/test_capture_tail_context.py
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["ADK_NIX_LIB"] = os.path.join(os.path.dirname(HERE), "nix")
sys.path.insert(0, HERE)
import capture_tail_context as ctc  # noqa: E402

PYVER = f"python{sys.version_info.major}.{sys.version_info.minor}"
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
# Where this host's interpreter, its shared objects and its distribution's
# sitecustomize live: the stand-in for the store Python's part of the closure.
HOST = [os.path.realpath(sys.base_prefix), "/usr", "/lib", "/lib64", "/etc"]


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, **GIT_ENV))


def run_main(out, **env):
    """main() writing out, with no ADK_NIX_* (ADK_NIX_LIB aside), PDK_ROOT,
    KICAD_CHIPLET_PYTHON, GDS_TO_KICAD_ROOT or cleared name set but env:
    (exit, record)."""
    keep = dict(os.environ)
    try:
        for name in list(os.environ):
            if (name.startswith(("ADK_NIX_", "PDK_ROOT", "KICAD_CHIPLET_PYTHON", "GDS_TO_KICAD_ROOT"))
                    or name in ctc.TAIL_SCRUB or name.lower() == "klayout") and name != "ADK_NIX_LIB":
                del os.environ[name]
        os.environ.update(env)
        with contextlib.redirect_stderr(io.StringIO()):
            code = ctc.main(["capture_tail_context.py", out])
    finally:
        os.environ.clear()
        os.environ.update(keep)
    with open(out) as handle:
        return code, json.load(handle)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)


class ExportProbe(Tmp):
    """The export interpreter, asked for real."""

    def setUp(self):
        super().setUp()
        self.fork = os.path.join(self.root, "store", "kicad-base")
        self.site = os.path.join(self.fork, "lib", PYVER, "site-packages")
        write(os.path.join(self.site, "pcbnew.py"),
              "import _pcbnew\n\ndef GetBuildVersion():\n    return 'stand-in'\n")
        write(os.path.join(self.site, "_pcbnew.py"), "")
        self.export = os.path.join(self.root, "store", "adk-tail-export-python")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", self.export], check=True)
        self.venv_site = os.path.join(self.export, "lib", PYVER, "site-packages")
        self.pth = os.path.join(self.venv_site, "_adk_nix_kicad_fork.pth")
        write(self.pth, self.site + "\n")
        self.closure = HOST + [self.export, self.fork]
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_probe(self, env=None):
        return ctc.probe([os.path.join(self.export, "bin", "python"), "-P", "-c", ctc.EXPORT_PROBE],
                         dict(self.env, **(env or {})))

    def test_the_fork_is_last_and_pcbnew_loads_from_it(self):
        record = self.run_probe()
        self.assertEqual(ctc.export_problems(record, self.export, self.fork, self.closure), [])
        reply = record["reply"]
        self.assertEqual(os.path.realpath(reply["path"][-1]), self.site)
        self.assertEqual(reply["build_version"], "stand-in")
        self.assertTrue(record["files"], "the trace named no file")

    def test_the_fork_first_through_pythonpath_refuses(self):
        record = self.run_probe({"PYTHONPATH": self.site})
        problems = ctc.export_problems(record, self.export, self.fork, self.closure)
        self.assertTrue(any("import path" in p for p in problems), problems)

    def test_no_pth_refuses(self):
        os.remove(self.pth)
        record = self.run_probe()
        problems = ctc.export_problems(record, self.export, self.fork, self.closure)
        self.assertEqual(len(problems), 1, problems)
        self.assertRegex(problems[0], r"the pcbnew probe exited 1")

    def test_pcbnew_outside_the_fork_refuses(self):
        other = os.path.join(self.root, "elsewhere", "site-packages")
        write(os.path.join(other, "pcbnew.py"), "import _pcbnew\n\ndef GetBuildVersion():\n    return 'x'\n")
        write(os.path.join(other, "_pcbnew.py"), "")
        # Ahead of the fork, which stays last.
        record = self.run_probe({"PYTHONPATH": other})
        self.assertEqual(os.path.realpath(record["reply"]["path"][-1]), self.site)
        problems = ctc.export_problems(record, self.export, self.fork, self.closure)
        self.assertTrue(any("pcbnew loaded from" in p for p in problems), problems)

    def test_another_interpreter_refuses(self):
        record = self.run_probe()
        other = os.path.join(self.root, "store", "another-venv")
        problems = ctc.export_problems(record, other, self.fork, self.closure)
        self.assertTrue(any("not the venv" in p for p in problems), problems)

    def test_a_host_library_outside_the_closure_refuses(self):
        record = self.run_probe()
        problems = ctc.export_problems(record, self.export, self.fork, [self.export, self.fork])
        self.assertTrue(any("outside the realised closure" in p for p in problems), problems)


class ExportReply(unittest.TestCase):
    """The export reader on replies in the probe's shape."""

    fork = "/nix/store/" + "f" * 32 + "-kicad-base"
    export = "/nix/store/" + "e" * 32 + "-adk-tail-export-python"
    site = fork + "/lib/python3.13/site-packages"

    def record(self, **reply):
        base = {"prefix": self.export, "version_info": [3, 13],
                "path": [self.export + "/lib/python3.13/site-packages", self.site],
                "pcbnew": self.site + "/pcbnew.py", "core": self.site + "/_pcbnew.so",
                "maps": [self.fork + "/bin/_pcbnew.kiface"]}
        base.update(reply)
        return {"exit": 0, "reply": base, "files": [], "other_loaders": [], "unread_trace": []}

    def problems(self, record):
        return ctc.export_problems(record, self.export, self.fork, [self.export, self.fork])

    def test_reply_passes(self):
        # The store paths do not exist here; realpath leaves them as they are.
        self.assertEqual(self.problems(self.record()), [])

    def test_a_mapped_object_outside_refuses(self):
        problems = self.problems(self.record(maps=["/usr/lib/libwx_gtk3u_core-3.2.so.0"]))
        self.assertEqual(problems, ["export interpreter: /usr/lib/libwx_gtk3u_core-3.2.so.0 "
                                    "mapped from outside the realised closure"])

    def test_the_core_outside_refuses(self):
        problems = self.problems(self.record(core="/usr/lib/python3/dist-packages/_pcbnew.so"))
        self.assertTrue(any(p.startswith("export interpreter: core loaded from") for p in problems))

    def test_a_trace_loader_without_a_file_refuses(self):
        record = self.record()
        record["other_loaders"] = ["import 'x' # <zipimporter object>"]
        self.assertTrue(self.problems(record))


class Worker(unittest.TestCase):
    venv = "/tmp/adk-nix-venv"
    pdk = "/nix/store/" + "p" * 32 + "-ihp-open-pdk-22f4335"
    g2k = "/srv/adk-tools/tools/gds_to_kicad"
    tmp = "/tmp"

    def record(self, files=(), **reply):
        base = {"via_stack": True, "error": None, "tmp": self.tmp,
                "pycell_lib": self.pdk + "/" + ctc.PCELL_LIB + "/__init__.py",
                "pin_list": self.g2k + "/pin_list.py", "pin_list_error": None}
        base.update(reply)
        return {"exit": 0, "reply": base, "files": list(files), "other_loaders": [],
                "unread_trace": []}

    def problems(self, record, g2k=None):
        return ctc.worker_problems(record, self.venv, self.pdk, g2k or self.g2k, [self.pdk])

    def test_placed_passes(self):
        files = [self.pdk + "/" + ctc.PCELL_LIB + "/sg13g2_tech.py",
                 self.venv + "/lib/python3.13/site-packages/klayout/db.py",
                 "/tmp/sg13g2_pycell_ab12cd/via_stack_code_pre.py",
                 self.g2k + "/pin_list.py", self.g2k + "/_paths.py"]
        self.assertEqual(self.problems(self.record(files=files)), [])

    def test_pin_list_not_imported_refuses_and_says_where_it_stops(self):
        problems = self.problems(self.record(pin_list=None,
                                             pin_list_error="FileNotFoundError: pin_list.py"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("FileNotFoundError: pin_list.py", problems[0])
        self.assertIn("stops at the first die with pin lists", problems[0])

    def test_pin_list_from_another_root_refuses(self):
        problems = self.problems(self.record(pin_list="/srv/fake-user/gds_to_kicad/pin_list.py"))
        self.assertTrue(any("PinList not imported from" in p for p in problems), problems)

    def test_another_file_under_the_root_refuses(self):
        problems = self.problems(self.record(pin_list=self.g2k + "/old/pin_list.py"))
        self.assertTrue(any("PinList not imported from" in p for p in problems), problems)

    def test_no_root_refuses(self):
        problems = ctc.worker_problems(self.record(), self.venv, self.pdk, "", [self.pdk])
        self.assertTrue(any("PinList not imported from" in p for p in problems), problems)

    def test_code_from_beside_the_root_refuses(self):
        problems = self.problems(self.record(files=["/srv/adk-tools/tools/gds_to_kicad-old/pin_list.py"]))
        self.assertEqual(problems, ["worker: /srv/adk-tools/tools/gds_to_kicad-old/pin_list.py "
                                    "loaded from outside the realised closure"])

    def test_no_via_stack_refuses_and_says_rectangles(self):
        problems = self.problems(self.record(via_stack=False, error="ModuleNotFoundError: tkinter"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("ModuleNotFoundError: tkinter", problems[0])
        self.assertIn("would draw rectangles instead", problems[0])

    def test_the_library_from_elsewhere_refuses(self):
        problems = self.problems(self.record(pycell_lib="/opt/pdks/" + ctc.PCELL_LIB + "/__init__.py"))
        self.assertTrue(any("sg13g2_pycell_lib loaded from" in p for p in problems), problems)

    def test_other_temporary_code_refuses(self):
        problems = self.problems(self.record(files=["/tmp/evil.py"]))
        self.assertEqual(problems, ["worker: /tmp/evil.py loaded from outside the realised closure"])

    def test_a_preprocessed_name_outside_its_directory_refuses(self):
        problems = self.problems(self.record(files=["/tmp/x/via_stack_code_pre.py"]))
        self.assertTrue(problems)

    def test_a_failed_probe_refuses(self):
        problems = self.problems({"exit": 1, "reply": None})
        self.assertEqual(problems, ["worker: the PCell probe exited 1 (no reply)"])


class WorkerProbePinList(Tmp):
    """The worker probe's PinList import, run for real by this interpreter,
    with a stand-in klayout.db that has the technology and library registered
    already, so that only the part after the via_stack is exercised."""

    STUB = ("class Technology:\n    @staticmethod\n    def technology_names():\n"
            "        return ['sg13g2']\n\nclass Library:\n    @staticmethod\n"
            "    def library_names():\n        return ['SG13_dev']\n\nclass _Box:\n"
            "    def to_s(self):\n        return '()'\n\nclass _Cell:\n    def bbox(self):\n"
            "        return _Box()\n\nclass Layout:\n    technology_name = None\n\n"
            "    def create_cell(self, *args):\n        return _Cell()\n")

    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.root, "stub")
        write(os.path.join(self.stub, "klayout", "__init__.py"), "")
        write(os.path.join(self.stub, "klayout", "db.py"), self.STUB)
        self.g2k = os.path.join(self.root, "gds_to_kicad")
        write(os.path.join(self.g2k, "pin_list.py"),
              "from _paths import atomic_write\n\nclass PinList:\n    pass\n")
        write(os.path.join(self.g2k, "_paths.py"), "def atomic_write():\n    pass\n")

    def run_probe(self, **env):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": self.stub,
                "PYTHONDONTWRITEBYTECODE": "1", "PDK_ROOT": os.path.join(self.root, "pdk"),
                "GDS_TO_KICAD_ROOT": self.g2k}
        base.update(env)
        base = {k: v for k, v in base.items() if v is not None}
        return ctc.probe([sys.executable, "-P", "-c", ctc.WORKER_PROBE], base)

    def test_pin_list_imports_from_the_root(self):
        record = self.run_probe()
        self.assertEqual(record["exit"], 0, record.get("stderr_other"))
        reply = record["reply"]
        self.assertIsNone(reply["pin_list_error"])
        self.assertEqual(reply["pin_list"], os.path.join(self.g2k, "pin_list.py"))
        self.assertIn(os.path.join(self.g2k, "_paths.py"), record["files"])
        self.assertFalse(os.path.exists(os.path.join(self.g2k, "__pycache__")))

    def test_no_root_is_an_error_in_the_reply(self):
        reply = self.run_probe(GDS_TO_KICAD_ROOT=None)["reply"]
        self.assertIsNone(reply["pin_list"])
        self.assertEqual(reply["pin_list_error"], "KeyError: 'GDS_TO_KICAD_ROOT'")

    def test_a_pin_list_without_the_class_is_an_error_in_the_reply(self):
        write(os.path.join(self.g2k, "pin_list.py"), "from _paths import atomic_write\n")
        reply = self.run_probe()["reply"]
        self.assertIsNone(reply["pin_list"])
        self.assertTrue(reply["pin_list_error"].startswith("AttributeError"), reply)

    def test_the_root_goes_ahead_of_the_import_path(self):
        # A helper of the same name earlier on the path, as a host tree would
        # put one; the root's own must be the one pin_list.py gets.
        write(os.path.join(self.stub, "_paths.py"), "raise ImportError('the path helper')\n")
        record = self.run_probe()
        reply = record["reply"]
        self.assertIsNone(reply["pin_list_error"], reply)
        self.assertIn(os.path.join(self.g2k, "_paths.py"), record["files"])
        self.assertNotIn(os.path.join(self.stub, "_paths.py"), record["files"])

    def test_a_root_without_its_helper_is_an_error_in_the_reply(self):
        os.remove(os.path.join(self.g2k, "_paths.py"))
        reply = self.run_probe()["reply"]
        self.assertIsNone(reply["pin_list"])
        self.assertTrue(reply["pin_list_error"].startswith("ModuleNotFoundError"), reply)


class Wrappers(Tmp):
    def setUp(self):
        super().setUp()
        self.store = "/nix/store/" + "b" * 32 + "-bash-5.3"
        self.wrappers = os.path.join(self.root, "adk-tail-wrappers")
        for name in ctc.COMMANDS:
            path = os.path.join(self.wrappers, "bin", name)
            write(path, f"#!{self.store}/bin/bash -p\nexec x \"$@\"\n")
            os.chmod(path, 0o555)
        self.env = {"PATH": os.path.join(self.wrappers, "bin") + ":/usr/bin:/bin"}

    def test_resolved_and_in_the_closure(self):
        problems = []
        record = ctc.wrappers_record(self.wrappers, self.env, [self.store], problems)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(record), sorted(ctc.COMMANDS))
        self.assertEqual(record["adk-drc"]["store_paths"], [self.store])

    def test_shadowed_refuses(self):
        host = os.path.join(self.root, "host")
        write(os.path.join(host, "chiplet-export"), "#!/bin/sh\n")
        os.chmod(os.path.join(host, "chiplet-export"), 0o755)
        problems = []
        ctc.wrappers_record(self.wrappers, {"PATH": host + ":" + self.env["PATH"]}, [self.store],
                            problems)
        self.assertEqual(problems, [f"wrapper: chiplet-export resolves to {host}/chiplet-export, "
                                    f"not {self.wrappers}/bin/chiplet-export"])

    def test_a_store_path_outside_the_closure_refuses(self):
        problems = []
        ctc.wrappers_record(self.wrappers, self.env, [], problems)
        self.assertEqual(len(problems), 3, problems)


class KicadCli(Tmp):
    def setUp(self):
        super().setUp()
        self.fork = os.path.join(self.root, "kicad-base-5d12a25667")
        self.cli = os.path.join(self.fork, "bin", ctc.KICAD_CLI)
        write(self.cli, "#!/bin/sh\n")
        os.chmod(self.cli, 0o555)
        self.env = {"PATH": "/usr/bin:/bin:" + os.path.join(self.fork, "bin")}

    def test_the_fork_s_last_on_path_and_in_the_closure(self):
        problems = []
        record = ctc.kicad_cli_record(self.fork, self.env, [self.fork], problems)
        self.assertEqual(problems, [])
        self.assertEqual(record, {"path": self.cli, "expected": self.cli, "resolved": self.cli})

    def test_one_ahead_refuses(self):
        host = os.path.join(self.root, "host")
        write(os.path.join(host, ctc.KICAD_CLI), "#!/bin/sh\n")
        os.chmod(os.path.join(host, ctc.KICAD_CLI), 0o755)
        problems = []
        record = ctc.kicad_cli_record(self.fork, {"PATH": host + ":" + self.env["PATH"]},
                                      [self.fork], problems)
        self.assertEqual(problems, [f"kicad-cli resolves to {host}/kicad-cli, not {self.cli}"])
        self.assertNotIn("resolved", record)

    def test_not_on_path_refuses(self):
        problems = []
        ctc.kicad_cli_record(self.fork, {"PATH": "/nonexistent"}, [self.fork], problems)
        self.assertEqual(problems, [f"kicad-cli resolves to None, not {self.cli}"])

    def test_a_link_out_of_the_closure_refuses(self):
        outside = os.path.join(self.root, "elsewhere", ctc.KICAD_CLI)
        write(outside, "#!/bin/sh\n")
        os.chmod(outside, 0o555)
        os.remove(self.cli)
        os.symlink(outside, self.cli)
        problems = []
        record = ctc.kicad_cli_record(self.fork, self.env, [self.fork], problems)
        self.assertEqual(record["resolved"], outside)
        self.assertEqual(problems, [f"kicad-cli: {outside} is outside the realised closure"])


class Plugin(Tmp):
    def setUp(self):
        super().setUp()
        self.up = os.path.join(self.root, "up")
        write(os.path.join(self.up, "plugins", "chiplet_export", "chiplet_export_cli.py"), "one\n")
        git("init", "-q", cwd=self.up)
        git("add", "-A", cwd=self.up)
        git("commit", "-q", "-m", "one", cwd=self.up)
        self.rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.up, capture_output=True,
                                  text=True, check=True).stdout.strip()
        self.tools = os.path.join(self.root, "adk-tools")
        write(os.path.join(self.tools, "README"), "x\n")
        git("init", "-q", cwd=self.tools)
        git("add", "-A", cwd=self.tools)
        git("update-index", "--add", "--cacheinfo", f"160000,{self.rev},{ctc.PLUGIN_PATH}",
            cwd=self.tools)
        git("commit", "-q", "-m", "base", cwd=self.tools)
        self.plugin = os.path.join(self.tools, ctc.PLUGIN_PATH)
        subprocess.run(["git", "clone", "-q", self.up, self.plugin], check=True, capture_output=True,
                       env=dict(os.environ, **GIT_ENV))

    def test_evidence_at_the_gitlink_passes(self):
        problems = []
        record = ctc.plugin_record(self.plugin, "evidence", self.tools, problems)
        self.assertEqual(problems, [])
        self.assertEqual(record["gitlink"], self.rev)

    def test_evidence_elsewhere_refuses(self):
        problems = []
        ctc.plugin_record(self.up, "evidence", self.tools, problems)
        self.assertTrue(any(" is not " in p for p in problems), problems)

    def test_evidence_at_another_commit_refuses(self):
        write(os.path.join(self.plugin, "extra.py"), "")
        git("add", "-A", cwd=self.plugin)
        git("commit", "-q", "-m", "two", cwd=self.plugin)
        problems = []
        ctc.plugin_record(self.plugin, "evidence", self.tools, problems)
        self.assertTrue(any(f"the gitlink is {self.rev}" in p for p in problems), problems)

    def test_dev_needs_only_a_clean_checkout(self):
        problems = []
        record = ctc.plugin_record(self.up, "dev", None, problems)
        self.assertEqual(problems, [])
        self.assertNotIn("gitlink", record)

    def test_dev_with_changes_refuses(self):
        write(os.path.join(self.up, "plugins", "chiplet_export", "chiplet_export_cli.py"), "two\n")
        problems = []
        ctc.plugin_record(self.up, "dev", None, problems)
        self.assertTrue(problems)


class GdsToKicad(Tmp):
    """The gds_to_kicad tree, on the same layout as the plugin's: a gitlink
    at tools/gds_to_kicad of a stand-in adk-tools checkout."""

    def setUp(self):
        super().setUp()
        self.up = os.path.join(self.root, "up")
        write(os.path.join(self.up, ctc.G2K_FILE), "class PinList:\n    pass\n")
        git("init", "-q", cwd=self.up)
        git("add", "-A", cwd=self.up)
        git("commit", "-q", "-m", "one", cwd=self.up)
        self.rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.up, capture_output=True,
                                  text=True, check=True).stdout.strip()
        self.tools = os.path.join(self.root, "adk-tools")
        write(os.path.join(self.tools, "README"), "x\n")
        git("init", "-q", cwd=self.tools)
        git("add", "-A", cwd=self.tools)
        git("update-index", "--add", "--cacheinfo", f"160000,{self.rev},{ctc.G2K_PATH}",
            cwd=self.tools)
        git("commit", "-q", "-m", "base", cwd=self.tools)
        self.tree = os.path.join(self.tools, ctc.G2K_PATH)

    def clone(self):
        subprocess.run(["git", "clone", "-q", self.up, self.tree], check=True, capture_output=True,
                       env=dict(os.environ, **GIT_ENV))

    def test_evidence_at_the_gitlink_passes(self):
        self.clone()
        problems = []
        record = ctc.gds_to_kicad_record(self.tree, "evidence", self.tools, problems)
        self.assertEqual(problems, [])
        self.assertEqual(record["gitlink"], self.rev)

    def test_an_uninitialised_submodule_refuses_and_names_the_file(self):
        os.makedirs(self.tree)
        problems = []
        ctc.gds_to_kicad_record(self.tree, "evidence", self.tools, problems)
        self.assertIn(f"gds_to_kicad: no {ctc.G2K_FILE} under {self.tree!r}", problems)
        self.assertTrue(any("is not the top of its checkout" in p for p in problems), problems)

    def test_unset_refuses(self):
        problems = []
        ctc.gds_to_kicad_record("", "evidence", self.tools, problems)
        self.assertIn(f"gds_to_kicad: no {ctc.G2K_FILE} under ''", problems)

    def test_evidence_at_another_commit_refuses(self):
        self.clone()
        write(os.path.join(self.tree, "extra.py"), "")
        git("add", "-A", cwd=self.tree)
        git("commit", "-q", "-m", "two", cwd=self.tree)
        problems = []
        ctc.gds_to_kicad_record(self.tree, "evidence", self.tools, problems)
        self.assertTrue(any(p.startswith("gds_to_kicad: at ") and f"the gitlink is {self.rev}" in p
                            for p in problems), problems)

    def test_dev_needs_only_a_clean_checkout_holding_the_file(self):
        problems = []
        record = ctc.gds_to_kicad_record(self.up, "dev", None, problems)
        self.assertEqual(problems, [])
        self.assertNotIn("gitlink", record)


class Scrub(unittest.TestCase):
    def test_the_list_is_the_hook_s(self):
        hook = os.path.join(HERE, "tail-env.sh")
        lib = os.path.join(os.path.dirname(HERE), "nix")
        done = subprocess.run(
            ["bash", "-c", 'ADK_NIX_LIB_ONLY=1 . "$1/cli-env.sh"; ADK_NIX_LIB_ONLY=1; . "$2"; '
             'printf "%s\\n" $ADK_NIX_TAIL_SCRUB', "-", lib, hook],
            capture_output=True, text=True, env=dict(os.environ, ADK_NIX_LIB=lib))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(tuple(done.stdout.split()), ctc.TAIL_SCRUB)

    def test_a_cleared_name_set_again_refuses(self):
        env = {"PATH": "/bin", "NIX_PYTHONPATH": "/x", "Klayout": "1", "KLAYOUT_PATH": "/k"}
        self.assertEqual(ctc.scrub_problems(env),
                         ["Klayout is set; the tail hook clears it",
                          "NIX_PYTHONPATH is set; the tail hook clears it"])


class NixReaders(unittest.TestCase):
    """Every name capture_tail_context.py takes from nix/'s readers exists there.

    It reads them as cc.<name> and cbc.<name> (nix/capture_context.py and
    nix/capture_build_context.py); a rename there would otherwise surface only
    on evidence entry, and some calls sit behind a catch that would hide it.
    """

    def test_every_name_it_uses_is_defined(self):
        import ast
        with open(os.path.join(HERE, "capture_tail_context.py")) as handle:
            tree = ast.parse(handle.read())
        used = {(node.value.id, node.attr) for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in ("cc", "cbc")}
        self.assertTrue({m for m, _ in used} == {"cc", "cbc"}, used)
        missing = sorted(f"{m}.{n}" for m, n in used if not hasattr(getattr(ctc, m), n))
        self.assertEqual(missing, [])


class Pdk(Tmp):
    """The PDK statement path, run for real on a stand-in lock and PDK tree
    written as the flake writes them, so that a change to nix/'s lock or
    statement readers that breaks this caller fails here and not on entry."""

    KLAYOUT_PY = "ihp-sg13g2/libs.tech/klayout/python/"
    GITLINKS = ["ihp-sg13g2/libs.tech/digital", KLAYOUT_PY + "pycell4klayout-api",
                KLAYOUT_PY + "pypreprocessor", "ihp-sg13g2/libs.tech/palace"]
    FETCHED = {"pycell4klayout-api": KLAYOUT_PY + "pycell4klayout-api",
               "pypreprocessor": KLAYOUT_PY + "pypreprocessor"}

    def setUp(self):
        super().setUp()
        self.pdk = os.path.join(self.root, "store", "a" * 32 + "-ihp-open-pdk")
        write(os.path.join(self.pdk, ".gitmodules"), "".join(
            f'[submodule "{g}"]\n\tpath = {g}\n\turl = https://example.invalid/{i}\n'
            for i, g in enumerate(self.GITLINKS)))
        for path in self.FETCHED.values():
            write(os.path.join(self.pdk, path, "__init__.py"), "")
        self.lock = os.path.join(self.root, "flake.lock")
        self.write_lock()
        self.write_statement()

    @staticmethod
    def rev(name):
        return (name.encode().hex() * 40)[:40]

    def write_lock(self, rename=None):
        nodes = {"root": {"inputs": {}}}
        for name in ("ihp-open-pdk", "pycell4klayout-api", "pypreprocessor"):
            nodes[name] = {"locked": {"type": "github", "owner": "IHP-GmbH", "repo": name,
                                      "rev": self.rev(name), "narHash": f"sha256-{name}="}}
            nodes["root"]["inputs"][rename if name == "ihp-open-pdk" and rename else name] = name
        with open(self.lock, "w") as handle:
            json.dump({"nodes": nodes, "root": "root", "version": 7}, handle)

    def write_statement(self, edit=None):
        value = {"format": "adk-tools-pdk-source/v1", "materialized_at": self.pdk,
                 "parent": {"input": "ihp-open-pdk", "rev": self.rev("ihp-open-pdk"),
                            "narHash": "sha256-ihp-open-pdk="},
                 "submodules": [{"path": path, "input": name, "rev": self.rev(name),
                                 "narHash": f"sha256-{name}="}
                                for name, path in sorted(self.FETCHED.items(), key=lambda i: i[1])],
                 "not_fetched": sorted(set(self.GITLINKS) - set(self.FETCHED.values()))}
        if edit:
            edit(value)
        path = os.path.join(self.pdk, "pdk-source.json")
        if os.path.exists(path):
            os.chmod(path, 0o644)
        with open(path, "w") as handle:
            handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        os.chmod(path, 0o444)

    def record(self):
        problems = []
        return ctc.pdk_record(self.pdk, self.lock, problems), problems

    def test_the_tree_the_lock_names_passes(self):
        record, problems = self.record()
        self.assertEqual(problems, [])
        self.assertEqual(sorted(record["lock"]), ["ihp-open-pdk", "pycell4klayout-api", "pypreprocessor"])
        self.assertEqual(record["statement"]["parent"]["rev"], self.rev("ihp-open-pdk"))
        self.assertRegex(record["source"]["sha256"], r"^[0-9a-f]{64}$")

    def test_a_parent_the_lock_does_not_name_refuses(self):
        self.write_statement(lambda v: v["parent"].update(rev="f" * 40))
        _, problems = self.record()
        self.assertTrue(any(p.startswith("pdk parent: evaluated rev 'fff") for p in problems), problems)

    def test_a_lock_without_the_parent_refuses(self):
        self.write_lock(rename="ihp-open-pdk-old")
        _, problems = self.record()
        self.assertIn("pdk: flake lock unreadable: KeyError: 'ihp-open-pdk is not a direct input of the flake'",
                      problems)

    def test_no_lock_refuses(self):
        os.remove(self.lock)
        record, problems = self.record()
        self.assertIn("unavailable", record["lock"])
        self.assertTrue(any(p.startswith("pdk: flake lock unreadable: FileNotFoundError: ") for p in problems),
                        problems)

    def test_main_reads_the_statement_of_the_pdk_it_is_given(self):
        code, record = run_main(os.path.join(self.root, "tail-context.json"),
                                ADK_NIX_SG13G2_PDK=self.pdk, ADK_NIX_FLAKE_LOCK=self.lock)
        self.assertEqual(code, 1)
        pdk = record["stores"]["pdk"]
        self.assertEqual(pdk["source"]["path"], os.path.join(self.pdk, "pdk-source.json"))
        self.assertEqual(pdk["statement"]["parent"]["rev"], self.rev("ihp-open-pdk"))
        self.assertEqual(sorted(pdk["lock"]), ["ihp-open-pdk", "pycell4klayout-api", "pypreprocessor"])
        self.assertFalse([p for p in record["problems"] if p.startswith(("pdk:", "pdk parent", "pdk submodule"))],
                         record["problems"])

    def test_no_statement_refuses(self):
        os.remove(os.path.join(self.pdk, "pdk-source.json"))
        record, problems = self.record()
        self.assertIsNone(record["source"]["sha256"])
        self.assertTrue(any(p.startswith("pdk: ") and
                            p.endswith("/pdk-source.json unreadable: No such file or directory") for p in problems),
                        problems)


class Main(Tmp):
    def test_nothing_set_refuses_and_still_writes_the_record(self):
        code, record = run_main(os.path.join(self.root, "tail-context.json"))
        self.assertEqual(code, 1)
        self.assertEqual(record["format"], ctc.FORMAT)
        self.assertEqual(record["kind"], "context-capture")
        self.assertIn("ADK_NIX_TAIL_WRAPPERS is not set", record["problems"])
        self.assertIn("export probe not run: no export interpreter or fork", record["problems"])
        # Whatever this host's PATH finds, nothing is expected without a fork.
        self.assertTrue(any(p.startswith("kicad-cli resolves to ") and p.endswith(", not None")
                            for p in record["problems"]), record["problems"])
        self.assertEqual(record["kicad_cli"]["expected"], None)
        self.assertIn(f"gds_to_kicad: no {ctc.G2K_FILE} under ''", record["problems"])
        self.assertEqual(record["gds_to_kicad"]["root"], "")
        self.assertIsNone(record["worker_probe"])
        # pdk_record runs: with no PDK set only the lock is read, and there is none.
        self.assertIn("lock", record["stores"]["pdk"])
        self.assertTrue(any(p.startswith("pdk: flake lock unreadable") for p in record["problems"]),
                        record["problems"])

    def test_a_cleared_name_refuses(self):
        code, record = run_main(os.path.join(self.root, "tail-context.json"),
                                NIX_PYTHONPATH="/host/site")
        self.assertEqual(code, 1)
        self.assertIn("NIX_PYTHONPATH is set; the tail hook clears it", record["problems"])

    def test_an_existing_record_is_not_overwritten(self):
        out = os.path.join(self.root, "tail-context.json")
        write(out, "kept\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ctc.main(["capture_tail_context.py", out]), 1)
        with open(out) as handle:
            self.assertEqual(handle.read(), "kept\n")


if __name__ == "__main__":
    unittest.main()
