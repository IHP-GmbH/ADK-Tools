#!/bin/bash
# Build the adk-tools image.
#
# Usage: [JOBS=N] [VERIFY_TAG=T] ./build.sh [TAG] [--skip-verify] [--tools-only]
#
#   TAG            image tag (default: dev). Result: adk-tools:TAG
#   JOBS           parallel compile jobs for the heavy stages (default: 8)
#   VERIFY_TAG     tag of the verify-stage image (default: verify). Result:
#                  adk-tools:VERIFY_TAG
#   --skip-verify  skip the verify stage (adk/gds_to_kicad/interposer pytest +
#                  demo regen + studio ctest + plugin pytest + adk-smoke).
#                  Only for quick iteration.
#   --tools-only   build the tools image for this tree's key and stop: the one
#                  build a tree whose key moved can run, so the two locks in
#                  docker/ can be read from it (ci/check_tools_locks.py).
#
# The tools image (the Dockerfile above its thin-layer line) builds first, and
# only when no image carries its key (ci/tools_key.py). The verify stage builds
# next, so a broken submodule combination never produces a tagged runtime image.
set -euo pipefail
cd "$(dirname "$0")"

TAG="dev"
SKIP_VERIFY=0
TOOLS_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --skip-verify) SKIP_VERIFY=1 ;;
        --tools-only) TOOLS_ONLY=1 ;;
        -*) echo "unknown option: $arg" >&2; exit 1 ;;
        "") echo "empty TAG argument" >&2; exit 1 ;;
        *) TAG="$arg" ;;
    esac
done

# The verify-stage image's tag. The default moves adk-tools:verify onto this
# build, and the image it named before keeps no name from here; pass a new name
# (verify-<commit>, say) to leave adk-tools:verify where it is. Checked before
# anything runs, and never the runtime tag, which would take the name straight
# back off the verify image.
VERIFY_TAG="${VERIFY_TAG:-verify}"
if [[ ! "$VERIFY_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
    echo "VERIFY_TAG is not a docker tag: '$VERIFY_TAG'" >&2
    exit 1
fi
if [ "$VERIFY_TAG" = "$TAG" ]; then
    echo "VERIFY_TAG and TAG are both '$TAG'; the runtime image would take the name off the verify image" >&2
    exit 1
fi

# The pin set and the commit this image is built from, carried as labels for
# a release lock to name (run.sh --examples/--lock compares them): the sha256 of
# ci/pin-set.json's bytes and `git rev-parse HEAD`. The pin set has to be the
# tracked file, unmodified, and equal to a regeneration from the pins it
# records, or the label would name a pin set this build did not use.
# The image also stages those bytes at
# /opt/adk-tools/staging/adk-tools/ci/pin-set.json, and the verify stage holds
# that file to PIN_SET_SHA256, the value labelled here.
PIN_SET=ci/pin-set.json
PIN_SET_LABEL=org.ihp.adk-tools.pin-set
REVISION_LABEL=org.opencontainers.image.revision
if ! git ls-files --error-unmatch "$PIN_SET" > /dev/null 2>&1; then
    echo "Refusing to build: $PIN_SET is not tracked, and the image labels its sha256." >&2
    exit 1
fi
if [ -n "$(git status --porcelain -- "$PIN_SET")" ]; then
    echo "Refusing to build: $PIN_SET differs from the commit, so its sha256 would" >&2
    echo "label a pin set that no commit records." >&2
    exit 1
fi
if ! python3 -B ci/pin_set.py --check > /dev/null; then
    python3 -B ci/pin_set.py --check >&2 || true
    echo "Refusing to build: $PIN_SET is not what the pins regenerate, so its" >&2
    echo "sha256 would label pins this image does not carry." >&2
    exit 1
fi
PIN_SET_SHA256="$(sha256sum "$PIN_SET" | cut -d' ' -f1)"
REVISION="$(git rev-parse HEAD)"

# The build context is the WORKING TREE, not the git index. Files that are
# untracked or git-ignored inside a submodule still enter it and get baked into
# the image unless .dockerignore excludes them, and release.sh's provenance gate
# (git status + git submodule status) cannot see them. Warn about anything not
# already covered, so an image built here does not silently differ from one
# built from a clean clone. Keep the pattern list in step with .dockerignore.
CONTEXT_EXCLUDED='(__pycache__|\.pytest_cache|\.venv|/build(-release|-debug)?/|/bin-(release|debug)/|SESSION_LOG\.md|/docs/$|tests/test_.*\.gds)'
strays="$( {
    # inside each registered submodule
    git submodule --quiet foreach \
        'git status --porcelain --ignored 2>/dev/null | cut -c4- | sed "s|^|$sm_path/|"' \
        2>/dev/null
    # and directly under tools/: a deregistered submodule leaves its whole working
    # tree behind (git cannot rmdir a non-empty one), which then enters the context
    # as an untracked directory nothing pins.
    git status --porcelain --ignored -- tools/ 2>/dev/null | cut -c4-
} | grep -Ev "$CONTEXT_EXCLUDED" || true )"
if [ -n "$strays" ]; then
    echo "warning: untracked/ignored files under tools/ will enter the build context:" >&2
    printf '  %s\n' $strays >&2
    echo "  they are NOT in git, so this image will differ from a clean-clone build." >&2
    echo "  Exclude them in .dockerignore, or build a release image from a fresh clone of the checkout." >&2
fi

# Sources that cannot be submodules (personal or private remotes) are staged
# into staging/ by commit. This refuses rather than guessing when a ref is
# unpinned, so an unset pin stops the build here and not at a docker step.
#
# It runs BEFORE the path check, not after, and the order is the point: the
# check resolves paths that reach into these trees, and staging/ on disk is
# whatever the PREVIOUS pins produced until this rewrites it. Checking first
# would validate the tree that is about to be replaced and pass a pin bump that
# moved a file. It still runs before the manifest, which records what was
# staged.
./stage-sources.sh

# The die views (staging.pin, "Die views"): a die run's records,
# pinned by die-views.sha256 and its sha256 in staging.pin, copied into
# die-views/ from the directory STAGE_DIE_VIEWS_DIR names and checked there.
# It refuses when that is unset, relative or absent, or holds other bytes. Here
# for the same reason as the line above: the manifest reads what it wrote.
./stage-die-views.sh

