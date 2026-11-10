"""bin/flow, run against the docker stub tests/run_sh uses.

bin/flow picks the release lock and the container's working directory and
hands one packaging-flow or chiplet-system-description call to run.sh --batch, so these
cases cover only what it adds: the program allowlist, where the lock comes
from, the image it names, /work as run.sh's ADK_WORK and the -w below it, the
refusals, the caps it defaults lower than run.sh's, that the argv, the exit
code and an empty stdin reach the container, and that verify-sign and
verify-accept alone take a terminal. run.sh's own checks are tested in
tests/run_sh. The last case holds .dockerignore to
keeping bin/flow out of the image's build context.
"""

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "run_sh"))
import test_run_sh as rs  # noqa: E402

FLOW = ROOT / "bin" / "flow"
OTHER_ID = "sha256:" + "f" * 64
# packaging-flow in the stub's container: its argv, then what its stdin held.
PACKAGING_FLOW = """#!/bin/sh
printf 'argv:'; printf ' [%s]' "$@"; echo
printf 'stdin:'; cat; echo
exit "${FLOW_TEST_EXIT:-0}"
"""


class FlowTests(rs.RunShEnv, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.work.mkdir()
        self.lock = self.base.resolve() / "locks" / "release-lock.json"
        self.lock.parent.mkdir()
        self.lock.write_text(rs.lock_text())
        self.env["RUN_SH_TEST_LABEL_PIN_SET"] = rs.LOCK_PIN_SET
        for name in ("ADK_FLOW_LOCK", "FLOW_TEST_EXIT"):
            self.env.pop(name, None)
        stub_dir = self.base / "stub bin"
        (stub_dir / "packaging-flow").write_text(PACKAGING_FLOW)
        (stub_dir / "packaging-flow").chmod(0o755)

    def flow(self, *args, stdin=subprocess.DEVNULL, cwd=None, **env):
        return subprocess.run(
            [str(FLOW), *args], env=dict(self.env, **env), cwd=cwd or self.work,
            stdin=stdin, capture_output=True, text=True, timeout=5, check=False)

    def assert_refused(self, result, line):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, line + "\n")
        self.assertEqual(self.calls(), [])

    def test_one_batch_call_of_packaging_flow(self):
        result = self.flow("--lock", str(self.lock), "--", "packaging-flow", "verify",
                           "runs/a b")
        argv = self.run_argv(result)
        at = argv.index(rs.IMAGE_ID)
        self.assertEqual(argv[at:], [rs.IMAGE_ID, "packaging-flow", "verify", "runs/a b"])
        self.assertEqual(argv[:2], ["run", "--rm"])
        self.assertEqual(argv[at - 2:at], ["-w", "/work"])
        self.assertNotIn("-i", argv)
        self.assertNotIn("-t", argv)
        self.assertIn(("-v", "%s:/work" % self.work), rs.pairs(argv))
        self.assertIn("run.sh: no display: --batch", result.stderr.splitlines())

    def test_a_display_and_a_terminal_are_not_taken(self):
        socket_path = self.x11 / "X7"
        sock = rs.socket.socket(rs.socket.AF_UNIX)
        sock.bind(str(socket_path))
        self.addCleanup(sock.close)
        with self.terminal() as (primary, secondary):
            result = self.flow("--lock", str(self.lock), "--", "packaging-flow", "status",
                               stdin=secondary, ADK_DISPLAY=":7")
        argv = self.run_argv(result)
        self.assertNotIn("-t", argv)
        self.assertFalse([v for f, v in rs.pairs(argv) if v.startswith("DISPLAY=")])

    def test_verify_sign_and_verify_accept_alone_take_a_terminal(self):
        # With stdin and stdout both terminals; everything else stays batch.
        with self.terminal() as (primary, secondary):
            for call, tty in ((("packaging-flow", "verify-sign", "--gate", "g"), True),
                              (("packaging-flow", "verify-accept", "runs/a", "lvs"), True),
                              (("packaging-flow", "status"), False),
                              (("packaging-flow", "verify-open", "runs/a"), False),
                              (("chiplet-system-description", "verify-sign"), False)):
                with self.subTest(call=call):
                    if self.calls_file.exists():
                        self.calls_file.unlink()
                    result = subprocess.run(
                        [str(FLOW), "--lock", str(self.lock), "--", *call], env=self.env,
                        cwd=self.work, stdin=secondary, stdout=secondary,
                        stderr=subprocess.PIPE, text=True, timeout=5, check=False)
                    argv = self.run_argv(result)
                    self.assertEqual("-t" in argv, tty)
                    self.assertEqual("-i" in argv, tty)

    def dry_run_with_a_display(self, *call):
        """The dry-run command's argv with display :7 on offer."""
        socket_path = self.x11 / "X7"
        if not socket_path.exists():
            sock = rs.socket.socket(rs.socket.AF_UNIX)
            sock.bind(str(socket_path))
            self.addCleanup(sock.close)
        result = self.flow("--dry-run", "--lock", str(self.lock), "--", *call,
                           ADK_DISPLAY=":7")
        self.assertEqual(result.returncode, 0, result.stderr)
        line, = result.stdout.splitlines()
        return shlex.split(line)

    def test_verify_open_alone_takes_the_display(self):
        argv = self.dry_run_with_a_display("packaging-flow", "verify-open", "runs/a",
                                           "klayout", "")
        self.assertIn(("-e", "DISPLAY=:7"), rs.pairs(argv))
        self.assertIn(("-v", "%s:/tmp/.X11-unix/X7:ro" % (self.x11 / "X7")), rs.pairs(argv))
        self.assertNotIn("-i", argv)
        self.assertNotIn("-t", argv)

    def test_run_and_verify_sign_take_no_display(self):
        for call in (("packaging-flow", "run", "system.chiplet"),
                     ("packaging-flow", "verify-sign", "runs/a"),
                     ("chiplet-system-description", "verify-open")):
            with self.subTest(call=call):
                argv = self.dry_run_with_a_display(*call)
                self.assertFalse([v for f, v in rs.pairs(argv) if v.startswith("DISPLAY=")])
                self.assertFalse([v for f, v in rs.pairs(argv) if ".X11-unix" in v])

    def test_the_argv_exit_code_and_an_empty_stdin_reach_the_container(self):
        # "--" ends bin/flow's options, so the program's own --lock passes on.
        result = subprocess.run(
            [str(FLOW), "--lock", str(self.lock), "--", "packaging-flow", "--lock", "x"],
            env=dict(self.env, RUN_SH_TEST_EXEC="1", FLOW_TEST_EXIT="3"),
            cwd=self.work, input="not for the container\n",
            capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, "argv: [--lock] [x]\nstdin:\n")

    def test_the_system_description_is_the_other_program(self):
        result = self.flow("--lock", str(self.lock), "--", "chiplet-system-description", "check",
                           "system.chiplet", "--json")
        argv = self.run_argv(result)
        at = argv.index(rs.IMAGE_ID)
        self.assertEqual(argv[at:], [rs.IMAGE_ID, "chiplet-system-description", "check",
                                     "system.chiplet", "--json"])

    def test_no_program_is_refused_naming_the_two(self):
        for args in ([], ["--"], ["--lock", "x", "--"]):
            with self.subTest(args=args):
                self.assert_refused(self.flow(*args), "error: no program: name "
                                    "packaging-flow or chiplet-system-description after --")

    def test_another_program_is_refused_naming_the_two(self):
        for program in ("bash", "sh", "packaging-flow2", "/usr/local/bin/packaging-flow",
                        "--dry-run"):
            with self.subTest(program=program):
                self.assert_refused(
                    self.flow("--lock", str(self.lock), "--", program, "status"),
                    "error: bin/flow runs packaging-flow or chiplet-system-description, not '%s'"
                    % program)

    def workdir(self, result):
        argv = self.run_argv(result)
        at = argv.index(rs.IMAGE_ID)
        self.assertEqual(argv[at - 2], "-w")
        self.assertIn(("-v", "%s:/work" % self.work), rs.pairs(argv))
        return argv[at - 1]

    def test_a_subfolder_of_the_workspace_is_the_same_place_below_work(self):
        example = self.work / "examples" / "a b"
        example.mkdir(parents=True)
        self.assertEqual(self.workdir(self.flow(
            "--lock", str(self.lock), "--", "packaging-flow", "status", cwd=example)),
            "/work/examples/a b")

    def test_work_defaults_to_run_shs_and_never_to_the_current_directory(self):
        self.env.pop("ADK_WORK")
        self.work = self.home.resolve() / "adk-work"
        (self.work / "ex").mkdir(parents=True)
        self.assertEqual(self.workdir(self.flow(
            "--lock", str(self.lock), "--", "packaging-flow", "status", cwd=self.work / "ex")),
            "/work/ex")

    def test_a_variable_set_but_empty_is_refused_naming_its_setting(self):
        # VS Code's tasks set all three from the folder's adkTools.* settings;
        # empty means the folder was not opened by bin/adk-vscode. Unset keeps
        # the defaults (the case above, and the lock cases below).
        for name, key in (("ADK_WORK", "work"), ("ADK_TOOLS_IMAGE", "image"),
                          ("ADK_FLOW_LOCK", "lock")):
            with self.subTest(name=name):
                self.reset()
                self.work.mkdir()
                self.assert_refused(
                    self.flow("--lock", str(self.lock), "--", "packaging-flow", "status",
                              **{name: ""}),
                    "error: %s is set but empty (in VS Code, the setting adkTools.%s); "
                    "run %s/bin/adk-vscode <folder>" % (name, key, ROOT.resolve()))

    def test_an_unset_workspace_is_not_refused_as_empty(self):
        self.env.pop("ADK_WORK")
        self.work = self.home.resolve() / "adk-work"
        self.work.mkdir()
        result = self.flow("--lock", str(self.lock), "--", "packaging-flow", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("set but empty", result.stderr)

    def test_a_current_directory_outside_the_workspace_is_refused_naming_both(self):
        outside = self.lock.parent
        self.assert_refused(
            self.flow("--lock", str(self.lock), "--", "packaging-flow", "status", cwd=outside),
            "error: the current directory (%s) is outside the workspace (%s), which /work "
            "always is; run bin/flow from inside it, or set ADK_WORK" % (outside, self.work))

    def test_symlinks_resolve_as_run_sh_resolves_the_workspace(self):
        """A link into the workspace runs where it points; a link out of it is
        outside; ADK_WORK through a link binds the physical directory."""
        (self.work / "sub").mkdir()
        into = self.base.resolve() / "into"
        os.symlink(self.work / "sub", into)
        self.assertEqual(self.workdir(self.flow(
            "--lock", str(self.lock), "--", "packaging-flow", "status", cwd=into)), "/work/sub")
        os.symlink(self.lock.parent, self.work / "out")
        self.calls_file.unlink()
        self.assert_refused(
            self.flow("--lock", str(self.lock), "--", "packaging-flow", "status",
                      cwd=self.work / "out"),
            "error: the current directory (%s) is outside the workspace (%s), which /work "
            "always is; run bin/flow from inside it, or set ADK_WORK"
            % (self.lock.parent, self.work))
        link = self.base.resolve() / "work link"
        os.symlink(self.work, link)
        self.assertEqual(self.workdir(self.flow(
            "--lock", str(self.lock), "--", "packaging-flow", "status",
            cwd=self.work / "sub", ADK_WORK=str(link))), "/work/sub")

    def test_the_lock_comes_from_the_flag_the_variable_or_the_current_directory(self):
        other = self.base.resolve() / "other.json"
        other.write_text(rs.lock_text(image=OTHER_ID))
        example = self.work / "example"
        example.mkdir()
        (self.work / "release-lock.json").write_text(rs.lock_text(image=OTHER_ID))
        in_work = example / "release-lock.json"
        in_work.write_text(rs.lock_text(image=OTHER_ID))
        for name, args, env, lock in (
                ("flag over variable", ["--lock", str(self.lock)],
                 {"ADK_FLOW_LOCK": str(other)}, self.lock),
                ("variable over the folder", [], {"ADK_FLOW_LOCK": str(self.lock)},
                 self.lock),
                ("the folder alone", [], {}, in_work)):
            with self.subTest(name):
                if lock == in_work:
                    in_work.write_text(rs.lock_text())
                if self.calls_file.exists():
                    self.calls_file.unlink()
                result = self.flow(*args, "--", "packaging-flow", "status", cwd=example,
                                   **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("run.sh: the image held to %s, no examples mounted" % lock,
                              result.stderr.splitlines())

    def test_no_lock_anywhere_is_refused_naming_the_three(self):
        self.assert_refused(self.flow("--", "packaging-flow", "status"), (
            "error: no release lock: pass --lock FILE, set ADK_FLOW_LOCK, or put "
            "release-lock.json in the current directory (%s)" % self.work))

    def test_a_lock_that_is_not_there_is_refused(self):
        missing = self.base / "missing.json"
        self.assert_refused(self.flow("--", "packaging-flow", "status",
                                      ADK_FLOW_LOCK=str(missing)),
                            "error: no release lock at %s" % missing)

    def test_a_workspace_that_is_not_there_is_refused_and_not_made(self):
        gone = self.base.resolve() / "no such workspace"
        self.assert_refused(self.flow("--lock", str(self.lock), "--", "packaging-flow",
                                      "status", ADK_WORK=str(gone)),
                            "error: the current directory (%s) is outside the workspace (%s), "
                            "which /work always is; run bin/flow from inside it, or set "
                            "ADK_WORK" % (self.work, gone))
        self.assertFalse(gone.exists())

    def test_a_lock_that_names_no_image_is_refused(self):
        self.lock.write_text('{"schema": "adk-examples.release-lock/1"}\n')
        self.assert_refused(self.flow("--lock", str(self.lock), "--", "packaging-flow", "status"),
                            "error: the release lock %s names no image "
                            "(sha256:<64 hex>)" % self.lock)

    def test_no_image_on_the_daemon_is_run_shs_refusal(self):
        result = self.flow("--lock", str(self.lock), "--", "packaging-flow", "status",
                           RUN_SH_TEST_MISSING=rs.IMAGE_ID)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.splitlines()[0],
                         "error: no image %s on this docker daemon" % rs.IMAGE_ID)
        self.assertNotIn("run", [c[0] for c in self.calls()])

    def test_a_schema_2_lock_on_the_containerd_store_runs_by_its_manifest_digest(self):
        self.lock.write_text(rs.lock_text(image=OTHER_ID, image_manifest=rs.IMAGE_ID,
                                          schema="adk-examples.release-lock/2"))
        argv = self.run_argv(self.flow("--lock", str(self.lock), "--", "packaging-flow",
                                       "status", RUN_SH_TEST_MISSING=OTHER_ID))
        self.assertIn(rs.IMAGE_ID, argv)

    def test_a_dry_run_names_the_digest_a_run_would_take(self):
        self.lock.write_text(rs.lock_text(image=OTHER_ID, image_manifest=rs.IMAGE_ID,
                                          schema="adk-examples.release-lock/2"))
        result = self.flow("--dry-run", "--lock", str(self.lock), "--", "packaging-flow",
                           "status", RUN_SH_TEST_MISSING=OTHER_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" %s packaging-flow status" % rs.IMAGE_ID, result.stdout)
        self.assertNotIn("run", [c[0] for c in self.calls()])

    def test_a_schema_2_lock_takes_the_config_digest_first(self):
        self.lock.write_text(rs.lock_text(image=OTHER_ID, image_manifest=rs.IMAGE_ID,
                                          schema="adk-examples.release-lock/2"))
        argv = self.run_argv(self.flow("--lock", str(self.lock), "--", "packaging-flow",
                                       "status"))
        self.assertIn(OTHER_ID, argv)

    def test_an_image_that_is_not_the_locks_runs_with_a_note(self):
        note = ("run.sh: the image %s is not the reference image %s the release lock %s names; "
                "it runs held to the lock's pin set\n" % (OTHER_ID, rs.IMAGE_ID, self.lock))
        for args, env in ((("--image", OTHER_ID), {}),
                          ((), {"ADK_TOOLS_IMAGE": OTHER_ID}),
                          (("--image", OTHER_ID), {"ADK_TOOLS_IMAGE": rs.IMAGE_ID})):
            with self.subTest(args=args, env=env):
                self.reset()
                self.work.mkdir()
                result = self.flow("--lock", str(self.lock), *args, "--", "packaging-flow",
                                   "status", **env)
                self.assertIn(OTHER_ID, self.run_argv(result))
                self.assertEqual(result.stderr.count(note), 1, result.stderr)

    def test_an_image_without_a_value_is_refused(self):
        self.assert_refused(self.flow("--image"), "error: --image needs a value")

    def test_dry_run_prints_the_command_and_runs_nothing(self):
        (self.work / "sub").mkdir()
        result = self.flow("--dry-run", "--lock", str(self.lock), "--", "packaging-flow",
                           "verify", "runs/a b", cwd=self.work / "sub")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("run", [c[0] for c in self.calls()])
        line, = result.stdout.splitlines()
        self.assertTrue(line.startswith("docker run --rm "), line)
        self.assertTrue(line.endswith(r" -w /work/sub %s packaging-flow verify runs/a\ b"
                                      % rs.IMAGE_ID), line)

    def test_the_caps_default_to_2_and_5g_unless_set(self):
        for env, cpus, mem in (({}, "2", "5g"), ({"ADK_CPUS": "", "ADK_MEM_LIMIT": ""}, "2", "5g"),
                               ({"ADK_CPUS": "3"}, "3", "5g"), ({"ADK_MEM_LIMIT": "7g"}, "2", "7g"),
                               ({"ADK_CPUS": "3", "ADK_MEM_LIMIT": "7g"}, "3", "7g")):
            with self.subTest(env=env):
                result = self.flow("--dry-run", "--lock", str(self.lock), "--",
                                   "packaging-flow", "run", **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = shlex.split(result.stdout)
                caps = {flag: argv[argv.index(flag) + 1]
                        for flag in ("--cpus", "--memory", "--memory-swap")}
                self.assertEqual(caps, {"--cpus": cpus, "--memory": mem, "--memory-swap": mem})


class DockerContext(unittest.TestCase):
    def test_dockerignore_keeps_the_host_launchers_out(self):
        """Last matching pattern wins, a pattern matches a path or a parent of
        it, and ** spans directories, as docker reads .dockerignore."""
        for launcher in ("bin/flow", "bin/adk-vscode"):
            excluded = False
            for line in (ROOT / ".dockerignore").read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                keep = line.startswith("!")
                pattern = line.lstrip("!").strip("/")
                rule = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") \
                    .replace(r"\?", "[^/]")
                if any(re.fullmatch(rule, path) for path in ("bin", launcher)):
                    excluded = not keep
            self.assertTrue(excluded, "%s would be in the build context, so in the image"
                            % launcher)


if __name__ == "__main__":
    unittest.main()
