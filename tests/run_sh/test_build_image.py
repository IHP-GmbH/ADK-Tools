"""openroad/build-image.sh, run against a docker and a git stub that record their argv.

The build goes through `docker buildx build --load`, which hostslot's docker shim sends to the
slot's own capped BuildKit, and its compile jobs default to the slot's CPUs.
"""

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "openroad" / "build-image.sh"
PIN = dict(line.split("=", 1) for line in (ROOT / "openroad" / "openroad.pin").read_text()
           .splitlines() if "=" in line and not line.startswith("#"))

DOCKER = """#!/bin/sh
echo "docker $*" >> "$LOG"
case "$1 $2" in
  "image inspect") exit 1 ;;
esac
exit 0
"""
GIT = """#!/bin/sh
case "$*" in *rev-parse*) echo "$COMMIT" ;; esac
exit 0
"""


class BuildImageTest(unittest.TestCase):
    def run_script(self, **env):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, text in (("docker", DOCKER), ("git", GIT)):
            (tmp / name).write_text(text)
            (tmp / name).chmod(0o755)
        (tmp / "src").mkdir()
        log = tmp / "log"
        base = {"PATH": f"{tmp}:/usr/bin:/bin", "LOG": str(log),
                "COMMIT": PIN["OPENROAD_COMMIT"].strip(), "OPENROAD_SRC": str(tmp / "src")}
        got = subprocess.run(["bash", str(SCRIPT), "--force"], capture_output=True, text=True,
                             env={**base, **env})
        return got, log.read_text().splitlines() if log.exists() else []

    def build_line(self, calls):
        (build,) = [c for c in calls if " build " in f"{c} "]
        return build

    def test_the_build_is_a_buildx_build_that_loads_its_image(self):
        got, calls = self.run_script(HOSTSLOT_CPUS="8")
        self.assertEqual(got.returncode, 0, got.stderr)
        build = self.build_line(calls)
        self.assertTrue(build.startswith("docker buildx build --load "), build)
        self.assertIn("numThreads=8", build)
        self.assertIn("-t " + PIN["OPENROAD_3DBLOX_IMAGE"].strip(), build)

    def test_jobs_default_and_override(self):
        for env, jobs in (({}, 8), ({"HOSTSLOT_CPUS": "4"}, 4), ({"JOBS": "6"}, 6),
                          ({"JOBS": "08"}, 8), ({"JOBS": "3", "HOSTSLOT_CPUS": "4"}, 3)):
            got, calls = self.run_script(**env)
            self.assertEqual(got.returncode, 0, (env, got.stderr))
            self.assertIn(f"numThreads={jobs} ", self.build_line(calls) + " ", env)

    def test_jobs_must_be_a_whole_number_above_zero(self):
        for jobs in ("0", "00", "x", "2.5", "-1"):
            got, calls = self.run_script(JOBS=jobs)
            self.assertEqual(got.returncode, 1, jobs)
            self.assertEqual([c for c in calls if "build" in c], [], jobs)


if __name__ == "__main__":
    unittest.main()