# The OpenROAD the examples route with, and the LibreLane and sg13g2 PDK a die
# built in the container uses. The runtime image carries them, and their closure
# (nix/host-closure.txt, below), in its own /nix/store and names them in its
# ENV; run.sh mounts them from the host's Nix store only for an image built
# before that.
# staging.pin's NIX_OPENROAD_OUTPATH, NIX_LIBRELANE_OUTPATH,
# NIX_LIBRELANE_PYTHON_OUTPATH and NIX_SG13G2_PDK_OUTPATH are the one place
# that names each (nix/check-openroad-pin.sh holds them to the flake; nothing
# here evaluates Nix, so only their shape is checked), and the runtime image
# carries each as a label, so run.sh reads them from the image it launches and
# not from whatever checkout run.sh sits in. Read in a fresh bash, the way
# stage-sources.sh sources the file, so the pin cannot change this shell.
OPENROAD_LABEL=org.ihp.adk-tools.nix-openroad
LIBRELANE_LABEL=org.ihp.adk-tools.nix-librelane
LIBRELANE_PYTHON_LABEL=org.ihp.adk-tools.nix-librelane-python
SG13G2_PDK_LABEL=org.ihp.adk-tools.nix-sg13g2-pdk
# The co-simulation, mixed-signal and Verilog-AMS engines and the waveform
# viewers the image does carry, labelled too: an image without the closure
# labels, or an ADK_OPENROAD outside the list, has run.sh mount the host's whole
# store over the image's, and it says when that hides them.
COSIM_PYTHON_LABEL=org.ihp.adk-tools.nix-cosim-python
COSIM_DEVSHELL_LABEL=org.ihp.adk-tools.nix-cosim-devshell
COSIM_IVERILOG_LABEL=org.ihp.adk-tools.nix-cosim-iverilog
MIXSIM_NGSPICE_LABEL=org.ihp.adk-tools.nix-mixsim-ngspice
MIXSIM_IVERILOG_LABEL=org.ihp.adk-tools.nix-mixsim-iverilog
SG13G2_OSDI_LABEL=org.ihp.adk-tools.nix-sg13g2-osdi
GTKWAVE_LABEL=org.ihp.adk-tools.nix-gtkwave
GAW_LABEL=org.ihp.adk-tools.nix-gaw
XSCHEM_LABEL=org.ihp.adk-tools.nix-xschem
VAMS_GNUCAP_LABEL=org.ihp.adk-tools.nix-vams-gnucap
VAMS_DEVSHELL_LABEL=org.ihp.adk-tools.nix-vams-devshell
pin_value() {
    bash -euo pipefail -c '. ./staging.pin >/dev/null 2>&1; printf "%s" "${'"$1"':-}"'
}
NIX_OPENROAD_OUTPATH="$(pin_value NIX_OPENROAD_OUTPATH)"
if [[ ! "$NIX_OPENROAD_OUTPATH" =~ ^/nix/store/[0-9a-z]{32}-openroad[-.+_0-9A-Za-z]*$ ]]; then
    echo "Refusing to build: staging.pin's NIX_OPENROAD_OUTPATH ('$NIX_OPENROAD_OUTPATH')" >&2
    echo "is not a /nix/store/<hash>-openroad path, and the image labels it for run.sh." >&2
    exit 1
fi
NIX_LIBRELANE_OUTPATH="$(pin_value NIX_LIBRELANE_OUTPATH)"
if [[ ! "$NIX_LIBRELANE_OUTPATH" =~ ^/nix/store/[0-9a-z]{32}-python3[.0-9]*-librelane-[-.+_0-9A-Za-z]*$ ]]; then
    echo "Refusing to build: staging.pin's NIX_LIBRELANE_OUTPATH ('$NIX_LIBRELANE_OUTPATH')" >&2
    echo "is not a /nix/store/<hash>-python3*-librelane-* path, and the image labels it for run.sh." >&2
    exit 1
fi
# The interpreter core preparation runs: the env's own bin/python3.<minor>, the
# file the build shell's ADK_NIX_LIBRELANE_PYTHON names, never its bin/python3
# link. The label carries that file, so run.sh passes it as it is.
NIX_LIBRELANE_PYTHON_OUTPATH="$(pin_value NIX_LIBRELANE_PYTHON_OUTPATH)"
if [[ ! "$NIX_LIBRELANE_PYTHON_OUTPATH" =~ ^/nix/store/[0-9a-z]{32}-python3-3\.([0-9]+)\.[0-9]+-env$ ]]; then
    echo "Refusing to build: staging.pin's NIX_LIBRELANE_PYTHON_OUTPATH ('$NIX_LIBRELANE_PYTHON_OUTPATH')" >&2
    echo "is not a /nix/store/<hash>-python3-3.<minor>.<patch>-env path, and the image labels its interpreter for run.sh." >&2
    exit 1
fi
LIBRELANE_PYTHON="$NIX_LIBRELANE_PYTHON_OUTPATH/bin/python3.${BASH_REMATCH[1]}"
NIX_SG13G2_PDK_OUTPATH="$(pin_value NIX_SG13G2_PDK_OUTPATH)"
if [[ ! "$NIX_SG13G2_PDK_OUTPATH" =~ ^/nix/store/[0-9a-z]{32}-ihp-open-pdk-[0-9a-f]{7}$ ]]; then
    echo "Refusing to build: staging.pin's NIX_SG13G2_PDK_OUTPATH ('$NIX_SG13G2_PDK_OUTPATH')" >&2
    echo "is not a /nix/store/<hash>-ihp-open-pdk-<7 hex> path, and the image labels it for run.sh." >&2
    exit 1
