"""The launchers, exercised with a docker stub that records its argv.

run.sh and openroad/check-3dblox.sh both take the adk-tools image by its ID
only. Both run the same refusal cases and must give the same answer, and the
parity test compares the two rules as written, so one cannot drift alone.
run.sh also checks the ID with the daemon and passes it into the container,
mounts the OpenROAD the image's label names, passes on the LibreLane and PDK
its other two labels name, and picks the X display and its cookie; those run
against temp trees and stubs only.
"""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
HEX = "0123456789abcdef" * 4
IMAGE_ID = "sha256:" + HEX
ID_RULE = "^sha256:[0-9a-f]{64}$"
LAUNCHERS = ("run.sh", "openroad/check-3dblox.sh")
# What both say after the first line of a refusal: where the ID comes from.
ANSWER = (
    "       A tag is not taken: the daemon is shared and a tag can be moved.\n"
    "       For an image you built, read its ID right after ./build.sh:\n"
    "         docker image inspect --format '{{.Id}}' adk-tools:dev\n")
STUB = """#!/usr/bin/python3
import json
import os
import re
import sys
with open(os.environ["RUN_SH_TEST_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
# docker info's DriverStatus: RUN_SH_TEST_DRIVER_STATUS, or the classic store's.
if sys.argv[1:2] == ["info"]:
    print(os.environ.get("RUN_SH_TEST_DRIVER_STATUS", '[["Backing Filesystem","extfs"]]'))
    sys.exit(0)
if sys.argv[1:3] == ["image", "inspect"]:
    formatted = "--format" in sys.argv
    # An image the daemon does not hold, or a daemon that cannot be reached:
    # inspecting fails as docker's does, with an empty line on stdout under
    # --format and the error on stderr.
    if sys.argv[-1] == os.environ.get("RUN_SH_TEST_MISSING"):
        if formatted:
            print()
        sys.stderr.write("Error response from daemon: No such image: %s\\n"
                         % sys.argv[-1])
        sys.exit(1)
    if os.environ.get("RUN_SH_TEST_NO_DAEMON"):
        if formatted:
            print()
        sys.stderr.write(os.environ["RUN_SH_TEST_NO_DAEMON"] + "\\n")
        sys.exit(1)
    # run.sh's ID read: the ID asked for, or RUN_SH_TEST_ID, after the
    # warning in RUN_SH_TEST_WARNING when there is one.
    if formatted and sys.argv[sys.argv.index("--format") + 1] == "{{.Id}}":
        if os.environ.get("RUN_SH_TEST_WARNING"):
            sys.stderr.write(os.environ["RUN_SH_TEST_WARNING"] + "\\n")
            sys.stderr.flush()
        print(os.environ.get("RUN_SH_TEST_ID", sys.argv[-1]))
        sys.exit(0)
    # run.sh's label reads: the label's value, empty when the image has none
    # (as docker's index does). RUN_SH_TEST_LABEL is OpenROAD's; another
    # label's is in RUN_SH_TEST_LABEL_<last part, upper case>, e.g.
    # ..._NIX_LIBRELANE. The closure-inside read ranges over the labels, so it
    # tells an empty label from none: "+<value>|" when the variable is set,
    # empty when it is not.
    # The image's ENV, one VAR=value a line: RUN_SH_TEST_ENV.
    if "--format" in sys.argv and \
            sys.argv[sys.argv.index("--format") + 1] == "{{range .Config.Env}}{{println .}}{{end}}":
        sys.stdout.write(os.environ.get("RUN_SH_TEST_ENV", ""))
        sys.exit(0)
    if "--format" in sys.argv:
        fmt = sys.argv[sys.argv.index("--format") + 1]
        ranged = re.search(r'eq \\$k "([^"]+)"', fmt)
        name = (ranged or re.search(r'Labels "([^"]+)"', fmt)).group(1)
        key = "RUN_SH_TEST_LABEL_" + name.rsplit(".", 1)[-1].upper().replace("-", "_")
        if ranged:
            print("" if key not in os.environ else "+%s|" % os.environ[key])
        else:
            default = (os.environ.get("RUN_SH_TEST_LABEL", "")
                       if name.endswith(".nix-openroad") else "")
            print(os.environ.get(key, default))
    sys.exit(0)
# run.sh deletes the X cookie it mounts once docker run returns; keep a copy.
for i, arg in enumerate(sys.argv):
    if arg == "-v" and sys.argv[i + 1].endswith(":/tmp/.Xauthority:ro"):
        src = sys.argv[i + 1][:-len(":/tmp/.Xauthority:ro")]
        with open(src) as f, open(os.environ["RUN_SH_TEST_COOKIE"], "w") as out:
            out.write(f.read())
# check-3dblox.sh passes only when the linter's run prints this marker.
if "-exit" in sys.argv:
    print("CHECK_3DBLOX_OK")
# docker run writes the container's ID to --cidfile; the stub's is this.
if sys.argv[1:2] == ["run"] and "--cidfile" in sys.argv:
    with open(sys.argv[sys.argv.index("--cidfile") + 1], "w") as cid:
        cid.write("stub-container")
# With RUN_SH_TEST_EXEC set, docker run runs its command here, with the stdin
# docker would give it (the caller's under -i, an empty one without) and the
# -e variables, but not the ADK_TOOLS_IMAGE run.sh itself was given.
if sys.argv[1:2] == ["run"] and os.environ.get("RUN_SH_TEST_EXEC"):
    import re
    import subprocess
    image = next(i for i, a in enumerate(sys.argv)
                 if re.fullmatch(r"sha256:[0-9a-f]{64}", a))
    interactive = {"-i", "-it", "--interactive"} & set(sys.argv[2:image])
    env = {k: v for k, v in os.environ.items() if k != "ADK_TOOLS_IMAGE"}
    env.update(sys.argv[i + 1].split("=", 1) for i in range(2, image)
               if sys.argv[i] == "-e")
    sys.exit(subprocess.run(
        sys.argv[image + 1:], env=env,
        stdin=None if interactive else subprocess.DEVNULL).returncode)
# ci/check_overlay.py's container: RUN_SH_TEST_OVERLAY_SAYS is its one
# reason line, RUN_SH_TEST_OVERLAY_STDERR what the refusing step wrote, and
# the check then refuses.
if sys.argv[1:2] == ["run"] and "--inner" in sys.argv:
    if os.environ.get("RUN_SH_TEST_OVERLAY_SAYS"):
        sys.stderr.write(os.environ.get("RUN_SH_TEST_OVERLAY_STDERR", ""))
        print(os.environ["RUN_SH_TEST_OVERLAY_SAYS"])
        sys.exit(1)
    sys.exit(0)
sys.exit(int(os.environ.get("RUN_SH_TEST_EXIT", "0")))
"""


class StubbedLauncher:
    """A launcher run against the docker stub; a subclass names its script."""

    SCRIPT = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="run sh ")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        stub_dir = self.base / "stub bin"
        stub_dir.mkdir()
        (stub_dir / "docker").write_text(STUB)
        (stub_dir / "docker").chmod(0o755)
        self.calls_file = self.base / "calls.jsonl"
        self.env = os.environ.copy()
        self.env.pop("ADK_TOOLS_IMAGE", None)
        self.env.pop("HOSTSLOT_ID", None)
        self.env.update({
            "PATH": str(stub_dir) + os.pathsep + self.env.get("PATH", ""),
            "RUN_SH_TEST_CALLS": str(self.calls_file),
            "RUN_SH_TEST_EXIT": "0",
        })

    def default_args(self):
        return []

    def invoke(self, image=None, args=None, cwd=None, script=None):
        env = self.env.copy()
        if image is not None:
            env["ADK_TOOLS_IMAGE"] = image
        if args is None:
            args = self.default_args()
        return subprocess.run(
            [str(script or ROOT / self.SCRIPT), *args], env=env, cwd=cwd,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=5, check=False)

    def calls(self):
        if not self.calls_file.exists():
            return []
        return [json.loads(line)
                for line in self.calls_file.read_text().splitlines()]

    def reset(self):
        # Each subtest starts from nothing, so one acceptance fails only its own.
        if self.calls_file.exists():
            self.calls_file.unlink()
        self.remove_created()


class ImageRuleCases(StubbedLauncher):
    """Refusal cases every launcher runs."""

    def assert_refused(self, result, first_line):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, first_line + "\n" + ANSWER)
        # Refused before anything happens: no docker call and nothing on disk.
        self.assertEqual(self.calls(), [])
        self.assert_nothing_created()

    def refused_as_not_an_id(self, image):
        with self.subTest(image=image):
            self.reset()
            self.assert_refused(
                self.invoke(image),
                "error: ADK_TOOLS_IMAGE must be an image ID, sha256:<64 hex> "
                "(got '%s')" % image)

    def unset_line(self):
        return ("error: ADK_TOOLS_IMAGE is unset or empty; %s has no default "
                "image" % Path(self.SCRIPT).name)

    def test_unset_is_refused(self):
        self.assert_refused(self.invoke(), self.unset_line())

    def test_empty_is_refused(self):
        self.assert_refused(self.invoke(""), self.unset_line())

    def test_tag_is_refused(self):
        for image in ("adk-tools:dev", "adk-tools:feature-abc1234", "adk-tools",
                      "adk-tools@" + IMAGE_ID, "localhost/" + HEX[:12]):
            self.refused_as_not_an_id(image)

    def test_short_hex_is_refused(self):
        for image in (HEX[:12], "sha256:" + HEX[:12], HEX[:8],
                      "sha256:" + HEX[:63]):
            self.refused_as_not_an_id(image)

    def test_other_spellings_of_an_id_are_refused(self):
        # Docker takes the bare 64 hex too; the launchers keep the one
        # spelling host-verify.sh takes. The rest are not IDs at all.
        for image in (HEX, "sha256:" + HEX.upper(), "SHA256:" + HEX,
                      IMAGE_ID + "0", IMAGE_ID + "\n", " " + IMAGE_ID,
                      IMAGE_ID + " adk-tools:dev", "sha512:" + HEX * 2):
            self.refused_as_not_an_id(image)


# run.sh's first docker call, before any label read.
ID_READ = ["image", "inspect", "--format", "{{.Id}}", IMAGE_ID]
STORE_READ = ["info", "--format", "{{json .DriverStatus}}"]
# The lines after "no image <ID>": always the first, on the containerd store all four.
NEVER_PULLS = ("       run.sh never pulls; build the image (README.md, Build) and pass its ID, "
               "or load its archive (docker load -i <file>).")
CONTAINERD_LINES = [
    "       This daemon uses the containerd image store, which finds an image loaded from an "
    "archive by another ID.",
    "       A release lock of schema /2 names that ID too (image_manifest): pass it instead, "
    "and no switch is needed.",
    "       If this ID came from a /1 release lock or an image archive, turn that store off and "
    "load the archive again;",
    "       docs/RUN_SH.md (\"When the image is not there\") says how, and that it hides the "
    "store's images until it is back on.",
]
MISSING_IMAGE_HEADING = "## When the image is not there"


def missing_image_blocks():
    """The quoted blocks of docs/RUN_SH.md's section that run.sh's containerd lines
    name, each as its lines."""
    doc = (ROOT / "docs" / "RUN_SH.md").read_text()
    section = doc.split("\n" + MISSING_IMAGE_HEADING + "\n", 1)[1].split("\n## ", 1)[0]
    return [block.splitlines() for block in re.findall(r"^```\n(.*?)\n```$", section,
                                                         re.M | re.S)]
# Read right after the ID, before OpenROAD's: whether the image carries the
# tools in its own store.
CLOSURE_INSIDE_LABEL = "org.ihp.adk-tools.nix-closure-inside"
INSIDE_READ = ["image", "inspect", "--format",
               '{{range $k, $v := .Config.Labels}}{{if eq $k "%s"}}+{{$v}}|{{end}}{{end}}'
               % CLOSURE_INSIDE_LABEL, IMAGE_ID]
IMAGE_LINE = ("run.sh: image %s, as ADK_TOOLS_IMAGE in the container"
              % IMAGE_ID)
OPENROAD_LABEL = "org.ihp.adk-tools.nix-openroad"
LABEL_READ = ["image", "inspect", "--format",
              '{{index .Config.Labels "%s"}}' % OPENROAD_LABEL, IMAGE_ID]
# Read right after OpenROAD's path is known (and only then): the closure the
# image lists for the host's store, and that list's sha256.
HOST_CLOSURE_LABEL = "org.ihp.adk-tools.nix-host-closure"
HOST_CLOSURE_SHA256_LABEL = "org.ihp.adk-tools.nix-host-closure-sha256"
CLOSURE_READS = [["image", "inspect", "--format",
                  '{{index .Config.Labels "%s"}}' % label, IMAGE_ID]
                 for label in (HOST_CLOSURE_LABEL, HOST_CLOSURE_SHA256_LABEL)]
CAPS_LINE = ("run.sh: memory cap 8g (swap off), 4 CPUs "
             "(ADK_MEM_LIMIT, ADK_CPUS)")
NO_OPENROAD_LINE = ("run.sh: no OpenROAD: route stages will refuse "
                    "(set ADK_OPENROAD)")
LIBRELANE_LABEL = "org.ihp.adk-tools.nix-librelane"
LIBRELANE_PYTHON_LABEL = "org.ihp.adk-tools.nix-librelane-python"
SG13G2_PDK_LABEL = "org.ihp.adk-tools.nix-sg13g2-pdk"
COSIM_DEVSHELL_LABEL = "org.ihp.adk-tools.nix-cosim-devshell"
COSIM_PYTHON_LABEL = "org.ihp.adk-tools.nix-cosim-python"
COSIM_IVERILOG_LABEL = "org.ihp.adk-tools.nix-cosim-iverilog"
MIXSIM_NGSPICE_LABEL = "org.ihp.adk-tools.nix-mixsim-ngspice"
MIXSIM_IVERILOG_LABEL = "org.ihp.adk-tools.nix-mixsim-iverilog"
SG13G2_OSDI_LABEL = "org.ihp.adk-tools.nix-sg13g2-osdi"
GTKWAVE_LABEL = "org.ihp.adk-tools.nix-gtkwave"
GAW_LABEL = "org.ihp.adk-tools.nix-gaw"
XSCHEM_LABEL = "org.ihp.adk-tools.nix-xschem"
VAMS_GNUCAP_LABEL = "org.ihp.adk-tools.nix-vams-gnucap"
VAMS_DEVSHELL_LABEL = "org.ihp.adk-tools.nix-vams-devshell"
STORE_READS = [["image", "inspect", "--format",
                '{{index .Config.Labels "%s"}}' % label, IMAGE_ID]
               for label in (LIBRELANE_LABEL, LIBRELANE_PYTHON_LABEL, SG13G2_PDK_LABEL)]
# Read after those, and like them only with a store mounted: the
# co-simulation engine's three, the mixed-signal engine's three, the
# viewers' three, then the Verilog-AMS engine's two.
COSIM_READS = [["image", "inspect", "--format",
                '{{index .Config.Labels "%s"}}' % label, IMAGE_ID]
               for label in (COSIM_DEVSHELL_LABEL, COSIM_PYTHON_LABEL,
                             COSIM_IVERILOG_LABEL,
                             MIXSIM_NGSPICE_LABEL, MIXSIM_IVERILOG_LABEL,
                             SG13G2_OSDI_LABEL, GTKWAVE_LABEL, GAW_LABEL,
                             XSCHEM_LABEL, VAMS_DEVSHELL_LABEL, VAMS_GNUCAP_LABEL)]
# Read last, and only when ADK_NIX_LIBRELANE goes in: the launcher's sha256
# and the sha256 of the LibreLane wrapper it runs.
LAUNCHER = "/opt/adk-tools/librelane-launcher/librelane"
LAUNCHER_SHA256_LABEL = "org.ihp.adk-tools.librelane-launcher-sha256"
LIBRELANE_WRAPPER_SHA256_LABEL = "org.ihp.adk-tools.nix-librelane-wrapper-sha256"
LAUNCHER_READS = [["image", "inspect", "--format",
                   '{{index .Config.Labels "%s"}}' % label, IMAGE_ID]
                  for label in (LAUNCHER_SHA256_LABEL, LIBRELANE_WRAPPER_SHA256_LABEL)]
# With no OpenROAD there is no store mounted: none of the three is read.
NO_STORE_LINES = [
    "run.sh: no %s: no Nix store is mounted (no OpenROAD); a page that "
    "builds a die will refuse" % var
    for var in ("ADK_NIX_LIBRELANE", "ADK_NIX_LIBRELANE_PYTHON", "ADK_NIX_SG13G2_PDK")]
NO_DISPLAY_LINE = ("run.sh: no display: GUI tools will not open "
                   "(set ADK_DISPLAY=:N)")
# A fake xauth: `-q -f SRC nlist :N` prints one entry when SRC holds the line
# cookie:N (what a real file with a cookie for :N answers), and
# `-q -f DST nmerge -` appends stdin to DST, or fails when told to.
XAUTH_STUB = """#!/usr/bin/python3
import os
import sys
_, _, _, path, verb, arg = sys.argv  # xauth -q -f PATH VERB ARG
if verb == "nlist":
    with open(path) as f:
        if "cookie" + arg + "\\n" in f.read():
            print("0100 0007 3232383035423 0002 3138 0012 "
                  "4d49542d4d414749432d434f4f4b49452d31 0010 00112233")
elif verb == "nmerge":
    if os.environ.get("RUN_SH_TEST_XAUTH_FAIL"):
        sys.exit(1)
    with open(path, "a") as f:
        f.write(sys.stdin.read())
"""


def pairs(argv):
    """The argv's (flag, value) pairs, for -v and -e."""
    return [(argv[i], argv[i + 1]) for i in range(len(argv) - 1)
            if argv[i] in ("-v", "-e")]


