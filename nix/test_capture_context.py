#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Tests for nix/capture_context.py: reading Nix's files, the origin checks,
and failing closed.

No Nix needed: the registration and lock inputs are written here in the
formats closureInfo and `nix flake lock` produce.

Usage: python3 nix/test_capture_context.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import warnings

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture_context  # noqa: E402

A = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-python3-3.13.9"
B = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-openroad-dcf36133"
C = "/nix/store/cccccccccccccccccccccccccccccccc-glibc-2.40"

# path, narHash, narSize, deriver (empty), reference count, references.
REGISTRATION = "\n".join([
    A, "sha256:0aaa", "100", "", "2", A, C,
    B, "sha256:0bbb", "200", "", "1", C,
    C, "sha256:0ccc", "300", "", "0",
]) + "\n"


class Registration(unittest.TestCase):
    def test_every_path_with_its_hash(self):
        got = capture_context.parse_registration(REGISTRATION)
        self.assertEqual(sorted(got), sorted([A, B, C]))
        self.assertEqual(got[A], {"narHash": "sha256:0aaa", "narSize": 100, "references": 2})
        self.assertEqual(got[C]["references"], 0)

    def test_a_reference_is_never_taken_for_an_entry(self):
        # The self-reference of A and the reference to C sit where a pattern
        # match would start a new entry.
        got = capture_context.parse_registration(REGISTRATION)
        self.assertEqual(got[A]["narHash"], "sha256:0aaa")
        self.assertEqual(got[C]["narHash"], "sha256:0ccc")

    def test_truncated_refuses(self):
        for cut in (REGISTRATION.splitlines()[:3], REGISTRATION.splitlines()[:6]):
            with self.assertRaises(ValueError):
                capture_context.parse_registration("\n".join(cut) + "\n")

    def test_bad_count_refuses(self):
        bad = REGISTRATION.replace("\n2\n", "\ntwo\n", 1)
        with self.assertRaises(ValueError):
            capture_context.parse_registration(bad)

    def test_closure_records_every_store_path(self):
        with tempfile.TemporaryDirectory() as info:
            with open(os.path.join(info, "store-paths"), "w") as handle:
                handle.write("\n".join([A, B, C]) + "\n")
            with open(os.path.join(info, "registration"), "w") as handle:
                handle.write(REGISTRATION)
            with open(os.path.join(info, "total-nar-size"), "w") as handle:
                handle.write("600\n")
            got = capture_context.closure(info)
        self.assertEqual([p["path"] for p in got["paths"]], [A, B, C])
        self.assertNotIn("paths_without_hash", got)
        self.assertEqual(got["total_nar_size"], 600)

    def test_closure_names_a_path_without_hash(self):
        with tempfile.TemporaryDirectory() as info:
            with open(os.path.join(info, "store-paths"), "w") as handle:
                handle.write("\n".join([A, B, C, "/nix/store/dddd-extra"]) + "\n")
            with open(os.path.join(info, "registration"), "w") as handle:
                handle.write(REGISTRATION)
            got = capture_context.closure(info)
        self.assertEqual(got["paths_without_hash"], ["/nix/store/dddd-extra"])

    def test_closure_unparsed_is_unavailable(self):
        with tempfile.TemporaryDirectory() as info:
            with open(os.path.join(info, "store-paths"), "w") as handle:
                handle.write(A + "\n")
            with open(os.path.join(info, "registration"), "w") as handle:
                handle.write(A + "\nsha256:0aaa\n")
            got = capture_context.closure(info)
        self.assertIn("unavailable", got)