fi
# The co-simulation engine's four, the mixed-signal engine's three, the two
# viewers and the Verilog-AMS engine's two, which the image does carry (below).
# engine_pin <variable> <store name, an ERE> <what it names>
engine_pin() {
    local value
    value="$(pin_value "$1")"
    if [[ ! "$value" =~ ^/nix/store/[0-9a-z]{32}-$2$ ]]; then
        echo "Refusing to build: staging.pin's $1 ('$value')" >&2
        echo "is not a /nix/store/<hash> path of $3, and the image carries it." >&2
        exit 1
    fi
    printf '%s' "$value"
}
NIX_COSIM_IVERILOG_OUTPATH="$(engine_pin NIX_COSIM_IVERILOG_OUTPATH 'iverilog-[-.+_0-9A-Za-z]*' Icarus)"
NIX_COSIM_COCOTB_OUTPATH="$(engine_pin NIX_COSIM_COCOTB_OUTPATH 'python3[.0-9]*-cocotb-[.0-9]*' cocotb)"
NIX_COSIM_PYTHON_OUTPATH="$(engine_pin NIX_COSIM_PYTHON_OUTPATH 'python3-[.0-9]*-env' 'a python env')"
NIX_COSIM_DEVSHELL_OUTPATH="$(engine_pin NIX_COSIM_DEVSHELL_OUTPATH 'cosim-devshell' 'the co-simulation devshell')"
NIX_MIXSIM_NGSPICE_OUTPATH="$(engine_pin NIX_MIXSIM_NGSPICE_OUTPATH 'ngspice-[.0-9]+' ngspice)"
NIX_MIXSIM_IVERILOG_OUTPATH="$(engine_pin NIX_MIXSIM_IVERILOG_OUTPATH 'iverilog-libvvp-[-.+_0-9A-Za-z]*' 'Icarus with libvvp')"
NIX_SG13G2_OSDI_OUTPATH="$(engine_pin NIX_SG13G2_OSDI_OUTPATH 'sg13g2-osdi-[0-9a-f]{7}' 'the sg13g2 OSDI models')"
NIX_GTKWAVE_OUTPATH="$(engine_pin NIX_GTKWAVE_OUTPATH 'gtkwave-[.0-9]+' GTKWave)"
NIX_GAW_OUTPATH="$(engine_pin NIX_GAW_OUTPATH 'gaw3-[0-9]{8}' GAW)"
NIX_XSCHEM_OUTPATH="$(engine_pin NIX_XSCHEM_OUTPATH 'xschem-[.0-9]+(RC)?-[0-9a-f]{7}' Xschem)"
NIX_VAMS_GNUCAP_OUTPATH="$(engine_pin NIX_VAMS_GNUCAP_OUTPATH \
    'gnucap-[0-9a-f]{7}-modelgen-verilog-[0-9a-f]{7}' 'gnucap and modelgen-verilog')"
NIX_VAMS_DEVSHELL_OUTPATH="$(engine_pin NIX_VAMS_DEVSHELL_OUTPATH 'vams-devshell' 'the Verilog-AMS devshell')"
# The die views' listing sha256, which stage-die-views.sh has held
# die-views.sha256 to; the runtime stage checks the copy against it again.
DIE_VIEWS_SHA256="$(pin_value STAGE_DIE_VIEWS_SHA256)"

# Preconditions, before an hour of build. Every path the Dockerfile and the
# shipped wrapper scripts address inside the image has to exist in the tree
# that is about to be copied into it. A pin bump can move one without changing
# a line here, and the build's own answer to that arrives at the step that
# trips over it. This one arrives now, and it is the same checker CI runs --
# with more coverage here than there, because CI has no staging/ and reports
# every path into one as skipped rather than checked.
if ! python3 ci/check_paths.py > /dev/null; then
    python3 ci/check_paths.py >&2 || true
    echo "" >&2
    echo "Refusing to build: the pinned tree does not have the shape the image" >&2
    echo "expects. Fix the paths or the pins; the build would fail on this an" >&2
    echo "hour from now, or ship it to users if the path is only in bin/." >&2
    exit 1
fi

# Every COPY, ADD and RUN is a layer, and overlay2 refuses a stack deeper than
# 128 at the step that crosses it, which in the verify stage is an hour in.
# Only the targets this run builds are held to the limit.
LAYER_TARGETS=(--target tools)
if [ "$TOOLS_ONLY" -eq 0 ]; then
    LAYER_TARGETS+=(--target studio-builder --target runtime)
    [ "$SKIP_VERIFY" -eq 1 ] || LAYER_TARGETS+=(--target verify)
fi
if ! python3 -B ci/check_layer_depth.py "${LAYER_TARGETS[@]}" >&2; then
    echo "Refusing to build: a target is too close to the overlay layer limit." >&2
    echo "Fold COPYs that land in one directory (ci/check_layer_depth.py)." >&2
    exit 1
fi

# The three engines and the viewers (staging.pin, "Co-simulation engine",
# "Mixed-signal engine", "Waveform viewers" and "Verilog-AMS engine"). The image
# carries the store paths nix/cosim-closure.txt, nix/mixsim-closure.txt,
# nix/viewer-closure.txt and nix/vams-closure.txt list. The first has to be
# exactly the runtime closure of the co-simulation env and Icarus as this
# host's store records it, the second exactly the closure of ngspice, the
# libvvp Icarus and the OSDI models less every path the first lists, the third
# exactly the closure of GTKWave and GAW less every path the other two list, the
# fourth exactly the closure of the Verilog-AMS devshell less every path the
# other three list, so no path is packed twice; each holds its own pins. That
# is a store query, not an evaluation: the paths have to be on this host
# already. The OSDI files' bytes are checked against nix/sg13g2-osdi.sha256,
# since their store path does not tell one build from another. Everything is
# checked before anything is packed. Each list goes into the build context as
# one archive, written so the same paths give the same bytes (names sorted,
# times, owners and hard links normalized), and the runtime stage unpacks all
# four under /nix/store.
COSIM_LIST=nix/cosim-closure.txt
COSIM_TAR=cosim-closure.tar
MIXSIM_LIST=nix/mixsim-closure.txt
MIXSIM_TAR=mixsim-closure.tar
VIEWER_LIST=nix/viewer-closure.txt
VIEWER_TAR=viewer-closure.tar
VAMS_LIST=nix/vams-closure.txt
VAMS_TAR=vams-closure.tar
OSDI_LIST=nix/sg13g2-osdi.sha256
cosim_refuse() {
    echo "Refusing to build: $*" >&2
    exit 1
}
# read_list <list> <pin...>: the list's paths, refusing a list of another
# shape or one that leaves out a pin.
read_list() {
    local list="$1" paths pin
    shift
    [ -f "$list" ] || cosim_refuse "$list is missing"
    paths="$(grep -v '^#' "$list" || true)"
    [ -n "$paths" ] || cosim_refuse "$list lists no store path"
    if grep -Evq '^/nix/store/[0-9a-z]{32}-[-.+_0-9A-Za-z]+$' <<< "$paths"; then
        cosim_refuse "$list has a line that is not a /nix/store/<hash>-<name> path"
    fi
    [ "$paths" = "$(LC_ALL=C sort -u <<< "$paths")" ] || \
        cosim_refuse "$list is not sorted, or names a path twice"
    for pin in "$@"; do
        grep -qxF "$pin" <<< "$paths" || cosim_refuse "$list does not list $pin"
    done
    printf '%s' "$paths"
}
COSIM_PATHS="$(read_list "$COSIM_LIST" "$NIX_COSIM_IVERILOG_OUTPATH" "$NIX_COSIM_COCOTB_OUTPATH" \
    "$NIX_COSIM_PYTHON_OUTPATH" "$NIX_COSIM_DEVSHELL_OUTPATH")" || exit 1
