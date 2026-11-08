#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for nix/capture_build_context.py: reading the launcher and the
import trace, and the probe run for real.

No Nix needed. The stand-in store holds a bash, a PDK tree and a LibreLane
whose wrapper, written as makeWrapper writes it, runs this interpreter with -S
over a script that first does what Nix's sitecustomize does with
NIX_PYTHONPATH and then discovers plugins as LibreLane does. The launcher is
the one nix/build-env.sh writes. The cores' interpreter env is a venv made
with --copies (a regular-file interpreter that names itself as
sys.executable) whose site-packages/librelane links to the stand-in
LibreLane's package, as buildEnv links it. The realised closure is given as
its paths (closureInfo's reader is tested in test_capture_context.py), with
this interpreter's prefix standing in for the store Python.

Usage: python3 nix/test_capture_build_context.py
"""

import contextlib
import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import py_compile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture_build_context as cbc  # noqa: E402
import capture_context as cc  # noqa: E402

PYTHON = os.path.realpath(sys.executable)
PREFIX = os.path.realpath(sys.base_prefix)
NAR = "sha256:" + "0" * 52
PYVER = f"python{sys.version_info.major}.{sys.version_info.minor}"
KLAYOUT_PY = "ihp-sg13g2/libs.tech/klayout/python/"
# The parent's five gitlinks at SG13G2_REF; the build fetches the two KLayout
# ones.
GITLINKS = ["ihp-sg13g2/libs.tech/digital", KLAYOUT_PY + "pycell4klayout-api",
            KLAYOUT_PY + "pypreprocessor", "ihp-sg13g2/libs.tech/openems/openems_ihp_sg13g2",
            "ihp-sg13g2/libs.tech/palace"]
FETCHED = {"pycell4klayout-api": KLAYOUT_PY + "pycell4klayout-api",
           "pypreprocessor": KLAYOUT_PY + "pypreprocessor"}

WRAPPED = r'''
import functools, importlib, os, pkgutil, site, sys
# What Nix's sitecustomize does before anything else runs.
paths = os.environ.pop("NIX_PYTHONPATH", None)
if paths:
    functools.reduce(lambda k, p: site.addsitedir(p, k), paths.split(":"), site._init_pathinfo())
plugins = {name: importlib.import_module(name) for _, name, _ in pkgutil.iter_modules()
           if name.startswith("librelane_plugin_")}
if sys.argv[1:] == ["--version"]:
    print("LibreLane v3.0.0rc0\n\nCopyright 2020-2025 the contributors.\n")
    if plugins:
        print("Discovered plugins:")
        for name in plugins:
            print(name)
sys.exit(int(os.environ.get("STAND_IN_EXIT", "0")))
'''


def h32(n):
    return f"{n:032d}".translate(str.maketrans("0123456789", "abcdefghij"))


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.chmod(path, 0o644)
    with open(path, "w") as handle:
        handle.write(text)
    if mode is not None:
        os.chmod(path, mode)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp(prefix="adk-build-context-"))
        self.addCleanup(self.cleanup)
        self.store = os.path.join(self.work, "store")
        self.bash = os.path.join(self.store, h32(1) + "-bash-5.3p3")
        os.makedirs(os.path.join(self.bash, "bin"))
        os.symlink(os.path.realpath(shutil.which("bash")), os.path.join(self.bash, "bin", "bash"))
        self.librelane = os.path.join(self.store, h32(2) + "-python3.13-librelane-3.0.0rc0")
        write(os.path.join(self.librelane, "bin", ".librelane-wrapped"), WRAPPED)
        write(os.path.join(self.librelane, "bin", "librelane"),
              f"#! {self.bash}/bin/bash -e\n"
              f"exec '{PYTHON}' -S '{self.librelane}/bin/.librelane-wrapped' \"$@\"\n", 0o555)
        self.package = os.path.join(self.librelane, "lib", PYVER, "site-packages", "librelane")
        write(os.path.join(self.package, "__init__.py"), "")
        self.pyenv = os.path.join(self.store, h32(5) + "-python3-3.13-env")
        subprocess.run([PYTHON, "-m", "venv", "--without-pip", "--copies", self.pyenv],
                       check=True, capture_output=True)
        self.site = os.path.join(self.pyenv, "lib", PYVER, "site-packages")
        os.symlink(self.package, os.path.join(self.site, "librelane"))
        self.interpreter = os.path.join(self.pyenv, "bin", PYVER)
        self.assertFalse(os.path.islink(self.interpreter))
        self.pdk = os.path.join(self.store, h32(3) + "-sg13g2-pdk")
        os.makedirs(os.path.join(self.pdk, "ihp-sg13g2", "libs.tech", "librelane"))
        write(os.path.join(self.pdk, ".gitmodules"), "".join(
            f'[submodule "{g}"]\n\tpath = {g}\n\turl = https://example.invalid/{i}\n'
            for i, g in enumerate(GITLINKS)))
        for path in FETCHED.values():
            write(os.path.join(self.pdk, path, "__init__.py"), "")
        self.statement_path = os.path.join(self.pdk, "pdk-source.json")
        self.write_statement()

        self.cf = os.path.join(self.work, "src", "chiplet-flow")
        self.sp = os.path.join(self.work, "src", "chiplet-spec")
        write(f"{self.cf}/src/chiplet_die_flow/__init__.py", "import chiplet_format_io\nx = 1\n")
        write(f"{self.cf}/src/librelane_plugin_chiplet/__init__.py", "from chiplet_die_flow import x\n")
        write(f"{self.sp}/reference/python/chiplet_format_io/__init__.py", "z = 3\n")
        self.description = f"{self.sp}/description/src/chiplet_system_description/__init__.py"
        write(self.description, "y = 2\n")
        # As the CLI shell exports them: chiplet-spec once per directory pip
        # installs from.
        self.trees = (f"chiplet-flow|{self.cf}|.\nchiplet-spec|{self.sp}|reference/python\n"
                      f"chiplet-spec|{self.sp}|description\n")
        self.roots = [f"{self.cf}/src", f"{self.sp}/reference/python"]

        self.lock = os.path.join(self.work, "flake.lock")
        self.write_lock()
        self.closure_paths = [self.bash, self.librelane, self.pyenv, self.pdk, PREFIX]
        self.launcher, self.sha = self.write_launcher(*self.roots)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ADK_NIX_LIBRELANE": self.librelane, "ADK_NIX_SG13G2_PDK": self.pdk,
            "ADK_NIX_LIBRELANE_PYTHON": self.interpreter,
            "PDK_ROOT": self.pdk, "PDK": "ihp-sg13g2",
            "CHIPLET_FLOW_LIBRELANE_LAUNCHER": self.launcher,
            "CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256": self.sha,
            "ADK_NIX_CLOSURE_INFO": os.path.join(self.work, "closure-info"),
            "ADK_NIX_FLAKE_LOCK": self.lock, "ADK_NIX_TREES": self.trees,
            **self.identities(),
        }
        self.out = os.path.join(self.work, "ctx", "build-context.json")
        os.makedirs(os.path.dirname(self.out))

    def cleanup(self):
        for root, dirs, files in os.walk(self.work):
            for name in dirs + files:
                path = os.path.join(root, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o755)
        shutil.rmtree(self.work)

    def write_lock(self, drop=None, nested_only=None, no_hash=None):
        nodes = {"root": {"inputs": {}}}
        for name, owner, repo in (("librelane", "librelane", "librelane"),
                                  ("ihp-open-pdk", "IHP-GmbH", "IHP-Open-PDK"),
                                  ("pycell4klayout-api", "IHP-GmbH", "pycell4klayout-api"),
                                  ("pypreprocessor", "IHP-GmbH", "pypreprocessor")):
            if name == drop:
                continue
            nodes[name] = {"locked": {"type": "github", "owner": owner, "repo": repo,
                                      "rev": self.rev(name), "narHash": f"sha256-{name}="}}
            if name == no_hash:
                del nodes[name]["locked"]["narHash"]
            if name != nested_only:
                nodes["root"]["inputs"][name] = name
        with open(self.lock, "w") as handle:
            json.dump({"nodes": nodes, "root": "root", "version": 7}, handle)

    @staticmethod
    def rev(name):
        return (name.encode().hex() * 40)[:40]

    def identities(self, librelane=None):
        """The LibreLane adkIdentity JSON the flake would export, agreeing
        with write_lock; librelane: a function that edits the parsed
        identity."""
        identity = {"input": {"rev": self.rev("librelane"), "narHash": "sha256-librelane="},
                    "patches": [{"file": "a.patch", "sha256": "a" * 64},
                                {"file": "b.patch", "sha256": "b" * 64}],
                    "patchedRev": "3" * 40}
        if librelane:
            librelane(identity)
        return {"ADK_NIX_LIBRELANE_IDENTITY": json.dumps(identity)}

    def write_statement(self, edit=None, raw=None, mode=0o444):
        """The PDK tree's pdk-source.json as the flake writes it (the bytes of
        builtins.toJSON and a newline), agreeing with write_lock and the tree;
        edit: a function that changes the value first; raw: other bytes."""
        value = {"format": "adk-tools-pdk-source/v1", "materialized_at": self.pdk,
                 "parent": {"input": "ihp-open-pdk", "rev": self.rev("ihp-open-pdk"),
                            "narHash": "sha256-ihp-open-pdk="},
                 "submodules": [{"path": path, "input": name, "rev": self.rev(name),
                                 "narHash": f"sha256-{name}="}
                                for name, path in sorted(FETCHED.items(), key=lambda i: i[1])],
                 "not_fetched": sorted(set(GITLINKS) - set(FETCHED.values()))}
        if edit:
            edit(value)
        if raw is None:
            raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if os.path.lexists(self.statement_path):
            os.remove(self.statement_path)
        with open(self.statement_path, "wb") as handle:
            handle.write(raw)
        os.chmod(self.statement_path, mode)
        return raw

    def write_launcher(self, *roots, drop_line=None, swap=None):
        """The launcher as build-env.sh writes it, in a new directory; with
        drop_line, that line taken out afterwards, or with swap (old, new) a
        substring replaced (a launcher build-env.sh would not write), and its
        digest taken again."""
        d = tempfile.mkdtemp(dir=self.work, prefix="launcher-")
        done = subprocess.run(
            ["bash", "-c", f"ADK_NIX_LIB_ONLY=1; . '{HERE}/cli-env.sh'; ADK_NIX_LIB_ONLY=1; "
                           f". '{HERE}/build-env.sh'; adk_nix_write_launcher \"$@\"", "_",
             d, self.librelane, *roots],
            capture_output=True, text=True, env=dict(os.environ, ADK_NIX_STORE_DIR=self.store))
        self.assertEqual(done.returncode, 0, done.stderr)
        path = os.path.join(d, "librelane")
        if drop_line:
            with open(path) as handle:
                lines = handle.read().splitlines(keepends=True)
            kept = [line for line in lines if not line.startswith(drop_line)]
            self.assertEqual(len(kept), len(lines) - 1)
            write(path, "".join(kept), 0o555)
        if swap:
            with open(path) as handle:
                text = handle.read()
            self.assertIn(swap[0], text)
            write(path, text.replace(swap[0], swap[1]), 0o555)
        return path, cc.sha256_file(path)

    def fake_closure(self, _info):
        record = {"closure_info": "stand-in", "registration_sha256": "0" * 64,
                  "paths": [{"path": p, "narHash": NAR, "narSize": 1, "references": 0}
                            for p in self.closure_paths]}
        for p, value in getattr(self, "bad_hash", {}).items():
            record["paths"] = [e if e["path"] != p else dict(e, narHash=value)
                               for e in record["paths"]]
        for p in getattr(self, "unhashed", []):
            record["paths"] = [e if e["path"] != p else {"path": p} for e in record["paths"]]
            record.setdefault("paths_without_hash", []).append(p)
        return record

    def capture(self, **env):
        """(exit, record, stderr) of one capture with the fixture's env,
        changed by env (None removes a variable)."""
        full = dict(self.env)
        for key, value in env.items():
            if value is None:
                full.pop(key, None)
            else:
                full[key] = value
        err = io.StringIO()
        with mock.patch.dict(os.environ, full, clear=True), \
                mock.patch.object(cc, "closure", self.fake_closure), \
                contextlib.redirect_stderr(err):
            code = cbc.main(["capture_build_context.py", self.out])
        with open(self.out) as handle:
            record = json.load(handle)
        os.remove(self.out)
        return code, record, err.getvalue()

    def refused(self, needle, **env):
        code, record, err = self.capture(**env)
        self.assertEqual(code, 1, record["problems"])
        self.assertTrue(any(needle in p for p in record["problems"]),
                        f"no problem mentions {needle!r}: {record['problems']}")
        self.assertIn(needle, err)
        return record


class Accepted(Fixture):
    def test_the_launcher_as_written(self):
        code, record, err = self.capture()
        self.assertEqual((code, record["problems"], err), (0, [], ""))
        probe = record["probe"]
        self.assertEqual(probe["version"], "3.0.0rc0")
        self.assertEqual(probe["plugins"], [cbc.PLUGIN])
        self.assertEqual(probe["other_loaders"], [])
        self.assertEqual(probe["loaded"]["chiplet_die_flow"],
                         [f"{self.cf}/src/chiplet_die_flow/__init__.py"])
        self.assertEqual(sorted(probe["loaded"]), sorted(cbc.PACKAGES))
        self.assertEqual(probe["loaded"]["chiplet_format_io"],
                         [f"{self.sp}/reference/python/chiplet_format_io/__init__.py"])
        self.assertEqual(record["roots"]["chiplet_format_io"], f"{self.sp}/reference/python")
        self.assertEqual(probe["required_at_start"], ["chiplet_die_flow", cbc.PLUGIN, "chiplet_format_io"])
        self.assertEqual(sorted(record["sources"]), ["chiplet-flow", "chiplet-spec"])
        self.assertTrue(any(f.startswith(PREFIX + "/") for f in probe["loaded_files"]))
        launcher = record["launcher"]
        self.assertEqual(launcher["actual_sha256"], self.sha)
        self.assertEqual(launcher["execs"], f"{self.librelane}/bin/librelane")
        self.assertEqual(launcher["interpreter"], f"{self.bash}/bin/bash")
        self.assertEqual(launcher["exec_shell"], f"{self.bash}/bin/bash")
        self.assertEqual(launcher["pythonpath"], self.roots)
        self.assertEqual(launcher["mode"], "0o555")
        self.assertEqual(record["librelane"]["closure"]["narHash"], NAR)
        self.assertEqual(sorted(record["pdk"]["lock"]),
                         ["ihp-open-pdk", "pycell4klayout-api", "pypreprocessor"])
        self.assertEqual([p["file"] for p in record["librelane"]["patches_applied"]],
                         ["a.patch", "b.patch"])
        self.assertEqual(record["librelane"]["patched_rev_claim"]["rev"], "3" * 40)
        self.assertIs(record["librelane"]["patched_rev_claim"]["verified_here"], False)
        with open(self.statement_path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(record["pdk"]["source"], {"path": self.statement_path, "sha256": digest})
        self.assertEqual(record["pdk"]["statement"]["parent"]["rev"], self.rev("ihp-open-pdk"))
        self.assertNotIn("evaluated_identity", record["pdk"])
        self.assertEqual(record["format"], cbc.FORMAT)
        interp = record["librelane"]["interpreter"]
        self.assertEqual(interp["path"], self.interpreter)
        self.assertEqual(interp["store_path"], self.pyenv)
        self.assertEqual(interp["closure"]["narHash"], NAR)
        self.assertEqual(interp["serves"]["package"], self.package)
        self.assertEqual(interp["serves"]["package_store_path"], self.librelane)
        self.assertEqual(interp["serves"]["found"], [os.path.join(self.site, "librelane")])
        self.assertEqual(interp["serves"]["resolved"], [self.package])
        self.assertEqual(interp["sha256"], cc.sha256_file(self.interpreter))
        self.assertEqual(interp["probe"]["argv"][:4], [self.interpreter, "-B", "-s", "-c"])
        self.assertEqual(interp["probe"]["set"], {"PYTHONNOUSERSITE": "1"})
        answer = interp["probe"]["answer"]
        self.assertEqual(answer["executable"], self.interpreter)
        self.assertEqual(os.path.realpath(answer["origin"]), f"{self.package}/__init__.py")

    def test_hostile_python_variables_do_not_reach_librelane(self):
        hostile = os.path.join(self.work, "hostile")
        write(f"{hostile}/evil.pth", "import evil_mod\n")
        write(f"{hostile}/evil_mod.py", "")
        write(f"{hostile}/librelane_plugin_evil/__init__.py", "")
        write(f"{hostile}/chiplet_die_flow/__init__.py", "")
        write(f"{hostile}/chiplet_format_io/__init__.py", "")
        write(f"{hostile}/librelane/__init__.py", "")
        write(f"{hostile}/bash_env", "export PYTHONPATH=" + hostile + "\n")
        code, record, _ = self.capture(NIX_PYTHONPATH=hostile, PYTHONPATH=hostile,
                                       NIX_PYTHONEXECUTABLE="/x/python", NIX_PYTHONPREFIX="/x",
                                       PYTHONSTARTUP=f"{hostile}/evil_mod.py",
                                       BASH_ENV=f"{hostile}/bash_env", ENV=f"{hostile}/bash_env")
        self.assertEqual((code, record["problems"]), (0, []))
        # The cores' interpreter is asked without them, as a core launch runs it.
        self.assertEqual(record["librelane"]["interpreter"]["probe"]["unset"],
                         ["BASH_ENV", "ENV", "NIX_PYTHONEXECUTABLE", "NIX_PYTHONPATH",
                          "NIX_PYTHONPREFIX", "PYTHONPATH"])

    def test_the_retired_pdk_identity_is_not_read(self):
        code, record, _ = self.capture(ADK_NIX_SG13G2_PDK_IDENTITY="{")
        self.assertEqual((code, record["problems"]), (0, []))

    def test_a_left_out_gitlink_present_as_an_empty_directory(self):
        os.makedirs(os.path.join(self.pdk, "ihp-sg13g2/libs.tech/palace"))
        code, record, _ = self.capture()
        self.assertEqual((code, record["problems"]), (0, []))

    def test_a_tree_declared_once_per_install_directory(self):
        # chiplet-spec's two rows are one tree: one root, one source record,
        # and the description's directory is not a root.
        code, record, _ = self.capture()
        self.assertEqual((code, record["problems"]), (0, []))
        self.assertEqual(record["roots"]["chiplet_format_io"], f"{self.sp}/reference/python")
        self.assertEqual(record["sources"]["chiplet-spec"], cc.git_state(self.sp))
        self.assertNotIn(f"{self.sp}/description/src", record["launcher"]["pythonpath"])

    def test_output_written_once(self):
        write(self.out, "earlier\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cbc.main(["capture_build_context.py", self.out]), 1)
        with open(self.out) as handle:
            self.assertEqual(handle.read(), "earlier\n")


class Refused(Fixture):
    def test_a_launcher_that_keeps_nix_pythonpath(self):
        # Without the drop, the store Python's sitecustomize adds the
        # directory, runs its .pth and LibreLane finds its plugin.
        self.launcher, self.sha = self.write_launcher(*self.roots, drop_line="for v in ")
        hostile = os.path.join(self.work, "hostile")
        write(f"{hostile}/evil.pth", "import evil_mod\n")
        write(f"{hostile}/evil_mod.py", "")
        write(f"{hostile}/librelane_plugin_evil/__init__.py", "")
        record = self.refused(f"{hostile}/evil_mod.py loaded from outside", NIX_PYTHONPATH=hostile,
                              CHIPLET_FLOW_LIBRELANE_LAUNCHER=self.launcher,
                              CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=self.sha)
        self.assertEqual(record["probe"]["plugins"], [cbc.PLUGIN, "librelane_plugin_evil"])

    def test_a_package_from_the_other_root(self):
        # The spec's root first on the path (the roots are compared as a set),
        # so a copy of the die flow there wins.
        self.launcher, self.sha = self.write_launcher(*reversed(self.roots))
        copy = f"{self.sp}/reference/python/chiplet_die_flow/__init__.py"
        write(copy, "x = 1\n")
        record = self.refused(f"chiplet_die_flow loaded from {copy}",
                              CHIPLET_FLOW_LIBRELANE_LAUNCHER=self.launcher,
                              CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=self.sha)
        self.assertTrue(any("chiplet_die_flow was not loaded from" in p for p in record["problems"]))

    def test_format_io_from_another_root(self):
        # chiplet-flow's root comes first on the path, so a copy there wins.
        write(f"{self.cf}/src/chiplet_format_io/__init__.py", "")
        record = self.refused(f"chiplet_format_io loaded from {self.cf}/src/chiplet_format_io/__init__.py")
        self.assertTrue(any("chiplet_format_io was not loaded from" in p for p in record["problems"]))

    def test_the_system_description_loaded_at_start(self):
        # The die flow imports no system description; one loaded at start lies
        # outside every root (its directory is not one) and is refused like any
        # other such file.
        write(f"{self.cf}/src/chiplet_die_flow/__init__.py",
              f"import sys\nsys.path.append({self.sp + '/description/src'!r})\n"
              "import chiplet_system_description\nimport chiplet_format_io\nx = 1\n")
        self.refused(f"{self.description} loaded from outside the realised closure and the roots")

    def test_format_io_not_loaded_at_start(self):
        # A die flow that imports the reader later is not the one staging.pin names.
        write(f"{self.cf}/src/chiplet_die_flow/__init__.py", "x = 1\n")
        record = self.refused(f"chiplet_format_io was not loaded from {self.sp}/reference/python")
        self.assertEqual(record["probe"]["loaded"]["chiplet_format_io"], [])
        self.assertIn("chiplet_format_io", record["probe"]["required_at_start"])

    def test_a_file_outside_the_closure_and_roots(self):
        stray = os.path.join(self.work, "outside")
        write(f"{stray}/stray.py", "")
        write(f"{self.sp}/reference/python/chiplet_format_io/__init__.py",
              f"import sys\nsys.path.append({stray!r})\nimport stray\n")
        self.refused(f"{stray}/stray.py loaded from outside the realised closure")

    def test_a_sourceless_pyc_outside_under_a_quote(self):
        # repr() quotes a path holding ' with ", which the trace must still read.
        stray = os.path.join(self.work, "out'side")
        write(f"{stray}/bare.py", "")
        py_compile.compile(f"{stray}/bare.py", cfile=f"{stray}/bare.pyc", doraise=True)
        os.remove(f"{stray}/bare.py")
        write(f"{self.sp}/reference/python/chiplet_format_io/__init__.py",
              f"import sys\nsys.path.append({stray!r})\nimport bare\n")
        self.refused(f"{stray}/bare.pyc loaded from outside the realised closure")

    def test_a_wrapper_run_under_another_bash(self):
        self.launcher, self.sha = self.write_launcher(
            *self.roots,
            swap=(f"exec '{self.bash}/bin/bash' -p -e", "exec '/bin/bash' -p -e"))
        self.refused("not one bash for both", CHIPLET_FLOW_LIBRELANE_LAUNCHER=self.launcher,
                     CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=self.sha)

    def test_another_plugin(self):
        write(f"{self.cf}/src/librelane_plugin_other/__init__.py", "")
        self.refused("expected ['librelane_plugin_chiplet']")

    def test_no_plugin(self):
        shutil.rmtree(f"{self.cf}/src/librelane_plugin_chiplet")
        record = self.refused("LibreLane discovered []")
        self.assertTrue(any("librelane_plugin_chiplet was not loaded" in p for p in record["problems"]))

    def test_librelane_exits_non_zero(self):
        self.refused("launcher --version exited 3", STAND_IN_EXIT="3")

    def test_digest_other_than_exported(self):
        self.refused("the shell exported " + "0" * 64,
                     CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256="0" * 64)

    def test_writable_launcher(self):
        os.chmod(self.launcher, 0o755)
        self.refused("is writable (0o755)")

    def test_launcher_through_a_link(self):
        link = os.path.join(self.work, "link-to-launcher")
        os.symlink(self.launcher, link)
        self.refused("is not a regular file", CHIPLET_FLOW_LIBRELANE_LAUNCHER=link)

    def test_launcher_for_another_librelane(self):
        other = os.path.join(self.store, h32(4) + "-python3.13-librelane-x")
        self.closure_paths.append(other)
        self.refused(f"not {other}/bin/librelane", ADK_NIX_LIBRELANE=other)

    def test_an_extra_root(self):
        extra = os.path.join(self.work, "extra")
        os.makedirs(extra)
        self.launcher, self.sha = self.write_launcher(*self.roots, extra)
        self.refused("launcher's PYTHONPATH is", CHIPLET_FLOW_LIBRELANE_LAUNCHER=self.launcher,
                     CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=self.sha)

    def test_a_root_missing(self):
        self.launcher, self.sha = self.write_launcher(f"{self.sp}/reference/python")
        self.refused("launcher's PYTHONPATH is", CHIPLET_FLOW_LIBRELANE_LAUNCHER=self.launcher,
                     CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=self.sha)

    def test_the_format_io_root_missing(self):
        self.launcher, self.sha = self.write_launcher(f"{self.cf}/src")
        self.refused("launcher's PYTHONPATH is", CHIPLET_FLOW_LIBRELANE_LAUNCHER=self.launcher,
                     CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=self.sha)

    def test_bash_outside_the_closure(self):
        self.closure_paths.remove(self.bash)
        self.refused("outside the realised closure")

    def test_librelane_outside_the_closure(self):
        self.closure_paths.remove(self.librelane)
        self.refused(f"librelane {self.librelane!r} is not a path of the realised closure")

    def test_interpreter_not_exported(self):
        self.refused("ADK_NIX_LIBRELANE_PYTHON is not set", ADK_NIX_LIBRELANE_PYTHON=None)

    def test_the_shell_s_own_python(self):
        # In the closure and in its store path's bin, but no librelane: the
        # negative control a core preparation refuses too.
        record = self.refused(f"serves [], not exactly {self.package}",
                              ADK_NIX_LIBRELANE_PYTHON=PYTHON)
        self.assertTrue(any("imports librelane from None" in p for p in record["problems"]))

    def test_the_generated_launcher(self):
        # The other negative control: a script outside the store, serving
        # nothing, that answers no probe.
        record = self.refused(f"librelane interpreter {self.launcher!r} is not in the realised "
                              f"closure", ADK_NIX_LIBRELANE_PYTHON=self.launcher)
        self.assertTrue(any("serves [], not exactly" in p for p in record["problems"]))
        self.assertTrue(any("probe gave no answer" in p for p in record["problems"]),
                        record["problems"])

    def test_interpreter_through_a_link(self):
        link = os.path.join(self.pyenv, "bin", "python-link")
        os.symlink(self.interpreter, link)
        self.refused("is a link, is reached through one", ADK_NIX_LIBRELANE_PYTHON=link)

    def test_interpreter_under_a_linked_store_path(self):
        linked = os.path.join(self.store, h32(6) + "-python3-3.13-env")
        os.symlink(self.pyenv, linked)
        self.closure_paths.append(linked)
        self.refused("is a link, is reached through one",
                     ADK_NIX_LIBRELANE_PYTHON=os.path.join(linked, "bin", PYVER))

    def test_interpreter_outside_the_closure(self):
        self.closure_paths.remove(self.pyenv)
        self.refused(f"librelane interpreter {self.interpreter!r} is not in the realised closure")

    def test_interpreter_store_path_without_a_hash(self):
        self.unhashed = [self.pyenv]
        self.refused(f"store path {self.pyenv} has no narHash")

    def test_interpreter_outside_its_store_path_s_bin(self):
        other = os.path.join(self.pyenv, "libexec", PYVER)
        os.makedirs(os.path.dirname(other))
        shutil.copy2(self.interpreter, other)
        self.refused(f"is not in {self.pyenv}/bin", ADK_NIX_LIBRELANE_PYTHON=other)

    def test_interpreter_not_executable(self):
        os.chmod(self.interpreter, 0o644)
        self.refused("is not executable")

    def test_interpreter_serving_another_librelane(self):
        other = os.path.join(self.store, h32(7) + "-python3.13-librelane-y", "lib", PYVER,
                             "site-packages", "librelane")
        write(os.path.join(other, "__init__.py"), "")
        os.remove(os.path.join(self.site, "librelane"))
        os.symlink(other, os.path.join(self.site, "librelane"))
        record = self.refused(f"serves [{other!r}], not exactly {self.package}")
        self.assertTrue(any(f"not from {self.package}" in p for p in record["problems"]))

    def test_interpreter_serving_it_twice(self):
        second = os.path.join(self.pyenv, "lib", "python3.99", "site-packages")
        os.makedirs(second)
        os.symlink(self.package, os.path.join(second, "librelane"))
        self.refused(f"serves [{self.package!r}, {self.package!r}], not exactly")

    def test_librelane_installing_two_packages(self):
        write(os.path.join(self.librelane, "lib", "python3.99", "site-packages", "librelane",
                           "__init__.py"), "")
        self.refused(f"{self.librelane} installs 2 librelane packages, not one")

    def test_librelane_package_through_a_link(self):
        real = os.path.join(self.work, "real-librelane")
        shutil.move(self.package, real)
        os.symlink(real, self.package)
        self.refused(f"its package {self.package} is reached through a link")

    def test_interpreter_importing_librelane_from_elsewhere(self):
        # The links are right; what the interpreter would import is not.
        elsewhere = os.path.join(self.work, "elsewhere")
        write(f"{elsewhere}/librelane/__init__.py", "")
        write(f"{self.site}/first.pth", f"import sys; sys.path.insert(0, {elsewhere!r})\n")
        self.refused(f"imports librelane from {elsewhere + '/librelane/__init__.py'!r}")

    def test_interpreter_naming_another_executable(self):
        # A script in the env's bin that runs another interpreter: that one
        # reports itself.
        os.remove(self.interpreter)
        write(self.interpreter, f"#!/bin/sh\nexec '{PYTHON}' \"$@\"\n", 0o555)
        self.refused(f"reports sys.executable {PYTHON!r}")

    def test_interpreter_missing(self):
        missing = os.path.join(self.pyenv, "bin", "python9.9")
        self.refused(f"librelane interpreter {missing}: No such file",
                     ADK_NIX_LIBRELANE_PYTHON=missing)

    def test_interpreter_that_cannot_be_run(self):
        # Regular and executable, in no format the kernel runs.
        os.remove(self.interpreter)
        write(self.interpreter, "not a program\n", 0o555)
        self.refused("librelane interpreter probe failed: OSError")

    def test_interpreter_probe_failing(self):
        # A right answer does not stand for a failed run.
        answer = json.dumps({"executable": self.interpreter,
                             "origin": f"{self.site}/librelane/__init__.py",
                             "locations": [f"{self.site}/librelane"]})
        os.remove(self.interpreter)
        write(self.interpreter, f"#!/bin/sh\necho '{answer}'\nexit 3\n", 0o555)
        self.refused("librelane interpreter probe exited 3")

    def test_pdk_root_elsewhere(self):
        self.refused("PDK_ROOT='/elsewhere'", PDK_ROOT="/elsewhere")

    def test_lock_without_an_input(self):
        self.write_lock(drop="pypreprocessor")
        self.refused("pdk: flake lock unreadable")

    def test_lock_input_without_a_narhash(self):
        self.write_lock(no_hash="pycell4klayout-api")
        self.refused("pycell4klayout-api is locked without a full rev and narHash")

    def test_closure_path_without_a_hash(self):
        self.unhashed = [self.librelane]
        record = self.refused(f"closure: 1 paths without a sha256 narHash, first {self.librelane}")
        self.assertTrue(any("has no narHash in the realised closure" in p for p in record["problems"]))

    def test_registered_dependency_with_an_empty_hash(self):
        # Registered, so not in paths_without_hash, but its hash is empty.
        self.bad_hash = {self.closure_paths[-1]: ""}
        self.refused(f"closure: 1 paths without a sha256 narHash, first {self.closure_paths[-1]}")

    def test_registered_dependency_with_a_malformed_hash(self):
        self.bad_hash = {self.closure_paths[-1]: "sha256:0"}
        self.refused("closure: 1 paths without a sha256 narHash")

    def test_identity_not_exported(self):
        self.refused("ADK_NIX_LIBRELANE_IDENTITY is not set", ADK_NIX_LIBRELANE_IDENTITY=None)

    def test_identity_json_but_not_an_object(self):
        for raw in ("null", "[]", "0", '"x"', "true"):
            with self.subTest(raw=raw):
                self.refused("the evaluated identity is not an object",
                             ADK_NIX_LIBRELANE_IDENTITY=raw)

    def test_identity_not_json(self):
        self.refused("ADK_NIX_LIBRELANE_IDENTITY is not JSON", ADK_NIX_LIBRELANE_IDENTITY="{")

    def test_identity_for_another_librelane_input(self):
        ids = self.identities(librelane=lambda i: i["input"].update(rev="f" * 40))
        self.refused("librelane input: evaluated rev", **ids)

    def test_a_patch_without_its_digest(self):
        ids = self.identities(librelane=lambda i: i["patches"][1].pop("sha256"))
        self.refused("librelane: patch 1 has no file name and sha256", **ids)

    def test_no_patched_rev_in_the_identity(self):
        ids = self.identities(librelane=lambda i: i.pop("patchedRev"))
        self.refused("names no LIBRELANE_PATCHED_REV", **ids)

    def test_no_statement(self):
        os.remove(self.statement_path)
        record = self.refused(f"pdk: {self.statement_path} unreadable")
        self.assertEqual(record["pdk"]["source"], {"path": self.statement_path, "sha256": None})

    def test_statement_through_a_link(self):
        real = os.path.join(self.work, "elsewhere.json")
        os.rename(self.statement_path, real)
        os.symlink(real, self.statement_path)
        self.refused(f"pdk: {self.statement_path} is not a regular file")

    def test_writable_statement(self):
        self.write_statement(mode=0o644)
        self.refused("is writable (0o644)")

    def test_statement_not_json(self):
        self.write_statement(raw=b"{")
        record = self.refused(f"pdk: {self.statement_path} is not JSON")
        self.assertEqual(record["pdk"]["source"]["sha256"], hashlib.sha256(b"{").hexdigest())

    def test_statement_not_an_object(self):
        for raw in (b"[]\n", b"null\n", b'"x"\n'):
            with self.subTest(raw=raw):
                self.write_statement(raw=raw)
                self.refused("pdk: the statement is not an object")

    def test_statement_not_in_the_flake_s_bytes(self):
        value = json.loads(self.write_statement())
        for raw in ((json.dumps(value, sort_keys=True, indent=2) + "\n").encode(),
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
                    (json.dumps(dict(reversed(list(value.items()))), separators=(",", ":"))
                     + "\n").encode()):
            with self.subTest(raw=raw[:40]):
                self.write_statement(raw=raw)
                self.refused("is not sorted-key compact JSON ending in one newline")

    def test_another_format(self):
        self.write_statement(lambda v: v.update(format="adk-tools-pdk-source/v0"))
        self.refused("format is 'adk-tools-pdk-source/v0'")

    def test_a_key_more(self):
        self.write_statement(lambda v: v.update(pdk="ihp-sg13g2"))
        self.refused("pdk: the statement's keys are")

    def test_materialized_elsewhere(self):
        # A tree copied out of the store keeps its builder's $out.
        self.write_statement(lambda v: v.update(materialized_at=v["materialized_at"] + "-copy"))
        self.refused(f"not at the exported {self.pdk!r}")

    def test_parent_other_than_locked(self):
        # --override-input: the evaluated input moves, the lock file does not.
        self.write_statement(lambda v: v["parent"].update(rev="f" * 40))
        self.refused("pdk parent: evaluated rev")

    def test_parent_for_another_input(self):
        self.write_statement(lambda v: v["parent"].update(input="ihp-sg13cmos5l"))
        self.refused("pdk: the statement's parent is not {input: 'ihp-open-pdk'")

    def test_submodule_other_than_locked(self):
        self.write_statement(lambda v: v["submodules"][1].update(narHash="sha256-other="))
        self.refused("pdk submodule pypreprocessor: evaluated narHash")

    def test_a_submodule_missing(self):
        self.write_statement(lambda v: v["submodules"].pop())
        record = self.refused("pdk: the statement's submodules name ['pycell4klayout-api']")
        self.assertTrue(any(".gitmodules names" in p for p in record["problems"]))

    def test_a_submodule_twice(self):
        self.write_statement(lambda v: v["submodules"].append(dict(v["submodules"][0])))
        self.refused("not ['pycell4klayout-api', 'pypreprocessor'] once each")

    def test_a_submodule_of_another_input(self):
        self.write_statement(lambda v: v["submodules"][0].update(input="digital"))
        self.refused("pdk: the statement's submodule 0 is not {path, input, rev, narHash}")

    def test_gitlink_paths_not_canonical(self):
        for bad, why in (("/abs", "absolute"), ("a//b", "not canonical"),
                         ("../palace", "not canonical"), ("a/./b", "not canonical"),
                         ("", "not a nonempty string"), (7, "not a nonempty string")):
            with self.subTest(bad=bad):
                self.write_statement(lambda v: v["not_fetched"].append(bad))
                self.refused(f"not_fetched path {bad!r} is {why}")

    def test_not_fetched_unsorted(self):
        self.write_statement(lambda v: v["not_fetched"].reverse())
        self.refused("pdk: the statement's not_fetched paths are not sorted and unique")

    def test_a_left_out_path_under_a_fetched_one(self):
        inner = FETCHED["pypreprocessor"] + "/vendored"
        self.write_statement(lambda v: v["not_fetched"].append(inner))
        self.refused("gitlink paths overlap")

    def test_a_gitlink_left_unaccounted(self):
        self.write_statement(lambda v: v["not_fetched"].remove("ihp-sg13g2/libs.tech/palace"))
        self.refused("pdk: the tree's .gitmodules names")

    def test_no_gitmodules_in_the_tree(self):
        os.remove(os.path.join(self.pdk, ".gitmodules"))
        self.refused("pdk: the tree's .gitmodules unreadable")

    def test_a_fetched_submodule_empty_in_the_tree(self):
        shutil.rmtree(os.path.join(self.pdk, FETCHED["pycell4klayout-api"]))
        os.makedirs(os.path.join(self.pdk, FETCHED["pycell4klayout-api"]))
        self.refused(f"submodule {FETCHED['pycell4klayout-api']} is not a filled directory")

    def test_a_left_out_path_with_files_in_the_tree(self):
        write(os.path.join(self.pdk, "ihp-sg13g2/libs.tech/palace/x.py"), "")
        self.refused("ihp-sg13g2/libs.tech/palace is left out by the statement but not empty")

    def test_lock_input_only_nested(self):
        self.write_lock(nested_only="librelane")
        self.refused("librelane is not a direct input of the flake")

    def test_a_tree_undeclared(self):
        trees = "".join(line + "\n" for line in self.trees.splitlines() if line.startswith("chiplet-spec|"))
        record = self.refused("declared no chiplet-flow tree", ADK_NIX_TREES=trees)
        self.assertIsNone(record["probe"])

    def test_the_spec_tree_undeclared(self):
        record = self.refused("declared no chiplet-spec tree", ADK_NIX_TREES=f"chiplet-flow|{self.cf}|.\n")
        self.assertIsNone(record["probe"])

    def test_one_tree_declared_as_two(self):
        other = os.path.join(self.work, "src", "chiplet-spec-2")
        trees = self.trees.replace(f"{self.sp}|description", f"{other}|description")
        record = self.refused(f"chiplet-spec is declared as both {self.sp} and {other}", ADK_NIX_TREES=trees)
        self.assertIsNone(record["probe"])

    def test_nothing_set_still_writes_a_record(self):
        record = self.refused("CHIPLET_FLOW_LIBRELANE_LAUNCHER is not set",
                              CHIPLET_FLOW_LIBRELANE_LAUNCHER=None)
        self.assertIsNone(record["probe"])


class LauncherTrees(unittest.TestCase):
    def test_the_roots_are_build_env_s(self):
        """PACKAGES and ROOT_DIR say what ADK_NIX_LAUNCHER_TREES says."""
        done = subprocess.run(["bash", "-c", f"ADK_NIX_LIB_ONLY=1; . '{HERE}/build-env.sh'; "
                                             "printf '%s' \"$ADK_NIX_LAUNCHER_TREES\""],
                              capture_output=True, text=True, check=True)
        packages, root_dir = {}, {}
        for line in done.stdout.splitlines():
            if line.strip():
                name, sub, pkgs = line.split("|")
                root_dir[name] = sub
                packages.update((pkg, name) for pkg in pkgs.split())
        self.assertEqual(packages, cbc.PACKAGES)
        self.assertEqual(root_dir, cbc.ROOT_DIR)
        self.assertLessEqual(set(cbc.AT_START), set(cbc.PACKAGES))


class Paths(unittest.TestCase):
    def test_within_is_by_component(self):
        self.assertTrue(cbc._within("a/b/c", "a/b"))
        self.assertTrue(cbc._within("a/b", "a/b"))
        self.assertFalse(cbc._within("a/bc", "a/b"))
        self.assertFalse(cbc._within("a", "a/b"))

    def test_gitmodule_paths(self):
        self.assertEqual(cbc.gitmodule_paths(
            '[submodule "x"]\n\tpath = a/b\n\turl = u\n[submodule "y"]\nPath=c d \n'),
            ["a/b", "c d"])


class Trace(unittest.TestCase):
    def test_every_file_line(self):
        files, loaders, unread, rest = cbc.parse_trace("\n".join([
            "# code object from /r/src/chiplet_die_flow/__init__.py",
            "# code object from '/s/lib/__pycache__/os.cpython-313.pyc'",
            "# /s/lib/__pycache__/os.cpython-313.pyc matches /s/lib/os.py",
            "# extension module 'math' loaded from '/s/lib-dynload/math.so'",
            "# extension module 'math' executed from '/s/lib-dynload/math.so'",
            "import 'os' # <_frozen_importlib_external.SourceFileLoader object at 0x7f00>",
            "import '_io' # <class '_frozen_importlib.BuiltinImporter'>",
            "import 'codecs' # <class '_frozen_importlib.FrozenImporter'>",
            "import 'ns' # <_frozen_importlib_external.NamespaceLoader object at 0xab>",
            "import 'z' # <zipimporter object \"/tmp/z.zip/\">",
            "# cleanup[2] removing os",
            "warning: something",
        ]))
        self.assertEqual(files, ["/r/src/chiplet_die_flow/__init__.py",
                                 "/s/lib/__pycache__/os.cpython-313.pyc",
                                 "/s/lib/__pycache__/os.cpython-313.pyc", "/s/lib/os.py",
                                 "/s/lib-dynload/math.so"])
        self.assertEqual(loaders, ["import 'z' # <zipimporter object \"/tmp/z.zip/\">"])
        self.assertEqual((unread, rest), ([], ["warning: something"]))

    def test_a_path_repr_quotes_with_double_quotes(self):
        files, _, unread, _ = cbc.parse_trace("\n".join([
            "# code object from \"/o'x/bare.pyc\"",
            "# extension module 'foo' loaded from \"/o'x/foo.so\"",
            "# extension module 'foo' executed from \"/o'x/foo.so\"",
            "# code object from '/a\\'b\"c/x.pyc'",
        ]))
        self.assertEqual((files, unread), (["/o'x/bare.pyc", "/o'x/foo.so", "/a'b\"c/x.pyc"], []))

    def test_a_load_line_it_cannot_read_is_kept(self):
        lines = ["# code object from 'unterminated",
                 "# extension module 'foo' loaded from somewhere",
                 "# /a b/x.pyc matches /a b/x.py"]
        files, _, unread, rest = cbc.parse_trace("\n".join(lines))
        self.assertEqual((files, unread, rest), ([], lines, []))

    def test_a_loader_that_names_no_file_is_refused(self):
        problems = []
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, "LibreLane v1\nDiscovered plugins:\nlibrelane_plugin_chiplet\n",
                "import 'chiplet_die_flow' # <custom.Loader object at 0x1>\n")):
            cbc.probe_launcher("/l", {p: "/r" for p in cbc.PACKAGES}, [], {}, problems)
        self.assertTrue(any("loader that names no file" in p for p in problems), problems)

    def test_version_and_plugins(self):
        self.assertEqual(cbc.parse_version("LibreLane v3.0.0\n\nCopyright\nDiscovered plugins:\n"
                                           "librelane_plugin_chiplet\nlibrelane_plugin_x -> 1.2\n"),
                         ("3.0.0", ["librelane_plugin_chiplet", "librelane_plugin_x"]))
        self.assertEqual(cbc.parse_version("LibreLane v3.0.0\n"), ("3.0.0", []))

    def test_a_checkout_path_named_like_a_package(self):
        roots = {"chiplet_die_flow": "/h/chiplet_format_io/src", cbc.PLUGIN: "/h/chiplet_format_io/src",
                 "chiplet_format_io": "/h/sp/reference/python"}
        self.assertEqual(cbc.classify("/h/chiplet_format_io/src/chiplet_die_flow/a.py", roots),
                         ("chiplet_die_flow", True))
        self.assertEqual(cbc.classify("/h/chiplet_format_io/src/other.py", roots), (None, False))
        self.assertEqual(cbc.classify("/nix/store/x-lib/chiplet_die_flow.py", roots),
                         ("chiplet_die_flow", False))
        self.assertEqual(cbc.classify("/nix/store/x-chiplet_die_flow-1/y.py", roots), (None, False))


def nix_python():
    found = os.environ.get("TEST_NIX_PYTHON") or \
        next(iter(sorted(glob.glob("/nix/store/*-python3-3.1[3-9]*/bin/python3"))), None)
    return found if found and os.access(found, os.X_OK) else None


@unittest.skipIf(nix_python() is None, "no Nix Python (set TEST_NIX_PYTHON)")
class Bootstrap(unittest.TestCase):
    """The capture's own interpreter, run as build-env.sh runs it (-I -S),
    does not read NIX_PYTHONPATH; the same interpreter without -S does, which
    shows the hostile directory would have been used."""

    def run_capture(self, *flags):
        work = tempfile.mkdtemp(prefix="adk-build-context-boot-")
        self.addCleanup(shutil.rmtree, work)
        marker = os.path.join(work, "ran")
        write(f"{work}/hostile/evil.pth", f"import os; open({marker!r}, 'w').close()\n")
        subprocess.run([nix_python(), *flags, os.path.join(HERE, "capture_build_context.py"),
                        os.path.join(work, "out.json")], capture_output=True,
                       env={"PATH": os.environ.get("PATH", ""), "NIX_PYTHONPATH": f"{work}/hostile"})
        return os.path.exists(marker)

    def test_as_build_env_runs_it(self):
        self.assertFalse(self.run_capture("-I", "-S"))

    def test_control_without_s(self):
        self.assertTrue(self.run_capture("-I"))


if __name__ == "__main__":
    unittest.main()