class Lock(unittest.TestCase):
    def test_locked_inputs(self):
        lock = {
            "nodes": {
                "root": {"inputs": {"nix-eda": "nix-eda", "librelane": "librelane"}},
                "nix-eda": {"locked": {"type": "github", "owner": "fossi-foundation",
                                       "repo": "nix-eda", "rev": "56e8e416",
                                       "narHash": "sha256-x", "lastModified": 1}},
                "librelane": {"locked": {"type": "github", "owner": "librelane",
                                         "repo": "librelane", "rev": "359bedc8",
                                         "narHash": "sha256-y"}},
            },
            "root": "root",
            "version": 7,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "flake.lock")
            with open(path, "w") as handle:
                json.dump(lock, handle)
            got = capture_context.flake_lock(path)
        self.assertEqual(set(got["inputs"]), {"nix-eda", "librelane"})
        self.assertEqual(got["inputs"]["nix-eda"]["rev"], "56e8e416")
        self.assertNotIn("lastModified", got["inputs"]["nix-eda"])
        self.assertEqual(len(got["sha256"]), 64)


VENV = "/state/evidence-venv-0123456789abcdef"
FLOW = "/work/staging/chiplet-flow"
ENV = "/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-python3-3.13.9-env"
OWN = "/nix/store/ffffffffffffffffffffffffffffffff-nix"


def dist(name, location, version, requires=None, entry_points=None):
    return {"name": name, "location": location, "version": version, "requires": requires,
            "entry_points": entry_points or []}


# An editable install of the die flow (tree chiplet-flow) as Python sees it:
# the .dist-info pip wrote in the venv, and the .egg-info the build left in the
# tree's src/.
EDITABLE_PAIR = [dist("chiplet_die_flow", VENV + "/lib/python3.13/site-packages", "0.1.0", ["pyyaml>=6.0"]),
                 dist("chiplet-die-flow", FLOW + "/src", "0.1.0", ["pyyaml>=6.0"])]


def origin(**change):
    """origin_problems() over a context with nothing wrong in it, except what
    `change` replaces."""
    context = dict(
        modules={"yaml": {"realpath": ENV + "/lib/python3.13/site-packages/yaml/__init__.py"},
                 "chiplet_die_flow": {"realpath": FLOW + "/src/chiplet_die_flow/__init__.py"}},
        trees={"chiplet_die_flow": FLOW},
        closure_paths=[A, ENV],
        venv=VENV,
        own_dir=OWN,
        sys_path=[OWN, A + "/lib/python3.13", VENV + "/lib/python3.13/site-packages",
                  ENV + "/lib/python3.13/site-packages", FLOW + "/src"],
        dists=[dist("PyYAML", ENV + "/lib/python3.13/site-packages", "6.0.3"),
               dist("chiplet_die_flow", VENV + "/lib/python3.13/site-packages", "0.1.0", ["pyyaml>=6.0"])],
        own_dists={"chiplet-die-flow": FLOW},
    )
    context.update(change)
    return capture_context.origin_problems(**context)