MIXSIM_PATHS="$(read_list "$MIXSIM_LIST" \
    "$NIX_MIXSIM_NGSPICE_OUTPATH" "$NIX_MIXSIM_IVERILOG_OUTPATH" "$NIX_SG13G2_OSDI_OUTPATH")" || exit 1
VIEWER_PATHS="$(read_list "$VIEWER_LIST" "$NIX_GTKWAVE_OUTPATH" "$NIX_GAW_OUTPATH" \
    "$NIX_XSCHEM_OUTPATH")" || exit 1
VAMS_PATHS="$(read_list "$VAMS_LIST" "$NIX_VAMS_GNUCAP_OUTPATH" "$NIX_VAMS_DEVSHELL_OUTPATH")" || exit 1
command -v nix-store > /dev/null || \
    cosim_refuse "no nix-store on PATH; $COSIM_LIST, $MIXSIM_LIST, $VIEWER_LIST, $VAMS_LIST and nix/host-closure.txt" \
        "are checked against this host's Nix store"
COSIM_CLOSURE="$(nix-store --query --requisites "$NIX_COSIM_DEVSHELL_OUTPATH" \
    "$NIX_COSIM_PYTHON_OUTPATH" "$NIX_COSIM_IVERILOG_OUTPATH" | LC_ALL=C sort -u)" || \
    cosim_refuse "the co-simulation engine is not in this host's Nix store" \
        "(nix build .#cosim-devshell in this flake)"
if [ "$COSIM_CLOSURE" != "$COSIM_PATHS" ]; then
    diff <(echo "$COSIM_PATHS") <(echo "$COSIM_CLOSURE") >&2 || true
    cosim_refuse "$COSIM_LIST is not the store's closure of $NIX_COSIM_DEVSHELL_OUTPATH," \
        "$NIX_COSIM_PYTHON_OUTPATH and $NIX_COSIM_IVERILOG_OUTPATH ('<' listed, '>' the store's)"
fi
MIXSIM_CLOSURE="$(nix-store --query --requisites "$NIX_MIXSIM_NGSPICE_OUTPATH" \
    "$NIX_MIXSIM_IVERILOG_OUTPATH" "$NIX_SG13G2_OSDI_OUTPATH" | LC_ALL=C sort -u)" || \
    cosim_refuse "the mixed-signal engine is not in this host's Nix store" \
        "(nix build .#mixsim-ngspice .#mixsim-iverilog .#sg13g2-osdi in this flake)"
MIXSIM_CLOSURE="$(LC_ALL=C comm -23 <(echo "$MIXSIM_CLOSURE") <(echo "$COSIM_PATHS"))"
if [ "$MIXSIM_CLOSURE" != "$MIXSIM_PATHS" ]; then
    diff <(echo "$MIXSIM_PATHS") <(echo "$MIXSIM_CLOSURE") >&2 || true
    cosim_refuse "$MIXSIM_LIST is not the store's closure of $NIX_MIXSIM_NGSPICE_OUTPATH," \
        "$NIX_MIXSIM_IVERILOG_OUTPATH and $NIX_SG13G2_OSDI_OUTPATH less $COSIM_LIST" \
        "('<' listed, '>' the store's)"
fi
VIEWER_CLOSURE="$(nix-store --query --requisites "$NIX_GTKWAVE_OUTPATH" "$NIX_GAW_OUTPATH" \
    "$NIX_XSCHEM_OUTPATH" | LC_ALL=C sort -u)" || \
    cosim_refuse "the viewers are not in this host's Nix store" \
        "(nix build .#gtkwave .#gaw .#xschem in this flake)"
VIEWER_CLOSURE="$(LC_ALL=C comm -23 <(echo "$VIEWER_CLOSURE") \
    <(LC_ALL=C sort -u <<< "$COSIM_PATHS"$'\n'"$MIXSIM_PATHS"))"
if [ "$VIEWER_CLOSURE" != "$VIEWER_PATHS" ]; then
    diff <(echo "$VIEWER_PATHS") <(echo "$VIEWER_CLOSURE") >&2 || true
    cosim_refuse "$VIEWER_LIST is not the store's closure of $NIX_GTKWAVE_OUTPATH," \
        "$NIX_GAW_OUTPATH and $NIX_XSCHEM_OUTPATH less $COSIM_LIST and $MIXSIM_LIST" \
        "('<' listed, '>' the store's)"
fi
VAMS_CLOSURE="$(nix-store --query --requisites "$NIX_VAMS_DEVSHELL_OUTPATH" "$NIX_VAMS_GNUCAP_OUTPATH" \
    | LC_ALL=C sort -u)" || \
    cosim_refuse "the Verilog-AMS engine is not in this host's Nix store" \
        "(nix build .#vams-devshell in this flake; it is not in the binary cache)"
VAMS_CLOSURE="$(LC_ALL=C comm -23 <(echo "$VAMS_CLOSURE") \
    <(LC_ALL=C sort -u <<< "$COSIM_PATHS"$'\n'"$MIXSIM_PATHS"$'\n'"$VIEWER_PATHS"))"
