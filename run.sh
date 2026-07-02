#!/bin/bash
# Run the adk-tools image with X11 and a persistent work directory.
#
# Usage: ./run.sh [command ...]        (default: interactive bash)
#
#   ADK_TOOLS_IMAGE   image to run (default: adk-tools:dev)
#   ADK_WORK          host work dir mounted at /work
#                     (default: ~/adk-work; created on first run)
#
# Container layout (seeded on start):
#   /work/example              wire-bond demo, ready to open (disposable)
#   /work/heterogenic-designs  your persistent work area (host-backed)
#
# Examples:
#   ./run.sh                                  # shell; `adk-tools` lists tools
#   ./run.sh kicad example/kicad/interposer_wire_bonding_demo.kicad_pro
#   ./run.sh chiplet-studio example/outputs/interposer_wire_bonding_demo.chiplet
#   ./run.sh adk-smoke
set -euo pipefail

IMAGE="${ADK_TOOLS_IMAGE:-adk-tools:dev}"

ADK_WORK="${ADK_WORK:-$HOME/adk-work}"
mkdir -p "$ADK_WORK"

# Hard memory boundary: cap the container (and thus every process inside it,
# collectively) so a runaway tool cannot exhaust host RAM and thrash swap.
# --memory-swap set equal to --memory disables swap for the container: at the
# limit the kernel OOM-kills a process inside the container instead of spilling
# to host swap. Tune with ADK_MEM_LIMIT (e.g. 32g); the default protects out of
# the box. cgroup v2 accounts swap, so the zero-swap cap is honored.
ADK_MEM_LIMIT="${ADK_MEM_LIMIT:-20g}"

# Minimal passwd/group so the container user has a name (avoids
# "I have no name!" prompts and user-lookup failures). A private mktemp dir
# (0700, unguessable name) instead of a predictable /tmp/...-$(id -u) path that
# a co-located user could pre-create to DoS the launch or, via a planted passwd
# symlink, redirect our write; cleaned up after the container exits.
ETC_DIR="$(mktemp -d "${TMPDIR:-/tmp}/adk-tools-etc.XXXXXX")"
trap 'rm -rf "$ETC_DIR"' EXIT
printf 'root:x:0:0:root:/root:/bin/bash\nadk:x:%s:%s:adk:/tmp:/bin/bash\n' \
    "$(id -u)" "$(id -g)" > "$ETC_DIR/passwd"
printf 'root:x:0:\nadk:x:%s:\n' "$(id -g)" > "$ETC_DIR/group"

ARGS=(
    --rm
    --memory "$ADK_MEM_LIMIT"
    --memory-swap "$ADK_MEM_LIMIT"
    --user "$(id -u):$(id -g)"
    -e HOME=/tmp
    -v "$ETC_DIR/passwd:/etc/passwd:ro"
    -v "$ETC_DIR/group:/etc/group:ro"
    -v "$ADK_WORK:/work"
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

# Not exec'd: the passwd/group bind-mounts must stay live for the container's
# lifetime, so the EXIT trap can only clean up the private etc dir once docker
# run returns. set -e propagates the container's exit code.
docker run "${ARGS[@]}" "$IMAGE" "$@"