class Origin(unittest.TestCase):
    def test_nothing_wrong(self):
        self.assertEqual(origin(), [])

    def test_third_party_from_the_host(self):
        got = origin(modules={"yaml": {"realpath": "/usr/lib/python3/dist-packages/yaml/__init__.py"}})
        self.assertEqual(len(got), 1)
        self.assertIn("outside the realised closure", got[0])

    def test_third_party_from_a_store_path_not_in_the_closure(self):
        stray = "/nix/store/gggggggggggggggggggggggggggggggg-python3.13-pyyaml/yaml/__init__.py"
        got = origin(modules={"yaml": {"realpath": stray}})
        self.assertIn("outside the realised closure", got[0])

    def test_ours_from_somewhere_else(self):
        # From the venv, say a regular (not editable) install left there.
        got = origin(modules={"chiplet_die_flow": {
            "realpath": VENV + "/lib/python3.13/site-packages/chiplet_die_flow/__init__.py"}})
        self.assertIn("not from its verified tree", got[0])

    def test_ours_from_a_sibling_directory_with_a_shared_prefix(self):
        got = origin(modules={"chiplet_die_flow": {
            "realpath": FLOW + "-dev/src/chiplet_die_flow/__init__.py"}})
        self.assertIn("not from its verified tree", got[0])

    def test_module_without_a_file(self):
        got = origin(modules={"chiplet_die_flow": {"realpath": None}})
        self.assertIn("cannot be checked", got[0])

    def test_failed_import_is_not_reported_twice(self):
        self.assertEqual(origin(modules={"chiplet_die_flow": {"import": "failed", "realpath": None}}), [])

    def test_host_directory_on_sys_path(self):
        got = origin(sys_path=[OWN, "/srv/fake-user/lib/python"])
        self.assertIn("sys.path entry /srv/fake-user/lib/python", got[0])

    def test_distribution_outside(self):
        got = origin(dists=[dist("PyYAML", "/usr/lib/python3/dist-packages", "6.0.3")])
        self.assertIn("distribution PyYAML", got[0])

    def test_third_party_twice(self):
        got = origin(dists=[dist("PyYAML", ENV + "/lib/python3.13/site-packages", "6.0.3"),
                            dist("pyyaml", VENV + "/lib/python3.13/site-packages", "6.0.3")])
        self.assertEqual(len(got), 1)
        self.assertIn("found 2 times", got[0])

    def test_editable_pair_of_ours_passes(self):
        self.assertEqual(origin(dists=EDITABLE_PAIR), [])

    def test_editable_pair_with_another_version(self):
        pair = [EDITABLE_PAIR[0], dist("chiplet-die-flow", FLOW + "/src", "0.2.0", ["pyyaml>=6.0"])]
        self.assertIn("found 2 times", origin(dists=pair)[0])

    def test_editable_pair_with_other_requirements(self):
        pair = [EDITABLE_PAIR[0], dist("chiplet-die-flow", FLOW + "/src", "0.1.0", [])]
        self.assertIn("found 2 times", origin(dists=pair)[0])

    def test_editable_pair_with_other_entry_points(self):
        pair = [EDITABLE_PAIR[0], dist("chiplet-die-flow", FLOW + "/src", "0.1.0", ["pyyaml>=6.0"],
                                       ["console_scripts:chiplet-die-flow=elsewhere:main"])]
        self.assertIn("found 2 times", origin(dists=pair)[0])

    def test_ours_twice_in_the_venv(self):
        pair = [EDITABLE_PAIR[0], dist("chiplet-die-flow", VENV + "/lib/python3.13/site-packages/x", "0.1.0",
                                       ["pyyaml>=6.0"])]
        self.assertIn("found 2 times", origin(dists=pair)[0])

    def test_ours_found_three_times(self):
        third = dist("chiplet-die-flow", FLOW + "/build/lib", "0.1.0", ["pyyaml>=6.0"])
        self.assertIn("found 3 times", origin(dists=EDITABLE_PAIR + [third])[0])

    def test_pair_in_a_tree_that_is_not_its_own(self):
        other = "/work/staging/chiplet-spec"
        pair = [EDITABLE_PAIR[0], dist("chiplet-die-flow", other + "/description/src", "0.1.0",
                                       ["pyyaml>=6.0"])]
        got = origin(dists=pair, trees={"chiplet_die_flow": FLOW, "chiplet_system_description": other},
                     own_dists={"chiplet-die-flow": FLOW, "chiplet-system-description": other})
        self.assertIn("found 2 times", got[0])

    def test_third_party_pair_like_ours_is_still_shadowing(self):
        # Same shape as the editable pair, but PyYAML is not ours: refused.
        pair = [dist("PyYAML", VENV + "/lib/python3.13/site-packages", "6.0.3"),
                dist("PyYAML", FLOW + "/src", "6.0.3")]
        self.assertIn("found 2 times", origin(dists=pair)[0])


class Checkout(unittest.TestCase):
    def state(self, **change):
        state = {"path": "/work/tools/adk", "toplevel": "/work/tools/adk",
                 "commit": "a" * 40, "clean": True}
        state.update(change)
        return capture_context.checkout_problems("ADK_ROOT", state)

    def test_clean_at_its_top(self):
        self.assertEqual(self.state(), [])

    def test_unset(self):
        self.assertEqual(capture_context.checkout_problems("ADK_ROOT", "unset"),
                         ["ADK_ROOT: no commit could be recorded"])

    def test_no_commit(self):
        self.assertIn("no commit", self.state(commit="unavailable: rev-parse HEAD failed")[0])

    def test_a_subdirectory_of_a_checkout(self):
        self.assertIn("not the top of its checkout", self.state(toplevel="/work")[0])

    def test_dirty(self):
        self.assertIn("is not clean", self.state(clean=False)[0])

    def test_status_that_failed(self):
        self.assertIn("is not clean", self.state(clean=None)[0])