if [ "$VAMS_CLOSURE" != "$VAMS_PATHS" ]; then
    diff <(echo "$VAMS_PATHS") <(echo "$VAMS_CLOSURE") >&2 || true
    cosim_refuse "$VAMS_LIST is not the store's closure of $NIX_VAMS_DEVSHELL_OUTPATH less" \
        "$COSIM_LIST, $MIXSIM_LIST and $VIEWER_LIST ('<' listed, '>' the store's)"
fi
# The runtime image's own store copy of OpenROAD and the three LibreLane store
# paths: nix/host-closure.txt, exactly their runtime closure as this host's
# store records it, checked like the lists above, and packed below into
# host-closure.tar once the lock check passes. The list also goes on the image
# as a label (the paths joined by single spaces) with its sha256 as another, so
# an older run.sh, which binds each listed path from the host on its own, never
# mounts the whole store over the image's.
HOST_LIST=nix/host-closure.txt
HOST_PATHS="$(read_list "$HOST_LIST" "$NIX_OPENROAD_OUTPATH" "$NIX_LIBRELANE_OUTPATH" \
    "$NIX_LIBRELANE_PYTHON_OUTPATH" "$NIX_SG13G2_PDK_OUTPATH")" || exit 1
HOST_CLOSURE="$(nix-store --query --requisites "$NIX_OPENROAD_OUTPATH" "$NIX_LIBRELANE_OUTPATH" \
    "$NIX_LIBRELANE_PYTHON_OUTPATH" "$NIX_SG13G2_PDK_OUTPATH" | LC_ALL=C sort -u)" || \
    cosim_refuse "OpenROAD, LibreLane, its interpreter or the sg13g2 PDK is not in this host's" \
        "Nix store (nix build .#openroad .#librelane .#librelane-python .#sg13g2-pdk in this flake)"
if [ "$HOST_CLOSURE" != "$HOST_PATHS" ]; then
    diff <(echo "$HOST_PATHS") <(echo "$HOST_CLOSURE") >&2 || true
    cosim_refuse "$HOST_LIST is not the store's closure of $NIX_OPENROAD_OUTPATH," \
        "$NIX_LIBRELANE_OUTPATH, $NIX_LIBRELANE_PYTHON_OUTPATH and $NIX_SG13G2_PDK_OUTPATH" \
        "('<' listed, '>' the store's)"
fi
HOST_CLOSURE_LABEL=org.ihp.adk-tools.nix-host-closure
HOST_CLOSURE_SHA256_LABEL=org.ihp.adk-tools.nix-host-closure-sha256
HOST_CLOSURE="$(paste -sd ' ' <<< "$HOST_PATHS")"
HOST_CLOSURE_SHA256="$(printf '%s' "$HOST_CLOSURE" | sha256sum | cut -d ' ' -f 1)"
nix/check-osdi-sha.sh "$NIX_SG13G2_OSDI_OUTPATH/osdi" "$OSDI_LIST" > /dev/null || \
    cosim_refuse "the OSDI models at $NIX_SG13G2_OSDI_OUTPATH are not the files $OSDI_LIST names"
# The runtime image carries the host closure in its own /nix/store (run.sh
# then binds none of the host's). nix/host-closure.lock pins each listed path's
# narHash; before anything is packed, every path has to be valid in this
# host's store with that narHash as the store registered it, and each one that
# is not is named. The image's label is the lock's sha256, and the verify stage
# recomputes each narHash in the image.
HOST_LOCK=nix/host-closure.lock
CLOSURE_INSIDE_LABEL=org.ihp.adk-tools.nix-closure-inside
[ -f "$HOST_LOCK" ] || cosim_refuse "$HOST_LOCK is missing"
python3 -B ci/check_store_closure.py --lock "$HOST_LOCK" --list "$HOST_LIST" --registered || \
    cosim_refuse "$HOST_LOCK is not what this host's store holds (above)"
CLOSURE_INSIDE="$(sha256sum < "$HOST_LOCK" | cut -d ' ' -f 1)"
# pack <paths> <archive>
pack() {
    sed 's|^/||' <<< "$1" | tar --create --file "$2.tmp" --directory / \
        --sort=name --format=gnu --mtime=@1 --owner=0 --group=0 --numeric-owner \
        --hard-dereference --files-from=- || cosim_refuse "could not pack the engine's closure ($2)"
    mv -f "$2.tmp" "$2"
}
pack "$COSIM_PATHS" "$COSIM_TAR"
pack "$MIXSIM_PATHS" "$MIXSIM_TAR"
pack "$VIEWER_PATHS" "$VIEWER_TAR"
pack "$VAMS_PATHS" "$VAMS_TAR"
# The host closure less what the four archives above already put in the store.
HOST_TAR=host-closure.tar
pack "$(LC_ALL=C comm -23 <(echo "$HOST_PATHS") \
    <(printf '%s\n' "$COSIM_PATHS" "$MIXSIM_PATHS" "$VIEWER_PATHS" "$VAMS_PATHS" | LC_ALL=C sort -u))" \
    "$HOST_TAR"
COSIM_ARGS=(--build-arg "NIX_COSIM_IVERILOG_OUTPATH=$NIX_COSIM_IVERILOG_OUTPATH"
            --build-arg "NIX_COSIM_COCOTB_OUTPATH=$NIX_COSIM_COCOTB_OUTPATH"
            --build-arg "NIX_COSIM_PYTHON_OUTPATH=$NIX_COSIM_PYTHON_OUTPATH"
            --build-arg "NIX_COSIM_DEVSHELL_OUTPATH=$NIX_COSIM_DEVSHELL_OUTPATH"
            --build-arg "NIX_MIXSIM_NGSPICE_OUTPATH=$NIX_MIXSIM_NGSPICE_OUTPATH"
            --build-arg "NIX_MIXSIM_IVERILOG_OUTPATH=$NIX_MIXSIM_IVERILOG_OUTPATH"
            --build-arg "NIX_SG13G2_OSDI_OUTPATH=$NIX_SG13G2_OSDI_OUTPATH"
            --build-arg "NIX_GTKWAVE_OUTPATH=$NIX_GTKWAVE_OUTPATH"
            --build-arg "NIX_GAW_OUTPATH=$NIX_GAW_OUTPATH"
            --build-arg "NIX_XSCHEM_OUTPATH=$NIX_XSCHEM_OUTPATH"
            --build-arg "NIX_VAMS_GNUCAP_OUTPATH=$NIX_VAMS_GNUCAP_OUTPATH"
            --build-arg "NIX_VAMS_DEVSHELL_OUTPATH=$NIX_VAMS_DEVSHELL_OUTPATH")

