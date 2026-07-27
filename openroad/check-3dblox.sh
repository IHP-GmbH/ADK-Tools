#!/bin/bash
# Export a .chiplet assembly to 3Dblox and lint it with OpenROAD.
#
# Usage: ./openroad/check-3dblox.sh <assembly.chiplet> [out-dir] [--pins REF=pins.json ...]
#
#   out-dir   default: <assembly dir>/build_3dblox
#   --pins    per-die pin list (gds_to_kicad's *.pins.json) making the export
#             bump-aware; repeatable, one per die reference.
#
#   ADK_TOOLS_IMAGE        image providing the exporter (default: adk-tools:dev)
#   OPENROAD_3DBLOX_IMAGE  image providing read_3dbx/check_3dblox
#                          (default: the tag in openroad.pin)
#
# Two containers on purpose: the exporter runs from the adk-tools image, so
# what gets tested is the exporter that actually ships, and OpenROAD stays in
# its own sidecar image (see README.md).
#
# Exits non-zero on any linter WARNING or ERROR, not only on a crash. Note
# what a clean run does and does not mean: check_3dblox lints the declared
# 3Dblox model, not the artwork. It catches self-inconsistent geometry
# (z gaps, overlaps, bumps outside their region) and is blind to a die that
# is merely placed wrong or swapped. Assembly DRC/LVS is the tool for that.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=openroad.pin
source "$HERE/openroad.pin"
IMAGE="${OPENROAD_3DBLOX_IMAGE}"
ADK_IMAGE="${ADK_TOOLS_IMAGE:-adk-tools:dev}"

CHIPLET=""
OUT_DIR=""
PINS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --pins) PINS+=("--pins" "${2:?--pins needs REF=path}"); shift 2 ;;
        -*) echo "unknown option: $1" >&2; exit 1 ;;
        *)  if [ -z "$CHIPLET" ]; then CHIPLET="$1"
            elif [ -z "$OUT_DIR" ]; then OUT_DIR="$1"
            else echo "unexpected argument: $1" >&2; exit 1
            fi; shift ;;
    esac
done
[ -n "$CHIPLET" ] || { echo "usage: check-3dblox.sh <assembly.chiplet> [out-dir]" >&2; exit 1; }
[ -f "$CHIPLET" ] || { echo "no such .chiplet: $CHIPLET" >&2; exit 1; }

CHIPLET=$(cd "$(dirname "$CHIPLET")" && pwd)/$(basename "$CHIPLET")
SRC_DIR=$(dirname "$CHIPLET")
NAME=$(basename "$CHIPLET" .chiplet)
OUT_DIR="${OUT_DIR:-$SRC_DIR/build_3dblox}"
mkdir -p "$OUT_DIR"
OUT_DIR=$(cd "$OUT_DIR" && pwd)

for image in "$ADK_IMAGE" "$IMAGE"; do
    docker image inspect "$image" >/dev/null 2>&1 || {
        echo "missing image: $image" >&2
        [ "$image" = "$IMAGE" ] && echo "build it with ./openroad/build-image.sh" >&2
        exit 1; }
done

# Bind the host uid so the exported files stay writable and readable on both
# sides; neither image runs as the invoking user by default.
DOCKER_COMMON=(--rm --user "$(id -u):$(id -g)" -v "$SRC_DIR:$SRC_DIR")
[ "$OUT_DIR" = "$SRC_DIR" ] || DOCKER_COMMON+=(-v "$OUT_DIR:$OUT_DIR")

echo "== export: $NAME -> $OUT_DIR"
docker run "${DOCKER_COMMON[@]}" -w "$OUT_DIR" \
    --entrypoint /opt/adk-tools/venv/bin/python3 "$ADK_IMAGE" \
    /opt/adk-tools/adk/openroad/chiplet2dbx.py \
        --chiplet "$CHIPLET" --out-dir "$OUT_DIR" --name "$NAME" "${PINS[@]}"

cat > "$OUT_DIR/check_3dblox.tcl" <<EOF
read_3dbx $NAME.3dbx
puts "READ_3DBX_OK"
check_3dblox
puts "CHECK_3DBLOX_OK"
exit
EOF

echo "== check_3dblox: $IMAGE"
log=$(docker run "${DOCKER_COMMON[@]}" -w "$OUT_DIR" \
    "$IMAGE" -exit "$OUT_DIR/check_3dblox.tcl" 2>&1)
echo "$log"

if printf '%s' "$log" | grep -qE '\[(WARNING|ERROR)'; then
    echo "check_3dblox reported findings (see log above)" >&2
    exit 1
fi
printf '%s' "$log" | grep -q "CHECK_3DBLOX_OK" || {
    echo "check_3dblox did not complete" >&2; exit 1; }
echo "== clean (declared-model lint only; artwork is not checked)"