class RunShEnv(StubbedLauncher):
    """run.sh with no display, no OpenROAD and no caps from the caller."""

    SCRIPT = "run.sh"

    def setUp(self):
        super().setUp()
        # run.sh binds the work dir by its physical path.
        self.work = self.base.resolve() / "work dir"
        self.tmpdir = self.base / "tmp"
        self.tmpdir.mkdir()
        self.home = self.base / "home"
        self.home.mkdir()
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        # A socket path must fit in 108 bytes, so the X11 dir is short.
        self.x11 = Path(tempfile.mkdtemp(prefix="x11 ", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.x11, True)
        # No display and no terminal: the argv then carries neither the X11
        # mounts nor -t, whatever the shell running the suite has.
        for name in ("ADK_MEM_LIMIT", "ADK_CPUS", "ADK_OPENROAD",
                     "ADK_DISPLAY", "DISPLAY", "XAUTHORITY", "ADK_NETWORK",
                     "CDPATH", "INTERCONNECT_PDK_ROOT"):
            self.env.pop(name, None)
        for name in [n for n in self.env if n.startswith("CHIPLET_FLOW_")]:
            del self.env[name]
        self.env.update({
            "ADK_WORK": str(self.work),
            "TMPDIR": str(self.tmpdir),
            "HOME": str(self.home),
            "ADK_X11_DIR": str(self.x11),
            "ADK_THINLINC_SESSIONS": str(self.sessions),
            "RUN_SH_TEST_COOKIE": str(self.base / "cookie seen"),
        })

    def assert_nothing_created(self):
        # No work dir and no passwd/group dir left behind.
        self.assertFalse(self.work.exists())
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def remove_created(self):
        shutil.rmtree(self.work, ignore_errors=True)

    @contextlib.contextmanager
    def terminal(self):
        """A pty: its primary stays open for the test, its secondary for the block."""
        primary, secondary = os.openpty()
        self.addCleanup(os.close, primary)
        try:
            yield primary, secondary
        finally:
            os.close(secondary)

    def work_line(self):
        return "run.sh: /work is %s" % self.work

    def fixed_lines(self, display=NO_DISPLAY_LINE):
        """The three notes every run opens with, in their order."""
        return [self.work_line(), IMAGE_LINE, display]

    def run_argv(self, result, code=0):
        """The argv of the one docker run, after the ID and label reads."""
        self.assertEqual(result.returncode, code, result.stderr)
        calls = self.calls()
        self.assertEqual([c[0] for c in calls].count("run"), 1, calls)
        self.assertEqual(calls[-1][0], "run")
        return calls[-1]


class RunShImageTests(RunShEnv, ImageRuleCases, unittest.TestCase):

    def test_valid_id_is_accepted(self):
        result = self.invoke(IMAGE_ID)
        self.run_argv(result)
        self.assertEqual(result.stderr, "\n".join(
            [*self.fixed_lines(), CAPS_LINE, NO_OPENROAD_LINE,
             *NO_STORE_LINES]) + "\n")
        self.assertEqual(self.calls()[:3], [ID_READ, INSIDE_READ, LABEL_READ])
        self.assertEqual(len(self.calls()), 4)
        self.assertTrue(self.work.is_dir())

    def test_the_container_gets_the_id(self):
        # The stub runs the command with the -e variables only, so the value
        # is the one docker run was given, not run.sh's own environment.
        self.env["RUN_SH_TEST_EXEC"] = "1"
        result = self.invoke(
            IMAGE_ID, ["sh", "-c", 'printf "%s\\n" "$ADK_TOOLS_IMAGE"'])
        argv = self.run_argv(result)
        self.assertEqual(result.stdout, IMAGE_ID + "\n")
        self.assertEqual([v for f, v in pairs(argv)
                          if v.startswith("ADK_TOOLS_IMAGE=")],
                         ["ADK_TOOLS_IMAGE=" + IMAGE_ID])

    def assert_id_refused(self, line):
        result = self.invoke(IMAGE_ID)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, line + "\n")
        self.assertEqual(self.calls(), [ID_READ])
        self.assert_nothing_created()

    def test_a_daemon_error_is_refused_with_its_first_line(self):
        error = ("Cannot connect to the Docker daemon at unix:///run/x.sock. "
                 "Is the docker daemon running?")
        self.env["RUN_SH_TEST_NO_DAEMON"] = error
        self.assert_id_refused(
            "error: docker image inspect %s failed: %s" % (IMAGE_ID, error))

    def test_another_id_from_the_daemon_is_refused(self):
        for answer in ("sha256:" + "f" * 64, "", IMAGE_ID + " "):
            with self.subTest(answer=answer):
                self.reset()
                self.env["RUN_SH_TEST_ID"] = answer
                self.assert_id_refused(
                    "error: docker image inspect %s gave another ID: '%s'"
                    % (IMAGE_ID, answer))

    def test_a_docker_warning_before_the_id_is_passed_over(self):
        self.env["RUN_SH_TEST_WARNING"] = (
            "WARNING: Error loading config file: open /x/config.json: "
            "permission denied")
        result = self.invoke(IMAGE_ID)
        self.run_argv(result)
        self.assertEqual(result.stderr.splitlines()[:3], self.fixed_lines())

    def test_work_is_bound_by_its_absolute_path(self):
        # docker takes a bare name in -v as a named volume. A relative
        # ADK_WORK is the directory under the caller's, bound by its absolute
        # path, and named once on stderr. CDPATH names a decoy that a bare
        # `cd work` would enter instead.
        caller = self.base / "caller dir"
        (self.base / "decoy" / "work").mkdir(parents=True)
        caller.mkdir()
        self.env["CDPATH"] = str(self.base / "decoy")
        here = caller.resolve() / "work"
        for value, work in (("work", here), ("./work", here),
                            (str(self.work), self.work),
                            (None, self.home.resolve() / "adk-work")):
            with self.subTest(ADK_WORK=value):
                self.reset()
                shutil.rmtree(here, ignore_errors=True)
                if value is None:
                    del self.env["ADK_WORK"]
                else:
                    self.env["ADK_WORK"] = value
                result = self.invoke(IMAGE_ID, cwd=caller)
                argv = self.run_argv(result)
                self.assertEqual([v for f, v in pairs(argv)
                                  if v.endswith(":/work")],
                                 ["%s:/work" % work])
                self.assertTrue(work.is_dir())
                lines = result.stderr.splitlines()
                self.assertEqual(lines[0], "run.sh: /work is %s" % work)
                self.assertEqual(
                    [l for l in lines if l.startswith("run.sh: /work")],
                    lines[:1])
        self.assertEqual(list((self.base / "decoy" / "work").iterdir()), [])

    def clone_copy(self):
        """run.sh copied into a clone under the temp tree, so every work dir
        tried against it is the temp tree's."""
        clone = self.base / "src" / "adk-tools"
        clone.mkdir(parents=True)
        shutil.copy2(ROOT / "run.sh", clone / "run.sh")
        return clone

    def assert_work_refused(self, clone, cases, verb, hint):
        real = clone.resolve()
        for value, cwd, work in cases:
            with self.subTest(ADK_WORK=value, cwd=cwd):
                self.reset()
                self.env["ADK_WORK"] = value
                result = self.invoke(IMAGE_ID, cwd=cwd,
                                     script=clone / "run.sh")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, (
                    "error: ADK_WORK (%s) %s this adk-tools clone (%s)\n"
                    "       %s\n") % (work, verb, real, hint))
                # Refused before anything is made, in the clone or beside it.
                self.assertEqual(self.calls(), [ID_READ, INSIDE_READ, LABEL_READ])
                self.assertEqual(list(self.tmpdir.iterdir()), [])
                self.assertEqual([p.name for p in clone.iterdir()],
                                 ["run.sh"])
                self.assertEqual([p.name for p in clone.parent.iterdir()],
                                 ["adk-tools"])

    def test_a_work_dir_that_holds_the_clone_is_refused(self):
        # A symlink to a parent is a parent; a sibling whose name the
        # clone's extends is not.
        clone = self.clone_copy()
        (self.base / "link").symlink_to(self.base / "src")
        real = clone.resolve()
        self.assert_work_refused(clone, (
            (str(clone), None, real),
            (str(clone.parent), None, real.parent),
            ("/", None, Path("/")),
            (".", clone, real),
            ("..", clone, real.parent),
            (str(self.base / "link"), None, real.parent)),
            "holds", "Keep the clone outside the work dir, or point "
            "ADK_WORK elsewhere.")
        sibling = clone.parent / "adk-tool"
        self.reset()
        self.env["ADK_WORK"] = str(sibling)
        argv = self.run_argv(self.invoke(IMAGE_ID, script=clone / "run.sh"))
        self.assertIn(("-v", "%s:/work" % sibling.resolve()), pairs(argv))

    def test_a_work_dir_inside_the_clone_is_refused(self):
        # Checked before the mkdir: none of these is made. A symlink into
        # the clone leads inside it.
        clone = self.clone_copy()
        (self.base / "into").symlink_to(clone)
        real = clone.resolve()
        self.assert_work_refused(clone, (
            ("work", clone, real / "work"),
            ("./sub", clone, real / "sub"),
            (str(clone / "runs" / "a"), None, real / "runs" / "a"),
            (str(self.base / "into" / "w"), None, real / "w")),
            "is inside", "A git clean in the clone would delete it; point "
            "ADK_WORK outside the clone.")

    def test_id_reaches_docker_run_unchanged(self):
        args = ["adk-smoke", "", "two words", "$HOME", "adk-tools:dev"]
        self.env["RUN_SH_TEST_EXIT"] = "37"
        argv = self.run_argv(self.invoke(IMAGE_ID, args), 37)
        [passwd] = [v for f, v in pairs(argv) if v.endswith(":/etc/passwd:ro")]
        etc = Path(passwd.split(":", 1)[0]).parent
        self.assertEqual(etc.parent, self.tmpdir)
        self.assertTrue(etc.name.startswith("adk-tools-etc."))
        self.assertEqual(argv, [
            "run", "--rm",
            "--memory", "8g", "--memory-swap", "8g", "--cpus", "4",
            "--user", "%d:%d" % (os.getuid(), os.getgid()),
            "-e", "HOME=/tmp",
            "-e", "ADK_TOOLS_IMAGE=%s" % IMAGE_ID,
            "-v", "%s/passwd:/etc/passwd:ro" % etc,
            "-v", "%s/group:/etc/group:ro" % etc,
            "-v", "%s:/work" % self.work,
            "-w", "/work",
            "-i",
            IMAGE_ID, *args])
        self.assertEqual(argv.count(IMAGE_ID), 1)
        self.assertFalse(etc.exists())

    def test_a_hostslot_run_labels_its_container_with_the_slot(self):
        argv = self.run_argv(self.invoke(IMAGE_ID))
        self.assertNotIn("--label", argv)
        self.reset()
        self.env["HOSTSLOT_ID"] = "20261029T200000Z-42"
        argv = self.run_argv(self.invoke(IMAGE_ID))
        i = argv.index("--label")
        self.assertEqual(argv[i + 1], "org.ihp.hostslot=20261029T200000Z-42")
        self.assertLess(i, argv.index(IMAGE_ID))

    def test_caps_come_from_the_caller(self):
        self.env.update({"ADK_MEM_LIMIT": "16g", "ADK_CPUS": "2"})
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertEqual(argv[2:8], ["--memory", "16g", "--memory-swap",
                                     "16g", "--cpus", "2"])
        self.assertIn("run.sh: memory cap 16g (swap off), 2 CPUs",
                      result.stderr)

    def test_an_unset_host_profile_passes_nothing(self):
        argv = self.run_argv(self.invoke(IMAGE_ID))
        self.assertFalse([a for a in argv if "CHIPLET_FLOW_HOST" in a])

    def test_a_set_host_profile_is_passed_as_it_is(self):
        for value in ("dedicated", "", "Dedicated yes"):
            with self.subTest(value=value):
                self.reset()
                self.env["CHIPLET_FLOW_HOST"] = value
                argv = self.run_argv(self.invoke(IMAGE_ID))
                self.assertEqual([a for a in argv if "CHIPLET_FLOW_HOST" in a],
                                 ["CHIPLET_FLOW_HOST=" + value])
                i = argv.index("CHIPLET_FLOW_HOST=" + value)
                self.assertEqual(argv[i - 1], "-e")
                self.assertEqual(argv[2:8], ["--memory", "8g", "--memory-swap", "8g",
                                             "--cpus", "4"])

    def test_a_set_die_project_is_passed_as_it_is(self):
        for args in ([], ["--batch", "true"]):
            with self.subTest(args=args):
                self.reset()
                self.env["CHIPLET_FLOW_DIE_PROJECT"] = "/work/die a/project"
                argv = self.run_argv(self.invoke(IMAGE_ID, args))
                self.assertEqual([a for a in argv if "CHIPLET_FLOW_" in a],
                                 ["CHIPLET_FLOW_DIE_PROJECT=/work/die a/project"])
                i = argv.index("CHIPLET_FLOW_DIE_PROJECT=/work/die a/project")
                self.assertEqual(argv[i - 1], "-e")

    def test_an_unset_die_project_passes_nothing(self):
        argv = self.run_argv(self.invoke(IMAGE_ID, ["--batch", "true"]))
        self.assertFalse([a for a in argv if "CHIPLET_FLOW_" in a])

    def test_only_the_listed_names_are_passed(self):
        self.env.update({
            "CHIPLET_FLOW_HOST": "dedicated",
            "CHIPLET_FLOW_DIE_PROJECT": "/work/p",
            "CHIPLET_FLOW_READY_DIES": "/work/ready",
            "CHIPLET_FLOW_THREAD_CAP": "2",
            "CHIPLET_FLOW_DIE_PROJECTS": "/work/q",
            "CHIPLET_FLOW_": "x",
            "INTERCONNECT_PDK_ROOT": "/work/pdk",
        })
        argv = self.run_argv(self.invoke(IMAGE_ID, ["--batch", "true"]))
        passed = [v for k, v in pairs(argv) if k == "-e"]
        self.assertEqual([v for v in passed if v.startswith(("CHIPLET_FLOW_", "INTERCONNECT_"))],
                         ["CHIPLET_FLOW_HOST=dedicated", "CHIPLET_FLOW_DIE_PROJECT=/work/p"])
        self.assertFalse([a for a in argv if "READY_DIES" in a or "THREAD_CAP" in a
                          or "PROJECTS" in a or "INTERCONNECT_PDK_ROOT" in a])

    def test_network_none_runs_with_no_network(self):
        self.env["ADK_NETWORK"] = "none"
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertEqual(argv[6:9], ["--cpus", "4", "--network=none"])
        self.assertEqual(argv.count("--network=none"), 1)
        self.assertEqual(result.stderr, "\n".join(
            [*self.fixed_lines(), CAPS_LINE, NO_OPENROAD_LINE,
             *NO_STORE_LINES, "run.sh: no network (ADK_NETWORK=none)"]) + "\n")

    def test_an_empty_network_is_the_default(self):
        self.env["ADK_NETWORK"] = ""
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertFalse([a for a in argv if a.startswith("--network")])
        self.assertEqual(result.stderr, "\n".join(
            [*self.fixed_lines(), CAPS_LINE, NO_OPENROAD_LINE,
             *NO_STORE_LINES]) + "\n")

    def test_any_other_network_is_refused(self):
        # Refused right after the ID read: no label read and nothing on disk.
        for network in ("host", "bridge", "None", "none ", " none",
                        "--network=host", "none\nhost"):
            with self.subTest(network=network):
                self.reset()
                self.env["ADK_NETWORK"] = network
                result = self.invoke(IMAGE_ID)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    result.stderr,
                    "error: ADK_NETWORK must be none or unset (got '%s')\n"
                    % network)
                self.assertEqual(self.calls(), [ID_READ])
                self.assert_nothing_created()

    def test_a_kill_names_the_memory_cap(self):
        self.env["RUN_SH_TEST_EXIT"] = "137"
        result = self.invoke(IMAGE_ID)
        self.run_argv(result, 137)
        self.assertTrue(result.stderr.endswith(
            "run.sh: exit 137: the container was killed (SIGKILL); if the "
            "memory cap (8g) did it, raise ADK_MEM_LIMIT\n"), result.stderr)
        self.assertEqual(list(self.tmpdir.iterdir()), [])


PIN_SET_LABEL = "org.ihp.adk-tools.pin-set"
LOCK_COMMIT = "c" * 40
LOCK_PIN_SET = "d" * 64


def lock_text(**values):
    """A release lock in its one byte form, canonical JSON."""
    lock = {"adk_tools": LOCK_COMMIT, "image": IMAGE_ID, "pin_set": LOCK_PIN_SET,
            "schema": "adk-examples.release-lock/1"}
    lock.update(values)
    lock = {k: v for k, v in lock.items() if v is not None}
    return json.dumps(lock, indent=2, sort_keys=True) + "\n"


def lock_form_cases():
    """(name, text) of locks not in the one form. tests/adk_vscode runs the
    same table through bin/adk-vscode, which reads the lock as run.sh does."""
    canonical = lock_text()
    return (
        ("a missing key", lock_text(pin_set=None)),
        ("an extra key", lock_text(note="x")),
        ("a short commit", lock_text(adk_tools="c" * 7)),
        ("a manifest without its prefix", lock_text(image_manifest="f" * 64,
                                                    schema="adk-examples.release-lock/2")),
        ("an image without its prefix", lock_text(image="e" * 64)),
        ("a prefixed pin set", lock_text(pin_set="sha256:" + LOCK_PIN_SET)),
        ("no final newline", canonical[:-1]),
        ("compact JSON", json.dumps(json.loads(canonical), sort_keys=True) + "\n"),
        ("unsorted keys", json.dumps(dict(reversed(json.loads(canonical).items())),
                                     indent=2) + "\n"),
        ("CRLF", canonical.replace("\n", "\r\n")),
        ("empty", ""))


class RunShLockTests(RunShEnv, unittest.TestCase):
    """--examples DIR and --lock FILE: the image is held to the lock."""

    def setUp(self):
        super().setUp()
        self.examples = self.base.resolve() / "examples repo"
        self.examples.mkdir()
        self.lock = self.examples / "release-lock.json"
        self.lock.write_text(lock_text())
        self.env["RUN_SH_TEST_LABEL_PIN_SET"] = LOCK_PIN_SET

    def mounts(self, argv):
        return [v for f, v in pairs(argv) if f == "-v" and v.endswith(":/examples:ro")]

    def assert_lock_refused(self, result, why):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("error: the release lock ", result.stderr)
        self.assertIn(why, result.stderr)
        self.assertNotIn("run", [c[0] for c in self.calls()])
        self.assert_nothing_created()

    def test_the_locked_image_runs_with_the_examples_read_only(self):
        result = self.invoke(IMAGE_ID, ["--examples", str(self.examples), "true"])
        argv = self.run_argv(result)
        self.assertEqual(self.mounts(argv), ["%s:/examples:ro" % self.examples])
        self.assertEqual(argv[-2:], [IMAGE_ID, "true"])
        self.assertIn("run.sh: examples %s at /examples (read-only), the image held to %s\n"
                      % (self.examples, self.lock), result.stderr)
        self.assertIn(["image", "inspect", "--format",
                       '{{index .Config.Labels "%s"}}' % PIN_SET_LABEL, IMAGE_ID], self.calls())

    def test_a_lock_alone_checks_the_image_and_mounts_nothing(self):
        other = self.base / "override.json"
        other.write_text(lock_text())
        argv = self.run_argv(self.invoke(IMAGE_ID, ["--lock", str(other), "true"]))
        self.assertEqual(self.mounts(argv), [])
        self.assertEqual(argv[-2:], [IMAGE_ID, "true"])

    def test_a_lock_given_with_the_examples_replaces_theirs(self):
        self.lock.write_text(lock_text(image="sha256:" + "e" * 64))
        other = self.base / "override.json"
        other.write_text(lock_text())
        result = self.invoke(
            IMAGE_ID, ["--examples", str(self.examples), "--lock", str(other), "true"])
        self.assertEqual(self.mounts(self.run_argv(result)), ["%s:/examples:ro" % self.examples])
        # The examples' own lock names another image; the one given is read instead.
        self.assertNotIn("is not the reference image", result.stderr)

    def test_without_the_options_no_label_of_the_lock_is_read(self):
        self.run_argv(self.invoke(IMAGE_ID, ["true"]))
        self.assertFalse([c for c in self.calls() if PIN_SET_LABEL in " ".join(c)])

    def other_image_note(self, reference):
        return ("run.sh: the image %s is not the reference image %s the release lock %s names; "
                "it runs held to the lock's pin set\n" % (IMAGE_ID, reference, self.lock))

    def test_another_image_runs_with_a_note(self):
        reference = "sha256:" + "e" * 64
        self.lock.write_text(lock_text(image=reference))
        result = self.invoke(IMAGE_ID, ["--examples", str(self.examples), "true"])
        self.assertEqual(self.run_argv(result)[-2:], [IMAGE_ID, "true"])
        self.assertEqual(result.stderr.count(self.other_image_note(reference)), 1, result.stderr)

    def test_the_reference_image_runs_without_the_note(self):
        result = self.invoke(IMAGE_ID, ["--examples", str(self.examples), "true"])
        self.run_argv(result)
        self.assertNotIn("is not the reference image", result.stderr)

    def test_another_image_of_another_pin_set_is_refused(self):
        self.lock.write_text(lock_text(image="sha256:" + "e" * 64))
        self.env["RUN_SH_TEST_LABEL_PIN_SET"] = "f" * 64
        result = self.invoke(IMAGE_ID, ["--examples", str(self.examples)])
        self.assert_lock_refused(result, "names the pin set %s; the image's %s label is '%s'"
                                 % (LOCK_PIN_SET, PIN_SET_LABEL, "f" * 64))
        # A refusal prints no notes, the other-image note included.
        self.assertNotIn("is not the reference image", result.stderr)

    def test_another_pin_set_label_is_refused(self):
        for label in ("f" * 64, ""):
            with self.subTest(label=label):
                self.reset()
                self.env["RUN_SH_TEST_LABEL_PIN_SET"] = label
                self.assert_lock_refused(
                    self.invoke(IMAGE_ID, ["--examples", str(self.examples)]),
                    "names the pin set %s; the image's %s label is '%s'"
                    % (LOCK_PIN_SET, PIN_SET_LABEL, label))

    def test_another_schema_is_refused(self):
        self.lock.write_text(lock_text(schema="adk-examples.release-lock/3"))
        self.assert_lock_refused(self.invoke(IMAGE_ID, ["--examples", str(self.examples)]),
                                 "is of schema 'adk-examples.release-lock/3'; run.sh reads "
                                 "adk-examples.release-lock/1 without image_manifest and "
                                 "adk-examples.release-lock/2 with it")

    def test_a_schema_2_lock_holds_the_image_by_either_digest(self):
        other = "sha256:" + "f" * 64
        for image, manifest in ((IMAGE_ID, other), (other, IMAGE_ID)):
            with self.subTest(image=image):
                self.reset()
                self.lock.write_text(lock_text(image=image, image_manifest=manifest,
                                               schema="adk-examples.release-lock/2"))
                argv = self.run_argv(self.invoke(IMAGE_ID, ["--examples", str(self.examples),
                                                            "true"]))
                self.assertEqual(argv[-2:], [IMAGE_ID, "true"])
        self.reset()
        self.lock.write_text(lock_text(image=other, image_manifest="sha256:" + "e" * 64,
                                       schema="adk-examples.release-lock/2"))
        result = self.invoke(IMAGE_ID, ["--examples", str(self.examples), "true"])
        self.run_argv(result)
        self.assertIn(self.other_image_note("%s (on the containerd store sha256:%s)"
                                            % (other, "e" * 64)), result.stderr)

    def test_the_manifest_key_and_the_schema_go_together(self):
        two = "adk-examples.release-lock/2"
        for text, why in (
                (lock_text(image_manifest="sha256:" + "f" * 64),
                 "reads adk-examples.release-lock/1 without image_manifest"),
                (lock_text(schema=two), "and %s with it" % two),
                (lock_text(image_manifest=IMAGE_ID, schema=two),
                 "names %s as both image and image_manifest" % IMAGE_ID)):
            with self.subTest(why=why):
                self.reset()
                self.lock.write_text(text)
                self.assert_lock_refused(
                    self.invoke(IMAGE_ID, ["--examples", str(self.examples)]), why)

    def test_a_lock_of_another_form_is_refused(self):
        for name, text in lock_form_cases():
            with self.subTest(name=name):
                self.reset()
                self.lock.write_bytes(text.encode())
                self.assert_lock_refused(
                    self.invoke(IMAGE_ID, ["--examples", str(self.examples)]),
                    "is not a release lock in its one form")

    def test_a_missing_lock_or_examples_dir_is_refused(self):
        self.lock.unlink()
        self.assert_lock_refused(self.invoke(IMAGE_ID, ["--examples", str(self.examples)]),
                                 "release-lock.json is not a file")
        self.reset()
        result = self.invoke(IMAGE_ID, ["--examples", str(self.base / "none")])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("is not a directory", result.stderr)
        self.assertEqual(self.calls(), [ID_READ])
        self.assert_nothing_created()

    def test_an_option_without_its_value_is_refused_before_docker(self):
        for args in (["--examples"], ["--lock"], ["--examples", ""]):
            with self.subTest(args=args):
                self.reset()
                result = self.invoke(IMAGE_ID, args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, "error: %s needs a value\n" % args[0])
                self.assertEqual(self.calls(), [])

    def test_the_options_only_lead_the_command(self):
        # After the first word that is not one of them, the rest is the command's.
        argv = self.run_argv(self.invoke(IMAGE_ID, ["echo", "--examples", "x"]))
        self.assertEqual(argv[-3:], ["echo", "--examples", "x"])
        self.assertEqual(self.mounts(argv), [])