# The LibreLane launcher a die built in the container runs: nix/build-env.sh's,
# the one the Nix build shell writes, for the LibreLane above with the image's
# staged trees on its import path (build-env.sh's ADK_NIX_LAUNCHER_TREES, under
# /opt/adk-tools/staging, where an overlay mounts too). Every path it names is
# fixed here, so it is written here, on the host whose store holds that
# LibreLane, into the build context. The roots are the image's paths, not this
# host's: only their spelling is checked here, and the verify stage checks them
# in the image. The runtime stage copies the file, the verify stage holds it to
# LAUNCHER_SHA256, and the runtime image labels that sha256 and the one of the
# LibreLane wrapper the launcher runs, which run.sh holds the host's to before
# it passes the launcher in.
LAUNCHER_FILE=librelane-launcher
LAUNCHER_SHA256_LABEL=org.ihp.adk-tools.librelane-launcher-sha256
LIBRELANE_WRAPPER_SHA256_LABEL=org.ihp.adk-tools.nix-librelane-wrapper-sha256
LAUNCHER_TEXT="$(
    ADK_NIX_LIB_ONLY=1 ADK_NIX_STORE_DIR=/nix/store
    . nix/cli-env.sh
    . nix/build-env.sh
    mapfile -t roots < <(adk_nix_staged_roots /opt/adk-tools/staging)
    adk_nix_launcher_text "$NIX_LIBRELANE_OUTPATH" "${roots[@]}"
)" || cosim_refuse "could not write the LibreLane launcher for $NIX_LIBRELANE_OUTPATH (nix/build-env.sh)"
printf '%s\n' "$LAUNCHER_TEXT" > "$LAUNCHER_FILE.tmp" && mv -f "$LAUNCHER_FILE.tmp" "$LAUNCHER_FILE" || \
    cosim_refuse "could not write $LAUNCHER_FILE"
LAUNCHER_SHA256="$(sha256sum < "$LAUNCHER_FILE" | cut -d ' ' -f 1)"
# run.sh holds the host's wrapper to this as a file, not a link; so is it here.
[ -f "$NIX_LIBRELANE_OUTPATH/bin/librelane" ] && [ ! -L "$NIX_LIBRELANE_OUTPATH/bin/librelane" ] || \
    cosim_refuse "$NIX_LIBRELANE_OUTPATH/bin/librelane is not a file of its own; the launcher runs it"
LIBRELANE_WRAPPER_SHA256="$(sha256sum < "$NIX_LIBRELANE_OUTPATH/bin/librelane" | cut -d ' ' -f 1)"
[[ "$LIBRELANE_WRAPPER_SHA256" =~ ^[0-9a-f]{64}$ ]] || \
    cosim_refuse "could not digest $NIX_LIBRELANE_OUTPATH/bin/librelane"

# The build host's half of the tool identity table (nix/tool_identity.py
# facts): every Nix pin staging.pin names, whether the image carries it, and
# the narHash this host's store records for each path it needs. The runtime
# stage serialises each path it carries, refuses one that differs, and writes
# /opt/adk-tools/tool-identity.json; a pin run.sh mounts from the host keeps
# these values. Every pin has to be in this host's store, but for the Nix
# lane's interpreter (NIX_PYTHON_OUTPATH): the image runs its own, so the
# facts leave that pin out, and refuse when it is missing or another pin
# takes the name of a tool the image names itself.
python3 -B nix/tool_identity.py facts --pin staging.pin --out tool-identity.facts.json \
    > /dev/null || cosim_refuse "could not write tool-identity.facts.json from this host's store"

# Version manifest from the pinned submodules and the staged sources (baked
# into the image, shown by the in-image `adk-tools` command).
python3 - > manifest.json <<'PY'
import json, subprocess, datetime, hashlib, pathlib

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
                       "--exclude", "*/*")
    except subprocess.CalledProcessError:
        describe = sha[:10]
    tools[name] = {"repo": url, "ref": sha, "describe": describe}

# Staged sources. Kept in their own key rather than merged into "tools",
# because they are not the same kind of thing and a reader must be able to tell
# them apart: a submodule entry is reproducible from this repository alone, a
# staged entry needs that commit to exist in a local checkout. Flattening the
# two would hide exactly the property that makes staging a compromise.
staged = {}
for tree in sorted(pathlib.Path("staging").glob("*/")):
    if not (tree / ".git").exists():
        continue
    name = tree.name
    # Read back from the staged tree itself, not from staging.pin. The pin is
    # what was asked for; this is what is actually in the build context, and
    # when those two disagree the manifest must say what shipped.
    staged[name] = {
        "ref": git("-C", str(tree), "rev-parse", "HEAD"),
        "source": "staged from a local checkout, not a submodule",
    }

# Die views, read back from die-views/ in the build context, which
# stage-die-views.sh wrote and checked, and from the listing that pins it. Their
# own key, like "staged": they come from neither a submodule nor a commit.
die_views = {
    "listing_sha256": hashlib.sha256(pathlib.Path("die-views.sha256").read_bytes()).hexdigest(),
    "layouts": sorted(p.name for p in pathlib.Path("die-views").iterdir() if p.is_dir()),
    "source": "staged from a local directory, pinned by the listing's sha256",
}

