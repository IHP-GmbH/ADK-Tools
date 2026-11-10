"""Client-death regressions with simulated subprocesses and daemon replies.

The launcher writes real temporary records, but no subprocess or Docker command runs.
"""
from __future__ import annotations

import io
import json
import os
import runpy
import signal
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch


LAUNCHER = Path(os.environ.get("HOST_VERIFY_LAUNCHER",
                              Path(__file__).resolve().parents[2] / "host-verify.sh")).resolve()
IMAGE = "sha256:" + "ab" * 32
EXITED_137 = "exited|2026-09-25T12:00:00Z|137|false\n"
# provenance() runs git; a stand-in of the same shape keeps the record a v1 record.
PROVENANCE = {"script": str(LAUNCHER), "sha256": "0" * 64, "adk_tools_commit": None,
              "script_modified_since_commit": None}
TESTS = Path(__file__).resolve().parents[1]
SCHEMA = TESTS.parent / "formats" / "adk-tools-host-verify-record.v1.schema.json"
_loader = SourceFileLoader("check_format", str(TESTS / "formats" / "check_format.py"))
check_format = module_from_spec(spec_from_loader("check_format", _loader))
_loader.exec_module(check_format)


class Client:
    """A controllable wait/poll boundary; the first wait can expose a race."""

    def __init__(self, returncode, first_wait=None):
        self.returncode = None
        self.final_returncode = returncode
        self.first_wait = first_wait

    def wait(self, timeout=None):
        if self.first_wait is not None:
            first_wait, self.first_wait = self.first_wait, None
            first_wait(self, timeout)
        self.returncode = self.final_returncode
        return self.returncode

    def poll(self):
        return self.returncode