def script(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


class Refusals(unittest.TestCase):
    """main() fails closed: each missing piece is a named problem and exit 1,
    with the record still written. Run in a subprocess, so the environment is
    exactly the one given. The context directory is tmp/ctx, the working
    directory unless a test moves it."""

    def capture(self, tmp, cwd=None, **env):
        ctx = os.path.join(tmp, "ctx")
        os.makedirs(ctx, exist_ok=True)
        out = os.path.join(ctx, "nix-context.json")
        base = {"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1",
                "ADK_NIX_CONTEXT_OUT": ctx}
        done = subprocess.run(
            [sys.executable, "-B", os.path.join(HERE, "capture_context.py"), out],
            env=dict(base, **env), cwd=cwd or ctx, capture_output=True, text=True, timeout=120)
        with open(out) as handle:
            return done.returncode, json.load(handle)["problems"]

    def test_bare_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, problems = self.capture(tmp)
        self.assertEqual(code, 1)
        for expected in ("flake lock:", "flake revision", "closure:", "OPENROAD is not set",
                         "ADK_NIX_TREES names", "ADK_NIX_ADK_TOOLS is not set",
                         "ADK_ROOT: no commit could be recorded", "running in"):
            self.assertTrue(any(p.startswith(expected) for p in problems), expected)
        self.assertFalse(any(p.startswith("working directory") for p in problems))
        self.assertFalse(any("is not empty" in p for p in problems))

    def test_openroad_that_fails(self):
        # It prints a version and exits 1: the exit status decides, not the text.
        with tempfile.TemporaryDirectory() as tmp:
            tool = script(tmp, "openroad", "echo v2.0-test; exit 1")
            code, problems = self.capture(tmp, OPENROAD=tool)
        self.assertEqual(code, 1)
        self.assertIn("openroad -version did not answer", problems)
        self.assertTrue(any("outside the realised closure" in p and "OpenROAD" in p for p in problems))

    def test_openroad_that_answers(self):
        # The control for the test above: same fake, exit 0.
        with tempfile.TemporaryDirectory() as tmp:
            tool = script(tmp, "openroad", "echo v2.0-test")
            _, problems = self.capture(tmp, OPENROAD=tool)
        self.assertNotIn("openroad -version did not answer", problems)

    def test_git_that_does_not_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.mkdir(bin_dir)
            script(bin_dir, "git", "exit 1")
            _, problems = self.capture(tmp, PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))
        self.assertIn("git --version did not answer", problems)

    def test_lock_and_closure_that_cannot_be_read(self):
        # Present but not in the shape expected: named problems, not a crash
        # before the record is written.
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "flake.lock")
            with open(lock, "w") as handle:
                handle.write("not json")
            info = os.path.join(tmp, "closure-info")
            os.mkdir(info)
            code, problems = self.capture(tmp, ADK_NIX_FLAKE_LOCK=lock, ADK_NIX_CLOSURE_INFO=info)
        self.assertEqual(code, 1)
        self.assertTrue(any(p.startswith("flake lock: flake lock unreadable") for p in problems))
        self.assertTrue(any(p.startswith("closure: closure unreadable") for p in problems))

    def test_adk_tools_at_another_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "adk-tools")
            git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                   "-c", "commit.gpgsign=false", "-C", repo]
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
            _, problems = self.capture(tmp, ADK_NIX_ADK_TOOLS=repo, ADK_NIX_FLAKE_REV="b" * 40)
        self.assertTrue(any(p.startswith("adk-tools checkout is at") for p in problems))

    def test_assembly_lvs_problems_reach_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, problems = self.capture(tmp, CHIPLET_FLOW_ASSEMBLY_LVS="/x")
        self.assertIn("CHIPLET_FLOW_ASSEMBLY_LVS is set in the shell (/x); lvs-verify would run it", problems)

    def test_run_from_another_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, problems = self.capture(tmp, cwd=tmp)
        self.assertTrue(any(p.startswith("working directory") for p in problems))

    def test_something_already_in_the_context_directory(self):
        # `python -m` would import this before anything the hook checked.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "ctx"))
            with open(os.path.join(tmp, "ctx", "yaml.py"), "w") as handle:
                handle.write("")
            _, problems = self.capture(tmp)
        self.assertTrue(any("is not empty: yaml.py" in p for p in problems))

    def test_install_tools_are_never_imported(self):
        # Importing setuptools puts its vendored packages on sys.path, and
        # every evidence shell would then see two typing_extensions.
        self.assertNotIn("setuptools", capture_context.THIRD_PARTY + capture_context.OURS)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "nix-context.json")
            done = subprocess.run(
                [sys.executable, "-B", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); import capture_context; "
                 "capture_context.main(['capture_context.py', sys.argv[2]]); "
                 "print('setuptools' in sys.modules, 'pip' in sys.modules)", HERE, out],
                env={"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"},
                cwd=tmp, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.stdout.strip().splitlines()[-1], "False False")