# LibreLane is deliberately absent from this manifest. It is pinned in
# staging.pin (LIBRELANE_REPO/REV/PATCHES/PATCHED_REV) but the Dockerfile does
# not install it yet, and this file records what SHIPPED, not what is pinned. An
# entry built from the pins alone reads exactly the same whether the image
# carries LibreLane or carries nothing, which is the one thing a manifest must
# never do.
#
# When the LibreLane stage lands, the entry and its verification go in together,
# never the entry alone: the Dockerfile asserts the installed LibreLane's own
# `git rev-parse HEAD` against what this writes, the way the staged
# chiplet-spec reader is already checked (Dockerfile, the chiplet_format_io
# assertion). Then the entry is true by construction or the build fails.
print(json.dumps({
    "image": "adk-tools",
    "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    # Namespaced tags (a slash in the name) are local marks, never pushed: no
    # image names one.
    "meta": git("describe", "--tags", "--always", "--dirty", "--exclude", "*/*"),
    "tools": tools,
    "staged": staged,
    "die_views": die_views,
}, indent=2))
PY

# is_checkout <dir>: <dir> is a checked-out submodule. An uninitialised one is
# an empty directory, where git answers for the repository around it; only a
# checkout whose top level is that directory counts.
is_checkout() {
    [ -d "$1" ] && [ "$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" = "$(cd "$1" && pwd -P)" ]
}

# The time KLayout's binaries are stamped with: the KLayout commit's, so a
# rebuild from the same sources gives the same bytes on any day (Dockerfile,
# studio-builder). Both image builds pass it, or the second would rebuild
# KLayout under another key.
KLAYOUT_DIR=tools/chiplet-studio/extern/klayout
KLAYOUT_SOURCE_DATE_EPOCH=
if is_checkout "$KLAYOUT_DIR"; then
    KLAYOUT_SOURCE_DATE_EPOCH="$(git -C "$KLAYOUT_DIR" log -1 --format=%ct)"
fi
if [[ ! "$KLAYOUT_SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]]; then
    echo "Refusing to build: $KLAYOUT_DIR has no commit to read its time from" >&2
    exit 1
fi

# The interposer PDK labels its layouts with the commit its tree is at, which
# the image's copy reads from a stamp the runtime stage writes from this
# gitlink (ci/check_interposer_label.py). That is only the copied tree's commit
# when the submodule is checked out clean at the committed gitlink. The tools
# image never reads the tree, so --tools-only skips this.
# require_clean_gitlink <dir>: the submodule at dir is checked out at its
# committed gitlink, with no tracked file changed.
require_clean_gitlink() {
    local dir="$1" pin head
    pin="$(git rev-parse HEAD:"$dir")"
    is_checkout "$dir" || cosim_refuse "$dir is not checked out"
    head="$(git -C "$dir" rev-parse HEAD)"
    [ "$head" = "$pin" ] || cosim_refuse "$dir is at $head, not its committed gitlink $pin"
    [ -z "$(git -C "$dir" status --porcelain --untracked-files=no)" ] || \
        cosim_refuse "$dir has tracked files that differ from $pin"
}
INTERPOSER_DIR=tools/OpenIntM4TM2
INTERPOSER_PIN="$(git rev-parse HEAD:"$INTERPOSER_DIR")"
if [ "$TOOLS_ONLY" -eq 0 ]; then
    require_clean_gitlink "$INTERPOSER_DIR"
fi

MANIFEST_B64="$(base64 -w0 manifest.json)"
export DOCKER_BUILDKIT=1
# Daemon's embedded builder: images land directly in `docker images`
# (container-driver builders like buildx_buildkit_* need --load and keep a
# duplicate multi-GB cache).
export BUILDX_BUILDER=default

JOBS="${JOBS:-8}"

# The tools image: the Dockerfile above its thin-layer line, everything this
# repository does not write. ci/tools_key.py keys it on that text and on every
# file and build argument it reads; the image is tagged adk-tools:tools-<key>
# and labelled with the whole key, and built only when no image carries the
# key. A change below the line then rebuilds only what is below it, on the
# same tools image. A tag that exists with another key, or none, is refused:
# it would build our code on tools this key does not name.
TOOLS_KEY_LABEL=org.ihp.adk-tools.tools-key
TOOLS_ID_LABEL=org.ihp.adk-tools.tools-image
TOOLS_KEY="$(python3 -B ci/tools_key.py --build-arg "JOBS=$JOBS" "${COSIM_ARGS[@]}")" || \
    cosim_refuse "ci/tools_key.py could not key the tools image"
TOOLS_REF="adk-tools:tools-${TOOLS_KEY:0:12}"
# The runtime stage first holds docker/*.lock to the tools image's own, which
# exist only once that image is built: a key that moved without its locks
# fails there, after the whole tools image. docker/tools-locks.txt records the
# key the locks were read from, and a tree that does not match it builds the
# tools image alone (--tools-only) or nothing.
if [ "$TOOLS_ONLY" -eq 0 ]; then
    python3 -B ci/check_tools_locks.py --key "$TOOLS_KEY" > /dev/null || \
        cosim_refuse "docker/*.lock are not the tools image's this tree keys (ci/check_tools_locks.py)"
fi
tools_label() {
    local value
    value="$(docker image inspect -f "{{index .Config.Labels \"$TOOLS_KEY_LABEL\"}}" "$TOOLS_REF" 2>/dev/null)" \
        || value=absent
    printf '%s' "${value:-unlabelled}"
}
case "$(tools_label)" in
    "$TOOLS_KEY")
        echo "== tools image $TOOLS_REF carries this key; not rebuilt" ;;
    absent)
        echo "== building the tools image $TOOLS_REF (key $TOOLS_KEY)"
        docker build --target tools -t "$TOOLS_REF" \
            --label "$TOOLS_KEY_LABEL=$TOOLS_KEY" \
            "${COSIM_ARGS[@]}" \
            --build-arg JOBS="$JOBS" .
        [ "$(tools_label)" = "$TOOLS_KEY" ] || cosim_refuse "$TOOLS_REF does not carry the key it was built for" ;;
    *)
        cosim_refuse "$TOOLS_REF exists with key '$(tools_label)', not $TOOLS_KEY" ;;
esac
TOOLS_ID="$(docker image inspect -f '{{.Id}}' "$TOOLS_REF")"
if [ "$TOOLS_ONLY" -eq 1 ]; then
    echo
    echo "Done: $TOOLS_REF ($TOOLS_ID); its locks are /opt/adk-tools/tools/*.lock"
    exit 0
