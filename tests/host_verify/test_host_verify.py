"""host-verify.sh against a stub docker: no daemon, no image, no slot.

Run: python3 -m unittest discover -s tests/host_verify -v   (from the adk-tools root)

The no-launch cases are the point. Each one asserts that the stub saw no `run` call and that nothing
was written where the launcher must not write. The launch cases pin the exact docker command line
and check that the command's exit code is passed through only when the container confirms it.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parents[1] / "host-verify.sh"
STUB_DIR = HERE / "stub"
IMG = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
USAGE, NOT_EXECUTED, INTERRUPTED, SOFTWARE, RECORD_REFUSED, TIMEOUT = 64, 66, 69, 70, 73, 75
VERIFY = ["python3", "-B", "-m", "packaging_flow.cli", "verify", "/work/pkg/outputs/run/manifest.json"]
CT_FORMAT = "{{.State.Status}}|{{.State.StartedAt}}|{{.State.ExitCode}}|{{.State.OOMKilled}}"
SCHEMA = HERE.parents[1] / "formats" / "adk-tools-host-verify-record.v1.schema.json"
_loader = SourceFileLoader("check_format", str(HERE.parent / "formats" / "check_format.py"))
check_format = module_from_spec(spec_from_loader("check_format", _loader))
_loader.exec_module(check_format)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "stub-state"
        self.state.mkdir()
        self.pkg = self.tmp / "pkg"
        (self.pkg / "outputs").mkdir(parents=True)
        (self.pkg / "dies").mkdir()
        self.keep = self.tmp / "keep"
        (self.keep / "run").mkdir(parents=True)
        (self.keep / "run" / "manifest.json").write_text("{}\n")
        self.out = self.tmp / "records"
        self.out.mkdir()
        self.env = {
            "PATH": f"{STUB_DIR}:{os.environ['PATH']}",
            "STUB_STATE": str(self.state),
            "STUB_INSPECT_ID": IMG,
            "STUB_INSPECT_REF": IMG,
        }

    def tearDown(self) -> None:
        for p in [self.tmp, *self.tmp.rglob("*")]:
            if p.is_dir() and not p.is_symlink():
                p.chmod(p.stat().st_mode | stat.S_IRWXU)
        self._tmp.cleanup()

    def argv(self, *args: str, record: Path | None = None, image: str = IMG) -> tuple[list[str], Path]:
        rec = record if record is not None else self.out / "r1"
        return [str(LAUNCHER), "--image", image, "--record", str(rec), *args], rec

    def launch(self, *args: str, record: Path | None = None, image: str = IMG, **stub: str):
        argv, rec = self.argv(*args, record=record, image=image)
        env = {**self.env, **stub}
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60), rec

    def standard(self, cmd: list[str] = VERIFY) -> list[str]:
        return ["--mount", f"{self.pkg}:/work/pkg", "--mount", f"{self.keep}:/work/pkg/outputs",
                "--env", "CHIPLET_FLOW_THREAD_CAP=2", "--", *cmd]

    def calls(self, verb: str | None = None) -> list[list[str]]:
        d = self.state / "calls"
        if not d.exists():
            return []
        out = []
        for f in sorted(d.iterdir()):
            args = f.read_bytes().split(b"\0")[:-1]
            args = [a.decode() for a in args]
            if verb is None or args[0] == verb:
                out.append(args)
        return out

    def assert_no_launch(self, result, code: int) -> None:
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(self.calls("run"), [], "a container was started")

    def record(self, rec: Path) -> dict:
        """record.json, checked against its schema in formats/ before any test reads it."""
        findings = check_format.check_file(SCHEMA, rec / "record.json")
        self.assertEqual(findings, [], f"{rec / 'record.json'} does not conform to {SCHEMA.name}")
        return json.loads((rec / "record.json").read_text())


class Usage(Base):
    """Refused before anything is written."""

    def check(self, *args: str, image: str = IMG) -> None:
        r, rec = self.launch(*args, image=image)
        self.assert_no_launch(r, USAGE)
        self.assertFalse(rec.exists())
        self.assertEqual(self.calls(), [], "docker was called")

    def test_a_tag_is_not_an_image_id(self):
        self.check(*self.standard(), image="adk-tools:feature-abc1234")

    def test_a_short_id_is_not_an_image_id(self):
        self.check(*self.standard(), image=IMG[:19])

    def test_no_command(self):
        self.check("--mount", f"{self.pkg}:/work/pkg", "--")

    def test_relative_or_unnormalized_target(self):
        for target in ("work/pkg", "/work/pkg/", "/work/../pkg", "/work//pkg", "/"):
            with self.subTest(target=target):
                self.check("--mount", f"{self.pkg}:{target}", "--", "true")

    def test_a_target_mapped_twice(self):
        self.check("--mount", f"{self.pkg}:/w", "--mount", f"{self.keep}:/w", "--", "true")

    def test_a_comma_cannot_reach_the_mount_option(self):
        odd = self.tmp / "a,b"
        odd.mkdir()
        self.check("--mount", f"{odd}:/w", "--", "true")

    def test_a_comma_reached_through_a_symlink(self):
        odd = self.tmp / "a,b"
        odd.mkdir()
        (self.tmp / "plain").symlink_to(odd)
        self.check("--mount", f"{self.tmp / 'plain'}:/w", "--", "true")

    def test_a_newline_in_the_literal_source(self):
        self.check("--mount", f"{self.pkg}\n:/w", "--", "true")

    def test_a_newline_in_the_resolved_source(self):
        odd = self.tmp / "pkg\n"
        odd.mkdir()
        (self.tmp / "lnk").symlink_to(odd)
        self.check("--mount", f"{self.tmp / 'lnk'}:/w", "--", "true")

    def test_a_newline_in_the_record_path(self):
        (self.out / "sub\n").mkdir()
        r, _ = self.launch(*self.standard(), record=self.out / "sub\n" / "r1")
        self.assert_no_launch(r, USAGE)
        self.assertEqual(list((self.out / "sub\n").iterdir()), [])

    def test_a_bad_env_name(self):
        self.check("--env", "1BAD=x", "--", "true")

    def test_bad_limits(self):
        for flag, value in (("--cpus", "two"), ("--cpus", "0"), ("--cpus", "0.0"), ("--cpus", "1000"),
                            ("--cpus", "1" * 400), ("--cpus", "1.0001"),
                            ("--memory", "0"), ("--memory", "0g"), ("--memory", "6"), ("--memory", "06g"),
                            ("--memory", "9999999999g"), ("--timeout", "0"), ("--timeout", "1.5")):
            with self.subTest(flag=flag, value=value):
                self.check(flag, value, *self.standard())


class RecordDirectory(Base):
    """Refused with exit 73 and nothing written."""

    def check(self, r, *untouched: Path) -> None:
        self.assert_no_launch(r, RECORD_REFUSED)
        self.assertEqual(self.calls(), [], "docker was called")

    def test_an_existing_empty_record_directory(self):
        rec = self.out / "r1"
        rec.mkdir()
        r, _ = self.launch(*self.standard(), record=rec)
        self.check(r)
        self.assertEqual(list(rec.iterdir()), [])

    def test_an_existing_record_is_not_touched(self):
        rec = self.out / "r1"
        rec.mkdir()
        (rec / "record.json").write_text('{"old": true}')
        r, _ = self.launch(*self.standard(), record=rec)
        self.check(r)
        self.assertEqual((rec / "record.json").read_text(), '{"old": true}')

    def test_a_trailing_slash_does_not_hide_an_existing_directory(self):
        rec = self.out / "r1"
        rec.mkdir()
        r, _ = self.launch(*self.standard(), record=Path(str(rec) + "//"))
        self.check(r)

    def test_a_dangling_symlink_is_not_followed(self):
        target = self.tmp / "elsewhere"
        rec = self.out / "r1"
        rec.symlink_to(target)
        r, _ = self.launch(*self.standard(), record=rec)
        self.check(r)
        self.assertFalse(target.exists())

    def test_a_record_parent_that_does_not_exist(self):
        r, _ = self.launch(*self.standard(), record=self.tmp / "absent" / "r1")
        self.check(r)
        self.assertFalse((self.tmp / "absent").exists())

    def test_a_record_parent_that_is_not_writable(self):
        self.out.chmod(0o555)
        r, rec = self.launch(*self.standard())
        self.check(r)
        self.assertFalse(rec.exists())

    def test_a_record_inside_an_input(self):
        r, rec = self.launch(*self.standard(), record=self.keep / "run" / "hv")
        self.check(r)
        self.assertFalse(rec.exists())

    def test_a_record_reached_through_a_symlink_into_an_input(self):
        link = self.tmp / "sneaky"
        link.symlink_to(self.keep)
        r, _ = self.launch(*self.standard(), record=link / "hv")
        self.check(r)
        self.assertFalse((self.keep / "hv").exists())

    def test_an_input_inside_the_record_directory(self):
        rec = self.out / "r1"
        r, _ = self.launch("--mount", f"{rec / 'stdout.txt'}:/in", "--", "true", record=rec)
        self.check(r)
        self.assertFalse(rec.exists())

    def test_a_dangling_input_that_points_into_the_record_directory(self):
        rec = self.out / "r1"
        (self.tmp / "dangling").symlink_to(rec / "argv.txt")
        r, _ = self.launch("--mount", f"{self.tmp / 'dangling'}:/in", "--", "true", record=rec)
        self.check(r)
        self.assertFalse(rec.exists())

    def test_a_missing_input_that_passes_through_the_record_directory(self):
        rec = self.out / "r1"
        (self.tmp / "through").symlink_to("records/r1/../../keep")
        for src in (f"{rec}/..", str(self.tmp / "through")):
            with self.subTest(src=src):
                r, _ = self.launch("--mount", f"{src}:/in", "--", "true", record=rec)
                self.check(r)
                self.assertIn("only resolves once", r.stderr)
                self.assertFalse(rec.exists())
                self.assertEqual(sorted(p.name for p in self.out.iterdir()), [])

    def test_a_record_parent_that_resolves_to_a_newline(self):
        (self.out / "sub\n").mkdir()
        (self.out / "s").symlink_to(self.out / "sub\n")
        r, _ = self.launch(*self.standard(), record=self.out / "s" / "r1")
        self.check(r)
        self.assertEqual(list((self.out / "sub\n").iterdir()), [])
        self.assertFalse((self.out / "sub").exists())

    def test_a_record_inside_the_root_input(self):
        r, rec = self.launch("--mount", "/:/host", "--", "true")
        self.check(r)
        self.assertFalse(rec.exists())


class NotExecuted(Base):
    """The context does not match: exit 66, recorded, no container."""

    def check(self, r, rec: Path, fragment: str) -> dict:
        self.assert_no_launch(r, NOT_EXECUTED)
        doc = self.record(rec)
        self.assertFalse(doc["executed"])
        self.assertEqual(doc["launcher_exit"], NOT_EXECUTED)
        self.assertIn(fragment, doc["not_executed_reason"])
        self.assertIsNone(doc["command_exit_code"])
        self.assertEqual(doc["docker_argv"], [])
        self.assertEqual((rec / "argv.txt").read_text(), "")
        return doc

    def test_an_image_the_daemon_does_not_have(self):
        r, rec = self.launch(*self.standard(), STUB_INSPECT_ID="")
        self.check(r, rec, "not present")

    def test_the_image_is_inspected_by_its_exact_id(self):
        self.launch(*self.standard())
        self.assertEqual(self.calls("image"), [["image", "inspect", "--format", "{{.Id}}", IMG]])

    def test_an_image_the_daemon_resolves_elsewhere(self):
        r, rec = self.launch(*self.standard(), STUB_INSPECT_ID=OTHER)
        doc = self.check(r, rec, "not to that exact ID")
        self.assertEqual(doc["image_id_inspected"], OTHER)

    def test_a_missing_input(self):
        r, rec = self.launch("--mount", f"{self.tmp / 'gone'}:/work/gone", "--", "true")
        self.check(r, rec, "does not exist")
        self.assertFalse((self.tmp / "gone").exists(), "the launcher created the missing input")

    def test_a_literal_path_that_only_resolves_lexically(self):
        for src in (f"{self.tmp}/absent/../pkg", f"{self.keep}/run/manifest.json/../../run", f"{self.keep}/run/manifest.json/"):
            with self.subTest(src=src):
                r, rec = self.launch("--mount", f"{src}:/in", "--", "true", record=self.out / str(abs(hash(src))))
                self.check(r, rec, "does not resolve")

    def test_a_missing_source_is_recorded_as_written(self):
        src = f"{self.tmp}/typo/.."
        r, rec = self.launch("--mount", f"{src}:/in", "--", "true")
        doc = self.check(r, rec, "does not resolve")
        self.assertEqual(doc["mounts"][0]["source"], src)
        argv, rec = self.argv("--mount", "typo/..:/in", "--", "true", record=self.out / "r2")
        r = subprocess.run(argv, env=self.env, cwd=self.tmp, capture_output=True, text=True, timeout=60)
        doc = self.check(r, rec, "does not resolve")
        self.assertEqual(doc["mounts"][0]["source"], f"{self.tmp}/typo/..")

    def test_no_scratch_is_left_behind(self):
        r, rec = self.launch("--mount", f"{self.tmp / 'gone'}:/in", "--", "true")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["r1"])
        self.assertEqual(sorted(p.name for p in rec.iterdir()),
                         ["argv.txt", "record.json", "stderr.txt", "stdout.txt"])

    def test_an_unreadable_input(self):
        (self.pkg / "dies").chmod(0)
        r, rec = self.launch("--mount", f"{self.pkg / 'dies'}:/work/dies", "--", "true")
        self.check(r, rec, "not readable")

    def test_a_nested_mountpoint_missing_from_its_read_only_parent(self):
        (self.pkg / "outputs").rmdir()
        r, rec = self.launch(*self.standard())
        self.check(r, rec, "mountpoint")
        self.assertFalse((self.pkg / "outputs").exists(), "the launcher created the mountpoint")

    def test_a_nested_mountpoint_of_the_wrong_kind(self):
        f = self.tmp / "one.json"
        f.write_text("{}")
        r, rec = self.launch("--mount", f"{self.pkg}:/work/pkg", "--mount", f"{f}:/work/pkg/dies",
                             "--", "true")
        self.check(r, rec, "is a directory")

    def test_the_innermost_enclosing_mount_owns_the_mountpoint(self):
        # /work/pkg/outputs/run lives in keep (mounted at /work/pkg/outputs), not in pkg.
        extra = self.tmp / "extra"
        extra.mkdir()
        r, rec = self.launch("--mount", f"{self.pkg}:/work/pkg", "--mount", f"{self.keep}:/work/pkg/outputs",
                             "--mount", f"{extra}:/work/pkg/outputs/run", "--", "true")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_resource_floor(self):
        r, rec = self.launch("--memory", "100000g", *self.standard())
        self.check(r, rec, "10 GiB")
        r, rec = self.launch("--cpus", "999", *self.standard(), record=self.out / "r2")
        self.check(r, rec, "cores")

    def test_a_daemon_error_is_a_launcher_failure_not_a_missing_image(self):
        r, rec = self.launch(*self.standard(), STUB_INSPECT_ERR="Cannot connect to the Docker daemon")
        self.assert_no_launch(r, SOFTWARE)
        doc = self.record(rec)
        self.assertIn("Cannot connect", doc["launcher_failure"])
        self.assertIsNone(doc["not_executed_reason"])


class Launch(Base):
    def test_a_copy_without_the_execute_bit(self):
        copy = self.tmp / "hv.sh"
        copy.write_bytes(LAUNCHER.read_bytes())
        copy.chmod(0o644)
        argv, rec = self.argv(*self.standard())
        r = subprocess.run(["bash", str(copy), *argv[1:]], env={**self.env, "STUB_RC": "0"},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.record(rec)["executed"])

    def test_the_exact_docker_command_line(self):
        r, rec = self.launch(*self.standard(), STUB_RC="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        (run,) = self.calls("run")
        doc = self.record(rec)
        uid, gid = os.getuid(), os.getgid()
        self.assertEqual(run, [
            "run", "--pull=never", "--network=none", "--name", doc["container"]["name"],
            "--user", f"{uid}:{gid}", "--cpus=2", "--memory=6g", "--memory-swap=6g",
            "-e", "HOME=/tmp", "-e", "CHIPLET_FLOW_THREAD_CAP=2",
            "--mount", f"type=bind,src={self.pkg.resolve()},dst=/work/pkg,readonly,"
            "bind-propagation=rprivate,bind-recursive=readonly",
            "--mount", f"type=bind,src={self.keep.resolve()},dst=/work/pkg/outputs,readonly,"
            "bind-propagation=rprivate,bind-recursive=readonly",
            IMG, *VERIFY])
        self.assertEqual(doc["docker_argv"], ["docker", *run])
        name = doc["container"]["name"]
        self.assertEqual(self.calls("container"), [["container", "inspect", "--format", CT_FORMAT, name]])
        self.assertEqual(self.calls("rm"), [["rm", "-f", name]])
        self.assertEqual(self.calls("kill"), [])

    def test_argv_is_preserved_exactly(self):
        cmd = ["python3", "-c", "print(1)", "a b", "*", "", "--rm", "x|y", "l1\nl2", "-v"]
        r, rec = self.launch(*self.standard(cmd))
        self.assertEqual(r.returncode, 0, r.stderr)
        (run,) = self.calls("run")
        self.assertEqual(run[run.index(IMG) + 1:], cmd)
        self.assertEqual(self.record(rec)["command"], cmd)

    def test_the_command_exit_code_passes_through(self):
        for code in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "130"):
            with self.subTest(code=code):
                r, rec = self.launch(*self.standard(), record=self.out / f"rc{code}", STUB_RC=code)
                self.assertEqual(r.returncode, int(code), r.stderr)
                doc = self.record(rec)
                self.assertTrue(doc["executed"])
                self.assertEqual(doc["command_exit_code"], int(code))
                self.assertEqual(doc["launcher_exit"], int(code))
                self.assertEqual(doc["container"]["exit_code"], int(code))
                self.assertTrue(doc["container"]["removed"])
                self.assertFalse(doc["timed_out"])

    def test_streams_are_kept_replayed_and_not_interpreted(self):
        r, rec = self.launch(*self.standard(), STUB_STDOUT="verify: H1", STUB_STDERR="warn: x",
                             STUB_RC="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "verify: H1\n")
        self.assertEqual(r.stderr, "warn: x\n")
        self.assertEqual((rec / "stdout.txt").read_text(), "verify: H1\n")
        self.assertEqual((rec / "stderr.txt").read_text(), "warn: x\n")
        doc = self.record(rec)
        self.assertNotIn("verdict", json.dumps(doc))
        self.assertEqual(sorted(p.name for p in rec.iterdir()),
                         ["argv.txt", "record.json", "stderr.txt", "stdout.txt"])

    def test_the_record(self):
        r, rec = self.launch(*self.standard(), STUB_RC="0")
        doc = self.record(rec)
        head = subprocess.run(["git", "-C", str(LAUNCHER.parent), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(doc["launcher"]["adk_tools_commit"], head)
        self.assertEqual(doc["format"], "adk-tools-host-verify-record/v1")
        self.assertEqual(doc["image"], IMG)
        self.assertEqual(doc["image_id_inspected"], IMG)
        self.assertEqual(doc["command"], VERIFY)
        self.assertEqual(doc["env"], ["CHIPLET_FLOW_THREAD_CAP=2"])
        self.assertEqual(doc["limits"], {"cpus": "2", "memory": "6g", "timeout_s": 900})
        self.assertEqual(doc["mounts"], [
            {"source": str(self.pkg.resolve()), "target": "/work/pkg", "readonly": True},
            {"source": str(self.keep.resolve()), "target": "/work/pkg/outputs", "readonly": True}])
        self.assertIsNone(doc["interrupted"])
        self.assertEqual(doc["launcher"]["script"], str(LAUNCHER))
        self.assertTrue((rec / "argv.txt").read_text().startswith("docker run "))

    def test_the_record_names_what_docker_was_given_when_a_symlink_moves(self):
        v1, v2 = self.tmp / "v1", self.tmp / "v2"
        v1.mkdir(); v2.mkdir()
        latest = self.tmp / "latest"
        latest.symlink_to(v1)
        argv, rec = self.argv("--mount", f"{latest}:/in", "--", "true")
        p = subprocess.Popen(argv, env={**self.env, "STUB_SLEEP": "2"},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_for_run()
        latest.unlink()
        latest.symlink_to(v2)
        p.communicate(timeout=30)
        (run,) = self.calls("run")
        self.assertIn(f"type=bind,src={v1},dst=/in,readonly,"
                      "bind-propagation=rprivate,bind-recursive=readonly", run)
        self.assertEqual(self.record(rec)["mounts"][0]["source"], str(v1))

    def test_the_caller_git_dir_does_not_redirect_the_commit(self):
        other = self.tmp / "other"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        r, rec = self.launch(*self.standard(), GIT_DIR=str(other / ".git"))
        head = subprocess.run(["git", "-C", str(LAUNCHER.parent), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(self.record(rec)["launcher"]["adk_tools_commit"], head)

    def test_the_commit_is_named_only_for_the_committed_script(self):
        repo = self.tmp / "repo"
        (repo / "ign").mkdir(parents=True)
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                                        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        git("init", "-q")
        (repo / "host-verify.sh").write_bytes(LAUNCHER.read_bytes())
        (repo / ".gitignore").write_text("ign/\n")
        git("add", "host-verify.sh", ".gitignore")
        git("commit", "-q", "-m", "x")
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        edited = LAUNCHER.read_bytes() + b"# edited\n"
        (repo / "ign" / "host-verify.sh").write_bytes(edited)

        def run(script: Path, n: str) -> dict:
            argv, rec = self.argv(*self.standard(), record=self.out / n)
            r = subprocess.run(["bash", str(script), *argv[1:]], env={**self.env, "STUB_RC": "0"},
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return self.record(rec)["launcher"]

        cases = [("clean", repo / "host-verify.sh", head, False),
                 ("ignored copy", repo / "ign" / "host-verify.sh", None, None)]
        for name, script, commit, modified in cases:
            with self.subTest(name):
                got = run(script, name.replace(" ", "-"))
                self.assertEqual((got["adk_tools_commit"], got["script_modified_since_commit"]), (commit, modified))
        git("update-index", "--assume-unchanged", "host-verify.sh")
        (repo / "host-verify.sh").write_bytes(edited)
        with self.subTest("edited behind assume-unchanged"):
            got = run(repo / "host-verify.sh", "hidden")
            self.assertEqual((got["adk_tools_commit"], got["script_modified_since_commit"]), (head, True))

    def test_the_inputs_are_left_byte_identical(self):
        before = {p: p.read_bytes() for p in self.keep.rglob("*") if p.is_file()}
        self.launch(*self.standard(), STUB_RC="7")
        after = {p: p.read_bytes() for p in self.keep.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_oom_is_recorded_and_the_code_passes_through(self):
        r, rec = self.launch(*self.standard(), STUB_RC="137", STUB_OOM="true")
        self.assertEqual(r.returncode, 137, r.stderr)
        doc = self.record(rec)
        self.assertTrue(doc["container"]["oom_killed"])
        self.assertFalse(doc["timed_out"])

    def wait_for_run(self, timeout: float = 10.0, running: bool = True) -> None:
        self.wait_for(lambda: self.calls("run") and (not running or any(self.state.glob("sleep_*"))),
                      timeout, "the stub never started")

    def wait_for(self, cond, timeout: float = 10.0, what: str = "condition") -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting: {what}")

    def no_stub_alive(self) -> None:
        for f in self.state.glob("sleep_*"):
            with self.assertRaises(ProcessLookupError):
                os.kill(int(f.read_text()), 0)


class LauncherOutcomes(Launch):
    def test_docker_that_creates_nothing(self):
        r, rec = self.launch(*self.standard(), STUB_CREATE="0")
        self.assertEqual(r.returncode, SOFTWARE, r.stderr)
        doc = self.record(rec)
        self.assertFalse(doc["executed"])
        self.assertFalse(doc["container"]["created"])
        self.assertEqual(doc["docker_client_exit"], 125)
        self.assertIsNone(doc["command_exit_code"])
        self.assertIn("did not create", doc["launcher_failure"])

    def test_docker_that_creates_but_never_starts(self):
        r, rec = self.launch(*self.standard(), STUB_START="0")
        self.assertEqual(r.returncode, SOFTWARE, r.stderr)
        doc = self.record(rec)
        self.assertFalse(doc["executed"])
        self.assertTrue(doc["container"]["created"])
        self.assertFalse(doc["container"]["started"])
        self.assertTrue(doc["container"]["removed"])

    def test_a_launcher_code_equal_to_the_container_exit_is_not_the_command_code(self):
        r, rec = self.launch(*self.standard(), STUB_RC="1", STUB_STATE_RC="70")
        self.assertEqual(r.returncode, SOFTWARE, r.stderr)
        self.assertIsNone(self.record(rec)["command_exit_code"])

    def test_a_container_left_in_another_state(self):
        r, rec = self.launch(*self.standard(), STUB_END_STATUS="running")
        self.assertEqual(r.returncode, SOFTWARE, r.stderr)
        self.assertIn("state running", self.record(rec)["launcher_failure"])

    def test_a_failed_container_inspect_is_a_launcher_failure_and_still_removes(self):
        r, rec = self.launch(*self.standard(), STUB_CT_INSPECT_ERR="Cannot connect to the Docker daemon")
        self.assertEqual(r.returncode, SOFTWARE, r.stderr)
        doc = self.record(rec)
        self.assertIn("Cannot connect", doc["launcher_failure"])
        self.assertIn("Cannot connect", doc["container"]["inspect_error"])
        for key in ("created", "started"):
            self.assertIsNone(doc["container"][key], key)
        self.assertIsNone(doc["executed"])
        self.assertEqual(len(self.calls("rm")), 1)
        self.assertTrue(doc["container"]["removed"])

    def test_a_launcher_failure_reports_a_leftover_container(self):
        r, rec = self.launch(*self.standard(), STUB_END_STATUS="running", STUB_RM_FAIL="1")
        self.assertEqual(r.returncode, SOFTWARE, r.stderr)
        self.assertIn("remove it by hand", r.stderr)
        self.assertFalse(self.record(rec)["container"]["removed"])

    def test_a_container_that_cannot_be_removed(self):
        r, rec = self.launch(*self.standard(), STUB_RC="3", STUB_RM_FAIL="1")
        self.assertEqual(r.returncode, 3, r.stderr)
        doc = self.record(rec)
        self.assertEqual(doc["command_exit_code"], 3)
        self.assertFalse(doc["container"]["removed"])
        self.assertIn("remove it by hand", r.stderr)

    def test_a_record_that_cannot_be_written(self):
        argv, rec = self.argv(*self.standard())
        r = subprocess.run(argv, env={**self.env, "STUB_RC": "0", "STUB_STDOUT": "verify: OK",
                                      "STUB_LOCK_DIR": str(rec)},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 74, r.stderr)
        self.assertIn("the outcome was exit 0", r.stderr)
        self.assertEqual(r.stdout, "verify: OK\n")
        self.assertFalse((rec / "record.json").exists())
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["r1"], "scratch left behind")

    def test_a_client_exit_the_container_does_not_confirm(self):
        # e.g. an attach failure: the client says 1, the container says 0
        r, rec = self.launch(*self.standard(), STUB_RC="1", STUB_STATE_RC="0")
        self.assertEqual(r.returncode, SOFTWARE, r.stderr)
        doc = self.record(rec)
        self.assertEqual(doc["docker_client_exit"], 1)
        self.assertEqual(doc["container"]["exit_code"], 0)
        self.assertIsNone(doc["command_exit_code"])

    def test_a_timeout_stops_the_container(self):
        t0 = time.monotonic()
        r, rec = self.launch("--timeout", "1", *self.standard(), STUB_SLEEP="30")
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(r.returncode, TIMEOUT, r.stderr)
        doc = self.record(rec)
        self.assertTrue(doc["timed_out"])
        self.assertTrue(doc["executed"])
        self.assertIsNone(doc["command_exit_code"])
        # docker kill with no --signal sends SIGKILL, the only signal a handler-less PID 1 obeys
        self.assertEqual(self.calls("kill"), [["kill", doc["container"]["name"]]])
        self.assertTrue(doc["container"]["removed"])
        self.no_stub_alive()

    def test_a_timeout_before_the_container_exists(self):
        t0 = time.monotonic()
        r, rec = self.launch("--timeout", "1", *self.standard(), STUB_CREATE_DELAY="3", STUB_SLEEP="30")
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(r.returncode, TIMEOUT, r.stderr)
        doc = self.record(rec)
        self.assertGreaterEqual(len(self.calls("kill")), 2, "the first kill cannot succeed")
        self.assertTrue(doc["container"]["removed"])
        self.no_stub_alive()

    def interrupt(self, sig: int, group: bool, when=None, again: float | None = None, **stub: str):
        argv, rec = self.argv(*self.standard())
        p = subprocess.Popen(argv, env={**self.env, "STUB_SLEEP": "30", **stub}, start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        (when or self.wait_for_run)()
        t0 = time.monotonic()
        send = (lambda: os.killpg(p.pid, sig)) if group else (lambda: p.send_signal(sig))
        send()
        if again is not None:
            time.sleep(again)
            try:
                send()
            except ProcessLookupError:
                pass
        p.communicate(timeout=60)
        return p.returncode, rec, time.monotonic() - t0

    def assert_interrupted(self, rc: int, rec: Path, took: float, name: str) -> dict:
        self.assertEqual(rc, INTERRUPTED)
        self.assertLess(took, 10)
        doc = self.record(rec)
        self.assertEqual(doc["interrupted"], name)
        self.assertIsNone(doc["command_exit_code"])
        self.assertTrue(all(c == ["kill", doc["container"]["name"]] for c in self.calls("kill")))
        self.no_stub_alive()
        self.assertEqual(list(self.state.glob("client_got_*")), [], "a signal reached the docker client")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["r1"], "scratch left behind")
        return doc

    def test_sigterm_to_the_launcher(self):
        doc = self.assert_interrupted(*self.interrupt(signal.SIGTERM, group=False), "TERM")
        self.assertGreaterEqual(len(self.calls("kill")), 1)
        self.assertTrue(doc["container"]["removed"])

    def test_a_signal_right_after_the_argv_is_written(self):
        # the window between the last check and the wait for the client
        doc = self.assert_interrupted(*self.interrupt(
            signal.SIGTERM, group=False,
            when=lambda: self.wait_for(lambda: (self.out / "r1" / "argv.txt").exists()
                                       and (self.out / "r1" / "argv.txt").stat().st_size > 0, 10, "argv"),
            STUB_SLEEP="5"), "TERM")
        self.assertTrue(doc["container"]["removed"])

    def test_a_burst_of_ctrl_c(self):
        argv, rec = self.argv(*self.standard())
        p = subprocess.Popen(argv, env={**self.env, "STUB_SLEEP": "30"}, start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_for(lambda: rec.exists(), 10, "record dir")
        t0 = time.monotonic()
        while p.poll() is None and time.monotonic() - t0 < 4:
            try:
                os.killpg(p.pid, signal.SIGINT)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        p.communicate(timeout=60)
        doc = self.assert_interrupted(p.returncode, rec, time.monotonic() - t0, "INT")
        self.assertNotEqual(doc["container"]["removed"], False)

    def test_a_late_signal_does_not_cut_the_replay(self):
        big = "x" * 120000
        argv, rec = self.argv(*self.standard())
        p = subprocess.Popen(argv, env={**self.env, "STUB_STDOUT": big, "STUB_RC": "0"},
                             start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_for(lambda: (rec / "record.json").exists(), 20, "record.json")
        time.sleep(0.5)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(out, big + "\n")

    def test_ctrl_c_to_the_foreground_group(self):
        doc = self.assert_interrupted(*self.interrupt(signal.SIGINT, group=True), "INT")
        self.assertTrue(doc["container"]["removed"])

    def test_hangup(self):
        doc = self.assert_interrupted(*self.interrupt(signal.SIGHUP, group=True), "HUP")
        self.assertTrue(doc["container"]["removed"])

    def test_a_signal_before_the_container_exists(self):
        doc = self.assert_interrupted(*self.interrupt(
            signal.SIGTERM, group=False, when=lambda: self.wait_for_run(running=False),
            STUB_CREATE_DELAY="2"), "TERM")
        self.assertGreaterEqual(len(self.calls("kill")), 2, "the first kill cannot succeed")
        self.assertTrue(doc["container"]["removed"])

    def test_a_signal_while_created_but_not_started(self):
        doc = self.assert_interrupted(*self.interrupt(
            signal.SIGINT, group=True, when=lambda: self.wait_for(lambda: any(self.state.glob("ct_*"))),
            STUB_START_DELAY="2"), "INT")
        self.assertTrue(doc["container"]["removed"])

    def test_a_second_ctrl_c_during_cleanup(self):
        doc = self.assert_interrupted(*self.interrupt(signal.SIGINT, group=True, again=0.5,
                                                      STUB_KILL_DELAY="2"), "INT")
        self.assertTrue(doc["container"]["removed"])

    def test_a_signal_while_the_image_is_inspected(self):
        rc, rec, took = self.interrupt(signal.SIGTERM, group=False,
                                       when=lambda: self.wait_for(lambda: self.calls("image")),
                                       STUB_INSPECT_DELAY="2")
        self.assertEqual(rc, INTERRUPTED)
        doc = self.record(rec)
        self.assertEqual(doc["interrupted"], "TERM")
        self.assertFalse(doc["executed"])
        self.assertEqual(self.calls("run"), [])

    def test_a_signal_after_the_command_finished(self):
        rc, rec, took = self.interrupt(signal.SIGTERM, group=False,
                                       when=lambda: self.wait_for(lambda: self.calls("container")),
                                       STUB_SLEEP="", STUB_RC="3", STUB_CT_INSPECT_DELAY="2")
        self.assertEqual(rc, 3)
        doc = self.record(rec)
        self.assertEqual(doc["command_exit_code"], 3)
        self.assertIsNone(doc["interrupted"])
        self.assertEqual(len(self.calls("rm")), 1)


class Races(Launch):
    """The signal races an earlier Bash launcher lost, kept as the regression for its successor."""

    def start(self, rec: Path, **stub: str) -> subprocess.Popen:
        argv, _ = self.argv(*self.standard(), record=rec)
        return subprocess.Popen(argv, env={**self.env, **stub}, start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def ended_consistently(self, p: subprocess.Popen, rec: Path, sig: signal.Signals) -> dict | None:
        """What every ending keeps, whatever the timing: the exit code is the record's, and a run without
        a record is one the signal ended before the record directory existed."""
        if not (rec / "record.json").exists():
            self.assertIn(p.returncode, (INTERRUPTED, -sig), "an exit with no record")
            self.assertFalse(rec.exists(), "a record directory without record.json")
            return None
        doc = self.record(rec)
        self.assertEqual(doc["launcher_exit"], p.returncode)
        self.assertNotIn(".host-verify-scratch", " ".join(q.name for q in self.out.iterdir()))
        self.assertEqual(list(self.state.glob("client_got_*")), [], "a signal reached the docker client")
        self.no_stub_alive()
        return doc

    def handlers_installed(self, p: subprocess.Popen) -> None:
        """Wait until the launcher catches TERM, INT and HUP (SigCgt bits 15, 2 and 1)."""
        def caught() -> bool:
            status = Path(f"/proc/{p.pid}/status").read_text()
            mask = int(status.split("SigCgt:")[1].split()[0], 16)
            return mask & 0x4003 == 0x4003
        self.wait_for(caught, 10, "the signal handlers")

    def test_a_signal_at_any_moment_before_the_end_is_an_interruption(self):
        # A signal while the launcher was mid-step crashed it with no record, or was dropped
        # and the run ended 0. With the command still running, every ending here is an interruption.
        for delay in (0, 0.005, 0.01, 0.02, 0.04, 0.08, 0.15, 0.3):
            for sig, group in ((signal.SIGTERM, False), (signal.SIGINT, True), (signal.SIGHUP, True)):
                with self.subTest(delay=delay, sig=sig.name):
                    rec = self.out / f"r-{delay}-{sig.name}"
                    p = self.start(rec, STUB_SLEEP="30")
                    self.handlers_installed(p)
                    time.sleep(delay)
                    os.killpg(p.pid, sig) if group else p.send_signal(sig)
                    stdout, stderr = p.communicate(timeout=60)
                    self.assertEqual(p.returncode, INTERRUPTED,
                                     f"stdout={stdout!r}\nstderr={stderr!r}\n"
                                     f"record={self.record(rec) if (rec / 'record.json').exists() else None}")
                    doc = self.ended_consistently(p, rec, sig)
                    if doc is not None:
                        self.assertEqual(doc["interrupted"], sig.name[3:])
                        self.assertIsNone(doc["command_exit_code"])
                        self.assertNotEqual(doc["container"]["removed"], False)

    def test_a_signal_during_the_start_is_ignored_or_handled_never_misreported(self):
        # Before the handlers exist a signal is ignored. It must never end the launcher with a code the
        # command could have returned (Python's own 1 for an interrupted start, or 130).
        for delay in (0, 0.002, 0.005, 0.01, 0.02, 0.04):
            for sig in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(delay=delay, sig=sig.name):
                    rec = self.out / f"s-{delay}-{sig.name}"
                    p = self.start(rec, STUB_SLEEP="0.3", STUB_RC="0")
                    time.sleep(delay)
                    os.killpg(p.pid, sig)
                    p.communicate(timeout=60)
                    self.assertIn(p.returncode, (0, INTERRUPTED, -sig))
                    self.ended_consistently(p, rec, sig)

    def test_a_signal_as_the_command_ends_keeps_code_and_record_equal(self):
        # A signal that met the end of the command made the launcher exit 255 while the record said
        # 3 or 69. Either answer is right here; they only have to be the same one.
        for delay in (0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.8):
            with self.subTest(delay=delay):
                rec = self.out / f"r-{delay}"
                p = self.start(rec, STUB_SLEEP="0.3", STUB_RC="3")
                time.sleep(delay)
                try:
                    p.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
                p.communicate(timeout=60)
                doc = self.ended_consistently(p, rec, signal.SIGTERM)
                if doc is not None:
                    self.assertIn(p.returncode, (3, INTERRUPTED))
                    if p.returncode == 3:
                        self.assertEqual(doc["command_exit_code"], 3)
                        self.assertIsNone(doc["interrupted"])
                    else:
                        self.assertEqual(doc["interrupted"], "TERM")
                        self.assertIsNone(doc["command_exit_code"])

    def test_sigkill_leaves_no_record_and_the_container_by_name(self):
        # The launcher killed outright left a worker behind that later wrote a false record. Nothing
        # may write one now, and argv.txt names the container still to be removed by hand.
        rec = self.out / "r1"
        p = self.start(rec, STUB_SLEEP="30")
        self.wait_for_run()
        os.kill(p.pid, signal.SIGKILL)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        time.sleep(0.5)
        self.assertFalse((rec / "record.json").exists())
        (run,) = self.calls("run")
        name = run[run.index("--name") + 1]
        self.assertIn(f"--name {name} ", (rec / "argv.txt").read_text())
        self.assertTrue((self.state / f"ct_{name}").exists(), "the container is gone, so nothing to recover")
        os.kill(int((self.state / f"sleep_{name}").read_text()), signal.SIGTERM)
        self.wait_for(lambda: "exited" in (self.state / f"ct_{name}").read_text(), 10, "the stub run to end")

    def test_the_docker_client_runs_in_a_session_of_its_own(self):
        # L: setsid forked when the caller's shell had job control, so signals meant for the worker
        # missed it. The client's session is checked directly, with job control asked for.
        rec = self.out / "r1"
        p = self.start(rec, STUB_SLEEP="30", SHELLOPTS="monitor")
        self.wait_for_run()
        (sleep_pid,) = [int(f.read_text()) for f in self.state.glob("sleep_*")]
        stat_of = lambda pid: Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        client_pid = int(stat_of(sleep_pid)[1])
        self.assertEqual(int(stat_of(client_pid)[3]), client_pid, "the client does not lead its own session")
        self.assertNotEqual(int(stat_of(client_pid)[3]), p.pid)
        os.killpg(p.pid, signal.SIGINT)
        p.communicate(timeout=60)
        self.assertEqual(p.returncode, INTERRUPTED)
        self.ended_consistently(p, rec, signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
