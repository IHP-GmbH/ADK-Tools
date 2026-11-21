"""release.sh: what it takes, and every way it stops before or after a push.

The argument cases run against a git stub that marks the first call and stops it, and docker
and gh stubs that fail loudly. The gate cases run against stubs of git, docker and gh that
answer as a scenario sets them and log every call, so no case reaches a daemon or a registry.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "release.sh"
REFUSED = "error: VERSION_TAG must look like vYYYY.MM or vYYYY.MM.N"
ID_REFUSED = "error: the image must be given by its ID"
IMAGE_ID = "sha256:" + "0123456789abcdef" * 4
DIGEST = "sha256:" + "fedcba9876543210" * 4
TAG = "v2026.11.3"
PUSH_REF = f"ghcr.io/ihp-gmbh/adk-tools:{TAG}"
REACHED = "REACHED_GIT"
FIRST_CALL_STUBS = {
    "git": f"#!/bin/sh\necho {REACHED} >&2\nexit 1\n",
    "docker": "#!/bin/sh\necho 'docker must not run' >&2\nexit 99\n",
    "gh": "#!/bin/sh\necho 'gh must not run' >&2\nexit 99\n",
}

GIT_STUB = r"""#!/bin/bash
echo "git $*" >>"$S_LOG"
case "$1" in
    status) printf '%s' "$S_STATUS" ;;
    submodule) echo " 0123456789abcdef0123456789abcdef01234567 tools/adk (heads/main)" ;;
    describe) echo "$S_DESCRIBE" ;;
    *) echo "unexpected git $*" >&2; exit 99 ;;
esac
"""

DOCKER_STUB = r"""#!/bin/bash
echo "docker $*" >>"$S_LOG"
case "$1" in
    image)
        if [ "$3" = --format ]; then
            case "$4" in
                '{{.Id}}') echo "$5" ;;
                '{{len .RepoTags}}') echo "$S_TAGS" ;;
                '{{range .RepoDigests}}{{println .}}{{end}}')
                    echo "ghcr.io/ihp-gmbh/adk-tools@$S_REPODIGEST" ;;
                *) echo "unexpected format $4" >&2; exit 99 ;;
            esac
        else
            [ "$S_EXISTING" = 1 ] || { echo "Error: No such image: $3" >&2; exit 1; }
            echo '[{}]'
        fi ;;
    run) echo "{\"meta\": \"$S_META\"}" ;;
    login) cat >/dev/null ;;
    logout|tag|rmi) ;;
    push) echo "$S_TAG: digest: $S_DIGEST size: 4321" ;;
    manifest)
        echo "manifest DOCKER_CONFIG=${DOCKER_CONFIG:-unset}" >>"$S_LOG"
        case "$S_ANON" in
            refused) echo "unauthorized: authentication required" >&2; exit 1 ;;
            denied) echo "denied: requested access to the resource is denied" >&2; exit 1 ;;
            bare) echo "Get \"https://ghcr.io/v2/ihp-gmbh/adk-tools/manifests/$S_TAG\": unauthorized" >&2; exit 1 ;;
            proxy) echo "Get \"https://ghcr.io/v2/x\": proxyconnect tcp: unauthorized" >&2; exit 1 ;;
            other_host) echo "Get \"https://proxy.example/\": unauthorized" >&2; exit 1 ;;
            open) echo '{"schemaVersion": 2}' ;;
            *) echo "dial tcp: lookup ghcr.io: no such host" >&2; exit 1 ;;
        esac ;;
    *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
"""

GH_STUB = r"""#!/bin/bash
echo "gh $*" >>"$S_LOG"
api=orgs/IHP-GmbH/packages/container/adk-tools
case "$*" in
    "auth token") echo token ;;
    "api user --jq .login") echo someone ;;
    "api $api --jq .visibility")
        n=$(( $(cat "$S_LOG.vis" 2>/dev/null || echo 0) + 1 ))
        echo "$n" >"$S_LOG.vis"
        if [ "$n" = 1 ]; then v="$S_BEFORE"; else v="$S_AFTER"; fi
        case "$v" in
            404) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
            401) echo "gh: Bad credentials (HTTP 401)" >&2; exit 1 ;;
            *) echo "$v" ;;
        esac ;;
    "api --paginate $api/versions --jq "*)
        [ "$S_VERSIONS" = fail ] && { echo "gh: Server Error (HTTP 500)" >&2; exit 1; }
        printf '%s\n' $S_VERSIONS ;;
    *) echo "unexpected gh $*" >&2; exit 99 ;;