fi
TOOLS_ARGS=(--build-arg "TOOLS_IMAGE=$TOOLS_REF")

# layers_on_tools <image>: the image's layers start with the tools image's, so
# it is our code on exactly those tools.
layers_on_tools() {
    python3 -B - "$(docker image inspect -f '{{json .RootFS.Layers}}' "$TOOLS_ID")" \
        "$(docker image inspect -f '{{json .RootFS.Layers}}' "$1")" "$1" <<'PY' || exit 1
import json, sys
tools, image = json.loads(sys.argv[1]), json.loads(sys.argv[2])
if image[:len(tools)] != tools:
    sys.exit("Refusing: %s is not built on the tools image's %d layers" % (sys.argv[3], len(tools)))
print("== %s: %d tools layers, %d above them" % (sys.argv[3], len(tools), len(image) - len(tools)))
PY
}

# The runtime stage's fixed ENV: the tools a die build runs, from the closure
# it carries, and the launcher with its sha256. Both builds pass the same, so
# the verify build's runtime stage is the runtime image's.
RUNTIME_ARGS=(--build-arg "NIX_OPENROAD_OUTPATH=$NIX_OPENROAD_OUTPATH"
              --build-arg "NIX_LIBRELANE_OUTPATH=$NIX_LIBRELANE_OUTPATH"
              --build-arg "NIX_LIBRELANE_PYTHON=$LIBRELANE_PYTHON"
              --build-arg "NIX_SG13G2_PDK_OUTPATH=$NIX_SG13G2_PDK_OUTPATH"
              --build-arg "LAUNCHER_SHA256=$LAUNCHER_SHA256"
              --build-arg "INTERPOSER_PIN=$INTERPOSER_PIN")

if [ "$SKIP_VERIFY" -eq 0 ]; then
    echo "== building verify stage (compiles everything + runs all suites)"
    docker build --target verify -t "adk-tools:$VERIFY_TAG" \
        "${TOOLS_ARGS[@]}" \
        "${COSIM_ARGS[@]}" \
        "${RUNTIME_ARGS[@]}" \
        --build-arg "DIE_VIEWS_SHA256=$DIE_VIEWS_SHA256" \
        --build-arg "KLAYOUT_SOURCE_DATE_EPOCH=$KLAYOUT_SOURCE_DATE_EPOCH" \
        --build-arg "PIN_SET_SHA256=$PIN_SET_SHA256" \
        --build-arg JOBS="$JOBS" \
        --build-arg MANIFEST_B64="$MANIFEST_B64" .
    layers_on_tools "adk-tools:$VERIFY_TAG"
fi

echo "== tagging lean runtime image: adk-tools:$TAG"
# The labels are image config only: no layer is rebuilt for them.
docker build --target runtime -t "adk-tools:$TAG" \
    --label "$PIN_SET_LABEL=$PIN_SET_SHA256" \
    --label "$REVISION_LABEL=$REVISION" \
    --label "$OPENROAD_LABEL=$NIX_OPENROAD_OUTPATH" \
    --label "$LIBRELANE_LABEL=$NIX_LIBRELANE_OUTPATH" \
    --label "$LIBRELANE_PYTHON_LABEL=$LIBRELANE_PYTHON" \
    --label "$SG13G2_PDK_LABEL=$NIX_SG13G2_PDK_OUTPATH" \
    --label "$LAUNCHER_SHA256_LABEL=$LAUNCHER_SHA256" \
    --label "$LIBRELANE_WRAPPER_SHA256_LABEL=$LIBRELANE_WRAPPER_SHA256" \
    --label "$HOST_CLOSURE_LABEL=$HOST_CLOSURE" \
    --label "$HOST_CLOSURE_SHA256_LABEL=$HOST_CLOSURE_SHA256" \
    --label "$CLOSURE_INSIDE_LABEL=$CLOSURE_INSIDE" \
    --label "$COSIM_PYTHON_LABEL=$NIX_COSIM_PYTHON_OUTPATH" \
    --label "$COSIM_DEVSHELL_LABEL=$NIX_COSIM_DEVSHELL_OUTPATH" \
    --label "$COSIM_IVERILOG_LABEL=$NIX_COSIM_IVERILOG_OUTPATH" \
    --label "$MIXSIM_NGSPICE_LABEL=$NIX_MIXSIM_NGSPICE_OUTPATH" \
    --label "$MIXSIM_IVERILOG_LABEL=$NIX_MIXSIM_IVERILOG_OUTPATH" \
    --label "$SG13G2_OSDI_LABEL=$NIX_SG13G2_OSDI_OUTPATH" \
    --label "$GTKWAVE_LABEL=$NIX_GTKWAVE_OUTPATH" \
    --label "$GAW_LABEL=$NIX_GAW_OUTPATH" \
    --label "$XSCHEM_LABEL=$NIX_XSCHEM_OUTPATH" \
    --label "$VAMS_GNUCAP_LABEL=$NIX_VAMS_GNUCAP_OUTPATH" \
    --label "$VAMS_DEVSHELL_LABEL=$NIX_VAMS_DEVSHELL_OUTPATH" \
    --label "$TOOLS_KEY_LABEL=$TOOLS_KEY" \
    --label "$TOOLS_ID_LABEL=$TOOLS_ID" \
    "${TOOLS_ARGS[@]}" \
    "${COSIM_ARGS[@]}" \
    "${RUNTIME_ARGS[@]}" \
    --build-arg "DIE_VIEWS_SHA256=$DIE_VIEWS_SHA256" \
    --build-arg "KLAYOUT_SOURCE_DATE_EPOCH=$KLAYOUT_SOURCE_DATE_EPOCH" \
    --build-arg JOBS="$JOBS" \
    --build-arg MANIFEST_B64="$MANIFEST_B64" .
layers_on_tools "adk-tools:$TAG"
[ "$(docker image inspect -f '{{.Id}}' "$TOOLS_REF")" = "$TOOLS_ID" ] || \
    cosim_refuse "$TOOLS_REF moved to another image during this build"

echo
echo "Done: adk-tools:$TAG on $TOOLS_REF ($TOOLS_ID)"
docker image ls adk-tools --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'