class RunShOverlayTests(RunShEnv, unittest.TestCase):
    """ADK_STAGING_OVERLAY: a checkout mounted read-only over a staged tree."""

    def setUp(self):
        super().setUp()
        self.env.pop("ADK_STAGING_OVERLAY", None)
        self.env.pop("ADK_CI", None)
        self.env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        # The suite's base path has a space in it, which an overlay refuses.
        self.repos = Path(tempfile.mkdtemp(prefix="run-sh-overlay-")).resolve()
        self.addCleanup(shutil.rmtree, self.repos, True)
        self.pf = self.checkout("pf")

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def checkout(self, name):
        """A clone with one commit: a tracked file and a tracked file its
        .gitignore names, which a tree of the checkout has to keep."""
        repo = self.repos / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        (repo / "code.py").write_text("x = 1\n")
        (repo / "kept.txt").write_text("tracked, and ignored\n")
        (repo / ".gitignore").write_text("kept.txt\n")
        self.git(repo, "add", "-f", "code.py", "kept.txt", ".gitignore")
        self.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
        return repo

    def overlay_run(self, value, args=("true",)):
        self.env["ADK_STAGING_OVERLAY"] = value
        result = self.invoke(IMAGE_ID, list(args))
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = [c for c in self.calls() if c[0] == "run"]
        self.assertEqual(runs[-1][-1:], list(args)[-1:])
        return result, runs

    def assert_overlay_refused(self, why):
        result = self.invoke(IMAGE_ID, ["true"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("error: ADK_STAGING_OVERLAY: ", result.stderr)
        self.assertIn(why, result.stderr)
        self.assertFalse([c for c in self.calls() if c[0] == "run" and "--inner" not in c])
        self.assert_nothing_created()
        return result

    def test_an_overlay_is_mounted_read_only_and_named(self):
        tree = self.git(self.pf, "rev-parse", "HEAD^{tree}")
        result, runs = self.overlay_run("packaging-flow=%s" % self.pf)
        check, launch = runs
        self.assertEqual(check[-2:], ["--inner", "packaging-flow"])
        self.assertIn("%s:/opt/adk-tools/staging/packaging-flow:ro" % self.pf, check)
        self.assertIn(("-v", "%s:/opt/adk-tools/staging/packaging-flow:ro" % self.pf),
                      list(pairs(launch)))
        self.assertIn(("-e", "ADK_STAGING_OVERLAY_TREES=packaging-flow=%s" % tree),
                      list(pairs(launch)))
        self.assertTrue(result.stderr.endswith(
            "run.sh: OVERLAY staging/packaging-flow <- %s (tree %s); this run is NOT the "
            "image's alone\n" % (self.pf, tree)), result.stderr)

    def test_a_dirty_checkout_is_named_by_its_own_tree_and_its_index_is_untouched(self):
        clean = self.git(self.pf, "rev-parse", "HEAD^{tree}")
        (self.pf / "new.py").write_text("y = 2\n")
        (self.pf / "code.py").write_text("x = 3\n")
        index = (self.pf / ".git" / "index").read_bytes()
        _, runs = self.overlay_run("packaging-flow=%s" % self.pf)
        trees = [v for f, v in pairs(runs[-1]) if v.startswith("ADK_STAGING_OVERLAY_TREES=")]
        tree = trees[0].rsplit("=", 1)[1]
        self.assertNotEqual(tree, clean)
        self.assertEqual(sorted(self.git(self.pf, "ls-tree", "--name-only", tree).split()),
                         [".gitignore", "code.py", "kept.txt", "new.py"])
        self.assertEqual(self.git(self.pf, "cat-file", "-p", tree + ":code.py"), "x = 3")
        self.assertEqual((self.pf / ".git" / "index").read_bytes(), index)

    def test_two_overlays_are_named_in_name_order(self):
        spec = self.checkout("spec")
        _, runs = self.overlay_run("packaging-flow=%s\tchiplet-spec=%s" % (self.pf, spec))
        self.assertEqual(len(runs), 3)
        named = [v for f, v in pairs(runs[-1]) if v.startswith("ADK_STAGING_OVERLAY_TREES=")]
        self.assertEqual(named, ["ADK_STAGING_OVERLAY_TREES=chiplet-spec=%s packaging-flow=%s" % (
            self.git(spec, "rev-parse", "HEAD^{tree}"), self.git(self.pf, "rev-parse", "HEAD^{tree}"))])

    def test_without_the_variable_nothing_changes(self):
        self.env["ADK_STAGING_OVERLAY"] = ""
        argv = self.run_argv(self.invoke(IMAGE_ID, ["true"]))
        self.assertFalse([a for a in argv if "staging" in a or "OVERLAY" in a])

    def test_a_variable_of_whitespace_only_is_refused(self):
        self.env["ADK_STAGING_OVERLAY"] = " \t"
        self.assert_overlay_refused("set but names no overlay")

    def test_a_name_that_is_no_staged_tree_is_refused(self):
        self.env["ADK_STAGING_OVERLAY"] = "adk-tools=%s" % self.pf
        self.assert_overlay_refused("'adk-tools' is not a staged tree")

    def test_a_name_given_twice_is_refused(self):
        self.env["ADK_STAGING_OVERLAY"] = "packaging-flow=%s packaging-flow=%s" % (self.pf, self.pf)
        self.assert_overlay_refused("packaging-flow is named twice")

    def test_a_path_with_whitespace_is_refused(self):
        spaced = self.base / "pf checkout"
        spaced.mkdir()
        self.env["ADK_STAGING_OVERLAY"] = "packaging-flow=%s" % spaced
        self.assert_overlay_refused("is not <name>=<path> (a path with whitespace is refused)")

    def test_a_path_that_is_no_checkout_top_is_refused(self):
        plain = self.repos / "plain"
        (plain / "sub").mkdir(parents=True)
        self.env["ADK_STAGING_OVERLAY"] = "packaging-flow=%s" % (plain / "sub")
        self.assert_overlay_refused("is not the top of a git checkout")
        self.env["ADK_STAGING_OVERLAY"] = "packaging-flow=%s" % (self.repos / "absent")
        self.assert_overlay_refused("is not a directory")

    def test_a_worktree_or_borrowed_objects_are_refused(self):
        wt = self.repos / "wt"
        self.git(self.pf, "worktree", "add", "-q", "--detach", str(wt))
        self.env["ADK_STAGING_OVERLAY"] = "packaging-flow=%s" % wt
        self.assert_overlay_refused(".git is not a directory")
        shared = self.repos / "shared"
        subprocess.run(["git", "clone", "-q", "--shared", str(self.pf), str(shared)],
                       env=self.env, check=True)
        self.env["ADK_STAGING_OVERLAY"] = "packaging-flow=%s" % shared
        self.assert_overlay_refused("borrows objects through alternates")

    def test_a_checkout_with_no_commit_is_refused(self):
        empty = self.repos / "empty"
        empty.mkdir()
        self.git(empty, "init", "-q")
        self.env["ADK_STAGING_OVERLAY"] = "packaging-flow=%s" % empty
        self.assert_overlay_refused("has no commit")

    def test_what_the_check_container_refuses_is_refused(self):
        self.env["RUN_SH_TEST_OVERLAY_SAYS"] = ("packaging-flow: packaging_flow-0.1.dist-info: "
                                                "entry points differ")
        self.env["ADK_STAGING_OVERLAY"] = "packaging-flow=%s" % self.pf
        self.assert_overlay_refused("packaging-flow: check_overlay: packaging-flow: "
                                    "packaging_flow-0.1.dist-info: entry points differ")

    def test_a_refusal_keeps_the_whole_stderr_of_the_step_that_refused(self):
        self.env.update({
            "RUN_SH_TEST_OVERLAY_SAYS": "packaging-flow: packaging_flow-0.1.dist-info: setuptools "
                                        "could not read the checkout's project: Please make sure",
            "RUN_SH_TEST_OVERLAY_STDERR": "Traceback (most recent call last):\n"
                                          "ImportError: Cannot import `packaging.licenses`.\n"
                                          "        Please make sure\n",
            "ADK_STAGING_OVERLAY": "packaging-flow=%s" % self.pf})
        stderr = self.assert_overlay_refused("packaging-flow: check_overlay: packaging-flow: "
                                             "packaging_flow-0.1.dist-info: setuptools could "
                                             "not read").stderr
        self.assertIn("Traceback (most recent call last):\n"
                      "ImportError: Cannot import `packaging.licenses`.\n"
                      "        Please make sure\n", stderr)
        self.assertLess(stderr.index("packaging.licenses"),
                        stderr.index("error: ADK_STAGING_OVERLAY: "))

    def test_a_release_lock_refuses_an_overlay_unless_ci(self):
        lock = self.base / "lock.json"
        lock.write_text(lock_text())
        self.env.update({"RUN_SH_TEST_LABEL_PIN_SET": LOCK_PIN_SET,
                         "ADK_STAGING_OVERLAY": "packaging-flow=%s" % self.pf})
        result = self.invoke(IMAGE_ID, ["--lock", str(lock), "true"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("refused under a release lock", result.stderr)
        self.assert_nothing_created()
        self.env["ADK_CI"] = "1"
        self.overlay_run("packaging-flow=%s" % self.pf, args=("--lock", str(lock), "true"))


class RunShStdinTests(RunShEnv, unittest.TestCase):
    """stdin always reaches the container; a TTY only when run.sh has one."""

    def test_a_piped_script_runs_in_the_container(self):
        # A pipe, not a terminal: what a release harness and anyone
        # running `./run.sh bash < script.sh` hand run.sh.
        self.env["RUN_SH_TEST_EXEC"] = "1"
        env = dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID)
        result = subprocess.run(
            [str(ROOT / self.SCRIPT), "bash"], env=env,
            input="echo one\necho two\n", capture_output=True, text=True,
            timeout=5, check=False)
        argv = self.run_argv(result)
        self.assertEqual(result.stdout, "one\ntwo\n")
        self.assertIn("-i", argv)
        self.assertNotIn("-t", argv)

    def test_a_terminal_also_gets_a_tty(self):
        with self.terminal() as (primary, secondary):
            env = dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID)
            result = subprocess.run(
                [str(ROOT / self.SCRIPT)], env=env, stdin=secondary,
                capture_output=True, text=True, timeout=5, check=False)
        argv = self.run_argv(result)
        at = argv.index("-w")
        self.assertEqual(argv[at:at + 5], ["-w", "/work", "-i", "-t", IMAGE_ID])


class RunShBatchTests(RunShEnv, unittest.TestCase):
    """--batch: no stdin, no TTY and no display; --dry-run: the command only."""

    def test_batch_takes_no_stdin_tty_or_display(self):
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(self.x11 / "X5"))
        self.addCleanup(sock.close)
        with self.terminal() as (primary, secondary):
            env = dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID, ADK_DISPLAY=":5")
            result = subprocess.run(
                [str(ROOT / self.SCRIPT), "--batch", "true"], env=env,
                stdin=secondary, capture_output=True, text=True, timeout=5,
                check=False)
        argv = self.run_argv(result)
        at = argv.index("-w")
        self.assertEqual(argv[at:at + 4], ["-w", "/work", IMAGE_ID, "true"])
        self.assertEqual(result.stderr.splitlines()[2], "run.sh: no display: --batch")

    def test_batch_does_not_read_adk_display(self):
        env = dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID, ADK_DISPLAY="not a display")
        result = subprocess.run(
            [str(ROOT / self.SCRIPT), "--batch", "true"], env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            check=False)
        self.run_argv(result)

    def test_batch_display_takes_the_display_and_no_stdin_or_tty(self):
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(self.x11 / "X5"))
        self.addCleanup(sock.close)
        with self.terminal() as (primary, secondary):
            env = dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID, ADK_DISPLAY=":5")
            result = subprocess.run(
                [str(ROOT / self.SCRIPT), "--batch", "--display", "true"], env=env,
                stdin=secondary, capture_output=True, text=True, timeout=5,
                check=False)
        argv = self.run_argv(result)
        self.assertNotIn("-i", argv)
        self.assertNotIn("-t", argv)
        self.assertIn(("-e", "DISPLAY=:5"), pairs(argv))
        self.assertIn(("-v", "%s:/tmp/.X11-unix/X5:ro" % (self.x11 / "X5")), pairs(argv))

    def test_display_without_batch_is_refused(self):
        env = dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID)
        result = subprocess.run(
            [str(ROOT / self.SCRIPT), "--display", "true"], env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "error: --display only goes with --batch; without it "
                                        "the display is taken anyway\n")
        self.assertEqual(self.calls(), [])

    def batch_tty(self, *command, stdin=None, stdout=None, input=None):
        return subprocess.run(
            [str(ROOT / self.SCRIPT), "--batch", "--tty", *command],
            env=dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID), stdin=stdin, stdout=stdout,
            stderr=subprocess.PIPE, input=input, text=True, timeout=5, check=False)

    def test_batch_tty_hands_a_terminal_to_the_command(self):
        self.env["RUN_SH_TEST_EXEC"] = "1"
        with self.terminal() as (primary, secondary):
            os.write(primary, b"yes\n")
            result = self.batch_tty("sh", "-c", 'read a; echo "answer: $a"',
                                    stdin=secondary, stdout=secondary)
        argv = self.run_argv(result)
        at = argv.index("-w")
        self.assertEqual(argv[at:at + 5], ["-w", "/work", "-i", "-t", IMAGE_ID])
        self.assertIn("--cidfile", argv)
        self.assertEqual(result.stderr.splitlines()[2], "run.sh: no display: --batch")
        self.assertIn(b"answer: yes", os.read(primary, 4096))

    def test_batch_tty_without_two_terminals_is_a_batch_call(self):
        # A pipe on either side (a task runner, CI, a redirect) takes neither,
        # and cat ends at once on the empty stdin instead of reading the
        # terminal.
        self.env["RUN_SH_TEST_EXEC"] = "1"
        command = ("sh", "-c", "cat; echo end >&2")
        with self.terminal() as (primary, secondary):
            for name, kwargs in (
                    ("stdout a pipe", dict(stdin=secondary, stdout=subprocess.PIPE)),
                    ("stdin a pipe", dict(input="not for the container\n", stdout=secondary))):
                with self.subTest(name):
                    self.reset()
                    result = self.batch_tty(*command, **kwargs)
                    argv = self.run_argv(result)
                    self.assertNotIn("-i", argv)
                    self.assertNotIn("-t", argv)
                    self.assertEqual(result.stderr.splitlines()[-1], "end")
                    self.assertIn(result.stdout, (None, ""))

    def end_by_signal(self, args, sig, group, raw=False):
        """run.sh at a terminal, sent sig once docker has written the container's ID.

        With raw, the command first puts the terminal in raw mode with no echo,
        as a docker client attached to a TTY does, and the signal waits for it;
        the result is then the exit code and whether echo was back after.
        """
        self.env["RUN_SH_TEST_EXEC"] = "1"
        command = ["sh", "-c", "stty raw -echo; exec sleep 30"] if raw else ["sleep", "30"]
        with self.terminal() as (primary, secondary):
            proc = subprocess.Popen(
                [str(ROOT / self.SCRIPT), *args, *command],
                env=dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID), stdin=secondary,
                stdout=secondary, stderr=subprocess.DEVNULL, start_new_session=True)
            echo = lambda: bool(termios.tcgetattr(secondary)[3] & termios.ECHO)
            try:
                deadline = time.monotonic() + 5
                while not list(self.tmpdir.glob("adk-tools-etc.*/cid")) or (raw and echo()):
                    self.assertLess(time.monotonic(), deadline, "no cidfile or no raw mode")
                    time.sleep(0.02)
                os.killpg(proc.pid, sig) if group else proc.send_signal(sig)
                code = proc.wait(timeout=5)
                return (code, echo()) if raw else code
            finally:
                # The stub's sleep outlives a killed stub; the group goes.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    def test_a_signal_kills_the_client_and_the_container(self):
        # A closing terminal sends its process group a HUP, a Ctrl-C before
        # the TTY is raw an INT, a supervisor run.sh alone a TERM. Each kills
        # the container by its cidfile, at a terminal with --tty or without
        # --batch.
        for args in (("--batch", "--tty"), ()):
            for sig, group, code in ((signal.SIGHUP, True, 129), (signal.SIGINT, True, 130),
                                     (signal.SIGTERM, False, 143)):
                with self.subTest(args=args, sig=sig.name):
                    self.reset()
                    self.assertEqual(self.end_by_signal(args, sig, group), code)
                    self.assertEqual(self.calls()[-1], ["kill", "stub-container"])
                    self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_a_term_gives_the_terminal_back(self):
        # A TERM to run.sh alone leaves the terminal open; the killed client
        # would leave it raw with no echo.
        for args in (("--batch", "--tty"), ()):
            with self.subTest(args=args):
                self.reset()
                self.assertEqual(self.end_by_signal(args, signal.SIGTERM, False, raw=True),
                                 (143, True))

    def test_tty_without_batch_is_refused(self):
        env = dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID)
        result = subprocess.run(
            [str(ROOT / self.SCRIPT), "--tty", "true"], env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
            check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "error: --tty only goes with --batch; without it "
                                        "the terminal is taken anyway\n")
        self.assertEqual(self.calls(), [])

    def test_a_batch_command_reads_an_empty_stdin(self):
        self.env["RUN_SH_TEST_EXEC"] = "1"
        env = dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID)
        result = subprocess.run(
            [str(ROOT / self.SCRIPT), "--batch", "cat"], env=env,
            input="not for the container\n", capture_output=True, text=True,
            timeout=5, check=False)
        self.run_argv(result)
        self.assertEqual(result.stdout, "")

    def cwd_run(self, rel):
        return subprocess.run(
            [str(ROOT / self.SCRIPT), "--batch", "--cwd", rel, "true"],
            env=dict(self.env, ADK_TOOLS_IMAGE=IMAGE_ID), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5, check=False)

    def test_cwd_starts_the_command_below_work(self):
        (self.work / "examples" / "a b").mkdir(parents=True)
        for rel, workdir in ((".", "/work"), ("examples", "/work/examples"),
                             ("examples/a b/", "/work/examples/a b")):
            with self.subTest(rel=rel):
                if self.calls_file.exists():
                    self.calls_file.unlink()
                argv = self.run_argv(self.cwd_run(rel))
                at = argv.index("-w")
                self.assertEqual(argv[at:at + 3], ["-w", workdir, IMAGE_ID])

    def test_cwd_that_leaves_work_is_refused(self):
        self.work.mkdir()
        (self.work / "a").mkdir()
        os.symlink(self.base, self.work / "out")
        for rel, line in (
                ("/a", "error: --cwd /a is absolute; it has to be relative to /work"),
                ("..", "error: --cwd .. has a '..' in it; it has to stay below /work"),
                ("a/../a", "error: --cwd a/../a has a '..' in it; it has to stay below /work"),
                ("out", "error: --cwd out: %s/out is not a directory of ADK_WORK with no "
                        "symlink on the way" % self.work),
                ("b", "error: --cwd b: %s/b is not a directory of ADK_WORK with no "
                      "symlink on the way" % self.work)):
            with self.subTest(rel=rel):
                if self.calls_file.exists():
                    self.calls_file.unlink()
                result = self.cwd_run(rel)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr.splitlines()[-1], line)
                self.assertNotIn("run", [c[0] for c in self.calls()])
        self.assertFalse((self.work / "b").exists())

    def test_dry_run_prints_the_command_and_creates_nothing(self):
        result = self.invoke(IMAGE_ID, ["--dry-run", "echo", "a b"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("run", [c[0] for c in self.calls()])
        line, = result.stdout.splitlines()
        self.assertTrue(line.startswith("docker run --rm --memory 8g "), line)
        self.assertTrue(line.endswith(r" -w /work -i %s echo a\ b" % IMAGE_ID), line)
        self.assertEqual(result.stderr.splitlines()[:3], self.fixed_lines())
        self.assert_nothing_created()


class RunShOpenroadTests(RunShEnv, unittest.TestCase):
    """The OpenROAD the image's label names, or ADK_OPENROAD, mounted ro."""

    def setUp(self):
        super().setUp()
        self.store = self.base / "store"
        self.openroad = self.store / ("0a1b2c3d" * 4 + "-openroad-2026-02-17")
        (self.openroad / "bin").mkdir(parents=True)
        binary = self.openroad / "bin" / "openroad"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

    def mounted(self):
        return [("-v", "%s:%s:ro" % (self.store, self.store)),
                ("-e", "OPENROAD=%s/bin/openroad" % self.openroad)]

    def assert_mounted(self, result, source):
        argv = self.run_argv(result)
        for pair in self.mounted():
            self.assertIn(pair, pairs(argv))
        self.assertIn("run.sh: OpenROAD %s (from %s)\n" % (self.openroad,
                                                          source),
                      result.stderr)

    def assert_refused_openroad(self, result, calls, words):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(words, result.stderr)
        self.assertEqual(self.calls(), [ID_READ, INSIDE_READ, *calls])
        self.assert_nothing_created()

    def test_the_label_names_the_openroad(self):
        self.env["RUN_SH_TEST_LABEL"] = str(self.openroad)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.calls()[:3], [ID_READ, INSIDE_READ, LABEL_READ])
        self.assert_mounted(result, "the image's %s label" % OPENROAD_LABEL)

    def test_adk_openroad_wins_and_reads_no_label(self):
        # Not its own: the LibreLane and PDK labels are still read.
        self.env["RUN_SH_TEST_LABEL"] = str(self.store / ("9" * 32 + "-openroad"))
        self.env["ADK_OPENROAD"] = str(self.openroad)
        result = self.invoke(IMAGE_ID)
        self.assert_mounted(result, "ADK_OPENROAD")
        self.assertEqual(self.calls()[:-1],
                         [ID_READ, INSIDE_READ, *CLOSURE_READS, *STORE_READS, *COSIM_READS])

    def test_an_openroad_not_on_this_host_is_refused(self):
        absent = self.store / ("1" * 32 + "-openroad-2026-02-17")
        not_executable = self.store / ("2" * 32 + "-openroad")
        (not_executable / "bin").mkdir(parents=True)
        (not_executable / "bin" / "openroad").write_text("")
        for path in (absent, not_executable):
            for how in ("label", "ADK_OPENROAD"):
                with self.subTest(path=path.name, how=how):
                    self.reset()
                    env = {"label": "RUN_SH_TEST_LABEL",
                           "ADK_OPENROAD": "ADK_OPENROAD"}[how]
                    self.env.pop("RUN_SH_TEST_LABEL", None)
                    self.env.pop("ADK_OPENROAD", None)
                    self.env[env] = str(path)
                    self.assert_refused_openroad(
                        self.invoke(IMAGE_ID),
                        [LABEL_READ] if how == "label" else [],
                        "no executable %s/bin/openroad" % path)

    def test_a_path_of_another_shape_is_refused(self):
        for path in ("/usr/bin", "relative/%s-openroad" % ("3" * 32),
                     str(self.store / "openroad"),
                     str(self.store / ("4" * 31 + "-openroad")),
                     str(self.openroad) + "/"):
            with self.subTest(path=path):
                self.reset()
                self.env["ADK_OPENROAD"] = path
                self.assert_refused_openroad(
                    self.invoke(IMAGE_ID), [],
                    "is not a <store>/<hash>-openroad path (got '%s')" % path)

    def test_no_label_mounts_nothing(self):
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertFalse([p for p in pairs(argv) if "openroad" in p[1].lower()
                          or p[1].startswith("OPENROAD=")])
        self.assertIn(NO_OPENROAD_LINE + "\n", result.stderr)

    def test_an_image_the_daemon_lacks_creates_nothing(self):
        # Stopped at the ID read, ADK_OPENROAD set or not.
        for openroad in (None, str(self.openroad)):
            with self.subTest(ADK_OPENROAD=openroad):
                self.reset()
                self.env.pop("ADK_OPENROAD", None)
                if openroad:
                    self.env["ADK_OPENROAD"] = openroad
                self.env["RUN_SH_TEST_MISSING"] = IMAGE_ID
                result = self.invoke(IMAGE_ID)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "error: no image %s on this docker daemon\n%s\n"
                                 % (IMAGE_ID, NEVER_PULLS))
                self.assertEqual(self.calls(), [ID_READ, STORE_READ])
                self.assert_nothing_created()

    def test_on_the_containerd_store_the_refusal_adds_the_archive_case(self):
        # docker 29.0.0's DriverStatus with the containerd snapshotter.
        self.env["RUN_SH_TEST_DRIVER_STATUS"] = '[["driver-type","io.containerd.snapshotter.v1"]]'
        self.env["RUN_SH_TEST_MISSING"] = IMAGE_ID
        result = self.invoke(IMAGE_ID)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.splitlines(),
                         ["error: no image %s on this docker daemon" % IMAGE_ID, NEVER_PULLS,
                          *CONTAINERD_LINES])
        self.assertEqual(self.calls(), [ID_READ, STORE_READ])
        self.assert_nothing_created()

    def test_the_doc_the_containerd_lines_name_quotes_them(self):
        # The heading they name is there, and its first quoted block is these lines.
        self.assertIn('("%s")' % MISSING_IMAGE_HEADING[3:], CONTAINERD_LINES[3])
        self.assertEqual(missing_image_blocks()[0], CONTAINERD_LINES)


