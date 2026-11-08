"""bin/adk-vscode, run against a fake code, a fake docker and a fake bin/flow.

PATH holds only the two fakes and links to the few host tools the launcher
calls, so a real VS Code on the host is never reached. The launcher runs from
a clone of its own whose bin/flow stands in for packaging-flow's tasks writer.
The fakes record their argv. Every refusal is checked to leave the folder as it was and to call code
not at all. The lock is read as run.sh reads it, so run.sh's table of locks
of another form runs here too.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "run_sh"))
import test_run_sh as rs  # noqa: E402

LAUNCHER = ROOT / "bin" / "adk-vscode"
OTHER_ID = "sha256:" + "f" * 64
VSIX_NAME = "adk-packaging-0.2.0.vsix"
TOOLS = ("bash", "python3", "realpath", "dirname", "basename", "awk", "sha256sum",
         "cat", "cmp", "mkdir", "sed", "mktemp", "rm", "ln")
CODE = """#!/usr/bin/python3
import json, os, sys
with open(os.environ["VSC_TEST_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("VSC_TEST_INSTALL_EXIT", "0")) if "--install-extension" in sys.argv else 0)
"""
DOCKER = """#!/usr/bin/python3
import json, os, sys
with open(os.environ["VSC_TEST_DOCKER_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["info"]:
    print(os.environ.get("VSC_TEST_DRIVER_STATUS", '[["Backing Filesystem","extfs"]]'))
    sys.exit(0)
if sys.argv[1:3] == ["image", "inspect"]:
    if os.environ.get("VSC_TEST_NO_DAEMON"):
        sys.stderr.write("\\nCannot connect to the Docker daemon at unix:///var/run/docker.sock.\\n")
        sys.exit(1)
    missing = os.environ.get("VSC_TEST_MISSING", "")
    if missing == "1" or sys.argv[-1] in missing.split():
        print()
        sys.stderr.write("Error response from daemon: No such image: %s\\n" % sys.argv[-1])
        sys.exit(1)
    if "Labels" in sys.argv[4]:
        print(os.environ.get("VSC_TEST_LABEL_PIN_SET", "<no value>"))
        sys.exit(0)
    print(sys.argv[-1])
    sys.exit(0)
sys.exit(2)
"""
# bin/flow as packaging-flow tasks --vscode answers it: the tasks file under --out.
# VSC_TEST_FLOW picks another answer: fail, nothing, link, or race (the folder's
# own tasks.json appears while it runs).
FLOW = """#!/usr/bin/python3
import json, os, sys
with open(os.environ["VSC_TEST_FLOW_CALLS"], "a") as calls:
    calls.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
        "env": [os.environ.get(k) for k in ("ADK_WORK", "ADK_TOOLS_IMAGE", "ADK_FLOW_LOCK")]})
        + "\\n")
how = os.environ.get("VSC_TEST_FLOW", "write")
if how == "fail":
    sys.exit(7)
if how == "nothing":
    sys.exit(0)
out = os.path.join(sys.argv[-1], ".vscode")
os.makedirs(out)
if how == "link":
    os.symlink("/etc/hostname", os.path.join(out, "tasks.json"))
    sys.exit(0)
if how == "race":
    os.makedirs(".vscode", exist_ok=True)
    open(os.path.join(".vscode", "tasks.json"), "w").write("theirs\\n")
open(os.path.join(out, "tasks.json"), "w").write(os.environ["VSC_TEST_TASKS"])
"""
TASKS = '{"version": "2.0.0", "tasks": []}\n'
SCRATCH = ".adk-vscode-tasks."
KEYS = ["adkTools.root", "adkTools.work", "adkTools.image", "adkTools.lock"]
INSPECT = ["image", "inspect", "--format", "{{.Id}}", rs.IMAGE_ID]
CONTAINERD = '[["driver-type","io.containerd.snapshotter.v1"]]'
# What to change on the containerd store: docs/RUN_SH.md's quoted block, word for word.
CONTAINERD_STEPS = [
    '       On Docker Desktop, turn off "Use containerd for pulling and storing images" '
    '(Settings, General).',
    '       On Docker Engine, set "features": {"containerd-snapshotter": false} in '
    '/etc/docker/daemon.json and restart the daemon.',
    "       A rootless daemon reads ~/.config/docker/daemon.json instead.",
    "       Then load the image's archive again (docker load -i <file>).",
    "       Images pulled, built or loaded while the containerd store is on are hidden while it "
    "is off, and come back when it is on again.",
]
MISSING_LINE = ("no image %s on this docker daemon; build the image and pass its ID "
                "(--image or ADK_TOOLS_IMAGE), or load its archive")


class AdkVscodeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        fakes = self.base / "fakes"
        fakes.mkdir()
        for name, text in (("code", CODE), ("docker", DOCKER)):
            (fakes / name).write_text(text)
            (fakes / name).chmod(0o755)
        tools = self.base / "tools"
        tools.mkdir()
        for tool in TOOLS:
            os.symlink(shutil.which(tool), tools / tool)
        self.work = self.base / "adk work"
        self.folder = self.work / "examples" / "two die"
        self.folder.mkdir(parents=True)
        self.lock = self.base / "locks" / "release-lock.json"
        self.lock.parent.mkdir()
        self.lock.write_text(rs.lock_text())
        self.calls_file = self.base / "calls"
        self.docker_calls_file = self.base / "docker calls"
        self.flow_calls_file = self.base / "flow calls"
        self.path = "%s:%s" % (fakes, tools)
        self.env = {"PATH": self.path, "HOME": str(self.base / "home"),
                    "ADK_WORK": str(self.work), "VSC_TEST_CALLS": str(self.calls_file),
                    "VSC_TEST_DOCKER_CALLS": str(self.docker_calls_file),
                    "VSC_TEST_FLOW_CALLS": str(self.flow_calls_file), "VSC_TEST_TASKS": TASKS}
        self.settings = self.folder / ".vscode" / "settings.json"
        self.tasks = self.folder / ".vscode" / "tasks.json"
        self.root = self.clone("adk-tools")
        self.launcher = self.root / "bin" / "adk-vscode"

    def tearDown(self):
        self.tmp.cleanup()

    def launch(self, *args, launcher=None, **env):
        return subprocess.run([str(launcher or self.launcher), *args], env=dict(self.env, **env),
                              cwd=self.base, capture_output=True, text=True, timeout=10,
                              check=False)

    def calls(self, path=None):
        path = path or self.calls_file
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def docker_calls(self):
        return self.calls(self.docker_calls_file)

    def flow_calls(self):
        return self.calls(self.flow_calls_file)

    def scratch(self):
        return [p.name for p in self.folder.iterdir() if p.name.startswith(SCRATCH)]

    def clone(self, name="clone", flow=True):
        """A copy of the launcher in a clone of its own, with a stub bin/flow."""
        clone = self.base / name
        (clone / "bin").mkdir(parents=True)
        shutil.copy2(LAUNCHER, clone / "bin" / "adk-vscode")
        if flow:
            (clone / "bin" / "flow").write_text(FLOW)
            (clone / "bin" / "flow").chmod(0o755)
        return clone

    def written(self):
        return json.loads(self.settings.read_text())

    def expected(self, lock=None):
        return {"adkTools.root": str(self.root), "adkTools.work": str(self.work),
                "adkTools.image": rs.IMAGE_ID, "adkTools.lock": str(lock or self.lock)}

    def vsix(self, sums=True, line=True, data=b"vsix bytes", name=VSIX_NAME, mark=" ",
             upper=False, eol="\n"):
        path = self.base / "release" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        if sums:
            digest = hashlib.sha256(b"vsix bytes").hexdigest()
            text = "%s  other.tar.zst%s" % ("0" * 64, eol)
            if line:
                text += "%s %s%s%s" % (digest.upper() if upper else digest, mark, name, eol)
            (path.parent / "SHA256SUMS").write_bytes(text.encode())
        return path

    def assert_refused(self, result, line, settings=None, lines=None):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        if lines is None:
            self.assertEqual(result.stderr.splitlines()[0], "error: " + line)
        else:
            self.assertEqual(result.stderr.splitlines(), ["error: " + line, *lines])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.flow_calls(), [])
        self.assertEqual(self.scratch(), [])
        if settings is None:
            self.assertFalse((self.folder / ".vscode").exists())
        else:
            self.assertEqual(self.settings.read_text(), settings)

    def test_the_four_settings_then_a_plain_code_folder(self):
        result = self.launch("--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written(), self.expected())
        self.assertEqual(list(self.written()), KEYS)
        self.assertEqual(self.calls(), [[str(self.folder)]])
        self.assertEqual(result.stderr, (
            "adk-vscode: tasks: running packaging-flow tasks --vscode through %s/bin/flow\n"
            "adk-vscode: tasks: %s\n"
            "adk-vscode: settings: %s (adkTools.root, adkTools.work, adkTools.image, "
            "adkTools.lock)\nadk-vscode: opening %s\n")
            % (self.root, self.tasks, self.settings, self.folder))
        # One inspect for the ID, and no container.
        self.assertEqual(self.docker_calls(), [INSPECT])

    def test_a_linked_launcher_names_the_clone_it_is_in(self):
        link = self.base / "home" / ".local" / "bin" / "adk-vscode"
        link.parent.mkdir(parents=True)
        os.symlink(self.launcher, link)
        result = self.launch("--lock", str(self.lock), str(self.folder), launcher=link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written(), self.expected())

    def test_a_launcher_outside_a_clone_is_refused(self):
        clone = self.clone(flow=False)
        self.assert_refused(self.launch("--lock", str(self.lock), str(self.folder),
                                        launcher=clone / "bin" / "adk-vscode"),
                            "%s has no bin/flow; run adk-vscode from an adk-tools clone" % clone)

    def test_a_work_dir_and_clone_inside_each_other_are_refused_with_run_sh_lines(self):
        clone = self.clone()
        launcher = clone / "bin" / "adk-vscode"
        inside = clone / "work"
        for work, line, rest in (
                (self.base, "ADK_WORK (%s) holds this adk-tools clone (%s)" % (self.base, clone),
                 "       Keep the clone outside the work dir, or point ADK_WORK elsewhere."),
                (inside, "ADK_WORK (%s) is inside this adk-tools clone (%s)" % (inside, clone),
                 "       A git clean in the clone would delete it; point ADK_WORK outside "
                 "the clone.")):
            with self.subTest(work=work):
                self.assert_refused(self.launch("--lock", str(self.lock), str(self.folder),
                                                launcher=launcher, ADK_WORK=str(work)),
                                    line, lines=[rest])

    def test_a_work_path_is_the_folder_under_adk_work(self):
        result = self.launch("--lock", str(self.lock), "/work/examples/two die")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [[str(self.folder)]])

    def test_the_lock_comes_from_the_flag_the_variable_or_the_folder(self):
        own = self.folder / "release-lock.json"
        own.write_text(rs.lock_text())
        for args, env, lock in (([], {}, own),
                                ([], {"ADK_FLOW_LOCK": str(self.lock)}, self.lock),
                                (["--lock", str(self.lock)], {}, self.lock)):
            with self.subTest(args=args, env=env):
                result = self.launch(*args, str(self.folder), **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.written()["adkTools.lock"], str(lock))

    def test_other_keys_and_their_order_are_kept_and_a_second_run_changes_nothing(self):
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps(
            {"editor.tabSize": 2, "adkTools.image": OTHER_ID, "files.exclude": {"x": True}}))
        self.assertEqual(self.launch("--lock", str(self.lock), str(self.folder)).returncode, 0)
        data = self.written()
        self.assertEqual(list(data), ["editor.tabSize", "adkTools.image", "files.exclude",
                                      "adkTools.root", "adkTools.work", "adkTools.lock"])
        self.assertEqual(data["editor.tabSize"], 2)
        self.assertEqual(data["files.exclude"], {"x": True})
        self.assertEqual(data["adkTools.image"], rs.IMAGE_ID)
        before = self.settings.read_bytes(), self.settings.stat().st_mtime_ns
        result = self.launch("--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.settings.read_bytes(), self.settings.stat().st_mtime_ns), before)
        self.assertEqual(result.stderr.splitlines()[:2],
                         ["adk-vscode: tasks: %s kept" % self.tasks,
                          "adk-vscode: settings: %s unchanged" % self.settings])

    def test_an_empty_file_or_one_with_a_byte_order_mark_is_read_as_vs_code_reads_it(self):
        self.settings.parent.mkdir()
        for name, data, kept in (("empty", b"", {}),
                                 ("blank", b"\n", {}),
                                 ("BOM", '\ufeff{"editor.tabSize": 2}'.encode(),
                                  {"editor.tabSize": 2})):
            with self.subTest(name=name):
                self.settings.write_bytes(data)
                result = self.launch("--lock", str(self.lock), str(self.folder))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.written(), dict(kept, **self.expected()))

    def test_a_linked_settings_file_stays_a_link_to_the_file_written(self):
        self.settings.parent.mkdir()
        shared = self.base / "shared settings.json"
        shared.write_text('{"editor.tabSize": 2}')
        os.symlink(shared, self.settings)
        result = self.launch("--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(json.loads(shared.read_text()),
                         dict({"editor.tabSize": 2}, **self.expected()))

    def test_a_linked_settings_file_it_cannot_write_through_is_refused(self):
        self.settings.parent.mkdir()
        locked = self.base / "locked"
        locked.mkdir()
        (locked / "settings.json").write_text("{}")
        cases = [("dangling", self.base / "gone.json")]
        if os.geteuid() != 0:
            cases.append(("read-only dir", locked / "settings.json"))
        locked.chmod(0o555)
        try:
            for name, target in cases:
                with self.subTest(name=name):
                    self.settings.unlink(missing_ok=True)
                    os.symlink(target, self.settings)
                    result = self.launch("--lock", str(self.lock), str(self.folder))
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr.splitlines(), [
                        "error: %s links to a file adk-vscode cannot write" % self.settings])
                    self.assertEqual(self.calls(), [])
        finally:
            locked.chmod(0o755)
        self.assertEqual((locked / "settings.json").read_text(), "{}")

    def test_a_hand_formatted_file_already_right_is_left_as_it_is(self):
        self.settings.parent.mkdir()
        values = dict(self.expected(), **{"editor.rulers": ["\u00e9t\u00e9"]})
        text = "{\n  " + ",\n  ".join(
            "%s: %s" % (json.dumps(k), json.dumps(v, ensure_ascii=False))
            for k, v in values.items()) + "\n}"
        self.settings.write_text(text, encoding="utf-8")
        before = self.settings.read_bytes(), self.settings.stat().st_mtime_ns
        result = self.launch("--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.settings.read_bytes(), self.settings.stat().st_mtime_ns), before)

    def test_a_rewrite_keeps_other_text_as_utf8_and_the_file_mode(self):
        self.settings.parent.mkdir()
        self.settings.write_text('{"editor.rulers": ["\u00e9t\u00e9"]}', encoding="utf-8")
        self.settings.chmod(0o600)
        result = self.launch("--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"\u00e9t\u00e9"', self.settings.read_text(encoding="utf-8"))
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.settings.parent.iterdir()),
                         ["settings.json", "tasks.json"])

    def test_a_vsix_whose_line_matches_is_installed_first(self):
        vsix = self.vsix()
        result = self.launch("--vsix", str(vsix), "--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [["--install-extension", str(vsix)], [str(self.folder)]])
        self.assertIn("adk-vscode: installed %s, sha256 matches %s/SHA256SUMS"
                      % (VSIX_NAME, vsix.parent), result.stderr.splitlines())

    def test_a_vsix_line_is_read_as_sha256sum_reads_it(self):
        for name, kwargs in (
                ("binary mode, a space in the name", {"name": "adk packaging.vsix", "mark": "*"}),
                ("a backslash in the name", {"name": "adk\\packaging.vsix"}),
                ("upper-case hex", {"upper": True}),
                ("CRLF", {"eol": "\r\n"})):
            with self.subTest(name=name):
                shutil.rmtree(self.base / "release", ignore_errors=True)
                self.calls_file.unlink(missing_ok=True)
                vsix = self.vsix(**kwargs)
                result = self.launch("--vsix", str(vsix), "--lock", str(self.lock),
                                     str(self.folder))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls()[0], ["--install-extension", str(vsix)])

    def test_vsix_refusals(self):
        missing = self.base / "nope.vsix"
        cases = (
            (lambda: missing, "--vsix %s is not a file" % missing),
            (lambda: self.vsix(sums=False),
             "no SHA256SUMS beside %s/%s; the vsix is not installed"
             % (self.base / "release", VSIX_NAME)),
            (lambda: self.vsix(line=False),
             "%s/SHA256SUMS has no line for %s" % (self.base / "release", VSIX_NAME)),
            (lambda: self.vsix(data=b"other bytes"),
             "%s does not match its line in %s/SHA256SUMS" % (VSIX_NAME, self.base / "release")),
        )
        for make, line in cases:
            with self.subTest(line=line):
                shutil.rmtree(self.base / "release", ignore_errors=True)
                vsix = make()
                self.assert_refused(self.launch("--vsix", str(vsix), "--lock", str(self.lock),
                                                str(self.folder)), line)

    def test_a_failed_install_writes_and_opens_nothing(self):
        vsix = self.vsix()
        result = self.launch("--vsix", str(vsix), "--lock", str(self.lock), str(self.folder),
                             VSC_TEST_INSTALL_EXIT="3")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.splitlines()[-1],
                         "error: code --install-extension %s failed (exit 3)" % vsix)
        self.assertEqual(self.calls(), [["--install-extension", str(vsix)]])
        self.assertFalse((self.folder / ".vscode").exists())
        self.assertEqual(self.scratch(), [])

    def test_a_folder_outside_adk_work_is_refused(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        for given in (str(outside), str(self.work / "not there"), "/work/../elsewhere"):
            with self.subTest(given=given):
                self.assert_refused(self.launch("--lock", str(self.lock), given),
                                    "%s is not under ADK_WORK (%s)" % (given, self.work))

    def test_lock_refusals(self):
        not_lock = self.base / "not-a-lock.json"
        not_lock.write_text("{}\n")
        other_schema = self.base / "other-schema.json"
        other_schema.write_text(rs.lock_text(schema="adk-examples.release-lock/3"))
        manifest_1 = self.base / "manifest-1.json"
        manifest_1.write_text(rs.lock_text(image_manifest=OTHER_ID))
        own = self.folder / "release-lock.json"
        cases = (
            ("--lock", ["--lock", str(not_lock)], {}, None,
             "%s is not a release lock" % not_lock),
            ("ADK_FLOW_LOCK", [], {"ADK_FLOW_LOCK": str(not_lock)}, None,
             "%s is not a release lock" % not_lock),
            ("not a file", ["--lock", str(self.base)], {}, None,
             "%s is not a release lock" % self.base),
            ("the folder's own", [], {}, "{}\n", "%s is not a release lock" % own),
            ("another schema", ["--lock", str(other_schema)], {}, None,
             "%s is of schema 'adk-examples.release-lock/3'; adk-vscode reads "
             "adk-examples.release-lock/1 without image_manifest and "
             "adk-examples.release-lock/2 with it" % other_schema),
            ("none", [], {}, None,
             "no release lock: pass --lock FILE, set ADK_FLOW_LOCK, or put "
             "release-lock.json in %s" % self.folder),
            ("another image of no pin set", ["--lock", str(self.lock)],
             {"ADK_TOOLS_IMAGE": OTHER_ID}, None,
             "%s names the pin set %s; the image's org.ihp.adk-tools.pin-set label is ''"
             % (self.lock, rs.LOCK_PIN_SET)),
            ("another image of another pin set", ["--lock", str(self.lock), "--image", OTHER_ID],
             {"VSC_TEST_LABEL_PIN_SET": "e" * 64}, None,
             "%s names the pin set %s; the image's org.ihp.adk-tools.pin-set label is '%s'"
             % (self.lock, rs.LOCK_PIN_SET, "e" * 64)),
            ("a manifest without its schema", ["--lock", str(manifest_1)], {}, None,
             "%s is of schema 'adk-examples.release-lock/1'; adk-vscode reads "
             "adk-examples.release-lock/1 without image_manifest and "
             "adk-examples.release-lock/2 with it" % manifest_1),
            ("own and given differ", ["--lock", str(self.lock)], {},
             rs.lock_text(image=OTHER_ID), "%s is not the lock given (%s)" % (own, self.lock)),
        )
        for name, args, env, own_text, line in cases:
            with self.subTest(name=name):
                own.unlink(missing_ok=True)
                if own_text is not None:
                    own.write_text(own_text)
                self.assert_refused(self.launch(*args, str(self.folder), **env), line)

    def test_a_lock_of_another_form_is_refused_as_run_sh_refuses_it(self):
        for name, text in rs.lock_form_cases():
            with self.subTest(name=name):
                self.lock.write_bytes(text.encode())
                self.assert_refused(self.launch("--lock", str(self.lock), str(self.folder)),
                                    "%s is not a release lock" % self.lock)

    def test_the_lock_pattern_and_schema_rule_are_run_sh_s(self):
        def line(path, start):
            lines = [line.strip() for line in path.read_text().splitlines()
                     if line.strip().startswith(start)]
            self.assertEqual(len(lines), 1, path)
            return lines[0]
        for start in ("LOCK_RE=", "LOCK_WANT="):
            with self.subTest(start=start):
                self.assertEqual(line(LAUNCHER, start), line(ROOT / "run.sh", start))

    def test_a_schema_2_lock_takes_the_manifest_digest_the_daemon_holds(self):
        self.lock.write_text(rs.lock_text(image=OTHER_ID, image_manifest=rs.IMAGE_ID,
                                          schema="adk-examples.release-lock/2"))
        result = self.launch("--lock", str(self.lock), str(self.folder),
                             VSC_TEST_MISSING=OTHER_ID, VSC_TEST_DRIVER_STATUS=CONTAINERD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written()["adkTools.image"], rs.IMAGE_ID)
        # The config digest is still the first choice, and ADK_TOOLS_IMAGE may be either.
        for env, want in (({}, OTHER_ID), ({"ADK_TOOLS_IMAGE": rs.IMAGE_ID}, rs.IMAGE_ID)):
            with self.subTest(env=env):
                self.assertEqual(self.launch("--lock", str(self.lock), str(self.folder),
                                             **env).returncode, 0)
                self.assertEqual(self.written()["adkTools.image"], want)

    def test_another_image_of_the_locks_pin_set_is_written_with_a_note(self):
        note = ("adk-vscode: the image %s is not the reference image %s the release lock %s "
                "names; it runs held to the lock's pin set" % (OTHER_ID, rs.IMAGE_ID, self.lock))
        for args, env in ((["--image", OTHER_ID], {}), ([], {"ADK_TOOLS_IMAGE": OTHER_ID}),
                          (["--image", OTHER_ID], {"ADK_TOOLS_IMAGE": rs.IMAGE_ID})):
            with self.subTest(args=args, env=env):
                result = self.launch("--lock", str(self.lock), *args, str(self.folder),
                                     VSC_TEST_LABEL_PIN_SET=rs.LOCK_PIN_SET, **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr.splitlines().count(note), 1, result.stderr)
                self.assertEqual(self.written()["adkTools.image"], OTHER_ID)

    def test_the_reference_image_reads_no_label(self):
        result = self.launch("--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("is not the reference image", result.stderr)
        self.assertFalse([c for c in self.docker_calls() if "Labels" in " ".join(c)])

    def test_a_schema_2_lock_whose_image_is_missing_names_its_manifest_not_a_switch(self):
        self.lock.write_text(rs.lock_text(image=OTHER_ID, image_manifest=rs.IMAGE_ID,
                                          schema="adk-examples.release-lock/2"))
        self.assert_refused(
            self.launch("--lock", str(self.lock), str(self.folder), VSC_TEST_MISSING="1",
                        VSC_TEST_DRIVER_STATUS=CONTAINERD),
            MISSING_LINE % OTHER_ID,
            lines=["       This daemon uses the containerd image store, which gives the release "
                   "archive the lock's image_manifest, %s, as its ID." % rs.IMAGE_ID])

    def test_an_image_the_daemon_lacks_is_refused(self):
        self.assert_refused(self.launch("--lock", str(self.lock), str(self.folder),
                                        VSC_TEST_MISSING="1"),
                            MISSING_LINE % rs.IMAGE_ID)
        self.assertEqual(self.docker_calls(), [INSPECT, ["info", "--format", "{{json .DriverStatus}}"]])

    def test_on_the_containerd_store_the_missing_image_line_names_the_store(self):
        self.assert_refused(
            self.launch("--lock", str(self.lock), str(self.folder), VSC_TEST_MISSING="1",
                        VSC_TEST_DRIVER_STATUS=CONTAINERD),
            MISSING_LINE % rs.IMAGE_ID,
            lines=["       This daemon uses the containerd image store, which does not find an "
                   "image by the ID the lock names.", *CONTAINERD_STEPS])

    def test_an_image_given_and_missing_gets_no_containerd_steps(self):
        # An ID given was built or loaded on this store, which finds it by that ID.
        self.assert_refused(
            self.launch("--lock", str(self.lock), "--image", OTHER_ID, str(self.folder),
                        VSC_TEST_MISSING="1", VSC_TEST_DRIVER_STATUS=CONTAINERD),
            MISSING_LINE % OTHER_ID, lines=[])
        self.assertEqual(self.docker_calls(), [INSPECT[:-1] + [OTHER_ID]])

    def test_the_lock_s_id_given_and_missing_gets_the_containerd_steps(self):
        self.assert_refused(
            self.launch("--lock", str(self.lock), "--image", rs.IMAGE_ID, str(self.folder),
                        VSC_TEST_MISSING="1", VSC_TEST_DRIVER_STATUS=CONTAINERD),
            MISSING_LINE % rs.IMAGE_ID,
            lines=["       This daemon uses the containerd image store, which does not find an "
                   "image by the ID the lock names.", *CONTAINERD_STEPS])

    def test_the_containerd_steps_are_run_sh_md_s(self):
        # docs/RUN_SH.md owns the steps run.sh points to: the second quoted block
        # under its heading (tests/run_sh holds the first to run.sh's lines).
        self.assertEqual(rs.missing_image_blocks()[1], CONTAINERD_STEPS)

    def test_a_docker_that_cannot_answer_is_not_called_a_missing_image(self):
        self.assert_refused(self.launch("--lock", str(self.lock), str(self.folder),
                                        VSC_TEST_NO_DAEMON="1"),
                            "docker image inspect %s failed: Cannot connect to the Docker "
                            "daemon at unix:///var/run/docker.sock." % rs.IMAGE_ID)

    def test_a_settings_file_that_is_not_plain_json_is_left_as_it_is(self):
        self.settings.parent.mkdir()
        text = '{\n    // a comment\n    "editor.tabSize": 2\n}\n'
        self.settings.write_text(text)
        self.assert_refused(self.launch("--lock", str(self.lock), str(self.folder)),
                            "%s is not plain JSON; adkTools.* left unwritten" % self.settings,
                            settings=text)

    def test_a_vscode_dir_it_cannot_write_is_refused_before_the_install(self):
        vsix = self.vsix()
        (self.folder / ".vscode").write_text("not a dir")
        result = self.launch("--vsix", str(vsix), "--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.splitlines(), [
            "error: %s/.vscode is not a directory adk-vscode can write" % self.folder])
        self.assertEqual(self.calls(), [])
        self.assertEqual((self.folder / ".vscode").read_text(), "not a dir")

    @unittest.skipIf(os.geteuid() == 0, "root writes a read-only directory")
    def test_a_folder_it_cannot_write_is_refused(self):
        self.folder.chmod(0o555)
        try:
            self.assert_refused(self.launch("--lock", str(self.lock), str(self.folder)),
                                "%s/.vscode is not a directory adk-vscode can write"
                                % self.folder)
        finally:
            self.folder.chmod(0o755)

    def test_a_command_missing_from_path_is_refused(self):
        for name in ("code", "docker"):
            with self.subTest(name=name):
                fake = self.base / "fakes" / name
                fake.rename(self.base / name)
                try:
                    self.assert_refused(self.launch("--lock", str(self.lock), str(self.folder)),
                                        "no %s command on PATH" % name)
                finally:
                    (self.base / name).rename(fake)

    def test_missing_tasks_are_written_by_the_flow_with_the_settings_it_writes(self):
        result = self.launch("--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tasks.read_text(), TASKS)
        self.assertFalse(self.tasks.is_symlink())
        [call] = self.flow_calls()
        self.assertEqual(call["argv"][:-1], ["--", "packaging-flow", "tasks", "--vscode", "--out"])
        self.assertTrue(call["argv"][-1].startswith(SCRATCH))
        self.assertEqual(call["cwd"], str(self.folder))
        written = self.written()
        self.assertEqual(call["env"], [written["adkTools.work"], written["adkTools.image"],
                                       written["adkTools.lock"]])
        self.assertEqual(self.scratch(), [])

    def test_a_tasks_file_already_there_is_kept_and_no_flow_runs(self):
        self.tasks.parent.mkdir()
        (self.base / "mine.json").write_text("mine too\n")
        for make in (lambda: self.tasks.write_text("mine\n"),
                     lambda: os.symlink(self.base / "mine.json", self.tasks)):
            with self.subTest(make=make):
                if os.path.lexists(self.tasks):
                    self.tasks.unlink()
                make()
                before = os.readlink(self.tasks) if self.tasks.is_symlink() \
                    else self.tasks.read_text()
                result = self.launch("--lock", str(self.lock), str(self.folder))
                self.assertEqual(result.returncode, 0, result.stderr)
                after = os.readlink(self.tasks) if self.tasks.is_symlink() \
                    else self.tasks.read_text()
                self.assertEqual(after, before)
                self.assertEqual(self.flow_calls(), [])
                self.assertIn("adk-vscode: tasks: %s kept" % self.tasks,
                              result.stderr.splitlines())

    def test_a_flow_that_fails_or_writes_no_file_leaves_the_folder_as_it_was(self):
        for how, line in (
                ("fail", "packaging-flow tasks --vscode failed (exit 7); nothing written"),
                ("nothing", "packaging-flow tasks --vscode wrote no regular tasks file; "
                            "nothing written"),
                ("link", "packaging-flow tasks --vscode wrote no regular tasks file; "
                         "nothing written")):
            with self.subTest(how=how):
                result = self.launch("--lock", str(self.lock), str(self.folder),
                                     VSC_TEST_FLOW=how)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.splitlines()[-1], "error: " + line)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.scratch(), [])
                self.assertFalse((self.folder / ".vscode").exists())

    def test_a_tasks_file_that_appears_while_the_flow_runs_is_kept(self):
        result = self.launch("--lock", str(self.lock), str(self.folder), VSC_TEST_FLOW="race")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.splitlines()[-1], "error: %s could not be written, or "
                         "appeared meanwhile; it is left as it is" % self.tasks)
        self.assertEqual(self.tasks.read_text(), "theirs\n")
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.scratch(), [])

    def test_a_tasks_link_to_nothing_is_refused_before_any_flow_runs(self):
        self.tasks.parent.mkdir()
        os.symlink("nowhere", self.tasks)
        result = self.launch("--lock", str(self.lock), str(self.folder))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.splitlines(), ["error: %s links to nothing; move it away "
                                                      "and run adk-vscode again" % self.tasks])
        self.assertEqual(os.readlink(self.tasks), "nowhere")
        self.assertEqual(self.flow_calls(), [])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.settings.exists())


if __name__ == "__main__":
    unittest.main()