class KLayout(unittest.TestCase):
    """klayout_tools: the binary on PATH and the klayout.db module, each held to
    what the flake names. A stand-in klayout package is put in sys.modules so
    the module side does not depend on the interpreter running the tests."""

    STORE = "/nix/store/kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk-klayout-0.30.6"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.declared = os.path.join(self.tmp, "klayout-store")
        os.makedirs(os.path.join(self.declared, "bin"))
        self.binary = script(os.path.join(self.declared, "bin"), "klayout", "echo KLayout 0.30.6")
        self.venv = os.path.join(self.tmp, "venv")
        saved = {k: sys.modules.get(k) for k in ("klayout", "klayout.db")}
        self.addCleanup(self.restore, saved)
        self.fake_module("0.30.6")

    def restore(self, saved):
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    def fake_module(self, version, path=None):
        top = types.ModuleType("klayout")
        top.__version__ = version
        db = types.ModuleType("klayout.db")
        db.__file__ = path or os.path.join(self.STORE + "-python", "klayout", "db", "__init__.py")
        top.db = db
        sys.modules["klayout"], sys.modules["klayout.db"] = top, db

    def env(self, **change):
        env = {"PATH": os.path.join(self.declared, "bin"), "ADK_NIX_KLAYOUT": self.declared,
               "ADK_NIX_KLAYOUT_IMAGE_VERSION": "0.30.5", "ADK_NIX_VENV": self.venv,
               "CHIPLET_FLOW_PADMAP_PYTHON": os.path.join(self.venv, "bin", "python")}
        env.update(change)
        return {k: v for k, v in env.items() if v is not None}

    def tools(self, env, closure=None):
        store_of = lambda path: self.STORE if path.startswith(self.declared) else None
        return capture_context.klayout_tools(
            env, [self.STORE] if closure is None else closure, store_of=store_of)

    def test_as_the_shell_sets_it(self):
        # The control for every test below: nothing wrong, the skew stated.
        record, problems = self.tools(self.env())
        self.assertEqual(problems, [])
        self.assertEqual(record["binary"]["realpath"], os.path.realpath(self.binary))
        self.assertEqual(record["binary"]["version"]["stdout"], "KLayout 0.30.6")
        self.assertEqual(record["module"]["version"], "0.30.6")
        self.assertEqual(record["skew"], {"shell": "0.30.6", "image": "0.30.5",
                                          "declared": True, "differs": True})

    def test_module_file_as_imported_and_resolved(self):
        # As in the shell: the Python output's site-packages/klayout is a link
        # into the KLayout output. The record keeps the imported path, which
        # names the output the import went through, and where it resolves.
        target = os.path.join(self.tmp, "klayout-out", "lib", "pymod", "klayout")
        os.makedirs(os.path.join(target, "db"))
        open(os.path.join(target, "db", "__init__.py"), "w").close()
        site = os.path.join(self.tmp, "klayout-python", "site-packages")
        os.makedirs(site)
        os.symlink(target, os.path.join(site, "klayout"))
        imported = os.path.join(site, "klayout", "db", "__init__.py")
        self.fake_module("0.30.6", path=imported)
        record, problems = self.tools(self.env())
        self.assertEqual(problems, [])
        self.assertEqual(record["module"]["file"], imported)
        self.assertEqual(record["module"]["realpath"],
                         os.path.join(os.path.realpath(target), "db", "__init__.py"))
        self.assertNotEqual(record["module"]["file"], record["module"]["realpath"])

    def test_no_declared_klayout(self):
        _, problems = self.tools(self.env(ADK_NIX_KLAYOUT=None))
        self.assertIn("ADK_NIX_KLAYOUT is not set", problems)

    def test_no_klayout_on_path(self):
        _, problems = self.tools(self.env(PATH=self.tmp))
        self.assertIn("no klayout on PATH", problems)

    def test_another_klayout_first_on_path(self):
        other = os.path.join(self.tmp, "host-bin")
        os.mkdir(other)
        script(other, "klayout", "echo KLayout 0.30.6")
        env = self.env()
        _, problems = self.tools(dict(env, PATH=other + os.pathsep + env["PATH"]))
        self.assertTrue(any(p.startswith("klayout on PATH is") for p in problems), problems)
        self.assertTrue(any("outside the realised closure" in p for p in problems), problems)

    def test_klayout_outside_the_closure(self):
        _, problems = self.tools(self.env(), closure=[])
        self.assertTrue(any("outside the realised closure" in p for p in problems), problems)

    def test_klayout_that_does_not_answer(self):
        script(os.path.join(self.declared, "bin"), "klayout", "echo KLayout 0.30.6; exit 1")
        _, problems = self.tools(self.env())
        self.assertIn("klayout -b -v did not answer", problems)

    def test_binary_and_module_disagree(self):
        self.fake_module("0.30.5")
        _, problems = self.tools(self.env())
        self.assertIn("klayout -b -v says 'KLayout 0.30.6', klayout.db is 0.30.5", problems)

    def test_module_that_does_not_import(self):
        sys.modules["klayout"] = None  # makes `import klayout` raise ModuleNotFoundError
        record, _ = self.tools(self.env())
        self.assertTrue(record["module"]["import"].startswith("failed: ModuleNotFoundError"),
                        record["module"])
        self.assertIsNone(record["skew"]["differs"])

    def test_no_image_version(self):
        record, problems = self.tools(self.env(ADK_NIX_KLAYOUT_IMAGE_VERSION=None))
        self.assertIn("ADK_NIX_KLAYOUT_IMAGE_VERSION is not set; the skew cannot be stated", problems)
        self.assertIsNone(record["skew"]["differs"])

    def test_same_version_is_no_skew(self):
        record, problems = self.tools(self.env(ADK_NIX_KLAYOUT_IMAGE_VERSION="0.30.6"))
        self.assertEqual(problems, [])
        self.assertFalse(record["skew"]["differs"])

    def test_padmap_interpreter_not_the_venv(self):
        for value in (None, "/usr/bin/python3"):
            _, problems = self.tools(self.env(CHIPLET_FLOW_PADMAP_PYTHON=value))
            self.assertTrue(any(p.startswith("CHIPLET_FLOW_PADMAP_PYTHON is") for p in problems),
                            value)

    def test_steering_variables(self):
        for name in capture_context.KLAYOUT_STEERING:
            _, problems = self.tools(self.env(**{name: ""}))
            self.assertEqual(problems, [f"{name} is set in the shell; it steers KLayout"])

    def test_steering_is_what_the_hook_scrubs_for_klayout(self):
        # The record re-checks every variable the hook clears for KLayout and
        # does not set again; a name added to one list and not the other fails.
        with open(os.path.join(HERE, "cli-env.sh")) as handle:
            text = handle.read()
        scrub = text.split('ADK_NIX_SCRUB="', 1)[1].split('"', 1)[0].split()
        for_klayout = {v for v in scrub if v.startswith("KLAYOUT_")
                       or v in ("LD_LIBRARY_PATH", "RUBYOPT", "RUBYLIB")}
        self.assertEqual(for_klayout - {"KLAYOUT_HOME", "KLAYOUT_PATH"},
                         set(capture_context.KLAYOUT_STEERING))