class RunShStorePathTests(RunShEnv, unittest.TestCase):
    """The LibreLane, its interpreter and the PDK the image's labels name, passed on, never
    refused."""

    def setUp(self):
        super().setUp()
        self.store = self.base / "store"
        openroad = self.store / ("0a1b2c3d" * 4 + "-openroad-2026-02-17")
        (openroad / "bin").mkdir(parents=True)
        (openroad / "bin" / "openroad").write_text("#!/bin/sh\n")
        (openroad / "bin" / "openroad").chmod(0o755)
        self.env["RUN_SH_TEST_LABEL"] = str(openroad)
        self.librelane = self.store / ("8bj13r4a" * 4 + "-python3.13-librelane-3.0.0rc0")
        (self.librelane / "bin").mkdir(parents=True)
        (self.librelane / "bin" / "librelane").write_text("#!/bin/sh\n")
        self.python = self.store / ("jys2hxxp" * 4 + "-python3-3.13.9-env") / "bin" / "python3.13"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("#!/bin/sh\n")
        (self.python.parent / "python3").symlink_to("python3.13")
        self.pdk = self.store / ("hx96lf7n" * 4 + "-ihp-open-pdk-22f4335")
        self.pdk.mkdir()
        (self.pdk / "pdk-source.json").write_text("{}\n")

    def labels(self, librelane, pdk, python=None):
        for key, value in (("RUN_SH_TEST_LABEL_NIX_LIBRELANE", librelane),
                           ("RUN_SH_TEST_LABEL_NIX_LIBRELANE_PYTHON", python),
                           ("RUN_SH_TEST_LABEL_NIX_SG13G2_PDK", pdk)):
            if value is None:
                self.env.pop(key, None)
            else:
                self.env[key] = str(value)

    def passed(self, argv):
        return [v for f, v in pairs(argv) if f == "-e" and v.startswith("ADK_NIX_")]

    def test_all_three_on_this_host_go_in(self):
        self.labels(self.librelane, self.pdk, self.python)
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertEqual(self.calls()[:8],
                         [ID_READ, INSIDE_READ, LABEL_READ, *CLOSURE_READS, *STORE_READS])
        self.assertEqual(self.passed(argv), [
            "ADK_NIX_LIBRELANE=%s" % self.librelane,
            "ADK_NIX_LIBRELANE_PYTHON=%s" % self.python,
            "ADK_NIX_SG13G2_PDK=%s" % self.pdk])
        # Through the OpenROAD mount: no mount of their own.
        self.assertEqual([v for f, v in pairs(argv) if f == "-v"
                          and v.startswith(str(self.store))],
                         ["%s:%s:ro" % (self.store, self.store)])
        self.assertIn("run.sh: ADK_NIX_LIBRELANE %s (from the image's %s label)\n"
                      % (self.librelane, LIBRELANE_LABEL), result.stderr)
        self.assertIn("run.sh: ADK_NIX_SG13G2_PDK %s (from the image's %s label)\n"
                      % (self.pdk, SG13G2_PDK_LABEL), result.stderr)
        self.assertIn("run.sh: ADK_NIX_LIBRELANE_PYTHON %s (from the image's %s label)\n"
                      % (self.python, LIBRELANE_PYTHON_LABEL), result.stderr)

    def test_the_interpreter_is_the_file_itself(self):
        # The env's bin/python3 is a link, and core preparation refuses one;
        # its folder is not the interpreter; and one not on this host is a note.
        link = self.python.parent / "python3"
        absent = self.store / ("1" * 32 + "-python3-3.13.9-env") / "bin" / "python3.13"
        for python, note in (
                (link, "run.sh: no ADK_NIX_LIBRELANE_PYTHON: the image's %s label is not a "
                       "<store>/<hash>-librelane-python path (got '%s')\n"
                       % (LIBRELANE_PYTHON_LABEL, link)),
                (self.python.parent.parent, "run.sh: no ADK_NIX_LIBRELANE_PYTHON: the image's "
                       "%s label is not a <store>/<hash>-librelane-python path (got '%s')\n"
                       % (LIBRELANE_PYTHON_LABEL, self.python.parent.parent)),
                (absent, "run.sh: needs Nix on the host: no ADK_NIX_LIBRELANE_PYTHON, %s is not on this host (nix "
                         "build .#librelane-python in the adk-tools flake); a page that builds "
                         "a die will refuse\n" % absent)):
            with self.subTest(python=str(python)):
                self.reset()
                self.labels(self.librelane, self.pdk, python)
                result = self.invoke(IMAGE_ID)
                self.assertEqual(self.passed(self.run_argv(result)), [
                    "ADK_NIX_LIBRELANE=%s" % self.librelane,
                    "ADK_NIX_SG13G2_PDK=%s" % self.pdk])
                self.assertIn(note, result.stderr)

    def test_an_interpreter_label_that_is_a_link_is_a_note(self):
        # The right name, but a link to the file: not taken.
        target = self.store / ("2" * 32 + "-python3-3.13.9-env") / "bin" / "python3.13"
        target.parent.mkdir(parents=True)
        self.python.unlink()
        self.python.symlink_to(target)
        target.write_text("#!/bin/sh\n")
        self.labels(self.librelane, self.pdk, self.python)
        result = self.invoke(IMAGE_ID)
        self.assertNotIn("ADK_NIX_LIBRELANE_PYTHON=%s" % self.python,
                         self.passed(self.run_argv(result)))
        self.assertIn("run.sh: no ADK_NIX_LIBRELANE_PYTHON: %s is a link, not the file itself; "
                      "a page that builds a die will refuse\n" % self.python, result.stderr)

    def test_not_on_this_host_is_a_note(self):
        absent = self.store / ("1" * 32 + "-python3.13-librelane-3.0.0rc0")
        no_record = self.store / ("2" * 32 + "-ihp-open-pdk-22f4335")
        no_record.mkdir()
        self.labels(absent, no_record)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.passed(self.run_argv(result)), [])
        self.assertIn("run.sh: needs Nix on the host: no ADK_NIX_LIBRELANE, %s is not on this host "
                      "(nix build .#librelane in the adk-tools flake); a page "
                      "that builds a die will refuse\n" % absent, result.stderr)
        self.assertIn("run.sh: needs Nix on the host: no ADK_NIX_SG13G2_PDK, %s is not on this host "
                      "(nix build .#sg13g2-pdk in the adk-tools flake)" % no_record,
                      result.stderr)

    def test_an_image_without_the_labels_is_a_note(self):
        self.labels(None, None)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.passed(self.run_argv(result)), [])
        for var, label in (("ADK_NIX_LIBRELANE", LIBRELANE_LABEL),
                           ("ADK_NIX_SG13G2_PDK", SG13G2_PDK_LABEL)):
            self.assertIn("run.sh: no %s: the image has no %s label; a page "
                          "that builds a die will refuse\n" % (var, label),
                          result.stderr)

    def test_a_path_of_another_shape_is_a_note(self):
        # Each label holds only its own package's name.
        openroad = self.env["RUN_SH_TEST_LABEL"]
        for librelane, pdk in ((openroad, self.librelane),
                               (str(self.librelane) + "/", "relative/" + self.pdk.name),
                               (self.store / "librelane", self.store / ("3" * 31 + "-ihp-open-pdk-22f4335"))):
            with self.subTest(librelane=str(librelane), pdk=str(pdk)):
                self.reset()
                self.labels(librelane, pdk)
                result = self.invoke(IMAGE_ID)
                self.assertEqual(self.passed(self.run_argv(result)), [])
                self.assertIn("run.sh: no ADK_NIX_LIBRELANE: the image's %s label is "
                              "not a <store>/<hash>-librelane path (got '%s')\n"
                              % (LIBRELANE_LABEL, librelane), result.stderr)
                self.assertIn("run.sh: no ADK_NIX_SG13G2_PDK: the image's %s label is "
                              "not a <store>/<hash>-sg13g2-pdk path (got '%s')\n"
                              % (SG13G2_PDK_LABEL, pdk), result.stderr)

    def test_another_store_than_openroads_is_a_note(self):
        elsewhere = self.base / "other store" / self.librelane.name
        (elsewhere / "bin").mkdir(parents=True)
        (elsewhere / "bin" / "librelane").write_text("#!/bin/sh\n")
        self.labels(elsewhere, self.pdk)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.passed(self.run_argv(result)),
                         ["ADK_NIX_SG13G2_PDK=%s" % self.pdk])
        self.assertIn("run.sh: no ADK_NIX_LIBRELANE: %s is not in %s, the store "
                      "mounted for OpenROAD\n" % (elsewhere, self.store), result.stderr)

    def launcher_labels(self, launcher=HEX, wrapper=None):
        if wrapper is None:
            wrapper = hashlib.sha256((self.librelane / "bin" / "librelane").read_bytes()).hexdigest()
        for key, value in (("RUN_SH_TEST_LABEL_LIBRELANE_LAUNCHER_SHA256", launcher),
                           ("RUN_SH_TEST_LABEL_NIX_LIBRELANE_WRAPPER_SHA256", wrapper)):
            if value is None:
                self.env.pop(key, None)
            else:
                self.env[key] = value

    def launcher_pair(self, argv):
        return [v for f, v in pairs(argv)
                if f == "-e" and v.startswith("CHIPLET_FLOW_LIBRELANE_LAUNCHER")]

    def test_the_launcher_goes_in_with_librelane(self):
        self.labels(self.librelane, self.pdk, self.python)
        self.launcher_labels()
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.launcher_pair(self.run_argv(result)), [
            "CHIPLET_FLOW_LIBRELANE_LAUNCHER=%s" % LAUNCHER,
            "CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=%s" % HEX])
        self.assertEqual(self.calls()[-3:-1], LAUNCHER_READS)
        self.assertIn("run.sh: CHIPLET_FLOW_LIBRELANE_LAUNCHER %s (sha256 from the image's %s "
                      "label)\n" % (LAUNCHER, LAUNCHER_SHA256_LABEL), result.stderr)

    def test_another_wrapper_on_this_host_keeps_the_launcher_out(self):
        # The same store path with other bytes, or a link where the wrapper was.
        self.labels(self.librelane, self.pdk, self.python)
        wrapper = self.librelane / "bin" / "librelane"
        note = ("run.sh: no CHIPLET_FLOW_LIBRELANE_LAUNCHER: %s on this host is not the wrapper "
                "the image was built against (its %s label, %s); a page that builds a die "
                "will refuse\n")
        self.launcher_labels(wrapper="f" * 64)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.launcher_pair(self.run_argv(result)), [])
        self.assertIn("ADK_NIX_LIBRELANE=%s" % self.librelane, self.passed(self.run_argv(result)))
        self.assertIn(note % (wrapper, LIBRELANE_WRAPPER_SHA256_LABEL, "f" * 64), result.stderr)
        self.reset()
        self.launcher_labels()
        real = self.librelane / "bin" / "librelane.real"
        wrapper.rename(real)
        wrapper.symlink_to(real.name)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.launcher_pair(self.run_argv(result)), [])
        self.assertIn(note % (wrapper, LIBRELANE_WRAPPER_SHA256_LABEL,
                              self.env["RUN_SH_TEST_LABEL_NIX_LIBRELANE_WRAPPER_SHA256"]),
                      result.stderr)

    def test_an_image_without_a_launcher_is_a_note(self):
        # An image built before the launcher: LibreLane still goes in, alone.
        self.labels(self.librelane, self.pdk, self.python)
        self.launcher_labels(launcher=None)
        self.env.pop("RUN_SH_TEST_LABEL_NIX_LIBRELANE_WRAPPER_SHA256")
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.launcher_pair(self.run_argv(result)), [])
        self.assertIn("ADK_NIX_LIBRELANE=%s" % self.librelane, self.passed(self.run_argv(result)))
        self.assertIn("run.sh: no CHIPLET_FLOW_LIBRELANE_LAUNCHER: the image has no %s label "
                      "and no %s label; a page that builds a die will refuse\n"
                      % (LAUNCHER_SHA256_LABEL, LIBRELANE_WRAPPER_SHA256_LABEL),
                      result.stderr)

    def test_a_missing_label_is_named(self):
        # Each label alone: the note names the one the image lacks, not a
        # sha256 it could not read.
        self.labels(self.librelane, self.pdk, self.python)
        for launcher, wrapper, missing in ((None, None, LAUNCHER_SHA256_LABEL),
                                           (HEX, "", LIBRELANE_WRAPPER_SHA256_LABEL)):
            with self.subTest(missing=missing):
                self.reset()
                self.launcher_labels(launcher, wrapper)
                if wrapper == "":
                    self.env.pop("RUN_SH_TEST_LABEL_NIX_LIBRELANE_WRAPPER_SHA256")
                result = self.invoke(IMAGE_ID)
                self.assertEqual(self.launcher_pair(self.run_argv(result)), [])
                self.assertIn("run.sh: no CHIPLET_FLOW_LIBRELANE_LAUNCHER: the image has no %s "
                              "label; a page that builds a die will refuse\n" % missing,
                              result.stderr)

    def test_labels_that_are_not_a_sha256_are_a_note(self):
        self.labels(self.librelane, self.pdk, self.python)
        for launcher, wrapper in (("abc", None), (HEX.upper(), None), (HEX, "x" * 64)):
            with self.subTest(launcher=launcher, wrapper=wrapper):
                self.reset()
                self.launcher_labels(launcher, wrapper)
                result = self.invoke(IMAGE_ID)
                self.assertEqual(self.launcher_pair(self.run_argv(result)), [])
                self.assertIn("run.sh: no CHIPLET_FLOW_LIBRELANE_LAUNCHER: the image's %s and %s "
                              "labels are not both a sha256; a page that builds a die will "
                              "refuse\n" % (LAUNCHER_SHA256_LABEL, LIBRELANE_WRAPPER_SHA256_LABEL),
                              result.stderr)

    def test_no_librelane_reads_no_launcher_label(self):
        absent = self.store / ("1" * 32 + "-python3.13-librelane-3.0.0rc0")
        self.labels(absent, self.pdk, self.python)
        self.launcher_labels(wrapper="f" * 64)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.launcher_pair(self.run_argv(result)), [])
        for read in LAUNCHER_READS:
            self.assertNotIn(read, self.calls())
        self.assertNotIn("CHIPLET_FLOW_LIBRELANE_LAUNCHER", result.stderr)

    def test_no_openroad_reads_neither_label(self):
        self.env.pop("RUN_SH_TEST_LABEL")
        self.labels(self.librelane, self.pdk)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.passed(self.run_argv(result)), [])
        self.assertEqual(self.calls()[:-1], [ID_READ, INSIDE_READ, LABEL_READ])
        for line in NO_STORE_LINES:
            self.assertIn(line + "\n", result.stderr)