esac
"""

SCENARIO = {
    "S_STATUS": "", "S_DESCRIBE": TAG, "S_META": TAG, "S_EXISTING": "0", "S_TAGS": "2",
    "S_BEFORE": "404", "S_AFTER": "private", "S_VERSIONS": "v2026.11 v2026.11.2",
    "S_ANON": "refused", "S_TAG": TAG, "S_DIGEST": DIGEST, "S_REPODIGEST": DIGEST,
}


def stub_dir(case, stubs):
    tmp = Path(case.enterContext(tempfile.TemporaryDirectory()))
    for name, text in stubs.items():
        (tmp / name).write_text(text)
        (tmp / name).chmod(0o755)
    return tmp


class ReleaseArgumentsTest(unittest.TestCase):
    def run_script(self, tag, image_id=IMAGE_ID):
        tmp = stub_dir(self, FIRST_CALL_STUBS)
        args = [tag] if image_id is None else [tag, image_id]
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                              env={"PATH": f"{tmp}:/usr/bin:/bin"}, timeout=60)

    def test_a_release_and_a_point_release_reach_the_provenance_checks(self):
        for tag in ("v2026.11", "v2026.11.1", "v2026.11.2", "v2026.11.10"):
            with self.subTest(tag=tag):
                got = self.run_script(tag)
                self.assertIn(REACHED, got.stderr)
                self.assertNotIn(REFUSED, got.stderr)

    def test_any_other_tag_is_refused_before_git(self):
        for tag in ("2026.11", "v2026.1", "v2026.11.", "v2026.11.0", "v2026.11.02",
                    "v2026.11.2.1", "v2026.11.x", "v2026.11-rc1", "v2026.11.2 "):
            with self.subTest(tag=tag):
                got = self.run_script(tag)
                self.assertEqual(got.returncode, 1, got.stderr)
                self.assertIn(REFUSED, got.stderr)
                self.assertNotIn(REACHED, got.stderr)

    def test_an_image_not_given_by_its_id_is_refused_before_git(self):
        for image_id in (None, "", "adk-tools:dev", "runtime-dd0e32d", IMAGE_ID[:-1],
                         IMAGE_ID.upper(), IMAGE_ID + "0", IMAGE_ID[len("sha256:"):]):
            with self.subTest(image_id=image_id):
                got = self.run_script(TAG, image_id)
                self.assertEqual(got.returncode, 1, got.stderr)
                self.assertIn(ID_REFUSED, got.stderr)
                self.assertNotIn(REACHED, got.stderr)

    def test_a_third_argument_is_refused_before_git(self):
        tmp = stub_dir(self, FIRST_CALL_STUBS)
        got = subprocess.run(["bash", str(SCRIPT), TAG, IMAGE_ID, "latest"],
                             capture_output=True, text=True,
                             env={"PATH": f"{tmp}:/usr/bin:/bin"}, timeout=60)
        self.assertEqual(got.returncode, 1, got.stderr)
        self.assertIn("usage:", got.stderr)
        self.assertNotIn(REACHED, got.stderr)


class ReleaseGateTest(unittest.TestCase):
    def run_gate(self, **changes):
        tmp = stub_dir(self, {"git": GIT_STUB, "docker": DOCKER_STUB, "gh": GH_STUB})
        log = tmp / "calls.log"
        log.touch()
        env = {"PATH": f"{tmp}:/usr/bin:/bin", "HOME": str(tmp), "S_LOG": str(log),
               "TMPDIR": str(tmp), **SCENARIO, **changes}
        got = subprocess.run(["bash", str(SCRIPT), TAG, IMAGE_ID], capture_output=True,
                             text=True, env=env, timeout=60)
        return got, log.read_text().splitlines()

    def assert_not_pushed(self, calls):
        self.assertFalse([c for c in calls if c.startswith(("docker push", "docker tag",
                                                            "docker login"))], calls)

    def test_a_first_release_is_pushed_once_under_its_tag_and_read_private(self):
        got, calls = self.run_gate()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual([c for c in calls if c.startswith("docker push")],
                         [f"docker push {PUSH_REF}"])
        self.assertIn(f"docker tag {IMAGE_ID} {PUSH_REF}", calls)
        self.assertFalse([c for c in calls if "latest" in c], calls)
        self.assertIn(f"Done: ghcr.io/ihp-gmbh/adk-tools@{DIGEST}", got.stdout)
        self.assertNotIn(f"docker manifest inspect {PUSH_REF}", calls[:calls.index(
            f"docker push {PUSH_REF}")])
        anon = [c for c in calls if c.startswith("manifest DOCKER_CONFIG=")]
        self.assertEqual(len(anon), 1, calls)
        self.assertNotEqual(anon[0], "manifest DOCKER_CONFIG=unset")
        self.assertIn("docker logout ghcr.io", calls)
        self.assertEqual(calls[-1], f"docker rmi {PUSH_REF}")

    def test_the_push_tag_stays_when_it_is_the_image_only_tag(self):
        got, calls = self.run_gate(S_TAGS="1")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertFalse([c for c in calls if c.startswith("docker rmi")], calls)
        self.assertIn("stays on this daemon", got.stderr)

    def test_a_private_package_takes_a_new_tag_and_refuses_a_published_one(self):
        got, calls = self.run_gate(S_BEFORE="private")
        self.assertEqual(got.returncode, 0, got.stderr)
        got, calls = self.run_gate(S_BEFORE="private", S_VERSIONS=f"v2026.11 {TAG}")
        self.assertEqual(got.returncode, 1, got.stderr)
        self.assertIn("already published", got.stderr)
        self.assert_not_pushed(calls)

    def test_a_refusal_before_the_push_pushes_nothing(self):
        cases = {
            "dirty tree": ({"S_STATUS": " M release.sh"}, 1, "working tree is dirty"),
            "head off the tag": ({"S_DESCRIBE": "v2026.11.2"}, 1, "not exactly at tag"),
            "image from another build": ({"S_META": f"{TAG}-dirty"}, 1, "was built from"),
            "push tag already local": ({"S_EXISTING": "1"}, 1, "already a tag"),
            "public package": ({"S_BEFORE": "public"}, 3, "not private; nothing pushed"),
            "visibility unreadable": ({"S_BEFORE": "401"}, 3, "cannot read the package"),
            "tags unreadable": ({"S_BEFORE": "private", "S_VERSIONS": "fail"}, 3,
                                "cannot read the package's published tags"),
        }
        for name, (changes, rc, message) in cases.items():
            with self.subTest(name):
                got, calls = self.run_gate(**changes)
                self.assertEqual(got.returncode, rc, got.stderr)
                self.assertIn(message, got.stderr)
                self.assert_not_pushed(calls)

    def test_a_package_not_provably_private_after_the_push_stops_with_3(self):
        cases = {
            "public after": ({"S_AFTER": "public"}, "not private; look at it now"),
            "unreadable after": ({"S_AFTER": "401"}, "cannot read the package"),
            "anonymous read works": ({"S_ANON": "open"}, "reads without a login"),
            "anonymous read fails otherwise": ({"S_ANON": "network"},
                                               "not as the registry's refusal"),
            "anonymous read fails at a proxy": (
                {"S_ANON": "proxy"}, "not as the registry's refusal"),
            "anonymous read refused by another host": (
                {"S_ANON": "other_host"}, "not as the registry's refusal"),
            "digest not the image's": ({"S_REPODIGEST": "sha256:" + "1" * 64},
                                       "is not one of"),
            "no digest printed": ({"S_DIGEST": "none"}, "printed no digest"),
        }
        for name, (changes, message) in cases.items():
            with self.subTest(name):
                got, calls = self.run_gate(**changes)
                self.assertEqual(got.returncode, 3, got.stderr)
                self.assertIn(message, got.stderr)
                self.assertNotIn("Done:", got.stdout)
                self.assertIn("docker logout ghcr.io", calls)

    def test_each_wording_of_the_registry_refusal_is_accepted(self):
        for anon in ("refused", "denied", "bare"):
            with self.subTest(anon):
                got, _ = self.run_gate(S_ANON=anon)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertIn("anonymous read refused", got.stdout)


if __name__ == "__main__":
    unittest.main()
