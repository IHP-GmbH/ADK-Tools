#!/bin/bash
# Build the adk-tools image.
#
# Usage: [JOBS=N] ./build.sh [TAG] [--skip-verify]
#
#   TAG            image tag (default: dev). Result: adk-tools:TAG
#   JOBS           parallel compile jobs for the heavy stages (default: 16)
#   --skip-verify  skip the verify stage (demo regen + studio ctest +
#                  plugin pytest + adk-smoke). Only for quick iteration.
#
# The verify stage builds first so a broken submodule combination never
# produces a tagged runtime image.
set -euo pipefail
cd "$(dirname "$0")"

TAG="dev"
SKIP_VERIFY=0
for arg in "$@"; do
    case "$arg" in
        --skip-verify) SKIP_VERIFY=1 ;;
        -*) echo "unknown option: $arg" >&2; exit 1 ;;
        *) TAG="$arg" ;;
    esac
done

# Version manifest from the pinned submodules (baked into the image, shown by
# the in-image `adk-tools` command).
python3 - > manifest.json <<'PY'
import json, subprocess, datetime

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

# .gitmodules section names can diverge from paths after `git mv`; map them.
section_by_path = {}
for line in git("config", "-f", ".gitmodules",
                "--get-regexp", r"submodule\..*\.path").splitlines():
    key, path = line.split(None, 1)
    section_by_path[path] = key[len("submodule."):-len(".path")]

tools = {}
for line in git("submodule", "status").splitlines():
    sha, path = line.split()[:2]
    sha = sha.lstrip("+-U")
    name = path.split("/", 1)[1]
    section = section_by_path.get(path, path)
    url = git("config", "-f", ".gitmodules", "--get", f"submodule.{section}.url")
    try:
        describe = git("-C", path, "describe", "--tags", "--always",
                       "--exclude", "backup/*")
    except subprocess.CalledProcessError:
        describe = sha[:10]
    tools[name] = {"repo": url, "ref": sha, "describe": describe}

print(json.dumps({
    "image": "adk-tools",
    "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "meta": git("describe", "--always", "--dirty"),
    "tools": tools,
}, indent=2))
PY

MANIFEST_B64="$(base64 -w0 manifest.json)"
export DOCKER_BUILDKIT=1
# Daemon's embedded builder: images land directly in `docker images`
# (container-driver builders like buildx_buildkit_* need --load and keep a
# duplicate multi-GB cache).
export BUILDX_BUILDER=default

JOBS="${JOBS:-16}"

# Pre-pass: kicad-builder alone. BuildKit otherwise runs kicad-builder and
# studio-builder concurrently, doubling the job count; sequencing keeps the
# machine at <= JOBS compile jobs total. Cache hit when kicad is unchanged.
echo "== building kicad-builder (sequenced so total jobs stay <= $JOBS)"
docker build --target kicad-builder \
    --build-arg JOBS="$JOBS" \
    --build-arg MANIFEST_B64="$MANIFEST_B64" .

if [ "$SKIP_VERIFY" -eq 0 ]; then
    echo "== building verify stage (compiles everything + runs all suites)"
    docker build --target verify -t adk-tools:verify \
        --build-arg JOBS="$JOBS" \
        --build-arg MANIFEST_B64="$MANIFEST_B64" .
fi

echo "== tagging lean runtime image: adk-tools:$TAG"
docker build --target runtime -t "adk-tools:$TAG" \
    --build-arg JOBS="$JOBS" \
    --build-arg MANIFEST_B64="$MANIFEST_B64" .

echo
echo "Done: adk-tools:$TAG"
docker image ls adk-tools --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'