class RunShCosimTests(RunShEnv, unittest.TestCase):
    """A note when the store mounted for OpenROAD hides an engine the host lacks."""

    def setUp(self):
        super().setUp()
        self.store = self.base / "store"
        openroad = self.store / ("0a1b2c3d" * 4 + "-openroad-2026-02-17")
        (openroad / "bin").mkdir(parents=True)
        (openroad / "bin" / "openroad").write_text("#!/bin/sh\n")
        (openroad / "bin" / "openroad").chmod(0o755)
        self.env["RUN_SH_TEST_LABEL"] = str(openroad)
        self.devshell = self.store / ("kcxwxk9f" * 4 + "-cosim-devshell")
        self.python = self.store / ("1czlxfa9" * 4 + "-python3-3.13.9-env")
        self.iverilog = self.store / ("nknbcs56" * 4 + "-iverilog-s20250103-60-gdb82380ce")

    def on_host(self, *paths):
        for path, holds in ((self.devshell, "python3"), (self.python, "python3"),
                            (self.iverilog, "vvp")):
            if path in paths:
                (path / "bin").mkdir(parents=True)
                (path / "bin" / holds).write_text("")

    def labels(self, devshell, python, iverilog):
        for key, value in (("RUN_SH_TEST_LABEL_NIX_COSIM_DEVSHELL", devshell),
                           ("RUN_SH_TEST_LABEL_NIX_COSIM_PYTHON", python),
                           ("RUN_SH_TEST_LABEL_NIX_COSIM_IVERILOG", iverilog)):
            if value is None:
                self.env.pop(key, None)
            else:
                self.env[key] = str(value)

    def cosim_lines(self, result):
        self.run_argv(result)
        return [line for line in result.stderr.splitlines() if "co-simulation" in line]

    def test_an_engine_in_the_mounted_store_says_nothing(self):
        self.on_host(self.devshell, self.python, self.iverilog)
        self.labels(self.devshell, self.python, self.iverilog)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.cosim_lines(result), [])
        self.assertEqual(self.calls()[:-1],
                         [ID_READ, INSIDE_READ, LABEL_READ, *CLOSURE_READS, *STORE_READS, *COSIM_READS])

    def test_an_engine_the_mount_hides_is_a_note(self):
        self.on_host(self.devshell, self.iverilog)
        self.labels(self.devshell, self.python, self.iverilog)
        self.assertEqual(self.cosim_lines(self.invoke(IMAGE_ID)), [
            "run.sh: no co-simulation engine: %s is not on this host, and the "
            "store mounted for OpenROAD hides the image's copy (nix build "
            ".#cosim-python in the adk-tools flake)" % self.python])
        self.reset()
        shutil.rmtree(self.iverilog)
        self.assertEqual(len(self.cosim_lines(self.invoke(IMAGE_ID))), 2)

    def test_a_devshell_the_mount_hides_is_a_note(self):
        self.on_host(self.python, self.iverilog)
        self.labels(self.devshell, self.python, self.iverilog)
        self.assertEqual(self.cosim_lines(self.invoke(IMAGE_ID)), [
            "run.sh: no co-simulation engine: %s is not on this host, and the "
            "store mounted for OpenROAD hides the image's copy (nix build "
            ".#cosim-devshell in the adk-tools flake)" % self.devshell])

    def test_an_engine_in_another_store_than_the_mounted_one_says_nothing(self):
        image_store = self.base / "image store"
        self.labels(image_store / self.devshell.name, image_store / self.python.name,
                    image_store / self.iverilog.name)
        self.assertEqual(self.cosim_lines(self.invoke(IMAGE_ID)), [])

    def test_an_image_without_the_labels_says_nothing(self):
        self.labels(None, None, None)
        self.assertEqual(self.cosim_lines(self.invoke(IMAGE_ID)), [])

    def test_a_path_of_another_shape_is_a_note(self):
        self.labels(self.python, self.iverilog, self.devshell)
        self.assertEqual(self.cosim_lines(self.invoke(IMAGE_ID)), [
            "run.sh: co-simulation: the image's %s label is not a "
            "<store>/<hash>-%s path (got '%s')" % (label, package, path)
            for label, package, path in (
                (COSIM_DEVSHELL_LABEL, "cosim-devshell", self.python),
                (COSIM_PYTHON_LABEL, "cosim-python", self.iverilog),
                (COSIM_IVERILOG_LABEL, "cosim-iverilog", self.devshell))])

    def test_a_mixed_signal_path_the_mount_hides_is_a_note(self):
        ngspice = self.store / ("mjipfrxz" * 4 + "-ngspice-45")
        osdi = self.store / ("anybmg3w" * 4 + "-sg13g2-osdi-22f4335")
        (osdi / "osdi").mkdir(parents=True)
        self.env["RUN_SH_TEST_LABEL_NIX_MIXSIM_NGSPICE"] = str(ngspice)
        self.env["RUN_SH_TEST_LABEL_NIX_SG13G2_OSDI"] = str(osdi)
        result = self.invoke(IMAGE_ID)
        self.run_argv(result)
        self.assertEqual(
            [line for line in result.stderr.splitlines() if "mixed-signal" in line],
            ["run.sh: no mixed-signal engine: %s is not on this host, and the "
             "store mounted for OpenROAD hides the image's copy (nix build "
             ".#mixsim-ngspice in the adk-tools flake)" % ngspice])

    def test_a_viewer_the_mount_hides_is_a_note(self):
        gtkwave = self.store / ("8kygk1ym" * 4 + "-gtkwave-3.3.121")
        gaw = self.store / ("7gjgfdhg" * 4 + "-gaw3-20250128")
        (gaw / "bin").mkdir(parents=True)
        (gaw / "bin" / "gaw").write_text("")
        self.env["RUN_SH_TEST_LABEL_NIX_GTKWAVE"] = str(gtkwave)
        self.env["RUN_SH_TEST_LABEL_NIX_GAW"] = str(gaw)
        result = self.invoke(IMAGE_ID)
        self.run_argv(result)
        self.assertEqual(
            [line for line in result.stderr.splitlines() if "viewer" in line],
            ["run.sh: no gtkwave viewer: %s is not on this host, and the "
             "store mounted for OpenROAD hides the image's copy (nix build "
             ".#gtkwave in the adk-tools flake)" % gtkwave])
        self.reset()
        self.env["RUN_SH_TEST_LABEL_NIX_GAW"] = str(gtkwave)
        result = self.invoke(IMAGE_ID)
        self.run_argv(result)
        self.assertIn("run.sh: gaw: the image's %s label is not a <store>/<hash>-gaw path "
                      "(got '%s')" % (GAW_LABEL, gtkwave), result.stderr.splitlines())
        self.reset()
        self.env["RUN_SH_TEST_LABEL_NIX_XSCHEM"] = str(gaw)
        result = self.invoke(IMAGE_ID)
        self.run_argv(result)
        self.assertIn("run.sh: xschem: the image's %s label is not a <store>/<hash>-xschem path "
                      "(got '%s')" % (XSCHEM_LABEL, gaw), result.stderr.splitlines())

    def test_a_verilog_ams_engine_the_mount_hides_is_a_note(self):
        shell = self.store / ("1dlfq2bb" * 4 + "-vams-devshell")
        gnucap = self.store / ("yawvmxra" * 4 + "-gnucap-b6678fe-modelgen-verilog-100e746")
        (gnucap / "bin").mkdir(parents=True)
        (gnucap / "bin" / "gnucap").write_text("")
        self.env["RUN_SH_TEST_LABEL_NIX_VAMS_DEVSHELL"] = str(shell)
        self.env["RUN_SH_TEST_LABEL_NIX_VAMS_GNUCAP"] = str(gnucap)
        result = self.invoke(IMAGE_ID)
        self.run_argv(result)
        self.assertEqual(
            [line for line in result.stderr.splitlines() if "Verilog-AMS" in line],
            ["run.sh: no Verilog-AMS engine: %s is not on this host, and the "
             "store mounted for OpenROAD hides the image's copy (nix build "
             ".#vams-devshell in the adk-tools flake)" % shell])
        self.reset()
        self.env["RUN_SH_TEST_LABEL_NIX_VAMS_GNUCAP"] = str(shell)
        result = self.invoke(IMAGE_ID)
        self.run_argv(result)
        self.assertIn("run.sh: Verilog-AMS: the image's %s label is not a <store>/<hash>-vams-gnucap "
                      "path (got '%s')" % (VAMS_GNUCAP_LABEL, shell), result.stderr.splitlines())

    def test_no_openroad_reads_neither_label(self):
        self.env.pop("RUN_SH_TEST_LABEL")
        self.labels(self.devshell, self.python, self.iverilog)
        self.assertEqual(self.cosim_lines(self.invoke(IMAGE_ID)), [])
        self.assertEqual(self.calls()[:-1], [ID_READ, INSIDE_READ, LABEL_READ])


class RunShClosureTests(RunShEnv, unittest.TestCase):
    """The host paths the image's closure label lists, each bound on its own."""

    def setUp(self):
        super().setUp()
        # The label joins paths with single spaces, so this store has none.
        self.store = Path(tempfile.mkdtemp(prefix="run-sh-store-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.store, True)
        self.openroad = self.store / ("0a1b2c3d" * 4 + "-openroad-2026-02-17")
        (self.openroad / "bin").mkdir(parents=True)
        (self.openroad / "bin" / "openroad").write_text("#!/bin/sh\n")
        (self.openroad / "bin" / "openroad").chmod(0o755)
        self.env["RUN_SH_TEST_LABEL"] = str(self.openroad)
        self.librelane = self.store / ("8bj13r4a" * 4 + "-python3.13-librelane-3.0.0rc0")
        (self.librelane / "bin").mkdir(parents=True)
        (self.librelane / "bin" / "librelane").write_text("#!/bin/sh\n")
        self.env_dir = self.store / ("jys2hxxp" * 4 + "-python3-3.13.9-env")
        self.python = self.env_dir / "bin" / "python3.13"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("#!/bin/sh\n")
        self.pdk = self.store / ("hx96lf7n" * 4 + "-ihp-open-pdk-22f4335")
        self.pdk.mkdir()
        (self.pdk / "pdk-source.json").write_text("{}\n")
        self.glibc = self.store / ("0" * 32 + "-glibc-2.40-66")
        self.glibc.mkdir()
        self.listed = sorted(str(p) for p in (self.openroad, self.librelane, self.env_dir,
                                              self.pdk, self.glibc))
        for key, value in (("RUN_SH_TEST_LABEL_NIX_LIBRELANE", self.librelane),
                           ("RUN_SH_TEST_LABEL_NIX_LIBRELANE_PYTHON", self.python),
                           ("RUN_SH_TEST_LABEL_NIX_SG13G2_PDK", self.pdk),
                           # Not on this host: the image's own copies stay visible.
                           ("RUN_SH_TEST_LABEL_NIX_COSIM_DEVSHELL",
                            self.store / ("kcxwxk9f" * 4 + "-cosim-devshell")),
                           ("RUN_SH_TEST_LABEL_NIX_GTKWAVE",
                            self.store / ("w" * 32 + "-gtkwave-3.3.121"))):
            self.env[key] = str(value)
        self.label(self.listed)

    def label(self, paths, closure=None, digest=None):
        closure = " ".join(paths) if closure is None else closure
        self.env["RUN_SH_TEST_LABEL_NIX_HOST_CLOSURE"] = closure
        self.env["RUN_SH_TEST_LABEL_NIX_HOST_CLOSURE_SHA256"] = (
            hashlib.sha256(closure.encode()).hexdigest() if digest is None else digest)

    def binds(self, argv):
        return [v for f, v in pairs(argv) if f == "-v" and v.startswith(str(self.store))]

    def assert_refused(self, result, words):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(words, result.stderr)
        self.assertEqual(self.calls(), [ID_READ, INSIDE_READ, LABEL_READ, *CLOSURE_READS])
        self.assert_nothing_created()

    def test_each_listed_path_is_bound_and_not_the_store(self):
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertEqual(self.binds(argv), ["%s:%s:ro" % (p, p) for p in self.listed])
        self.assertNotIn(("-v", "%s:%s:ro" % (self.store, self.store)), pairs(argv))
        self.assertIn(("-e", "OPENROAD=%s/bin/openroad" % self.openroad), pairs(argv))
        self.assertEqual([v for f, v in pairs(argv) if v.startswith("ADK_NIX_")], [
            "ADK_NIX_LIBRELANE=%s" % self.librelane,
            "ADK_NIX_LIBRELANE_PYTHON=%s" % self.python,
            "ADK_NIX_SG13G2_PDK=%s" % self.pdk])
        self.assertIn("run.sh: store: 5 of 5 paths the image's %s label lists, each bound "
                      "read-only\n" % HOST_CLOSURE_LABEL, result.stderr)
        # Nothing hides the engines and viewers, so their labels are not read.
        self.assertEqual(self.calls()[:-1],
                         [ID_READ, INSIDE_READ, LABEL_READ, *CLOSURE_READS, *STORE_READS, *LAUNCHER_READS])
        self.assertNotIn("co-simulation", result.stderr)
        self.assertNotIn("viewer", result.stderr)

    def test_a_listed_path_not_on_this_host_is_counted(self):
        absent = str(self.store / ("1" * 32 + "-zlib-1.3.1"))
        self.label(sorted(self.listed + [absent]))
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.binds(self.run_argv(result)),
                         ["%s:%s:ro" % (p, p) for p in self.listed])
        self.assertIn("run.sh: store: 5 of 6 paths", result.stderr)
        self.assertIn("run.sh: needs Nix on the host: 1 of 6 paths the image's %s label "
                      "lists are not on this host (nix build .#openroad .#librelane "
                      ".#librelane-python .#sg13g2-pdk in the adk-tools flake)\n"
                      % HOST_CLOSURE_LABEL, result.stderr)

    def test_a_list_its_digest_does_not_name_is_refused(self):
        self.label(self.listed, digest=hashlib.sha256(
            (" ".join(self.listed) + "\n").encode()).hexdigest())
        self.assert_refused(self.invoke(IMAGE_ID),
                            "error: the image's %s label is not the one its %s label names"
                            % (HOST_CLOSURE_LABEL, HOST_CLOSURE_SHA256_LABEL))

    def test_one_label_alone_is_refused(self):
        for key in ("RUN_SH_TEST_LABEL_NIX_HOST_CLOSURE",
                    "RUN_SH_TEST_LABEL_NIX_HOST_CLOSURE_SHA256"):
            with self.subTest(missing=key):
                self.reset()
                self.label(self.listed)
                self.env.pop(key)
                self.assert_refused(self.invoke(IMAGE_ID),
                                    "error: the image has only one of the %s and %s labels"
                                    % (HOST_CLOSURE_LABEL, HOST_CLOSURE_SHA256_LABEL))

    def test_a_list_of_another_shape_is_refused(self):
        joined = " ".join(self.listed)
        elsewhere = "/elsewhere/%s-zlib-1.3.1" % ("1" * 32)
        for closure in (joined.replace(" ", "  ", 1), joined + " ", " " + joined,
                        joined.replace(" ", "\t", 1),
                        joined + " " + elsewhere, joined + " relative/%s-zlib" % ("1" * 32),
                        joined + " %s/zlib" % self.store,
                        joined + " %s/zlib/lib" % self.listed[0]):
            with self.subTest(closure=closure[-40:]):
                self.reset()
                self.label(None, closure=closure)
                self.assert_refused(self.invoke(IMAGE_ID),
                                    "error: the image's %s label is not %s/<hash>-<name> paths "
                                    "joined by single spaces" % (HOST_CLOSURE_LABEL, self.store))

    def test_a_list_without_the_labels_openroad_is_refused(self):
        self.label([p for p in self.listed if p != str(self.openroad)])
        self.assert_refused(self.invoke(IMAGE_ID),
                            "error: the image's %s label does not list %s, the OpenROAD its "
                            "%s label names" % (HOST_CLOSURE_LABEL, self.openroad,
                                                OPENROAD_LABEL))

    def test_adk_openroad_in_the_list_binds_the_list(self):
        self.env["ADK_OPENROAD"] = str(self.openroad)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.binds(self.run_argv(result)),
                         ["%s:%s:ro" % (p, p) for p in self.listed])
        self.assertEqual(self.calls()[:-1],
                         [ID_READ, INSIDE_READ, *CLOSURE_READS, *STORE_READS, *LAUNCHER_READS])

    def test_adk_openroad_outside_the_list_mounts_the_whole_store(self):
        other = self.store / ("9" * 32 + "-openroad")
        (other / "bin").mkdir(parents=True)
        (other / "bin" / "openroad").write_text("#!/bin/sh\n")
        (other / "bin" / "openroad").chmod(0o755)
        self.env["ADK_OPENROAD"] = str(other)
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertEqual(self.binds(argv), ["%s:%s:ro" % (self.store, self.store)])
        self.assertIn(("-e", "OPENROAD=%s/bin/openroad" % other), pairs(argv))
        self.assertIn("run.sh: store: all of %s, since the image's %s label does not list %s "
                      "(ADK_OPENROAD); it hides the image's own store paths\n"
                      % (self.store, HOST_CLOSURE_LABEL, other), result.stderr)
        # The whole store hides the engines again, so their labels are read.
        self.assertEqual(self.calls()[:-1],
                         [ID_READ, INSIDE_READ, *CLOSURE_READS, *STORE_READS, *COSIM_READS,
                          *LAUNCHER_READS])
        self.assertIn("no co-simulation engine", result.stderr)

    def test_a_librelane_path_outside_the_list_is_a_note(self):
        outside = self.store / ("2" * 32 + "-python3.13-librelane-3.0.0rc0")
        (outside / "bin").mkdir(parents=True)
        (outside / "bin" / "librelane").write_text("#!/bin/sh\n")
        self.env["RUN_SH_TEST_LABEL_NIX_LIBRELANE"] = str(outside)
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertNotIn(("-e", "ADK_NIX_LIBRELANE=%s" % outside), pairs(argv))
        self.assertIn("run.sh: no ADK_NIX_LIBRELANE: %s is not in the image's %s label, so it "
                      "is not mounted; a page that builds a die will refuse\n"
                      % (outside, HOST_CLOSURE_LABEL), result.stderr)

    def test_no_nix_on_the_host_is_refused(self):
        missing = self.store / "no store"
        self.env["RUN_SH_TEST_LABEL"] = str(missing / self.openroad.name)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("error: needs Nix on the host: Nix is not installed (no %s), and this "
                      "release needs it\n" % missing, result.stderr)
        self.assertIn("nix build .#openroad .#librelane .#librelane-python .#sg13g2-pdk",
                      result.stderr)
        self.assertEqual(self.calls(), [ID_READ, INSIDE_READ, LABEL_READ])
        self.assert_nothing_created()


ENV_READ = ["image", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}",
            IMAGE_ID]
LAUNCHER_SHA256_READ = LAUNCHER_READS[0]


class RunShClosureInsideTests(RunShEnv, unittest.TestCase):
    """An image that carries its tools in its own store: its fixed ENV, no host store."""

    LAUNCHER_SHA256 = "fedcba9876543210" * 4
    TOOLS = {
        "OPENROAD": "/nix/store/%s-openroad-2026-02-17/bin/openroad" % ("0a1b2c3d" * 4),
        "ADK_NIX_LIBRELANE": "/nix/store/%s-python3.13-librelane-3.0.0rc0" % ("8bj13r4a" * 4),
        "ADK_NIX_LIBRELANE_PYTHON":
            "/nix/store/%s-python3-3.13.9-env/bin/python3.13" % ("jys2hxxp" * 4),
        "ADK_NIX_SG13G2_PDK": "/nix/store/%s-ihp-open-pdk-22f4335" % ("hx96lf7n" * 4),
        "CHIPLET_FLOW_LIBRELANE_LAUNCHER": LAUNCHER,
        "CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256": LAUNCHER_SHA256,
    }

    def setUp(self):
        super().setUp()
        self.image_env(self.TOOLS)
        self.env["RUN_SH_TEST_LABEL_NIX_CLOSURE_INSIDE"] = HEX
        self.env["RUN_SH_TEST_LABEL_LIBRELANE_LAUNCHER_SHA256"] = self.LAUNCHER_SHA256
        # The labels an older run.sh reads stay on the image; this one reads none.
        self.env["RUN_SH_TEST_LABEL"] = self.TOOLS["OPENROAD"][:-len("/bin/openroad")]
        self.env["RUN_SH_TEST_LABEL_NIX_LIBRELANE"] = self.TOOLS["ADK_NIX_LIBRELANE"]

    def image_env(self, tools):
        self.env["RUN_SH_TEST_ENV"] = "".join(
            "%s=%s\n" % kv for kv in [("PATH", "/usr/bin:/bin"), *tools.items()])

    def assert_refused_inside(self, result, words, calls):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("error: " + words, result.stderr)
        self.assertEqual(self.calls(), [ID_READ, INSIDE_READ, *calls])
        self.assert_nothing_created()

    def test_the_image_carries_its_tools_and_the_host_store_stays_out(self):
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertEqual(self.calls()[:-1],
                         [ID_READ, INSIDE_READ, ENV_READ, LAUNCHER_SHA256_READ])
        self.assertEqual([v for f, v in pairs(argv) if "/nix" in v or "store" in v], [])
        self.assertEqual([v for f, v in pairs(argv) if f == "-e" and v.split("=")[0] in
                          self.TOOLS], [])
        t = self.TOOLS
        self.assertEqual(result.stderr, "\n".join([
            *self.fixed_lines(), CAPS_LINE,
            "run.sh: OpenROAD %s (the image's own, in its ENV)" % t["OPENROAD"],
            "run.sh: store: none of the host's; the image carries OpenROAD, LibreLane, its "
            "interpreter, the sg13g2 PDK and the launcher (its %s label, %s)"
            % (CLOSURE_INSIDE_LABEL, HEX),
            *["run.sh: %s %s (the image's own, in its ENV)" % (name, t[name]) for name in
              ("ADK_NIX_LIBRELANE", "ADK_NIX_LIBRELANE_PYTHON", "ADK_NIX_SG13G2_PDK")],
            "run.sh: CHIPLET_FLOW_LIBRELANE_LAUNCHER %s (the image's own, in its ENV), sha256 %s "
            "(its ENV and its %s label)" % (LAUNCHER, self.LAUNCHER_SHA256,
                                            LAUNCHER_SHA256_LABEL)]) + "\n")

    def test_adk_openroad_is_ignored(self):
        self.env["ADK_OPENROAD"] = "/nix/store/%s-openroad-2026-02-17" % ("9" * 32)
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertIn("run.sh: ADK_OPENROAD ignored: the image's own OpenROAD runs\n",
                      result.stderr)
        self.assertEqual([v for f, v in pairs(argv) if "/nix" in v], [])

    def test_a_label_that_is_not_a_sha256_is_refused(self):
        # Empty included: only an image without the label takes the host's store.
        for value in ("", "<no value>", HEX.upper(), HEX[:63], HEX + "0", " " + HEX,
                      HEX + "\n", "sha256:" + HEX):
            with self.subTest(value=value):
                self.reset()
                self.env["RUN_SH_TEST_LABEL_NIX_CLOSURE_INSIDE"] = value
                self.assert_refused_inside(
                    self.invoke(IMAGE_ID),
                    "the image's %s label is not a sha256 (got '%s')"
                    % (CLOSURE_INSIDE_LABEL, value), [])

    def test_an_image_env_without_one_of_its_tools_is_refused(self):
        for name in self.TOOLS:
            with self.subTest(name=name):
                self.reset()
                self.image_env({k: v for k, v in self.TOOLS.items() if k != name})
                self.assert_refused_inside(
                    self.invoke(IMAGE_ID),
                    "the image's %s label says it carries its tools, but its ENV sets no %s"
                    % (CLOSURE_INSIDE_LABEL, name), [ENV_READ])

    def test_an_entry_with_no_value_unsets_the_name(self):
        # A later entry wins; a bare name, which no Dockerfile ENV writes, is
        # read as no value (run.sh then refuses); an "=" inside a value is the
        # value's.
        for name in ("OPENROAD", "ADK_NIX_SG13G2_PDK"):
            with self.subTest(name=name):
                self.reset()
                self.image_env(self.TOOLS)
                self.env["RUN_SH_TEST_ENV"] += name + "\n"
                self.assert_refused_inside(
                    self.invoke(IMAGE_ID),
                    "the image's %s label says it carries its tools, but its ENV sets no %s"
                    % (CLOSURE_INSIDE_LABEL, name), [ENV_READ])
        self.reset()
        self.image_env(self.TOOLS)
        self.env["RUN_SH_TEST_ENV"] += "OPENROAD\nOPENROAD=%s\n" % self.TOOLS["OPENROAD"]
        self.assertEqual(self.invoke(IMAGE_ID).returncode, 0)
        self.reset()
        self.image_env(dict(self.TOOLS, ADK_NIX_SG13G2_PDK=self.TOOLS["ADK_NIX_SG13G2_PDK"] + "=x"))
        self.assertIn("run.sh: ADK_NIX_SG13G2_PDK %s=x (the image's own, in its ENV)\n"
                      % self.TOOLS["ADK_NIX_SG13G2_PDK"], self.invoke(IMAGE_ID).stderr)

    def test_a_launcher_sha256_other_than_its_label_is_refused(self):
        for label in ("0" * 64, ""):
            with self.subTest(label=label):
                self.reset()
                self.env["RUN_SH_TEST_LABEL_LIBRELANE_LAUNCHER_SHA256"] = label
                self.assert_refused_inside(
                    self.invoke(IMAGE_ID),
                    "the image's CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256 (%s) is not its "
                    "%s label ('%s')" % (self.LAUNCHER_SHA256, LAUNCHER_SHA256_LABEL, label),
                    [ENV_READ, LAUNCHER_SHA256_READ])

    def test_an_image_without_the_label_takes_the_host_store(self):
        del self.env["RUN_SH_TEST_LABEL_NIX_CLOSURE_INSIDE"]
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.calls()[:3], [ID_READ, INSIDE_READ, LABEL_READ])
        self.assertNotIn(ENV_READ, self.calls())
        self.assertNotIn("the image's own", result.stderr)


