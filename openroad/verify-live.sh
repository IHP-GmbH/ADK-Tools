#!/bin/bash
# Run the chiplet2dbx test suite against the pinned OpenROAD image.
#
# Usage: ./openroad/verify-live.sh
#
#   OPENROAD_3DBLOX_IMAGE  image under test (default: the tag in openroad.pin)
#
# Why this exists as a separate gate: the suite's live tests skip themselves
# when the OpenROAD image is absent, so a green run inside the adk-tools build
# would prove nothing about the OpenROAD path. This script requires the image
# up front and then refuses to pass if any live test skipped anyway.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"

# shellcheck source=openroad.pin
source "$HERE/openroad.pin"
IMAGE="${OPENROAD_3DBLOX_IMAGE}"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "missing image: $IMAGE" >&2
    echo "build it with ./openroad/build-image.sh" >&2
    exit 1; }

# Same klayout wheel the image pins, so the ADK conftest imports the module
# this suite is actually released against.
KLAYOUT_PIP=$(grep -oP '^ARG KLAYOUT_PIP=\K.*' "$ROOT/Dockerfile")

VENV="$HERE/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "== creating $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet pytest pyyaml "klayout==${KLAYOUT_PIP:?}"
fi

TESTS="$ROOT/tools/adk/tests/test_chiplet2dbx.py"
[ -f "$TESTS" ] || { echo "missing $TESTS (submodules checked out?)" >&2; exit 1; }

# Pin the interconnect PDK to this repo's submodule instead of letting the
# exporter's sibling walk find some other checkout on the machine.
export INTERCONNECT_PDK_ROOT="$ROOT/tools/IHP-Interconnect-IntM4TM2"

# The suite's real-assembly tests default to a wire-bond demo that lives
# outside this repository; point them at the demo adk-tools ships instead,
# so nothing skips for want of an external checkout.
export CHIPLET2DBX_DEMO_CHIPLET="$ROOT/examples/two_die_interposer/outputs/two_die_interposer.chiplet"
export CHIPLET2DBX_DEMO_PINS="$ROOT/examples/two_die_interposer/chiplets/metal_test_chiplet.pins.json"

echo "== full suite"
"$VENV/bin/python" -m pytest "$TESTS" -q -p no:cacheprovider

echo "== live subset (must not skip)"
live=$("$VENV/bin/python" -m pytest "$TESTS" -q -p no:cacheprovider -k live)
echo "$live" | tail -3
if echo "$live" | grep -q "skipped"; then
    echo "live tests skipped despite $IMAGE being present" >&2
    exit 1
fi
echo "$live" | grep -q "passed" || { echo "no live test ran" >&2; exit 1; }

echo "== OpenROAD path verified against $IMAGE"