class Z3(unittest.TestCase):
    """z3_solver: the library's own version against its z3-solver metadata. A
    stand-in z3 module is put in sys.modules and the metadata is passed in."""

    def setUp(self):
        saved = sys.modules.get("z3")
        self.addCleanup(lambda: sys.modules.__setitem__("z3", saved) if saved is not None
                        else sys.modules.pop("z3", None))
        z3 = types.ModuleType("z3")
        z3.get_version_string = lambda: "4.15.4"
        sys.modules["z3"] = z3

    def solver(self, metadata):
        def version_of(name):
            self.assertEqual(name, "z3-solver")
            if metadata is None:
                raise capture_context.importlib.metadata.PackageNotFoundError(name)
            return metadata
        return capture_context.z3_solver(version_of)

    def test_library_and_metadata_agree(self):
        self.assertEqual(self.solver("4.15.4"), ({"library": "4.15.4", "distribution": "4.15.4"}, []))

    def test_metadata_that_claims_another_version(self):
        record, problems = self.solver("4.15.8")
        self.assertEqual(record["distribution"], "4.15.8")
        self.assertEqual(problems, ["z3 reports 4.15.4, its z3-solver metadata 4.15.8"])

    def test_no_metadata(self):
        self.assertEqual(self.solver(None)[1], ["z3 reports 4.15.4, its z3-solver metadata is missing"])

    def test_a_failed_import_is_recorded_not_reported_twice(self):
        # Reported once, by name, because z3 is on the imported list.
        self.assertIn("z3", capture_context.THIRD_PARTY)
        sys.modules["z3"] = None  # import z3 raises ImportError
        record, problems = self.solver("4.15.4")
        self.assertTrue(record["import"].startswith("failed: "))
        self.assertEqual(problems, [])

    def test_a_library_that_does_not_report_its_version(self):
        def broken():
            raise OSError("libz3.so: cannot open shared object file")
        sys.modules["z3"].get_version_string = broken
        record, problems = self.solver("4.15.4")
        self.assertTrue(record["library"].startswith("failed: OSError"))
        self.assertEqual(problems, ["z3 imported but did not report its version: "
                                    "OSError: libz3.so: cannot open shared object file"])


