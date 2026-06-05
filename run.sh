#!/bin/bash
# Run the adk-tools image with X11 and the current directory mounted at /work.
#
# Usage: ./run.sh [command ...]        (default: interactive bash)
#
#   ADK_TOOLS_IMAGE   image to run (default: adk-tools:dev)
#
# Examples:
#   ./run.sh                      # shell; run `adk-tools` inside for the list
#   ./run.sh kicad board.kicad_pcb
#   ./run.sh chiplet-studio design.chiplet
#   ./run.sh adk-smoke
set -euo pipefail

IMAGE="${ADK_TOOLS_IMAGE:-adk-tools:dev}"

# Minimal passwd/group so the container user has a name (avoids
# "I have no name!" prompts and user-lookup failures). Reused per uid.
ETC_DIR="/tmp/adk-tools-etc-$(id -u)"
mkdir -p "$ETC_DIR"
printf 'root:x:0:0:root:/root:/bin/bash\nadk:x:%s:%s:adk:/tmp:/bin/bash\n' \
    "$(id -u)" "$(id -g)" > "$ETC_DIR/passwd"
printf 'root:x:0:\nadk:x:%s:\n' "$(id -g)" > "$ETC_DIR/group"

ARGS=(
    --rm
    --user "$(id -u):$(id -g)"
    -e HOME=/tmp
    -v "$ETC_DIR/passwd:/etc/passwd:ro"
    -v "$ETC_DIR/group:/etc/group:ro"
    -v "$PWD:/work"
    -w /work
)
# Interactive TTY only when we actually have one (scripted/CI launches do not)
[ -t 0 ] && ARGS+=(-it)

# X11 (works on local X and ThinLinc sessions; GUI tools need it, CLI does not)
if [ -n "${DISPLAY:-}" ]; then
    ARGS+=(-e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix)
    XAUTH="${XAUTHORITY:-$HOME/.Xauthority}"
    if [ -f "$XAUTH" ]; then
        ARGS+=(-v "$XAUTH:/tmp/.Xauthority:ro" -e XAUTHORITY=/tmp/.Xauthority)
    fi
fi

exec docker run "${ARGS[@]}" "$IMAGE" "$@"
