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

ARGS=(
    --rm -it
    --user "$(id -u):$(id -g)"
    -e HOME=/tmp
    -v "$PWD:/work"
    -w /work
)

# X11 (works on local X and ThinLinc sessions; GUI tools need it, CLI does not)
if [ -n "${DISPLAY:-}" ]; then
    ARGS+=(-e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix)
    XAUTH="${XAUTHORITY:-$HOME/.Xauthority}"
    if [ -f "$XAUTH" ]; then
        ARGS+=(-v "$XAUTH:/tmp/.Xauthority:ro" -e XAUTHORITY=/tmp/.Xauthority)
    fi
fi

exec docker run "${ARGS[@]}" "$IMAGE" "$@"