class RunShDisplayTests(RunShEnv, unittest.TestCase):
    """The display: ADK_DISPLAY, DISPLAY :N, ThinLinc's last; our socket only."""

    def setUp(self):
        super().setUp()
        stub_dir = self.base / "stub bin"
        (stub_dir / "xauth").write_text(XAUTH_STUB)
        (stub_dir / "xauth").chmod(0o755)
        # Two ThinLinc sessions; 14 is stale and has no socket, last is 18.
        for n in ("14", "18"):
            (self.sessions / n).mkdir()
            (self.sessions / n / "Xauthority").write_text("cookie:%s\n" % n)
        (self.sessions / "last").symlink_to("18")
        self.sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.sock.close)
        self.sock.bind(str(self.x11 / "X18"))
        # ~/.Xauthority holds only a stale cookie, for another display.
        (self.home / ".Xauthority").write_text("cookie:10\n")

    def display_pairs(self, argv):
        return [p for p in pairs(argv)
                if p[1].startswith(("DISPLAY=", "XAUTHORITY="))
                or "X11-unix" in p[1] or ".Xauthority" in p[1]]

    def cookie_seen(self):
        return Path(self.env["RUN_SH_TEST_COOKIE"]).read_text()

    def assert_display_18(self, result, source, cookie_from):
        argv = self.run_argv(result)
        [cookie] = [v for f, v in pairs(argv)
                    if v.endswith(":/tmp/.Xauthority:ro")]
        self.assertEqual(Path(cookie.split(":", 1)[0]).parent.parent,
                         self.tmpdir)
        self.assertEqual(self.display_pairs(argv), [
            ("-e", "DISPLAY=:18"),
            ("-v", "%s/X18:/tmp/.X11-unix/X18:ro" % self.x11),
            ("-v", cookie),
            ("-e", "XAUTHORITY=/tmp/.Xauthority")])
        # One wildcard-host entry: family ffff in place of the file's 0100.
        [entry] = self.cookie_seen().splitlines()
        self.assertTrue(entry.startswith("ffff 0007 "), entry)
        self.assertEqual(result.stderr.splitlines()[:3], self.fixed_lines(
            "run.sh: display :18 (from %s), cookie from %s"
            % (source, cookie_from)))

    def test_thinlinc_last_is_the_display_without_DISPLAY(self):
        self.assert_display_18(self.invoke(IMAGE_ID), "ThinLinc session 18",
                               self.sessions / "18" / "Xauthority")

    def test_DISPLAY_and_ADK_DISPLAY_pick_it(self):
        for env, source in ((("DISPLAY", ":18.0"), "DISPLAY"),
                            (("ADK_DISPLAY", ":18"), "ADK_DISPLAY")):
            with self.subTest(env=env):
                self.reset()
                self.env.pop("DISPLAY", None)
                self.env.pop("ADK_DISPLAY", None)
                self.env[env[0]] = env[1]
                self.assert_display_18(self.invoke(IMAGE_ID), source,
                                       self.sessions / "18" / "Xauthority")

    def test_XAUTHORITY_comes_first_when_it_holds_the_cookie(self):
        own = self.base / "own Xauthority"
        own.write_text("cookie:18\n")
        self.env["XAUTHORITY"] = str(own)
        self.assert_display_18(self.invoke(IMAGE_ID), "ThinLinc session 18",
                               own)

    def test_a_cookie_that_cannot_be_written_stops_the_run(self):
        self.env["RUN_SH_TEST_XAUTH_FAIL"] = "1"
        result = self.invoke(IMAGE_ID)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr.splitlines()[-1],
                         "error: could not write the cookie for :18 from %s "
                         "into run.sh's private dir"
                         % (self.sessions / "18" / "Xauthority"))
        self.assertEqual(self.calls(), [ID_READ, INSIDE_READ, LABEL_READ])
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_a_stale_home_cookie_is_never_mounted(self):
        (self.sessions / "18" / "Xauthority").unlink()
        result = self.invoke(IMAGE_ID)
        argv = self.run_argv(result)
        self.assertEqual(self.display_pairs(argv), [
            ("-e", "DISPLAY=:18"),
            ("-v", "%s/X18:/tmp/.X11-unix/X18:ro" % self.x11)])
        self.assertEqual(result.stderr.splitlines()[:3], self.fixed_lines(
            "run.sh: display :18 (from ThinLinc session 18), no cookie for "
            ":18 found; the server has to admit your uid"))

    def test_thinlinc_takes_only_our_socket(self):
        # Read with an id that answers another uid, 18's socket is someone
        # else's: the ThinLinc guess refuses it, a display the caller names
        # keeps it (a local Xorg's socket is commonly root's).
        real_id = shutil.which("id")
        stub = self.base / "stub bin" / "id"
        stub.write_text(
            "#!/bin/sh\n"
            '[ "$*" = "-u" ] && { echo %d; exit 0; }\n'
            'exec %s "$@"\n' % (os.getuid() + 1, real_id))
        stub.chmod(0o755)
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.display_pairs(self.run_argv(result)), [])
        self.assertEqual(result.stderr.splitlines()[:3], self.fixed_lines(
            "run.sh: no display: %s/X18 (from ThinLinc session 18) is "
            "another user's X socket" % self.x11))
        for env, source in ((("DISPLAY", ":18"), "DISPLAY"),
                            (("ADK_DISPLAY", ":18"), "ADK_DISPLAY")):
            with self.subTest(env=env):
                self.reset()
                self.env.pop("DISPLAY", None)
                self.env.pop("ADK_DISPLAY", None)
                self.env[env[0]] = env[1]
                self.assert_display_18(self.invoke(IMAGE_ID), source,
                                       self.sessions / "18" / "Xauthority")

    def test_a_display_without_its_socket_is_not_taken(self):
        # The stale session 14 has no socket, named or found.
        (self.sessions / "last").unlink()
        (self.sessions / "last").symlink_to("14")
        for env, source in (({}, "ThinLinc session 14"),
                            ({"ADK_DISPLAY": ":14"}, "ADK_DISPLAY"),
                            ({"DISPLAY": ":14"}, "DISPLAY")):
            with self.subTest(env=env):
                self.reset()
                self.env.pop("DISPLAY", None)
                self.env.pop("ADK_DISPLAY", None)
                self.env.update(env)
                result = self.invoke(IMAGE_ID)
                self.assertEqual(self.display_pairs(self.run_argv(result)), [])
                self.assertEqual(
                    result.stderr.splitlines()[:3], self.fixed_lines(
                        "run.sh: no display: %s/X14 (from %s) is not an X "
                        "socket" % (self.x11, source)))

    def test_none_and_a_malformed_ADK_DISPLAY(self):
        self.env["ADK_DISPLAY"] = "none"
        result = self.invoke(IMAGE_ID)
        self.assertEqual(self.display_pairs(self.run_argv(result)), [])
        # Still the third note, and the only one about the display.
        lines = result.stderr.splitlines()
        self.assertEqual(lines[:3], self.fixed_lines(
            "run.sh: no display: ADK_DISPLAY=none"))
        self.assertEqual([l for l in lines if "display" in l], lines[2:3])
        for value in ("18", ":18.0", "host:18", ":", "None"):
            with self.subTest(value=value):
                self.reset()
                self.env["ADK_DISPLAY"] = value
                result = self.invoke(IMAGE_ID)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr.splitlines()[-1],
                                 "error: ADK_DISPLAY must be :N or none "
                                 "(got '%s')" % value)
                self.assertEqual(self.calls(), [ID_READ, INSIDE_READ, LABEL_READ])
                self.assert_nothing_created()


class Check3dbloxImageTests(ImageRuleCases, unittest.TestCase):
    SCRIPT = "openroad/check-3dblox.sh"

    def setUp(self):
        super().setUp()
        self.src = self.base / "assembly dir"
        self.src.mkdir()
        # The stub never reads it; the launcher only needs it to exist.
        self.chiplet = self.src / "demo.chiplet"
        self.chiplet.write_text("{}\n")
        self.out = self.src / "build_3dblox"
        pin = (ROOT / "openroad" / "openroad.pin").read_text()
        [self.linter] = re.findall(r"^OPENROAD_3DBLOX_IMAGE=(\S+)$", pin,
                                   re.MULTILINE)

    def default_args(self):
        return [str(self.chiplet)]

    def assert_nothing_created(self):
        # The default out-dir would be made next to the assembly.
        self.assertEqual(sorted(p.name for p in self.src.iterdir()),
                         ["demo.chiplet"])

    def remove_created(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def test_the_id_is_checked_before_the_arguments(self):
        # A missing or malformed ID gets its own refusal whatever follows,
        # arguments the parser itself would reject included.
        for args in (["--no-such-option"], [], ["--pins"],
                     [str(self.src / "absent.chiplet")],
                     [str(self.chiplet), "out", "extra"]):
            for image, first_line in (
                    (None, self.unset_line()),
                    ("adk-tools:dev",
                     "error: ADK_TOOLS_IMAGE must be an image ID, "
                     "sha256:<64 hex> (got 'adk-tools:dev')")):
                with self.subTest(args=args, image=image):
                    self.reset()
                    self.assert_refused(self.invoke(image, args), first_line)

    def test_an_image_the_daemon_lacks_creates_nothing(self):
        # A well-formed ID is looked up before the out-dir is made, and so is
        # the linter's tag: either one missing stops the run with nothing on
        # disk, the default out-dir and a given one alike.
        given = self.base / "given out"
        for missing, calls, stderr in (
                (IMAGE_ID, [["image", "inspect", IMAGE_ID]],
                 "missing image: %s\n" % IMAGE_ID),
                (self.linter, [["image", "inspect", IMAGE_ID],
                               ["image", "inspect", self.linter]],
                 "missing image: %s\n"
                 "build it with ./openroad/build-image.sh\n" % self.linter)):
            for args in ([str(self.chiplet)], [str(self.chiplet), str(given)]):
                with self.subTest(missing=missing, args=args):
                    self.reset()
                    shutil.rmtree(given, ignore_errors=True)
                    self.env["RUN_SH_TEST_MISSING"] = missing
                    result = self.invoke(IMAGE_ID, args)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, stderr)
                    self.assertEqual(self.calls(), calls)
                    self.assert_nothing_created()
                    self.assertFalse(given.exists())

    def test_valid_id_is_accepted(self):
        result = self.invoke(IMAGE_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith(
            "== clean (declared-model lint only; artwork is not checked)\n"),
            result.stdout)
        self.assertEqual([call[:2] for call in self.calls()], [
            ["image", "inspect"], ["image", "inspect"], ["run", "--rm"],
            ["run", "--rm"]])
        self.assertTrue((self.out / "check_3dblox.tcl").is_file())

    def test_id_reaches_the_exporter_unchanged(self):
        # The linter is openroad.pin's tag; the environment's is not read.
        self.env["OPENROAD_3DBLOX_IMAGE"] = "openroad-3dblox:from-the-env"
        pins = ["--pins", "U1=pins file.json"]
        result = self.invoke(IMAGE_ID, [str(self.chiplet), *pins])
        self.assertEqual(result.returncode, 0, result.stderr)
        common = ["run", "--rm",
                  "--user", "%d:%d" % (os.getuid(), os.getgid()),
                  "-v", "%s:%s" % (self.src, self.src),
                  "-v", "%s:%s" % (self.out, self.out),
                  "-w", str(self.out)]
        self.assertEqual(self.calls(), [
            ["image", "inspect", IMAGE_ID],
            ["image", "inspect", self.linter],
            [*common,
             "--entrypoint", "/opt/adk-tools/venv/bin/python3", IMAGE_ID,
             "/opt/adk-tools/adk/openroad/chiplet2dbx.py",
             "--chiplet", str(self.chiplet), "--out-dir", str(self.out),
             "--name", "demo", *pins],
            [*common, self.linter, "-exit",
             "%s/check_3dblox.tcl" % self.out]])


class LauncherRuleParityTests(unittest.TestCase):
    def test_both_launchers_write_the_same_id_rule(self):
        # Each reads ADK_TOOLS_IMAGE once, with an empty default, into the
        # variable it then matches once: the rule host-verify.sh applies,
        # spelled as a bash ERE. The cases above only sample the rule; this
        # compares it whole.
        rules = {}
        for script in LAUNCHERS:
            text = (ROOT / script).read_text()
            reads = re.findall(r'^(\w+)="\$\{ADK_TOOLS_IMAGE:-\}"$', text,
                               re.MULTILINE)
            self.assertEqual(len(reads), 1, script)
            self.assertEqual(text.count("ADK_TOOLS_IMAGE:-"), 1, script)
            rules[script] = re.findall(r'"\$%s" =~ (\S+) \]\]' % reads[0],
                                       text)
        self.assertEqual(rules, {script: [ID_RULE] for script in LAUNCHERS})

    def test_build_sh_labels_what_run_sh_reads(self):
        # build.sh writes the fourteen labels on the runtime image and run.sh
        # reads them by name: one name each, set once in each script.
        labels = {"OPENROAD_LABEL": (OPENROAD_LABEL, "NIX_OPENROAD_OUTPATH"),
                  "LIBRELANE_LABEL": (LIBRELANE_LABEL, "NIX_LIBRELANE_OUTPATH"),
                  "LIBRELANE_PYTHON_LABEL": (LIBRELANE_PYTHON_LABEL, "LIBRELANE_PYTHON"),
                  "SG13G2_PDK_LABEL": (SG13G2_PDK_LABEL, "NIX_SG13G2_PDK_OUTPATH"),
                  "COSIM_DEVSHELL_LABEL": (COSIM_DEVSHELL_LABEL,
                                           "NIX_COSIM_DEVSHELL_OUTPATH"),
                  "COSIM_PYTHON_LABEL": (COSIM_PYTHON_LABEL, "NIX_COSIM_PYTHON_OUTPATH"),
                  "COSIM_IVERILOG_LABEL": (COSIM_IVERILOG_LABEL,
                                           "NIX_COSIM_IVERILOG_OUTPATH"),
                  "MIXSIM_NGSPICE_LABEL": (MIXSIM_NGSPICE_LABEL,
                                           "NIX_MIXSIM_NGSPICE_OUTPATH"),
                  "MIXSIM_IVERILOG_LABEL": (MIXSIM_IVERILOG_LABEL,
                                            "NIX_MIXSIM_IVERILOG_OUTPATH"),
                  "SG13G2_OSDI_LABEL": (SG13G2_OSDI_LABEL, "NIX_SG13G2_OSDI_OUTPATH"),
                  "GTKWAVE_LABEL": (GTKWAVE_LABEL, "NIX_GTKWAVE_OUTPATH"),
                  "GAW_LABEL": (GAW_LABEL, "NIX_GAW_OUTPATH"),
                  "XSCHEM_LABEL": (XSCHEM_LABEL, "NIX_XSCHEM_OUTPATH"),
                  "VAMS_GNUCAP_LABEL": (VAMS_GNUCAP_LABEL, "NIX_VAMS_GNUCAP_OUTPATH"),
                  "VAMS_DEVSHELL_LABEL": (VAMS_DEVSHELL_LABEL, "NIX_VAMS_DEVSHELL_OUTPATH")}
        build = (ROOT / "build.sh").read_text()
        for var, (label, pin) in labels.items():
            for script in ("build.sh", "run.sh"):
                text = (ROOT / script).read_text()
                self.assertEqual(
                    re.findall(r"^%s=(\S+)$" % var, text, re.MULTILINE),
                    [label], script)
            self.assertEqual(build.count('--label "$%s=$%s"' % (var, pin)), 1)


    def test_build_sh_labels_the_launcher_run_sh_passes(self):
        # One name each, set once in each script; build.sh labels the runtime
        # image with both digests and hands the verify stage the launcher's,
        # and run.sh passes the file at the path the Dockerfile copies it to.
        build = (ROOT / "build.sh").read_text()
        run = (ROOT / "run.sh").read_text()
        for var, label in (("LAUNCHER_SHA256_LABEL", LAUNCHER_SHA256_LABEL),
                           ("LIBRELANE_WRAPPER_SHA256_LABEL", LIBRELANE_WRAPPER_SHA256_LABEL)):
            for script, text in (("build.sh", build), ("run.sh", run)):
                self.assertEqual(re.findall(r"^%s=(\S+)$" % var, text, re.MULTILINE),
                                 [label], script)
        runtime = build[build.index('docker build --target runtime'):]
        self.assertEqual(runtime.count('--label "$LAUNCHER_SHA256_LABEL=$LAUNCHER_SHA256"'), 1)
        self.assertEqual(runtime.count(
            '--label "$LIBRELANE_WRAPPER_SHA256_LABEL=$LIBRELANE_WRAPPER_SHA256"'), 1)
        # Both builds take the launcher's sha256 in the runtime stage's ARGs: the
        # runtime ENV holds it and the verify stage checks it.
        verify = build[build.index('docker build --target verify'):build.index(
            'docker build --target runtime')]
        runtime_args = build[build.index('RUNTIME_ARGS=('):build.index(
            'docker build --target verify')]
        self.assertEqual(runtime_args.count('--build-arg "LAUNCHER_SHA256=$LAUNCHER_SHA256"'), 1)
        self.assertEqual(verify.count('"${RUNTIME_ARGS[@]}"'), 1)
        self.assertEqual(runtime.count('"${RUNTIME_ARGS[@]}"'), 1)
        self.assertEqual(re.findall(r"^LIBRELANE_LAUNCHER=(\S+)$", run, re.MULTILINE), [LAUNCHER])
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertEqual(dockerfile.count(
            "COPY --chmod=0555 librelane-launcher %s\n" % LAUNCHER), 1)
        self.assertEqual(dockerfile.count("--launcher %s --sha256 \"$LAUNCHER_SHA256\"" % LAUNCHER), 1)

    def test_the_launcher_roots_are_where_an_overlay_mounts(self):
        # build.sh writes the launcher over build-env.sh's staged roots under
        # /opt/adk-tools/staging, one per launcher tree at
        # /opt/adk-tools/staging/<name>/<subdirectory>; each <name> is a tree
        # run.sh overlays, at that same /opt/adk-tools/staging/<name>.
        build = (ROOT / "build.sh").read_text()
        run = (ROOT / "run.sh").read_text()
        self.assertEqual(build.count(
            'mapfile -t roots < <(adk_nix_staged_roots /opt/adk-tools/staging)'), 1)
        out = subprocess.run(
            ["bash", "-c", 'ADK_NIX_LIB_ONLY=1; . "$1/nix/cli-env.sh"; ADK_NIX_LIB_ONLY=1; '
                           '. "$1/nix/build-env.sh"; printf "%s" "$ADK_NIX_LAUNCHER_TREES"; '
                           'echo ---; adk_nix_staged_roots /opt/adk-tools/staging',
             "_", str(ROOT)], capture_output=True, text=True, check=True).stdout
        trees, roots = out.split("---\n")
        rows = [line.split("|") for line in trees.splitlines() if line]
        names = [row[0] for row in rows]
        self.assertEqual(names, ["chiplet-flow", "chiplet-spec"])
        self.assertEqual(roots.splitlines(),
                         ["/opt/adk-tools/staging/%s/%s" % (row[0], row[1]) for row in rows])
        staged = re.findall(r'^STAGED_TREES="([^"]*)"$', run, re.MULTILINE)
        self.assertEqual(len(staged), 1)
        self.assertLessEqual(set(names), set(staged[0].split()))
        self.assertEqual(run.count('OVERLAY_ARGS+=(-v "$path:/opt/adk-tools/staging/$name:ro")'), 1)

    def test_build_sh_labels_the_closure_run_sh_binds(self):
        # The list's paths joined by single spaces with no newline, and the
        # sha256 of exactly those bytes, on the runtime image; run.sh checks it
        # the same way.
        build = (ROOT / "build.sh").read_text()
        run = (ROOT / "run.sh").read_text()
        for var, label in (("HOST_CLOSURE_LABEL", HOST_CLOSURE_LABEL),
                           ("HOST_CLOSURE_SHA256_LABEL", HOST_CLOSURE_SHA256_LABEL)):
            for script, text in (("build.sh", build), ("run.sh", run)):
                self.assertEqual(re.findall(r"^%s=(\S+)$" % var, text, re.MULTILINE),
                                 [label], script)
        runtime = build[build.index('docker build --target runtime'):]
        self.assertEqual(runtime.count('--label "$HOST_CLOSURE_LABEL=$HOST_CLOSURE"'), 1)
        self.assertEqual(runtime.count(
            '--label "$HOST_CLOSURE_SHA256_LABEL=$HOST_CLOSURE_SHA256"'), 1)
        lines = [line for line in build.splitlines()
                 if line.startswith(("HOST_CLOSURE=\"$(paste", "HOST_CLOSURE_SHA256="))]
        self.assertEqual(len(lines), 2, lines)
        paths = ["/nix/store/%s-a" % ("a" * 32), "/nix/store/%s-b" % ("b" * 32)]
        out = subprocess.run(["bash", "-c", "\n".join(lines) + '\nprintf "%s\\n%s" '
                              '"$HOST_CLOSURE_SHA256" "$HOST_CLOSURE"'],
                             env={"PATH": "/usr/bin:/bin", "HOST_PATHS": "\n".join(paths)},
                             capture_output=True, text=True, check=True).stdout
        joined = " ".join(paths)
        self.assertEqual(out, hashlib.sha256(joined.encode()).hexdigest() + "\n" + joined)
        self.assertIn('[ "$(printf \'%s\' "$CLOSURE" | sha256sum | cut -d \' \' -f 1)" '
                      '!= "$CLOSURE_SHA256" ]', run)

    def test_build_sh_labels_the_pin_set_run_sh_reads(self):
        build = (ROOT / "build.sh").read_text()
        for script in ("build.sh", "run.sh"):
            text = (ROOT / script).read_text()
            self.assertEqual(re.findall(r"^PIN_SET_LABEL=(\S+)$", text, re.MULTILINE),
                             [PIN_SET_LABEL], script)
        self.assertEqual(build.count('--label "$PIN_SET_LABEL=$PIN_SET_SHA256"'), 1)
        # On the runtime image, which run.sh launches; the verify image has none.
        runtime = build[build.index('docker build --target runtime'):]
        self.assertIn('--label "$PIN_SET_LABEL=$PIN_SET_SHA256"', runtime)