class AssemblyLvs(unittest.TestCase):
    """assembly_lvs: the command lvs-verify runs must be the venv's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.venv = os.path.join(self.tmp, "venv")
        self.host = os.path.join(self.tmp, "host")
        os.makedirs(os.path.join(self.venv, "bin"))
        os.makedirs(self.host)
        script(os.path.join(self.venv, "bin"), "assembly-lvs", "exit 0")
        script(self.host, "assembly-lvs", "exit 0")

    def check(self, *path, **extra):
        env = dict({"PATH": os.pathsep.join(path), "ADK_NIX_VENV": self.venv}, **extra)
        return capture_context.assembly_lvs(env)

    def test_the_venvs_first(self):
        record, problems = self.check(os.path.join(self.venv, "bin"), self.host)
        self.assertEqual(problems, [])
        self.assertEqual(record["path"], os.path.join(self.venv, "bin", "assembly-lvs"))

    def test_another_first(self):
        _, problems = self.check(self.host, os.path.join(self.venv, "bin"))
        self.assertEqual(problems, [f"assembly-lvs on PATH is {self.host}/assembly-lvs, "
                                    f"not this venv's {self.venv}/bin/assembly-lvs"])

    def test_none_on_path(self):
        self.assertEqual(self.check(self.tmp)[1], ["no assembly-lvs on PATH"])

    def test_the_variable_lvs_verify_reads_first(self):
        _, problems = self.check(os.path.join(self.venv, "bin"), CHIPLET_FLOW_ASSEMBLY_LVS="/x")
        self.assertEqual(problems, ["CHIPLET_FLOW_ASSEMBLY_LVS is set in the shell (/x); lvs-verify would run it"])


class Trees(unittest.TestCase):
    def test_every_tree_the_hook_installs_is_checked(self):
        # A tree added to ADK_NIX_EDITABLE and not here would be installed and
        # never held to its checkout; the reverse would refuse every entry.
        with open(os.path.join(HERE, "cli-env.sh")) as handle:
            text = handle.read()
        block = text.split('ADK_NIX_EDITABLE="', 1)[1].split('"', 1)[0]
        names = [line.split("|")[0] for line in block.split() if line]
        self.assertEqual(set(names), set(capture_context.TREE_MODULES))
        self.assertEqual(set(names), set(capture_context.TREE_DISTS))
        # One module and one distribution per row: per directory pip installs.
        for name in set(names):
            self.assertEqual(len(capture_context.TREE_MODULES[name]), names.count(name), name)
            self.assertEqual(len(capture_context.TREE_DISTS[name]), names.count(name), name)
        self.assertEqual(sorted(capture_context.OURS),
                         sorted(m for mods in capture_context.TREE_MODULES.values() for m in mods))

    def test_a_tree_installed_from_two_directories_is_one_tree(self):
        text = "chiplet-spec|/s/spec|reference/python\nchiplet-spec|/s/spec|description\n"
        self.assertEqual(capture_context.declared_trees(text), {"chiplet-spec": "/s/spec"})

    def test_one_name_declared_as_two_trees_is_refused(self):
        text = "chiplet-spec|/s/spec|reference/python\nchiplet-spec|/s/other|description\n"
        with self.assertRaisesRegex(ValueError, "chiplet-spec is declared as both /s/spec and /s/other"):
            capture_context.declared_trees(text)


class Module(unittest.TestCase):
    # A module whose __version__ warns with `category` when read; returns the
    # record and the warnings that reached the caller.
    def read_warning_version(self, category):
        mod = types.ModuleType("adk_nix_warning_version")
        mod.__file__ = "/nix/store/x/adk_nix_warning_version.py"

        def attribute(name):
            if name == "__version__":
                warnings.warn("use importlib.metadata", category, stacklevel=2)
                return "4.25.0"
            raise AttributeError(name)

        mod.__getattr__ = attribute
        sys.modules[mod.__name__] = mod
        try:
            with warnings.catch_warnings(record=True) as seen:
                warnings.simplefilter("always")
                record = capture_context.module(mod.__name__, {})
        finally:
            del sys.modules[mod.__name__]
        return record, [(w.category, str(w.message)) for w in seen]

    def test_a_deprecated_version_attribute_is_read_without_a_warning(self):
        record, seen = self.read_warning_version(DeprecationWarning)
        self.assertEqual(record["__version__"], "4.25.0")
        self.assertEqual(seen, [])

    def test_any_other_warning_from_the_version_attribute_still_surfaces(self):
        # Only DeprecationWarning is silenced. FutureWarning is what a package
        # uses to tell its users, so it must not be swallowed with the noise.
        record, seen = self.read_warning_version(FutureWarning)
        self.assertEqual(record["__version__"], "4.25.0")
        self.assertEqual(seen, [(FutureWarning, "use importlib.metadata")])


class ParentShell(unittest.TestCase):
    def test_the_shell_that_started_the_script_is_recorded(self):
        # "; true" keeps bash from replacing itself with the last command, as
        # the hook's bash is not replaced by the capture it starts.
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import capture_context; "
                "print(json.dumps(capture_context.parent_shell()))")
        done = subprocess.run(["bash", "-c", '"$0" -B -c "$1" "$2"; true', sys.executable, code, HERE],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout),
                         {"realpath": os.path.realpath(shutil.which("bash"))})


class Record(unittest.TestCase):
    def test_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "nix-context.json")
            with open(out, "w") as handle:
                handle.write("{}")
            self.assertEqual(capture_context.main(["capture_context.py", out]), 1)
            with open(out) as handle:
                self.assertEqual(handle.read(), "{}")


if __name__ == "__main__":
    unittest.main()