class ClientDeath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        handlers = {sig: signal.getsignal(sig) for sig in signals}
        try:
            namespace = runpy.run_path(str(LAUNCHER), run_name="host_verify_client_death_tests")
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        cls.Run = namespace["Run"]
        # runpy can return a copy; patch the namespace the methods actually use.
        cls.launcher_globals = cls.Run.execute.__globals__

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.clock = 0.0
        self.calls = []
        self.sequence = 0
        actual_open = open

        def open_with_memory(path, *args, **kwargs):
            if path == "/proc/meminfo":
                return io.StringIO("MemAvailable: 67108864 kB\n")
            return actual_open(path, *args, **kwargs)

        globals_patch = patch.dict(self.launcher_globals, {"STOP": None, "open": open_with_memory})
        globals_patch.start()
        self.addCleanup(globals_patch.stop)

    def launch(self, client, container=None, on_inspect=None):
        self.sequence += 1
        record = self.directory / f"record-{self.sequence}"
        record.mkdir()
        run = self.Run({"--image": IMAGE, "--cpus": "1", "--memory": "1k", "--timeout": "1"},
                       [], [], ["true"], str(self.directory), str(record))

        def helper(argv, **kwargs):
            self.calls.append(argv)
            if argv[:3] == ["docker", "image", "inspect"]:
                return 0, IMAGE + "\n", ""
            if argv[:3] == ["docker", "container", "inspect"]:
                if on_inspect is not None:
                    on_inspect()
                if container is None:
                    return 1, "", f"Error: No such container: {run.name}"
                return 0, container, ""
            if argv == ["docker", "kill", run.name]:
                # External client death while stop() is waiting on a daemon request.
                client.returncode = client.final_returncode
                return 1, "", f"Error: No such container: {run.name}"
            if argv == ["docker", "rm", "-f", run.name]:
                if container is None:
                    return 1, "", f"Error: No such container: {run.name}"
                return 0, run.name + "\n", ""
            self.fail(f"unexpected helper call: {argv}")

        with patch.object(run, "helper", side_effect=helper), \
                patch.object(run, "provenance", return_value=PROVENANCE), \
                patch.object(self.launcher_globals["subprocess"], "Popen", return_value=client), \
                patch.object(self.launcher_globals["os"], "sched_getaffinity", return_value=set(range(8))), \
                patch.object(self.launcher_globals["time"], "monotonic", side_effect=lambda: self.clock), \
                patch.dict(self.launcher_globals, {"say": lambda message: None}):
            code = run.main()
        self.assertEqual(check_format.check_file(SCHEMA, record / "record.json"), [],
                         f"{record / 'record.json'} does not conform to {SCHEMA.name}")
        document = json.loads((record / "record.json").read_text())
        self.assertEqual(document["launcher_exit"], code)
        self.assertEqual([call for call in self.calls if call[:2] == ["docker", "rm"]][-1:],
                         [["docker", "rm", "-f", run.name]], "named cleanup was not attempted")
        return code, document

    def assert_unknown(self, document):
        self.assertIsNone(document["executed"])
        self.assertIsNone(document["container"]["created"])
        self.assertIsNone(document["container"]["started"])
        self.assertIsNone(document["command_exit_code"])
        self.assertFalse(document["container"]["removed"])

    def test_signal_death_with_no_container_is_unknown(self):
        for sig in (signal.SIGKILL, signal.SIGTERM):
            with self.subTest(signal=sig):
                code, document = self.launch(Client(-sig))
                self.assertEqual(code, 70)
                self.assertEqual(document["docker_client_exit"], 128 + sig)
                self.assert_unknown(document)
                self.assertTrue(document["launcher_failure"])
                self.assertFalse(document["timed_out"])
                self.assertIsNone(document["interrupted"])

    def test_signal_death_cannot_confirm_matching_container_exit(self):
        code, document = self.launch(Client(-signal.SIGKILL), EXITED_137)
        self.assertEqual(code, 70)
        self.assertEqual(document["docker_client_exit"], 137)
        self.assertEqual(document["container"]["exit_code"], 137)
        self.assertTrue(document["executed"])
        self.assertTrue(document["container"]["removed"])
        self.assertIsNone(document["command_exit_code"])
        self.assertTrue(document["launcher_failure"])

    def test_normal_exit_137_still_confirms_the_command_exit(self):
        code, document = self.launch(Client(137), EXITED_137)
        self.assertEqual(code, 137)
        self.assertEqual(document["docker_client_exit"], 137)
        self.assertEqual(document["command_exit_code"], 137)
        self.assertTrue(document["container"]["removed"])
        self.assertIsNone(document["launcher_failure"])

    def test_matching_launcher_signal_at_client_completion_is_an_interruption(self):
        for sig, name in ((signal.SIGINT, "INT"), (signal.SIGHUP, "HUP")):
            with self.subTest(signal=sig):
                self.launcher_globals["STOP"] = None

                def interrupt(client, timeout):
                    self.launcher_globals["STOP"] = name

                code, document = self.launch(Client(-sig, interrupt))
                self.assertEqual(code, 69)
                self.assertEqual(document["interrupted"], name)
                self.assertEqual(document["docker_client_exit"], 128 + sig)
                self.assertFalse(document["timed_out"])
                self.assertIsNone(document["launcher_failure"])
                self.assert_unknown(document)

    def test_matching_launcher_signal_during_inspection_does_not_reclassify_client_death(self):
        def late_interrupt():
            self.launcher_globals["STOP"] = "INT"

        code, document = self.launch(Client(-signal.SIGINT), on_inspect=late_interrupt)
        self.assertEqual(code, 70)
        self.assertEqual(document["docker_client_exit"], 130)
        self.assertIsNone(document["interrupted"])
        self.assertTrue(document["launcher_failure"])
        self.assert_unknown(document)

    def test_unrelated_launcher_signal_does_not_reclassify_client_death(self):
        def unrelated_interrupt(client, timeout):
            self.launcher_globals["STOP"] = "TERM"

        code, document = self.launch(Client(-signal.SIGKILL, unrelated_interrupt))
        self.assertEqual(code, 70)
        self.assertEqual(document["docker_client_exit"], 137)
        self.assertIsNone(document["interrupted"])
        self.assertTrue(document["launcher_failure"])
        self.assert_unknown(document)

    def test_normal_exit_137_with_a_noted_launcher_signal_still_confirms_the_command(self):
        def interrupt(client, timeout):
            self.launcher_globals["STOP"] = "INT"

        code, document = self.launch(Client(137, interrupt), EXITED_137)
        self.assertEqual(code, 137)
        self.assertEqual(document["docker_client_exit"], 137)
        self.assertEqual(document["command_exit_code"], 137)
        self.assertTrue(document["container"]["removed"])
        self.assertIsNone(document["interrupted"])
        self.assertIsNone(document["launcher_failure"])

    def test_external_death_inside_stop_preserves_interruption(self):
        def interrupt(client, timeout):
            self.launcher_globals["STOP"] = "TERM"
            raise subprocess.TimeoutExpired("docker run", timeout)

        code, document = self.launch(Client(-signal.SIGKILL, interrupt))
        self.assertEqual(code, 69)
        self.assertEqual(document["interrupted"], "TERM")
        self.assertFalse(document["timed_out"])
        self.assertEqual(document["docker_client_exit"], 137)
        self.assert_unknown(document)
        self.assertTrue(any(call[:2] == ["docker", "kill"] for call in self.calls))

    def test_external_death_inside_stop_preserves_timeout(self):
        def time_out(client, timeout):
            self.clock = 2.0
            raise subprocess.TimeoutExpired("docker run", timeout)

        code, document = self.launch(Client(-signal.SIGKILL, time_out))
        self.assertEqual(code, 75)
        self.assertTrue(document["timed_out"])
        self.assertIsNone(document["interrupted"])
        self.assertEqual(document["docker_client_exit"], 137)
        self.assert_unknown(document)
        self.assertTrue(any(call[:2] == ["docker", "kill"] for call in self.calls))

    def test_exception_recovery_observes_an_already_dead_client(self):
        def fail_after_death(client, timeout):
            client.returncode = -signal.SIGKILL
            raise RuntimeError("injected failure after client death")

        code, document = self.launch(Client(-signal.SIGKILL, fail_after_death))
        self.assertEqual(code, 70)
        self.assertEqual(document["docker_client_exit"], 137)
        self.assert_unknown(document)
        self.assertIn("injected failure", document["launcher_failure"])
        self.assertFalse(any(call[:2] == ["docker", "kill"] for call in self.calls))


if __name__ == "__main__":
    unittest.main()