def pin_set_repo(directory, pin_set=b'{"format":"adk-pin-set/1"}\n'):
    """directory as a git repository whose commit tracks ci/pin-set.json, with
    a ci/pin_set.py stub that exits STUB_PIN_SET_RC (default 0): what build.sh
    reads before it labels the image."""
    ci = Path(directory, "ci")
    ci.mkdir(exist_ok=True)
    (ci / "pin-set.json").write_bytes(pin_set)
    (ci / "pin_set.py").write_text(
        "import os\nimport sys\nsys.exit(int(os.environ.get('STUB_PIN_SET_RC', '0')))\n")
    git = ["git", "-C", str(directory), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "ci/pin-set.json"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "pin set"], check=True)


class BuildShVerifyTagTests(unittest.TestCase):
    # build.sh copied alone into a temp dir, with a stage-sources.sh stub that
    # exits 3: its first side effect. A refused VERIFY_TAG exits 1 before it;
    # an accepted one reaches it, and nothing past it runs.

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(ROOT / "build.sh", self.dir / "build.sh")
        stub = self.dir / "stage-sources.sh"
        stub.write_text("#!/bin/sh\necho STAGED\nexit 3\n")
        stub.chmod(0o755)
        pin_set_repo(self.dir)

    def build(self, *args, verify_tag=None):
        env = {k: v for k, v in os.environ.items() if k != "VERIFY_TAG"}
        if verify_tag is not None:
            env["VERIFY_TAG"] = verify_tag
        return subprocess.run(["bash", str(self.dir / "build.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=30)

    def assertRefused(self, result, why):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(why, result.stderr)
        self.assertNotIn("STAGED", result.stdout)

    def assertAccepted(self, result):
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("STAGED", result.stdout)

    def test_default_and_a_new_name_pass_the_check(self):
        self.assertAccepted(self.build())
        self.assertAccepted(self.build(verify_tag="verify-abc1234"))

    def test_a_name_docker_cannot_tag_is_refused(self):
        for bad in ("bad tag", "verify:1", "-x", ".x", "a" * 129, ""):
            with self.subTest(bad=bad):
                result = self.build(verify_tag=bad)
                if bad == "":
                    # Empty means unset: the default applies.
                    self.assertAccepted(result)
                else:
                    self.assertRefused(result, "is not a docker tag")

    def test_the_runtime_tag_is_refused(self):
        self.assertRefused(self.build(verify_tag="dev"), "VERIFY_TAG and TAG")
        self.assertRefused(self.build("feature-x", verify_tag="feature-x"),
                           "VERIFY_TAG and TAG")

    def test_the_verify_stage_is_tagged_from_it(self):
        build = (ROOT / "build.sh").read_text()
        self.assertEqual(build.count('--target verify -t "adk-tools:$VERIFY_TAG"'), 1)
        self.assertNotIn("-t adk-tools:verify", build)


class BuildShToolsImageTests(unittest.TestCase):
    # The tools image is built once per ci/tools_key.py key, and both builds
    # below the thin-layer line stand on the image that key tags.

    def setUp(self):
        self.build = (ROOT / "build.sh").read_text()

    def command(self, target):
        start = self.build.index("docker build --target %s " % target)
        return self.build[start:self.build.index(" .\n", start)]

    def test_the_tools_image_is_built_under_its_key(self):
        self.assertEqual(self.build.count("docker build --target tools "), 1)
        tools = self.command("tools")
        self.assertIn('-t "$TOOLS_REF"', tools)
        self.assertIn('--label "$TOOLS_KEY_LABEL=$TOOLS_KEY"', tools)
        self.assertIn('TOOLS_REF="adk-tools:tools-${TOOLS_KEY:0:12}"', self.build)

    def test_verify_and_runtime_build_on_it(self):
        self.assertIn('TOOLS_ARGS=(--build-arg "TOOLS_IMAGE=$TOOLS_REF")', self.build)
        for target in ("verify", "runtime"):
            with self.subTest(target=target):
                self.assertIn('"${TOOLS_ARGS[@]}"', self.command(target))
        self.assertEqual(self.build.count('layers_on_tools "adk-tools:$'), 2)

    def test_the_runtime_names_its_tools_by_key_and_id(self):
        runtime = self.command("runtime")
        self.assertIn('--label "$TOOLS_KEY_LABEL=$TOOLS_KEY"', runtime)
        self.assertIn('--label "$TOOLS_ID_LABEL=$TOOLS_ID"', runtime)

    def test_locks_of_another_key_are_refused_before_anything_builds(self):
        check = self.build.index('ci/check_tools_locks.py --key "$TOOLS_KEY"')
        self.assertLess(self.build.index('TOOLS_KEY="$(python3 -B ci/tools_key.py'), check)
        self.assertLess(check, self.build.index("docker build --target tools "))
        guarded = self.build[self.build.rindex("\n", 0, check):check]
        self.assertNotIn("TOOLS_ONLY", guarded)
        self.assertIn('if [ "$TOOLS_ONLY" -eq 0 ]; then',
                      self.build[self.build.rindex("\nif ", 0, check):check])

    def test_tools_only_stops_before_the_stages_on_the_tools_image(self):
        stop = self.build.index('if [ "$TOOLS_ONLY" -eq 1 ]; then')
        self.assertLess(self.build.index('TOOLS_ID="$(docker image inspect'), stop)
        self.assertLess(stop, self.build.index("docker build --target verify "))
        self.assertIn("exit 0", self.build[stop:self.build.index("\nfi\n", stop)])


class BuildShPinSetTests(unittest.TestCase):
    # build.sh copied alone into a git repository that tracks ci/pin-set.json,
    # with a ci/pin_set.py stub and a stage-sources.sh stub that exits 3, its
    # first side effect after the pin-set checks.

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(ROOT / "build.sh", self.dir / "build.sh")
        stub = self.dir / "stage-sources.sh"
        stub.write_text("#!/bin/sh\necho STAGED\nexit 3\n")
        stub.chmod(0o755)
        pin_set_repo(self.dir)

    def build(self, check_rc=0):
        env = {k: v for k, v in os.environ.items() if k != "VERIFY_TAG"}
        env["STUB_PIN_SET_RC"] = str(check_rc)
        return subprocess.run(["bash", str(self.dir / "build.sh")], env=env,
                              capture_output=True, text=True, timeout=30)

    def assertRefused(self, result, why):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Refusing to build: ci/pin-set.json " + why, result.stderr)
        self.assertNotIn("STAGED", result.stdout)

    def test_a_clean_tracked_pin_set_that_regenerates_passes(self):
        result = self.build()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("STAGED", result.stdout)

    def test_a_modified_pin_set_is_refused(self):
        (self.dir / "ci" / "pin-set.json").write_text("{}\n")
        self.assertRefused(self.build(), "differs from the commit")

    def test_an_untracked_pin_set_is_refused(self):
        subprocess.run(["git", "-C", str(self.dir), "rm", "-q", "--cached",
                        "ci/pin-set.json"], check=True)
        self.assertRefused(self.build(), "is not tracked")

    def test_a_missing_pin_set_is_refused(self):
        (self.dir / "ci" / "pin-set.json").unlink()
        self.assertRefused(self.build(), "differs from the commit")

    def test_a_pin_set_the_pins_do_not_regenerate_is_refused(self):
        self.assertRefused(self.build(check_rc=1), "is not what the pins regenerate")


class BuildShStorePinTests(unittest.TestCase):
    # build.sh copied alone with a staging.pin of fifteen store pins, a
    # stage-sources.sh stub that passes and a ci/check_paths.py stub that
    # fails: a pin of the wrong shape refuses first, and good pins get as far
    # as the paths check. Only the shape is checked; nothing evaluates Nix.

    PINS = {
        "NIX_OPENROAD_OUTPATH": "/nix/store/%s-openroad-2026-02-17" % ("a" * 32),
        "NIX_LIBRELANE_OUTPATH": "/nix/store/%s-python3.13-librelane-3.0.0rc0" % ("b" * 32),
        "NIX_LIBRELANE_PYTHON_OUTPATH": "/nix/store/%s-python3-3.13.9-env" % ("o" * 32),
        "NIX_SG13G2_PDK_OUTPATH": "/nix/store/%s-ihp-open-pdk-22f4335" % ("c" * 32),
        "NIX_COSIM_IVERILOG_OUTPATH":
            "/nix/store/%s-iverilog-s20250103-60-gdb82380ce" % ("d" * 32),
        "NIX_COSIM_COCOTB_OUTPATH": "/nix/store/%s-python3.13-cocotb-2.0.0" % ("e" * 32),
        "NIX_COSIM_PYTHON_OUTPATH": "/nix/store/%s-python3-3.13.9-env" % ("f" * 32),
        "NIX_COSIM_DEVSHELL_OUTPATH": "/nix/store/%s-cosim-devshell" % ("j" * 32),
        "NIX_MIXSIM_NGSPICE_OUTPATH": "/nix/store/%s-ngspice-45" % ("g" * 32),
        "NIX_MIXSIM_IVERILOG_OUTPATH":
            "/nix/store/%s-iverilog-libvvp-s20250103-60-gdb82380ce" % ("h" * 32),
        "NIX_SG13G2_OSDI_OUTPATH": "/nix/store/%s-sg13g2-osdi-22f4335" % ("i" * 32),
        "NIX_GTKWAVE_OUTPATH": "/nix/store/%s-gtkwave-3.3.121" % ("k" * 32),
        "NIX_GAW_OUTPATH": "/nix/store/%s-gaw3-20250128" % ("l" * 32),
        "NIX_XSCHEM_OUTPATH": "/nix/store/%s-xschem-3.4.8RC-8d2088e" % ("p" * 32),
        "NIX_VAMS_GNUCAP_OUTPATH":
            "/nix/store/%s-gnucap-b6678fe-modelgen-verilog-100e746" % ("m" * 32),
        "NIX_VAMS_DEVSHELL_OUTPATH": "/nix/store/%s-vams-devshell" % ("n" * 32),
    }

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(ROOT / "build.sh", self.dir / "build.sh")
        (self.dir / "stage-sources.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.dir / "stage-sources.sh").chmod(0o755)
        (self.dir / "stage-die-views.sh").write_text(
            '#!/bin/sh\necho DIE_VIEWS_STAGED\nexit "${STUB_DIE_VIEWS_RC:-0}"\n')
        (self.dir / "stage-die-views.sh").chmod(0o755)
        (self.dir / "ci").mkdir()
        (self.dir / "ci" / "check_paths.py").write_text("import sys\nsys.exit(5)\n")
        pin_set_repo(self.dir)

    def build(self, stager_rc=0, **pins):
        values = dict(self.PINS, **pins)
        (self.dir / "staging.pin").write_text("".join(
            '%s="%s"\n' % (k, v) for k, v in values.items() if v is not None))
        env = {k: v for k, v in os.environ.items() if k != "VERIFY_TAG"}
        env["STUB_DIE_VIEWS_RC"] = str(stager_rc)
        return subprocess.run(["bash", str(self.dir / "build.sh")], cwd=self.dir,
                              env=env, capture_output=True, text=True, timeout=30)

    def test_a_refused_die_views_stage_stops_the_build(self):
        good = self.build()
        self.assertIn("DIE_VIEWS_STAGED", good.stdout)
        self.assertIn("the pinned tree does not have the shape", good.stderr)
        refused = self.build(stager_rc=1)
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("DIE_VIEWS_STAGED", refused.stdout)
        self.assertNotIn("staging.pin's", refused.stderr)
        self.assertNotIn("the pinned tree", refused.stderr)

    def test_both_image_builds_get_the_die_views_pin(self):
        build = (ROOT / "build.sh").read_text()
        self.assertEqual(
            build.count('--build-arg "DIE_VIEWS_SHA256=$DIE_VIEWS_SHA256"'), 2)
        self.assertEqual(build.count('DIE_VIEWS_SHA256="$(pin_value STAGE_DIE_VIEWS_SHA256)"'), 1)
        self.assertEqual(build.count('"die_views": die_views,'), 1)
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertEqual(dockerfile.count("ARG DIE_VIEWS_SHA256"), 1)
        self.assertEqual(dockerfile.count("ENV ADK_DIE_VIEWS=/opt/adk-tools/die-views"), 1)
        self.assertIn('--expect-sha256 "$DIE_VIEWS_SHA256" --manifest', dockerfile)

    def test_good_pins_pass_the_shape_check(self):
        result = self.build()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("staging.pin's", result.stderr)
        self.assertIn("the pinned tree does not have the shape", result.stderr)

    def test_a_pin_of_another_shape_is_refused(self):
        a, b, c = (self.PINS[k] for k in ("NIX_OPENROAD_OUTPATH",
                                          "NIX_LIBRELANE_OUTPATH",
                                          "NIX_SG13G2_PDK_OUTPATH"))
        for var, bad in (("NIX_LIBRELANE_OUTPATH", None),
                         ("NIX_LIBRELANE_OUTPATH", a),
                         ("NIX_LIBRELANE_OUTPATH", b + "/bin/librelane"),
                         ("NIX_LIBRELANE_PYTHON_OUTPATH", None),
                         ("NIX_LIBRELANE_PYTHON_OUTPATH", b),
                         ("NIX_LIBRELANE_PYTHON_OUTPATH",
                          self.PINS["NIX_LIBRELANE_PYTHON_OUTPATH"] + "/bin/python3.13"),
                         ("NIX_LIBRELANE_PYTHON_OUTPATH",
                          "/nix/store/%s-python3-env" % ("o" * 32)),
                         ("NIX_SG13G2_PDK_OUTPATH", None),
                         ("NIX_SG13G2_PDK_OUTPATH", b),
                         ("NIX_SG13G2_PDK_OUTPATH", c.replace("/nix/store", "/other/store")),
                         ("NIX_COSIM_IVERILOG_OUTPATH", None),
                         ("NIX_COSIM_IVERILOG_OUTPATH", a),
                         ("NIX_COSIM_COCOTB_OUTPATH", self.PINS["NIX_COSIM_PYTHON_OUTPATH"]),
                         ("NIX_COSIM_PYTHON_OUTPATH", self.PINS["NIX_COSIM_COCOTB_OUTPATH"]),
                         ("NIX_COSIM_PYTHON_OUTPATH",
                          self.PINS["NIX_COSIM_PYTHON_OUTPATH"] + "/bin/python3"),
                         ("NIX_COSIM_DEVSHELL_OUTPATH", None),
                         ("NIX_COSIM_DEVSHELL_OUTPATH", self.PINS["NIX_COSIM_PYTHON_OUTPATH"]),
                         ("NIX_COSIM_DEVSHELL_OUTPATH",
                          self.PINS["NIX_COSIM_DEVSHELL_OUTPATH"] + "/bin"),
                         ("NIX_COSIM_DEVSHELL_OUTPATH", "/opt/adk-tools/cosim-devshell"),
                         ("NIX_MIXSIM_NGSPICE_OUTPATH", None),
                         ("NIX_MIXSIM_NGSPICE_OUTPATH", "/nix/store/%s-ngspice" % ("g" * 32)),
                         ("NIX_MIXSIM_IVERILOG_OUTPATH", self.PINS["NIX_COSIM_IVERILOG_OUTPATH"]),
                         ("NIX_SG13G2_OSDI_OUTPATH", c),
                         ("NIX_SG13G2_OSDI_OUTPATH",
                          self.PINS["NIX_SG13G2_OSDI_OUTPATH"] + "/osdi"),
                         ("NIX_GTKWAVE_OUTPATH", None),
                         ("NIX_GTKWAVE_OUTPATH", self.PINS["NIX_GAW_OUTPATH"]),
                         ("NIX_GAW_OUTPATH", self.PINS["NIX_GTKWAVE_OUTPATH"]),
                         ("NIX_GAW_OUTPATH", self.PINS["NIX_GAW_OUTPATH"] + "/bin/gaw"),
                         ("NIX_XSCHEM_OUTPATH", None),
                         ("NIX_XSCHEM_OUTPATH", self.PINS["NIX_GAW_OUTPATH"]),
                         ("NIX_XSCHEM_OUTPATH", "/nix/store/%s-xschem-3.4.8RC" % ("p" * 32)),
                         ("NIX_VAMS_GNUCAP_OUTPATH", None),
                         ("NIX_VAMS_GNUCAP_OUTPATH", self.PINS["NIX_VAMS_DEVSHELL_OUTPATH"]),
                         ("NIX_VAMS_GNUCAP_OUTPATH",
                          "/nix/store/%s-gnucap-b6678fe" % ("m" * 32)),
                         ("NIX_VAMS_DEVSHELL_OUTPATH", None),
                         ("NIX_VAMS_DEVSHELL_OUTPATH", self.PINS["NIX_COSIM_DEVSHELL_OUTPATH"]),
                         ("NIX_VAMS_DEVSHELL_OUTPATH", "/opt/adk-tools/vams-devshell")):
            with self.subTest(var=var, bad=bad):
                result = self.build(**{var: bad})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("Refusing to build: staging.pin's %s ('%s')"
                              % (var, bad or ""), result.stderr)
                self.assertNotIn("the pinned tree", result.stderr)


class BuildShEngineClosureTests(unittest.TestCase):
    # build.sh copied alone with good pins, stubs for stage-sources.sh,
    # ci/check_paths.py and nix/check-osdi-sha.sh that pass,
    # nix/cosim-closure.txt, nix/mixsim-closure.txt, nix/viewer-closure.txt and
    # nix/vams-closure.txt, nix/host-closure.txt, and a nix-store that prints
    # STUB_MIXSIM_CLOSURE when asked of ngspice, STUB_VIEWER_CLOSURE when asked
    # of GTKWave, STUB_VAMS_CLOSURE when asked of the Verilog-AMS devshell,
    # STUB_HOST_CLOSURE when asked of OpenROAD and STUB_CLOSURE otherwise. The store paths are made up, so lists the checks accept go on
    # to the packing, which then cannot find them: that refusal is how a test
    # sees the checks pass.

    PINS = BuildShStorePinTests.PINS
    ENGINE = [BuildShStorePinTests.PINS[k] for k in (
        "NIX_COSIM_IVERILOG_OUTPATH", "NIX_COSIM_COCOTB_OUTPATH", "NIX_COSIM_PYTHON_OUTPATH",
        "NIX_COSIM_DEVSHELL_OUTPATH")]
    MIXSIM = [BuildShStorePinTests.PINS[k] for k in (
        "NIX_MIXSIM_NGSPICE_OUTPATH", "NIX_MIXSIM_IVERILOG_OUTPATH", "NIX_SG13G2_OSDI_OUTPATH")]
    VIEWERS = [BuildShStorePinTests.PINS[k] for k in ("NIX_GTKWAVE_OUTPATH", "NIX_GAW_OUTPATH",
                                                      "NIX_XSCHEM_OUTPATH")]
    VAMS = [BuildShStorePinTests.PINS[k] for k in ("NIX_VAMS_DEVSHELL_OUTPATH",
                                                   "NIX_VAMS_GNUCAP_OUTPATH")]
    HOST = [BuildShStorePinTests.PINS[k] for k in (
        "NIX_OPENROAD_OUTPATH", "NIX_LIBRELANE_OUTPATH", "NIX_LIBRELANE_PYTHON_OUTPATH",
        "NIX_SG13G2_PDK_OUTPATH")]
    GLIBC = "/nix/store/%s-glibc-2.40-66" % ("0" * 32)
    FFTW = "/nix/store/%s-fftw-double-3.3.10" % ("2" * 32)
    GTK = "/nix/store/%s-gtk+3-3.24.49" % ("3" * 32)
    GCC = "/nix/store/%s-gcc-14.3.0" % ("4" * 32)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(ROOT / "build.sh", self.dir / "build.sh")
        for script, body in (("stage-sources.sh", "#!/bin/sh\nexit 0\n"),
                             ("stage-die-views.sh", "#!/bin/sh\nexit 0\n"),
                             ("bin/nix-store",
                              '#!/bin/sh\necho "$*" >> "$STUB_ARGS"\n'
                              '[ -z "$STUB_FAILS" ] || case "$*" in *"$STUB_FAILS"*) exit 1 ;; esac\n'
                              '[ "$2" != --hash ] || { shift 2; for p; do echo "sha256:$STUB_HASH"; done; exit 0; }\n'
                              'case "$3" in\n'
                              '    *-ngspice-*) printf \'%s\' "$STUB_MIXSIM_CLOSURE" ;;\n'
                              '    *-gtkwave-*) printf \'%s\' "$STUB_VIEWER_CLOSURE" ;;\n'
                              '    *-vams-devshell) printf \'%s\' "$STUB_VAMS_CLOSURE" ;;\n'
                              '    *-openroad-*) printf \'%s\' "$STUB_HOST_CLOSURE" ;;\n'
                              '    *) printf \'%s\' "$STUB_CLOSURE" ;;\n'
                              'esac\n'),
                             ("nix/check-osdi-sha.sh",
                              '#!/bin/sh\necho "$*" > "$STUB_OSDI_ARGS"\nexit "${STUB_OSDI_RC:-0}"\n')):
            (self.dir / script).parent.mkdir(exist_ok=True)
            (self.dir / script).write_text(body)
            (self.dir / script).chmod(0o755)
        (self.dir / "ci").mkdir()
        (self.dir / "ci" / "check_paths.py").write_text("import sys\nsys.exit(0)\n")
        (self.dir / "ci" / "check_layer_depth.py").write_text(
            "import os\nimport sys\n"
            "open(os.environ['STUB_LAYER_ARGS'], 'w').write(' '.join(sys.argv[1:]))\n"
            "sys.exit(int(os.environ.get('STUB_LAYER_DEPTH_RC', '0')))\n")
        shutil.copy(ROOT / "ci" / "check_store_closure.py", self.dir / "ci")
        pin_set_repo(self.dir)
        (self.dir / "staging.pin").write_text("".join(
            '%s="%s"\n' % kv for kv in self.PINS.items()))
        self.paths = sorted(self.ENGINE + [self.GLIBC])
        self.listing(self.paths)
        # The mixed-signal list leaves out what the co-simulation one has.
        self.mixsim = sorted(self.MIXSIM + [self.FFTW])
        self.listing(self.mixsim, name="mixsim-closure.txt")
        # The viewers' list leaves out what both engines' lists have.
        self.viewers = sorted(self.VIEWERS + [self.GTK])
        self.listing(self.viewers, name="viewer-closure.txt")
        # The Verilog-AMS list leaves out what the other three have.
        self.vams = sorted(self.VAMS + [self.GCC])
        self.listing(self.vams, name="vams-closure.txt")
        # The host's list is the whole closure, whatever the image's lists hold.
        self.host = sorted(self.HOST + [self.GLIBC])
        self.listing(self.host, name="host-closure.txt")
        # The lock gives each host path the narHash the stub's store registers.
        self.lock(self.host)

    def lock(self, paths, sha256=HEX):
        (self.dir / "nix" / "host-closure.lock").write_text(
            "# the host closure's narHashes\n" + "".join("%s %s\n" % (p, sha256) for p in paths))

    def listing(self, paths, header="# the engine's closure\n", name="cosim-closure.txt"):
        (self.dir / "nix" / name).write_text(header + "".join(p + "\n" for p in paths))

    def build(self, closure=None, fails=None, nix_store=True, mixsim=None, osdi_rc=0,
              viewers=None, vams=None, host=None, registered=HEX, layer_rc=0, args=()):
        path = ("%s:" % (self.dir / "bin") if nix_store else "") + "/usr/bin:/bin"
        env = {k: v for k, v in os.environ.items() if k != "VERIFY_TAG"}
        env.update(PATH=path, STUB_ARGS=str(self.dir / "args"),
                   STUB_CLOSURE="\n".join(self.paths if closure is None else closure) + "\n",
                   STUB_MIXSIM_CLOSURE="\n".join(
                       sorted(self.mixsim + [self.GLIBC]) if mixsim is None else mixsim) + "\n",
                   STUB_VIEWER_CLOSURE="\n".join(
                       sorted(self.viewers + [self.GLIBC, self.FFTW])
                       if viewers is None else viewers) + "\n",
                   STUB_VAMS_CLOSURE="\n".join(
                       sorted(self.vams + [self.GLIBC, self.GTK])
                       if vams is None else vams) + "\n",
                   STUB_HOST_CLOSURE="\n".join(self.host if host is None else host) + "\n",
                   STUB_FAILS=fails or "", STUB_HASH=registered,
                   STUB_OSDI_ARGS=str(self.dir / "osdi-args"), STUB_OSDI_RC=str(osdi_rc),
                   STUB_LAYER_DEPTH_RC=str(layer_rc), STUB_LAYER_ARGS=str(self.dir / "layer-args"))
        (self.dir / "args").unlink(missing_ok=True)
        return subprocess.run(["bash", str(self.dir / "build.sh"), *args], cwd=self.dir,
                              env=env, capture_output=True, text=True, timeout=30)

    def assertRefused(self, result, why):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Refusing to build: " + why, result.stderr)
        self.assertFalse((self.dir / "cosim-closure.tar").exists())
        self.assertFalse((self.dir / "mixsim-closure.tar").exists())
        self.assertFalse((self.dir / "viewer-closure.tar").exists())
        self.assertFalse((self.dir / "vams-closure.tar").exists())

    def test_a_target_at_the_layer_limit_refuses_before_any_store_read(self):
        result = self.build(layer_rc=1)
        self.assertRefused(result, "a target is too close to the overlay layer limit")
        self.assertFalse((self.dir / "args").exists())

    def test_the_layer_limit_holds_only_the_targets_the_run_builds(self):
        for args, targets in (((), "tools studio-builder runtime verify"),
                              (("--skip-verify",), "tools studio-builder runtime"),
                              (("--tools-only",), "tools")):
            with self.subTest(args=args):
                self.build(args=args)
                self.assertEqual((self.dir / "layer-args").read_text(),
                                 " ".join("--target " + t for t in targets.split()))

    def test_the_store_closure_is_asked_of_each_list(self):
        result = self.build()
        self.assertRefused(result, "could not pack the engine's closure (cosim-closure.tar)")
        self.assertEqual((self.dir / "args").read_text().splitlines(),
                         [" ".join(["--query", "--requisites",
                                    self.PINS["NIX_COSIM_DEVSHELL_OUTPATH"],
                                    self.PINS["NIX_COSIM_PYTHON_OUTPATH"],
                                    self.PINS["NIX_COSIM_IVERILOG_OUTPATH"]]),
                          " ".join(["--query", "--requisites", *self.MIXSIM]),
                          " ".join(["--query", "--requisites", *self.VIEWERS]),
                          " ".join(["--query", "--requisites", *self.VAMS]),
                          " ".join(["--query", "--requisites", *self.HOST]),
                          " ".join(["--query", "--hash", *self.host])])
        self.assertEqual((self.dir / "osdi-args").read_text().split(),
                         [self.PINS["NIX_SG13G2_OSDI_OUTPATH"] + "/osdi",
                          "nix/sg13g2-osdi.sha256"])

    def test_a_host_lock_this_store_does_not_hold_is_refused(self):
        # Named per path and before anything is packed.
        self.lock(self.host, sha256="f" * 64)
        result = self.build()
        self.assertRefused(result, "nix/host-closure.lock is not what this host's store holds")
        for path in self.host:
            self.assertIn("host closure: %s has narHash %s on this build host, not the lock's %s"
                          % (path, HEX, "f" * 64), result.stderr)
        self.lock(self.host)
        result = self.build(fails="--hash")
        self.assertRefused(result, "nix/host-closure.lock is not what this host's store holds")
        for path in self.host:
            self.assertIn("host closure: %s is not valid in this build host's store" % path,
                          result.stderr)
        self.assertFalse((self.dir / "host-closure.tar").exists())

    def test_a_host_lock_missing_or_of_other_paths_is_refused(self):
        self.lock(self.host[1:])
        result = self.build()
        self.assertRefused(result, "nix/host-closure.lock is not what this host's store holds")
        self.assertIn("host closure: nix/host-closure.lock does not list exactly the paths "
                      "nix/host-closure.txt lists", result.stderr)
        (self.dir / "nix" / "host-closure.lock").unlink()
        self.assertRefused(self.build(), "nix/host-closure.lock is missing")

    def test_a_list_other_than_the_store_closure_is_refused(self):
        for closure in (self.paths[1:], self.paths + ["/nix/store/%s-zlib-1.3.1" % ("1" * 32)]):
            with self.subTest(closure=closure):
                result = self.build(closure=closure)
                self.assertRefused(result, "nix/cosim-closure.txt is not the store's closure")

    def test_a_mixed_signal_list_other_than_its_closure_less_the_other_is_refused(self):
        why = "nix/mixsim-closure.txt is not the store's closure of " + self.MIXSIM[0]
        for mixsim in (self.mixsim[1:],
                       sorted(self.mixsim + ["/nix/store/%s-zlib-1.3.1" % ("1" * 32)])):
            with self.subTest(mixsim=mixsim):
                self.assertRefused(self.build(mixsim=mixsim), why)
        # A path both engines need belongs to the co-simulation list only.
        self.listing(sorted(self.mixsim + [self.GLIBC]), name="mixsim-closure.txt")
        self.assertRefused(self.build(), why)

    def test_a_viewer_list_other_than_its_closure_less_the_others_is_refused(self):
        why = "nix/viewer-closure.txt is not the store's closure of " + self.VIEWERS[0]
        for viewers in (self.viewers[1:],
                        sorted(self.viewers + ["/nix/store/%s-zlib-1.3.1" % ("1" * 32)])):
            with self.subTest(viewers=viewers):
                self.assertRefused(self.build(viewers=viewers), why)
        # A path an engine already brings belongs to that engine's list only.
        for other in (self.GLIBC, self.FFTW):
            with self.subTest(other=other):
                self.listing(sorted(self.viewers + [other]), name="viewer-closure.txt")
                self.assertRefused(self.build(), why)
        self.listing(self.viewers, name="viewer-closure.txt")

    def test_a_verilog_ams_list_other_than_its_closure_less_the_others_is_refused(self):
        why = "nix/vams-closure.txt is not the store's closure of " + self.VAMS[0]
        for vams in (self.vams[1:],
                     sorted(self.vams + ["/nix/store/%s-zlib-1.3.1" % ("1" * 32)])):
            with self.subTest(vams=vams):
                self.assertRefused(self.build(vams=vams), why)
        # A path an engine or a viewer already brings belongs to that list only.
        for other in (self.GLIBC, self.FFTW, self.GTK):
            with self.subTest(other=other):
                self.listing(sorted(self.vams + [other]), name="vams-closure.txt")
                self.assertRefused(self.build(), why)
        self.listing(self.vams, name="vams-closure.txt")

    def test_a_host_list_other_than_its_closure_is_refused(self):
        why = "nix/host-closure.txt is not the store's closure of " + self.HOST[0]
        for host in (self.host[1:],
                     sorted(self.host + ["/nix/store/%s-zlib-1.3.1" % ("1" * 32)])):
            with self.subTest(host=host):
                self.assertRefused(self.build(host=host), why)

    def test_a_host_list_of_the_wrong_shape_is_refused(self):
        name = "host-closure.txt"
        for paths, why in (
                (None, "is missing"),
                (list(reversed(self.host)), "is not sorted, or names a path twice"),
                ([p for p in self.host if "openroad" not in p],
                 "does not list " + self.PINS["NIX_OPENROAD_OUTPATH"]),
                ([p for p in self.host if "ihp-open-pdk" not in p],
                 "does not list " + self.PINS["NIX_SG13G2_PDK_OUTPATH"])):
            with self.subTest(why=why):
                if paths is None:
                    (self.dir / "nix" / name).unlink()
                else:
                    self.listing(paths, name=name)
                self.assertRefused(self.build(), "nix/%s %s" % (name, why))
                self.listing(self.host, name=name)

    def test_an_engine_missing_from_the_store_is_refused(self):
        self.assertRefused(self.build(fails="python3-3.13.9-env"),
                           "the co-simulation engine is not in this host's Nix store")
        self.assertRefused(self.build(fails="ngspice-45"),
                           "the mixed-signal engine is not in this host's Nix store")
        self.assertRefused(self.build(fails="gtkwave-3.3.121"),
                           "the viewers are not in this host's Nix store")
        self.assertRefused(self.build(fails="vams-devshell"),
                           "the Verilog-AMS engine is not in this host's Nix store")
        self.assertRefused(self.build(fails="openroad-2026-02-17"),
                           "OpenROAD, LibreLane, its interpreter or the sg13g2 PDK is not in "
                           "this host's Nix store")

    def test_osdi_files_other_than_the_list_are_refused(self):
        self.assertRefused(self.build(osdi_rc=1),
                           "the OSDI models at %s are not the files nix/sg13g2-osdi.sha256 names"
                           % self.PINS["NIX_SG13G2_OSDI_OUTPATH"])

    def test_no_nix_store_is_refused(self):
        if shutil.which("nix-store", path="/usr/bin:/bin"):
            self.skipTest("this host has nix-store in /usr/bin or /bin")
        self.assertRefused(self.build(nix_store=False), "no nix-store on PATH")

    def test_a_list_of_the_wrong_shape_is_refused(self):
        unsorted = "is not sorted, or names a path twice"
        cases = (
            (None, "is missing"),
            ([], "lists no store path"),
            (self.paths + ["/nix/store/short-name"],
             "has a line that is not a /nix/store/<hash>-<name> path"),
            (list(reversed(self.paths)), unsorted),
            (self.paths + self.paths[-1:], unsorted),
            ([p for p in self.paths if "cocotb" not in p],
             "does not list " + self.PINS["NIX_COSIM_COCOTB_OUTPATH"]),
            ([p for p in self.paths if "cosim-devshell" not in p],
             "does not list " + self.PINS["NIX_COSIM_DEVSHELL_OUTPATH"]),
        )
        for paths, why in cases:
            with self.subTest(why=why):
                if paths is None:
                    (self.dir / "nix" / "cosim-closure.txt").unlink()
                else:
                    self.listing(paths)
                self.assertRefused(self.build(), "nix/cosim-closure.txt " + why)
                self.listing(self.paths)

    def test_a_mixed_signal_list_of_the_wrong_shape_is_refused(self):
        name = "mixsim-closure.txt"
        for paths, why in (
                (None, "is missing"),
                (list(reversed(self.mixsim)), "is not sorted, or names a path twice"),
                ([p for p in self.mixsim if "osdi" not in p],
                 "does not list " + self.PINS["NIX_SG13G2_OSDI_OUTPATH"])):
            with self.subTest(why=why):
                if paths is None:
                    (self.dir / "nix" / name).unlink()
                else:
                    self.listing(paths, name=name)
                self.assertRefused(self.build(), "nix/%s %s" % (name, why))
                self.listing(self.mixsim, name=name)

    def test_a_viewer_list_of_the_wrong_shape_is_refused(self):
        name = "viewer-closure.txt"
        for paths, why in (
                (None, "is missing"),
                (list(reversed(self.viewers)), "is not sorted, or names a path twice"),
                ([p for p in self.viewers if "gaw3" not in p],
                 "does not list " + self.PINS["NIX_GAW_OUTPATH"]),
                ([p for p in self.viewers if "xschem" not in p],
                 "does not list " + self.PINS["NIX_XSCHEM_OUTPATH"])):
            with self.subTest(why=why):
                if paths is None:
                    (self.dir / "nix" / name).unlink()
                else:
                    self.listing(paths, name=name)
                self.assertRefused(self.build(), "nix/%s %s" % (name, why))
                self.listing(self.viewers, name=name)

    def test_a_verilog_ams_list_of_the_wrong_shape_is_refused(self):
        name = "vams-closure.txt"
        for paths, why in (
                (None, "is missing"),
                (list(reversed(self.vams)), "is not sorted, or names a path twice"),
                ([p for p in self.vams if "modelgen" not in p],
                 "does not list " + self.PINS["NIX_VAMS_GNUCAP_OUTPATH"]),
                ([p for p in self.vams if "vams-devshell" not in p],
                 "does not list " + self.PINS["NIX_VAMS_DEVSHELL_OUTPATH"])):
            with self.subTest(why=why):
                if paths is None:
                    (self.dir / "nix" / name).unlink()
                else:
                    self.listing(paths, name=name)
                self.assertRefused(self.build(), "nix/%s %s" % (name, why))
                self.listing(self.vams, name=name)


class StageDieViewsTests(unittest.TestCase):
    # stage-die-views.sh and the checker copied into a temp checkout, a source
    # directory of two made-up records in another temp directory, the listing
    # written from it and staging.pin pinning the listing. Each refusal case
    # changes one thing from the case that stages, and nothing is written into
    # die-views/ when the source is refused.

    A, B = "a" * 64, "b" * 64

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        (self.repo / "ci").mkdir()
        shutil.copy(ROOT / "stage-die-views.sh", self.repo / "stage-die-views.sh")
        shutil.copy(ROOT / "ci" / "check_die_views.py", self.repo / "ci" / "check_die_views.py")
        self.outer = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.outer)
        self.src = self.outer / "run"
        for layout in (self.A, self.B):
            d = self.src / "records" / layout
            (d / "netlist").mkdir(parents=True)
            (d / "eda-views.json").write_text(
                json.dumps({"bound_to": {"layout_sha256": layout}}))
            (d / "netlist" / "core.nl.v").write_text("module core; endmodule\n")
        self.write_listing()

    def write_listing(self, pin=None):
        records = self.src / "records"
        lines = sorted("%s  records/%s\n" % (
            hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(records).as_posix())
            for p in records.rglob("*") if p.is_file() and not p.is_symlink())
        lines.sort(key=lambda l: l.split("  ", 1)[1])
        listing = self.repo / "die-views.sha256"
        listing.write_text("".join(lines))
        pin = pin or hashlib.sha256(listing.read_bytes()).hexdigest()
        (self.repo / "staging.pin").write_text('STAGE_DIE_VIEWS_SHA256="%s"\n' % pin)

    def snapshot(self):
        out = []
        for p in sorted(self.outer.rglob("*")):
            st = p.lstat()
            out.append((p.relative_to(self.outer).as_posix(), st.st_mode, st.st_size,
                        st.st_mtime_ns, p.read_bytes() if p.is_file() and not p.is_symlink()
                        else None))
        return out

    def stage(self, *args, src="SRC", path=None):
        env = {k: v for k, v in os.environ.items() if k != "STAGE_DIE_VIEWS_DIR"}
        if src == "SRC":
            env["STAGE_DIE_VIEWS_DIR"] = str(self.src)
        elif src is not None:
            env["STAGE_DIE_VIEWS_DIR"] = src
        if path is not None:
            env["PATH"] = "%s:%s" % (path, env["PATH"])
        return subprocess.run(["bash", str(self.repo / "stage-die-views.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def assertRefused(self, result, why):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(why, result.stderr)
        self.assertFalse((self.repo / "die-views").exists())

    def test_the_listed_records_are_staged_and_the_source_is_only_read(self):
        before = self.snapshot()
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("holds 4 files in 2 records", result.stdout)
        staged = sorted(p.relative_to(self.repo / "die-views").as_posix()
                        for p in (self.repo / "die-views").rglob("*") if p.is_file())
        self.assertEqual(staged, sorted(
            "%s/%s" % (l, f) for l in (self.A, self.B)
            for f in ("eda-views.json", "netlist/core.nl.v")))
        for rel in staged:
            self.assertEqual((self.repo / "die-views" / rel).read_bytes(),
                             (self.src / "records" / rel).read_bytes())
        self.assertEqual(self.snapshot(), before)

    def test_check_only_writes_nothing(self):
        before = self.snapshot()
        result = self.stage("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / "die-views").exists())
        self.assertEqual(self.snapshot(), before)

    def test_the_variable_unset_or_relative_refuses(self):
        self.assertRefused(self.stage(src=None), "STAGE_DIE_VIEWS_DIR is not set")
        self.assertRefused(self.stage(src=""), "STAGE_DIE_VIEWS_DIR is not set")
        self.assertRefused(self.stage(src="run"), "must be an absolute path, not run")
        self.assertRefused(self.stage(src=str(self.outer / "absent")), "is not a directory")

    def test_a_source_that_is_a_symlink_refuses(self):
        link = self.outer / "link"
        link.symlink_to(self.src)
        self.assertRefused(self.stage(src=str(link)), "is not a directory")

    def test_one_extra_file_refuses(self):
        (self.src / "records" / self.A / "netlist" / "stray.v").write_text("x\n")
        self.assertRefused(self.stage(), "is not listed")

    def test_one_missing_file_refuses(self):
        (self.src / "records" / self.B / "netlist" / "core.nl.v").unlink()
        self.assertRefused(self.stage(), "is listed and missing")

    def test_a_listing_one_byte_off_refuses(self):
        listing = self.repo / "die-views.sha256"
        text = listing.read_text()
        listing.write_text(("0" if text[0] != "0" else "1") + text[1:])
        self.assertRefused(self.stage(), "not the pinned")

    def test_a_pin_that_is_not_a_sha256_refuses(self):
        self.write_listing(pin="9e908014")
        self.assertRefused(self.stage(), "is not a sha256")

    def test_a_directory_name_that_differs_from_its_binding_refuses(self):
        record = self.src / "records" / self.A / "eda-views.json"
        record.write_text(json.dumps({"bound_to": {"layout_sha256": self.B}}))
        self.write_listing()
        self.assertRefused(self.stage(), "holds a record bound to %s" % self.B)

    def test_a_symlink_in_the_source_refuses(self):
        p = self.src / "records" / self.A / "netlist" / "core.nl.v"
        keep = self.outer / "core.nl.v"
        keep.write_bytes(p.read_bytes())
        p.unlink()
        p.symlink_to(keep)
        self.assertRefused(self.stage(), "is a symlink")

    def test_a_symlink_in_the_copy_refuses(self):
        # An install that links instead of copying: the source passes, the
        # copy does not.
        bindir = self.outer / "bin"
        bindir.mkdir()
        (bindir / "install").write_text(
            '#!/bin/sh\nmkdir -p "$(dirname "$6")" && ln -s "$5" "$6"\n')
        (bindir / "install").chmod(0o755)
        result = self.stage(path=str(bindir))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("is a symlink", result.stderr)
        self.assertIn("the copy in die-views is not what", result.stderr)

if __name__ == "__main__":
    unittest.main()
