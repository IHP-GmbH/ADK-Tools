#!/bin/bash
# Run the adk-tools image with X11 and a persistent work directory.
#
# Usage: ADK_TOOLS_IMAGE=sha256:<64 hex> ./run.sh [--examples DIR] [--lock FILE]
#                                                 [--batch [--display] [--tty]] [--dry-run]
#                                                 [--cwd REL]
#                                                 [command ...]
#                                        (default command: interactive bash)
#
#   --examples DIR    an examples checkout, mounted read-only at /examples. The
#                     image is held to its release-lock.json
#   --lock FILE       hold the image to this release lock instead (with
#                     --examples, in place of DIR/release-lock.json; alone, the
#                     image is checked and nothing is mounted)
#   --batch           a non-interactive call: no stdin, no TTY and no display,
#                     whatever the caller has (bin/flow runs every call this way)
#   --display         with --batch only: the call takes the display after all,
#                     by the ADK_DISPLAY/DISPLAY rules below; still no stdin and
#                     no TTY (bin/flow passes it for the call that opens a window)
#   --tty             with --batch only: when run.sh's stdin and stdout are both
#                     terminals, the call takes that stdin and a TTY after all,
#                     so a program can ask the person; still no display unless
#                     --display. Otherwise it stays a batch call (bin/flow passes
#                     it for the calls that ask)
#   --dry-run         make every check, then print the docker command on stdout
#                     instead of running it; ADK_WORK is not created
#   --cwd REL         start the command in /work/REL rather than /work. REL is
#                     relative, has no "..", and names a directory of ADK_WORK
#                     with no symlink on the way (bin/flow passes the caller's
#                     place in the workspace this way)
#
#   ADK_TOOLS_IMAGE   ID of the image to run, sha256:<64 hex>. Required: there
#                     is no default, and a tag is refused (see below)
#   ADK_WORK          host work dir mounted at /work (default: ~/adk-work;
#                     created on first run; a relative path is taken from the
#                     current directory); refused when it holds this clone
#                     or lies inside it
#   ADK_MEM_LIMIT     container memory cap, swap off (default: 8g)
#   ADK_CPUS          container CPU cap (default: 4)
#   ADK_OPENROAD      OpenROAD to mount, a <store>/<hash>-openroad-* path
#                     (default: the one the image's label names, see below);
#                     one the image's closure label does not list mounts the
#                     whole host store. Ignored, with a note, for an image
#                     that carries its tools (the nix-closure-inside label)
#   ADK_NIX_LIBRELANE, ADK_NIX_LIBRELANE_PYTHON, ADK_NIX_SG13G2_PDK
#                     not read here. An image that carries its tools sets
#                     them, and the launcher pair, in its own ENV; for an
#                     older image they are passed into the container when
#                     the image's labels name them and they are on this host
#                     (see below), and with ADK_NIX_LIBRELANE, the image's
#                     LibreLane launcher goes in as
#                     CHIPLET_FLOW_LIBRELANE_LAUNCHER and its sha256, when
#                     this host's LibreLane wrapper is the one the image was
#                     built against
#   ADK_DISPLAY       X display, :N, or none for no display (default: DISPLAY
#                     when it is a local :N, else the newest ThinLinc session)
#   ADK_NETWORK       none to run the container with no network (default:
#                     docker's default network)
#   CHIPLET_FLOW_HOST, CHIPLET_FLOW_DIE_PROJECT
#                     not read here: each passed into the container as it is
#                     when set, never when unset. CHIPLET_FLOW_HOST is
#                     chiplet-flow's host profile (dedicated is for a machine
#                     of its own, and the caps above always apply);
#                     CHIPLET_FLOW_DIE_PROJECT names a die's LibreLane
#                     project for a system.chiplet builds entry, a path inside
#                     the container (under /work), never translated. No other
#                     caller variable is passed as it is, whatever its prefix
#   ADK_STAGING_OVERLAY
#                     "<name>=<path>[ ...]": run a host checkout's code in place
#                     of the image's staged tree <name>, mounted read-only (see
#                     below); refused under a release lock unless ADK_CI=1
#
# The container gets ADK_TOOLS_IMAGE, the ID it was started from. What a
# launcher calling this script can rely on (input, output, exit codes) is in
# docs/RUN_SH.md.
#
# Container layout (nothing is written into /work unless asked):
#   /examples                  an adk-examples checkout, read-only (--examples);
#                              each example's README.md says what to run
#   /work/heterogenic-designs  your persistent work area (host-backed), made by
#                              adk-new-project
#
# Examples:
#   export ADK_TOOLS_IMAGE=sha256:<64 hex>    # the ID of the image to run
#   ./run.sh                                  # shell; `adk-tools` lists tools
#   ./run.sh --examples ~/adk-examples        # with the examples at /examples
set -euo pipefail

# One line on stderr per choice made for the user, so a run says what it got.
note() { echo "run.sh: $*" >&2; }

# run.sh's own options come first; the first word that is not one starts the
# command. Read before anything else and checked later, once the image is known.
EXAMPLES=""
LOCK_FILE=""
CWD_REL=""
BATCH=""
BATCH_DISPLAY=""
BATCH_TTY=""
DRY_RUN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --batch) BATCH=1; shift ;;
        --display) BATCH_DISPLAY=1; shift ;;
        --tty) BATCH_TTY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --examples|--lock|--cwd)
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                echo "error: $1 needs a value" >&2
                exit 1
            fi
            case "$1" in
                --examples) EXAMPLES="$2" ;;
                --lock) LOCK_FILE="$2" ;;
                *) CWD_REL="$2" ;;
            esac
            shift 2
            ;;
        *) break ;;
    esac
done
if [ -n "$BATCH_DISPLAY" ] && [ -z "$BATCH" ]; then
    echo "error: --display only goes with --batch; without it the display is taken anyway" >&2
    exit 1
fi
if [ -n "$BATCH_TTY" ] && [ -z "$BATCH" ]; then
    echo "error: --tty only goes with --batch; without it the terminal is taken anyway" >&2
    exit 1
fi
# The container's stdin: the caller's, or none. A batch call takes none, unless
# --tty finds the person there: stdin and stdout both terminals. TTY: the
# caller's stdin is a terminal, so the container gets a TTY too (docker refuses
# -t on any other stdin).
STDIN=caller
if [ -n "$BATCH" ]; then
    STDIN=none
    [ -z "$BATCH_TTY" ] || { [ -t 0 ] && [ -t 1 ] && STDIN=caller; }
fi
TTY=""
[ "$STDIN" = none ] || [ ! -t 0 ] || TTY=1

# The image is named by its ID, never by a tag: the daemon is shared and a tag
# on it can be moved to another image between two runs. The rule is
# sha256:<64 lowercase hex>, what `docker image inspect --format '{{.Id}}'`
# prints and what host-verify.sh takes. Docker reads that (or the same 64 hex
# without the prefix) as an ID, never as a name. Anything shorter, the
# 12-character IDs `docker images` shows included, is looked up as a repository
# name first, so a tag of that name wins, and when nothing local matches,
# docker run tries to pull it.
#
# For an ID docker never pulls: one the daemon does not hold fails the ID read
# below with "No such image", before anything else is done.
IMAGE="${ADK_TOOLS_IMAGE:-}"
if [[ ! "$IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    if [ -z "$IMAGE" ]; then
        echo "error: ADK_TOOLS_IMAGE is unset or empty; run.sh has no default image" >&2
    else
        echo "error: ADK_TOOLS_IMAGE must be an image ID, sha256:<64 hex> (got '$IMAGE')" >&2
    fi
    echo "       A tag is not taken: the daemon is shared and a tag can be moved." >&2
    echo "       For an image you built, read its ID right after ./build.sh:" >&2
    echo "         docker image inspect --format '{{.Id}}' adk-tools:dev" >&2
    exit 1
fi

# The daemon's own ID for the image, read before anything else: the container
# gets the ID as ADK_TOOLS_IMAGE, so it has to be the string the daemon itself
# gives. stdout and stderr are read together. On success the ID is the last
# line, after any warning docker printed; on failure stdout is an empty line and
# the first line with text is docker's error.
if ID_READ="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>&1)"; then
    ID_SEEN="${ID_READ##*$'\n'}"
    if [ "$ID_SEEN" != "$IMAGE" ]; then
        echo "error: docker image inspect $IMAGE gave another ID: '$ID_SEEN'" >&2
        exit 1
    fi
else
    case "$ID_READ" in
        *"No such image"*)
            echo "error: no image $IMAGE on this docker daemon" >&2
            echo "       run.sh never pulls; build the image (README.md, Build) and pass its ID, or load its archive (docker load -i <file>)." >&2
            # The containerd image store names an image loaded from an archive
            # by its manifest's digest, and finds none by its config digest, the
            # ID a release lock and an archive name. An image built on that
            # store is found by its own ID, so a typo reads the same: this is
            # advice for the one case, not the cause (docs/RUN_SH.md).
            if [[ "$(docker info --format '{{json .DriverStatus}}' 2>/dev/null)" == *io.containerd.snapshotter* ]]; then
                echo "       This daemon uses the containerd image store, which finds an image loaded from an archive by another ID." >&2
                echo "       A release lock of schema /2 names that ID too (image_manifest): pass it instead, and no switch is needed." >&2
                echo "       If this ID came from a /1 release lock or an image archive, turn that store off and load the archive again;" >&2
                echo "       docs/RUN_SH.md (\"When the image is not there\") says how, and that it hides the store's images until it is back on." >&2
            fi
            ;;
        *)
            echo "error: docker image inspect $IMAGE failed: $(sed -n '/[^[:space:]]/{p;q}' <<< "$ID_READ")" >&2
            ;;
    esac
    exit 1
fi

# The release lock (an examples checkout's release-lock.json, or --lock FILE):
# the ID of the reference image the examples were checked on, the sha256 of the
# pin set it was built from and the adk-tools commit the examples go with. The
# lock has one byte form, canonical JSON with exactly these keys; any other is
# refused rather than read loosely. Schema /2 adds a fifth, image_manifest: the
# ID the containerd image store gives the reference image's archive (its
# manifest's digest; that store cannot read the config digest `image` names), so
# the reference image may be either ID. Any image has to carry the lock's pin
# set in build.sh's pin-set label; one that is not the reference image (one a
# user built, say) runs after a note naming both IDs. The commit is not read: a
# build of the same pin set may come from another adk-tools commit.
PIN_SET_LABEL=org.ihp.adk-tools.pin-set
LOCK_SCHEMA_1=adk-examples.release-lock/1
LOCK_SCHEMA_2=adk-examples.release-lock/2
EXAMPLES_ARGS=()
LOCK_NOTE=""
REFERENCE_NOTE=""
if [ -n "$EXAMPLES" ]; then
    if [ ! -d "$EXAMPLES" ]; then
        echo "error: --examples $EXAMPLES is not a directory" >&2
        exit 1
    fi
    EXAMPLES="$(realpath -- "$EXAMPLES")"
    [ -n "$LOCK_FILE" ] || LOCK_FILE="$EXAMPLES/release-lock.json"
fi
if [ -n "$LOCK_FILE" ]; then
    lock_refuse() {
        echo "error: the release lock $LOCK_FILE $*" >&2
        exit 1
    }
    [ -f "$LOCK_FILE" ] || lock_refuse "is not a file"
    LOCK_TEXT="$(cat -- "$LOCK_FILE" && printf x)" || lock_refuse "cannot be read"
    LOCK_TEXT="${LOCK_TEXT%x}"
    LOCK_RE=$'^\\{\n  "adk_tools": "([0-9a-f]{40})",\n  "image": "(sha256:[0-9a-f]{64})",\n(  "image_manifest": "(sha256:[0-9a-f]{64})",\n)?  "pin_set": "([0-9a-f]{64})",\n  "schema": "([^"]*)"\n\\}\n$'
    if [[ ! "$LOCK_TEXT" =~ $LOCK_RE ]]; then
        lock_refuse "is not a release lock in its one form: exactly adk_tools (40 hex)," \
            "image (sha256:<64 hex>), image_manifest (sha256:<64 hex>, schema /2 only)," \
            "pin_set (64 hex) and schema, as canonical JSON" \
            "(sorted keys, two-space indent, final newline)"
    fi
    LOCK_IMAGE="${BASH_REMATCH[2]}"
    LOCK_MANIFEST="${BASH_REMATCH[4]}"
    LOCK_PIN_SET="${BASH_REMATCH[5]}"
    # The schema follows from the keys (tests/adk_vscode holds this line equal).
    LOCK_WANT="$LOCK_SCHEMA_1"; [ -z "$LOCK_MANIFEST" ] || LOCK_WANT="$LOCK_SCHEMA_2"
    [ "${BASH_REMATCH[6]}" = "$LOCK_WANT" ] || lock_refuse "is of schema '${BASH_REMATCH[6]}';" \
        "run.sh reads $LOCK_SCHEMA_1 without image_manifest and $LOCK_SCHEMA_2 with it"
    [ "$LOCK_MANIFEST" != "$LOCK_IMAGE" ] || \
        lock_refuse "names $LOCK_IMAGE as both image and image_manifest"
    if [ "$IMAGE" != "$LOCK_IMAGE" ] && [ "$IMAGE" != "$LOCK_MANIFEST" ]; then
        REFERENCE_NOTE="the image $IMAGE is not the reference image $LOCK_IMAGE${LOCK_MANIFEST:+ (on the containerd store $LOCK_MANIFEST)} the release lock $LOCK_FILE names; it runs held to the lock's pin set"
    fi
    value="$(docker image inspect --format "{{index .Config.Labels \"$PIN_SET_LABEL\"}}" "$IMAGE")" || exit 1
    [ "$value" != "<no value>" ] || value=""
    [ "$value" = "$LOCK_PIN_SET" ] || \
        lock_refuse "names the pin set $LOCK_PIN_SET; the image's $PIN_SET_LABEL label is '$value'"
    if [ -n "$EXAMPLES" ]; then
        EXAMPLES_ARGS=(-v "$EXAMPLES:/examples:ro")
        LOCK_NOTE="examples $EXAMPLES at /examples (read-only), the image held to $LOCK_FILE"
    else
        LOCK_NOTE="the image held to $LOCK_FILE, no examples mounted"
    fi
fi

# ADK_STAGING_OVERLAY="<name>=<path>[ <name>=<path>...]": a host checkout
# mounted read-only over the image's staged tree /opt/adk-tools/staging/<name>,
# so the venv's editable install runs its code with the image's tools. Each is
# refused before docker starts unless: <name> is a staged tree, named once;
# <path> is the top of a git checkout whose .git is a directory without
# alternates (the image's tools read a staged tree's commit with git inside the
# container, where a worktree's gitfile points nowhere); and its entry points
# and top-level packages are the image's (ci/check_overlay.py, in a throwaway
# container; a refusal prints the check's whole output, its reason last).
# Under a release lock an overlay is refused unless ADK_CI=1. The
# identity is the tree of everything git would commit there, tracked or not,
# written with a throwaway index, so the checkout's own index is untouched. The
# container gets the trees as ADK_STAGING_OVERLAY_TREES, and each overlay
# prints one OVERLAY line, in a form a CI runner parses.
STAGED_TREES="assembly-lvs chiplet-cosim chiplet-flow chiplet-spec interposer-pnr packaging-flow"
OVERLAY_ARGS=()
OVERLAY_LINES=()
OVERLAY_TREES=""
if [ -n "${ADK_STAGING_OVERLAY:-}" ]; then
    overlay_refuse() {
        echo "error: ADK_STAGING_OVERLAY: $*" >&2
        exit 1
    }
    if [ -n "$LOCK_FILE" ] && [ "${ADK_CI:-}" != 1 ]; then
        overlay_refuse "refused under a release lock ($LOCK_FILE); a release run is the image's alone"
    fi
    read -ra OVERLAY_ITEMS -d '' <<< "$ADK_STAGING_OVERLAY" || true
    [ "${#OVERLAY_ITEMS[@]}" -gt 0 ] || \
        overlay_refuse "set but names no overlay; unset it, or name <name>=<path>"
    CHECKOUT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
    OVERLAY_SEEN=" "
    OVERLAY_PAIRS=()
    for item in "${OVERLAY_ITEMS[@]}"; do
        [[ "$item" =~ ^[a-z][-a-z]*=.+$ ]] || \
            overlay_refuse "'$item' is not <name>=<path> (a path with whitespace is refused)"
    done
    for item in "${OVERLAY_ITEMS[@]}"; do
        [[ "$item" =~ ^([a-z][-a-z]*)=(.+)$ ]]
        name="${BASH_REMATCH[1]}"
        path="${BASH_REMATCH[2]}"
        case " $STAGED_TREES " in
            *" $name "*) ;;
            *) overlay_refuse "'$name' is not a staged tree ($STAGED_TREES)" ;;
        esac
        case "$OVERLAY_SEEN" in
            *" $name "*) overlay_refuse "$name is named twice" ;;
        esac
        OVERLAY_SEEN+="$name "
        [ -d "$path" ] || overlay_refuse "$name: $path is not a directory"
        path="$(realpath -e -- "$path")"
        top="$(git -C "$path" rev-parse --show-toplevel 2>/dev/null)" || top=""
        [ "$top" = "$path" ] || overlay_refuse "$name: $path is not the top of a git checkout"
        if [ ! -d "$path/.git" ] || [ -L "$path/.git" ]; then
            overlay_refuse "$name: $path/.git is not a directory (a worktree or a submodule's gitfile);" \
                "use a clone"
        fi
        [ ! -e "$path/.git/objects/info/alternates" ] || \
            overlay_refuse "$name: $path borrows objects through alternates; use a clone without --shared"
        git -C "$path" rev-parse --verify --quiet HEAD > /dev/null || \
            overlay_refuse "$name: $path has no commit"
        index_dir="$(mktemp -d "${TMPDIR:-/tmp}/adk-tools-overlay.XXXXXX")"
        tree="$(export GIT_INDEX_FILE="$index_dir/index"
                git -C "$path" read-tree HEAD && git -C "$path" add -A && git -C "$path" write-tree)" \
            || tree=""
        rm -rf "$index_dir"
        [[ "$tree" =~ ^[0-9a-f]{40}$ ]] || overlay_refuse "$name: git could not write the tree of $path"
        if ! why="$(python3 -B "$CHECKOUT/ci/check_overlay.py" --image "$IMAGE" --name "$name" \
            --path "$path" 2>&1)"; then
            head -n -1 <<< "$why" >&2
            overlay_refuse "$name: $(tail -n 1 <<< "$why")"
        fi
        OVERLAY_PAIRS+=("$name=$tree")
        OVERLAY_ARGS+=(-v "$path:/opt/adk-tools/staging/$name:ro")
        OVERLAY_LINES+=("OVERLAY staging/$name <- $path (tree $tree); this run is NOT the image's alone")
    done
    OVERLAY_TREES="$(printf '%s\n' "${OVERLAY_PAIRS[@]}" | LC_ALL=C sort | paste -sd ' ')"
    OVERLAY_ARGS+=(-e "ADK_STAGING_OVERLAY_TREES=$OVERLAY_TREES")
fi

# The network: docker's default unless ADK_NETWORK=none. Only that one value is
# taken; any other is refused rather than handed to docker as a network name.
case "${ADK_NETWORK:-}" in
    "") NETWORK_ARGS=() ;;
    none) NETWORK_ARGS=(--network=none) ;;
    *)
        echo "error: ADK_NETWORK must be none or unset (got '$ADK_NETWORK')" >&2
        exit 1
        ;;
esac

# OpenROAD, LibreLane, its interpreter, the sg13g2 PDK and the launcher. An
# image that carries them in its own /nix/store (the
# org.ihp.adk-tools.nix-closure-inside label: the sha256 of the closure lock it
# was built with) holds OPENROAD, ADK_NIX_LIBRELANE, ADK_NIX_LIBRELANE_PYTHON,
# ADK_NIX_SG13G2_PDK and the launcher pair as fixed ENV: no part of the host's
# store goes in, and the host needs no Nix. Everything below that reads the
# host's store is for an image without the label (an image built without
# it): the examples' route stages need an OpenROAD, and the host's Nix store
# has it. ADK_OPENROAD names it; otherwise the image does, in a label build.sh
# writes from staging.pin's NIX_OPENROAD_OUTPATH, read here without starting a
# container. It is read from the image and not from this checkout's staging.pin
# because the two need not match: this run.sh can launch a release's image.
# An image without that label needs ADK_OPENROAD. Such a run needs Nix on the
# host; what of the store goes in is decided below.
CLOSURE_INSIDE_LABEL=org.ihp.adk-tools.nix-closure-inside
# docker's index prints the same for an empty label as for none, so this one
# ranges over the labels: "+<value>|" when the image has it, nothing when not.
# The bar keeps a trailing newline in the value from being stripped. A label
# that is there, empty included, has to be a sha256.
CLOSURE_INSIDE="$(docker image inspect --format \
    "{{range \$k, \$v := .Config.Labels}}{{if eq \$k \"$CLOSURE_INSIDE_LABEL\"}}+{{\$v}}|{{end}}{{end}}" \
    "$IMAGE")" || exit 1
if [ -n "$CLOSURE_INSIDE" ]; then
    CLOSURE_INSIDE="${CLOSURE_INSIDE#+}"
    CLOSURE_INSIDE="${CLOSURE_INSIDE%|}"
    if [[ ! "$CLOSURE_INSIDE" =~ ^[0-9a-f]{64}$ ]]; then
        echo "error: the image's $CLOSURE_INSIDE_LABEL label is not a sha256 (got '$CLOSURE_INSIDE')" >&2
        exit 1
    fi
fi
# Such an image's tools, from its fixed ENV, read from the image's config (no
# container is started): the notes name each one, and none is passed in. An
# image with the label and without one of them, or whose launcher sha256 is not
# its launcher label's, is refused.
INSIDE_NAMES=(OPENROAD ADK_NIX_LIBRELANE ADK_NIX_LIBRELANE_PYTHON ADK_NIX_SG13G2_PDK
              CHIPLET_FLOW_LIBRELANE_LAUNCHER CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256)
declare -A INSIDE_ENV=()
if [ -n "$CLOSURE_INSIDE" ]; then
    image_env="$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
        "$IMAGE")" || exit 1
    # A later entry wins, as in the container. An entry with no "=" is one no
    # Dockerfile ENV writes, and run.sh does not rely on how Docker would treat
    # it: it reads as no value, and the check below refuses the image.
    while IFS= read -r line; do
        for name in "${INSIDE_NAMES[@]}"; do
            [ "${line%%=*}" = "$name" ] || continue
            case "$line" in
                *=*) INSIDE_ENV["$name"]="${line#*=}" ;;
                *) unset 'INSIDE_ENV[$name]' ;;
            esac
        done
    done <<< "$image_env"
    for name in "${INSIDE_NAMES[@]}"; do
        if [ -z "${INSIDE_ENV[$name]:-}" ]; then
            echo "error: the image's $CLOSURE_INSIDE_LABEL label says it carries its tools, but its ENV sets no $name" >&2
            exit 1
        fi
    done
    inside_launcher_sha="$(docker image inspect \
        --format '{{index .Config.Labels "org.ihp.adk-tools.librelane-launcher-sha256"}}' \
        "$IMAGE")" || exit 1
    if [ "$inside_launcher_sha" != "${INSIDE_ENV[CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256]}" ]; then
        echo "error: the image's CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256 (${INSIDE_ENV[CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256]}) is not its org.ihp.adk-tools.librelane-launcher-sha256 label ('$inside_launcher_sha')" >&2
        exit 1
    fi
fi
OPENROAD_LABEL=org.ihp.adk-tools.nix-openroad
OPENROAD_PATH="${ADK_OPENROAD:-}"
OPENROAD_FROM=ADK_OPENROAD
if [ -n "$CLOSURE_INSIDE" ]; then
    OPENROAD_PATH=""
elif [ -z "$OPENROAD_PATH" ]; then
    OPENROAD_FROM="the image's $OPENROAD_LABEL label"
    OPENROAD_PATH="$(docker image inspect \
        --format "{{index .Config.Labels \"$OPENROAD_LABEL\"}}" "$IMAGE")" || exit 1
    [ "$OPENROAD_PATH" != "<no value>" ] || OPENROAD_PATH=""
fi
HOST_PACKAGES=".#openroad .#librelane .#librelane-python .#sg13g2-pdk"
OPENROAD_ARGS=()
if [ -n "$OPENROAD_PATH" ]; then
    if [[ ! "$OPENROAD_PATH" =~ ^(/.+)/[0-9a-z]{32}-openroad[^/]*$ ]]; then
        echo "error: OpenROAD from $OPENROAD_FROM is not a <store>/<hash>-openroad path (got '$OPENROAD_PATH')" >&2
        exit 1
    fi
    OPENROAD_STORE="${BASH_REMATCH[1]}"
    if [ ! -d "$OPENROAD_STORE" ]; then
        echo "error: needs Nix on the host: Nix is not installed (no $OPENROAD_STORE), and this release needs it" >&2
        echo "       Install Nix, then run nix build $HOST_PACKAGES in the adk-tools clone at the commit the release names." >&2
        exit 1
    fi
    if [ ! -f "$OPENROAD_PATH/bin/openroad" ] || [ ! -x "$OPENROAD_PATH/bin/openroad" ]; then
        echo "error: needs Nix on the host: OpenROAD from $OPENROAD_FROM is not on this host: no executable $OPENROAD_PATH/bin/openroad" >&2
        echo "       Realise it from the adk-tools flake (nix build .#openroad), or set ADK_OPENROAD." >&2
        exit 1
    fi
    OPENROAD_ARGS=(-e "OPENROAD=$OPENROAD_PATH/bin/openroad")
fi

# What goes in from the host's store. build.sh labels the image with the
# closure of OpenROAD and the three LibreLane store paths below (the paths
# joined by single spaces) and with that label's sha256, computed when the
# image was built, so no launch asks Nix for it. Each path of the list is bound
# read-only on its own, at its own path, and the image's own /nix/store stays
# visible beside them: the engines and viewers it carries are never hidden. A
# listed path this host lacks is not bound; the notes count them. An image with
# neither label, or an ADK_OPENROAD the list does not
# name, has no closure to go by: the whole store goes in, as before, and hides
# the image's own (the engines' notes below say what that costs).
HOST_CLOSURE_LABEL=org.ihp.adk-tools.nix-host-closure
HOST_CLOSURE_SHA256_LABEL=org.ihp.adk-tools.nix-host-closure-sha256
CLOSURE_BOUND=""
declare -A BOUND=()
STORE_NOTES=()
if [ -n "$OPENROAD_PATH" ]; then
    CLOSURE="$(docker image inspect \
        --format "{{index .Config.Labels \"$HOST_CLOSURE_LABEL\"}}" "$IMAGE")" || exit 1
    [ "$CLOSURE" != "<no value>" ] || CLOSURE=""
    CLOSURE_SHA256="$(docker image inspect \
        --format "{{index .Config.Labels \"$HOST_CLOSURE_SHA256_LABEL\"}}" "$IMAGE")" || exit 1
    [ "$CLOSURE_SHA256" != "<no value>" ] || CLOSURE_SHA256=""
    if [ -n "$CLOSURE" ] && [ -n "$CLOSURE_SHA256" ]; then
        if [ "$(printf '%s' "$CLOSURE" | sha256sum | cut -d ' ' -f 1)" != "$CLOSURE_SHA256" ]; then
            echo "error: the image's $HOST_CLOSURE_LABEL label is not the one its $HOST_CLOSURE_SHA256_LABEL label names" >&2
            exit 1
        fi
        read -r -a LISTED <<< "$CLOSURE"
        IN_LIST=""
        SHAPE=""
        [ "${LISTED[*]}" = "$CLOSURE" ] || SHAPE=1
        for p in "${LISTED[@]}"; do
            if [[ ! "$p" =~ ^(/.+)/[0-9a-z]{32}-[-.+_0-9A-Za-z]+$ ]] \
                    || [ "${BASH_REMATCH[1]}" != "$OPENROAD_STORE" ]; then
                SHAPE=1
            fi
            [ "$p" != "$OPENROAD_PATH" ] || IN_LIST=1
        done
        if [ -n "$SHAPE" ]; then
            echo "error: the image's $HOST_CLOSURE_LABEL label is not $OPENROAD_STORE/<hash>-<name> paths joined by single spaces" >&2
            exit 1
        fi
        if [ -z "$IN_LIST" ] && [ "$OPENROAD_FROM" != ADK_OPENROAD ]; then
            echo "error: the image's $HOST_CLOSURE_LABEL label does not list $OPENROAD_PATH, the OpenROAD its $OPENROAD_LABEL label names" >&2
            exit 1
        fi
    elif [ -n "$CLOSURE" ] || [ -n "$CLOSURE_SHA256" ]; then
        echo "error: the image has only one of the $HOST_CLOSURE_LABEL and $HOST_CLOSURE_SHA256_LABEL labels" >&2
        exit 1
    else
        IN_LIST=""
    fi
    if [ -n "$IN_LIST" ]; then
        missing=0
        for p in "${LISTED[@]}"; do
            if [ -e "$p" ] || [ -L "$p" ]; then
                OPENROAD_ARGS+=(-v "$p:$p:ro")
                BOUND["$p"]=1
            else
                missing=$((missing + 1))
            fi
        done
        CLOSURE_BOUND=1
        STORE_NOTES+=("store: ${#BOUND[@]} of ${#LISTED[@]} paths the image's $HOST_CLOSURE_LABEL label lists, each bound read-only")
        [ "$missing" -eq 0 ] || STORE_NOTES+=("needs Nix on the host: $missing of ${#LISTED[@]} paths the image's $HOST_CLOSURE_LABEL label lists are not on this host (nix build $HOST_PACKAGES in the adk-tools flake)")
    else
        OPENROAD_ARGS+=(-v "$OPENROAD_STORE:$OPENROAD_STORE:ro")
        if [ -n "$CLOSURE" ]; then
            STORE_NOTES+=("store: all of $OPENROAD_STORE, since the image's $HOST_CLOSURE_LABEL label does not list $OPENROAD_PATH (ADK_OPENROAD); it hides the image's own store paths")
        fi
    fi
fi

# LibreLane, its interpreter and the sg13g2 PDK, for a die built in the
# container from an image that does not carry them; build.sh labels the store paths
# staging.pin names (NIX_LIBRELANE_OUTPATH, NIX_LIBRELANE_PYTHON_OUTPATH's
# bin/python3.<minor>, NIX_SG13G2_PDK_OUTPATH), and each goes in as
# ADK_NIX_LIBRELANE, ADK_NIX_LIBRELANE_PYTHON or ADK_NIX_SG13G2_PDK, the names
# the Nix build shell sets. The interpreter is a file, not a folder: it has to
# be a regular file and not a link, as core preparation takes it.
# They come through the store mounted for OpenROAD, so a path goes in only when
# it is in that store, on this host and, when the closure is bound, in the
# list bound. Anything else is a note, not a refusal: most runs build no die,
# and the page that does refuses.
STORE_ARGS=()
# store_path <label> <variable> <store name, an ERE> <file it holds> <package>
# (an empty <file it holds>: the path is itself that file)
store_path() {
    local label="$1" var="$2" name="$3" holds="$4" package="$5" path rest top
    if [ -z "${OPENROAD_STORE:-}" ]; then
        STORE_NOTES+=("no $var: no Nix store is mounted (no OpenROAD); a page that builds a die will refuse")
        return
    fi
    path="$(docker image inspect --format "{{index .Config.Labels \"$label\"}}" "$IMAGE")" || exit 1
    [ "$path" != "<no value>" ] || path=""
    # The store path the label's path sits in (the interpreter is a file in one).
    rest="${path#"$OPENROAD_STORE"/}"
    top="$OPENROAD_STORE/${rest%%/*}"
    if [ -z "$path" ]; then
        STORE_NOTES+=("no $var: the image has no $label label; a page that builds a die will refuse")
    elif [[ ! "$path" =~ ^(/.+)/[0-9a-z]{32}-$name$ ]]; then
        STORE_NOTES+=("no $var: the image's $label label is not a <store>/<hash>-$package path (got '$path')")
    elif [ "${BASH_REMATCH[1]}" != "$OPENROAD_STORE" ]; then
        STORE_NOTES+=("no $var: $path is not in $OPENROAD_STORE, the store mounted for OpenROAD")
    elif [ ! -f "$path${holds:+/$holds}" ]; then
        STORE_NOTES+=("needs Nix on the host: no $var, $path is not on this host (nix build .#$package in the adk-tools flake); a page that builds a die will refuse")
    elif [ -n "$CLOSURE_BOUND" ] && [ -z "${BOUND["$top"]:-}" ]; then
        STORE_NOTES+=("no $var: $path is not in the image's $HOST_CLOSURE_LABEL label, so it is not mounted; a page that builds a die will refuse")
    elif [ -z "$holds" ] && [ -L "$path" ]; then
        STORE_NOTES+=("no $var: $path is a link, not the file itself; a page that builds a die will refuse")
    else
        STORE_ARGS+=(-e "$var=$path")
        STORE_PASSED["$var"]="$path"
        STORE_NOTES+=("$var $path (from the image's $label label)")
    fi
}
declare -A STORE_PASSED=()
LIBRELANE_LABEL=org.ihp.adk-tools.nix-librelane
LIBRELANE_PYTHON_LABEL=org.ihp.adk-tools.nix-librelane-python
SG13G2_PDK_LABEL=org.ihp.adk-tools.nix-sg13g2-pdk
if [ -n "$CLOSURE_INSIDE" ]; then
    STORE_NOTES+=("store: none of the host's; the image carries OpenROAD, LibreLane, its interpreter, the sg13g2 PDK and the launcher (its $CLOSURE_INSIDE_LABEL label, $CLOSURE_INSIDE)")
    [ -z "${ADK_OPENROAD:-}" ] || STORE_NOTES+=("ADK_OPENROAD ignored: the image's own OpenROAD runs")
    for name in ADK_NIX_LIBRELANE ADK_NIX_LIBRELANE_PYTHON ADK_NIX_SG13G2_PDK; do
        STORE_NOTES+=("$name ${INSIDE_ENV[$name]} (the image's own, in its ENV)")
    done
    STORE_NOTES+=("CHIPLET_FLOW_LIBRELANE_LAUNCHER ${INSIDE_ENV[CHIPLET_FLOW_LIBRELANE_LAUNCHER]} (the image's own, in its ENV), sha256 ${INSIDE_ENV[CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256]} (its ENV and its org.ihp.adk-tools.librelane-launcher-sha256 label)")
else
    store_path "$LIBRELANE_LABEL" ADK_NIX_LIBRELANE \
        'python3[.0-9]*-librelane-[-.+_0-9A-Za-z]*' bin/librelane librelane
    store_path "$LIBRELANE_PYTHON_LABEL" ADK_NIX_LIBRELANE_PYTHON \
        'python3-3\.[0-9]+\.[0-9]+-env/bin/python3\.[0-9]+' '' librelane-python
    store_path "$SG13G2_PDK_LABEL" ADK_NIX_SG13G2_PDK \
        'ihp-open-pdk-[0-9a-f]{7}' pdk-source.json sg13g2-pdk
fi

# The co-simulation, mixed-signal and Verilog-AMS engines and the waveform
# viewers. The image carries them in its own Nix store (COSIM_DEVSHELL,
# MIXSIM_DEVSHELL and VAMS_DEVSHELL link into it, as do gtkwave, gaw and xschem
# on PATH), and build.sh labels the devshell, the python env and Icarus of the
# first engine, ngspice, its Icarus and the OSDI models of the second, the
# devshell and gnucap of the third, and the three viewers. With the closure bound
# nothing hides them, so nothing is said. When the whole store goes in for
# OpenROAD, at its own path, and that is the store one of them sits in, the
# mount hides the image's copy and it has to be in the host's store too. Only
# that case is said, as a note: most runs simulate and view nothing. An image
# without the labels carries none of them.
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
# cosim_path <label> <store name, an ERE> <file it holds> <package> [<engine> [<kind>]]
cosim_path() {
    local label="$1" name="$2" holds="$3" package="$4" engine="${5:-co-simulation}"
    local kind="${6:-engine}" path
    [ -n "${OPENROAD_STORE:-}" ] || return 0
    [ -z "$CLOSURE_BOUND" ] || return 0
    path="$(docker image inspect --format "{{index .Config.Labels \"$label\"}}" "$IMAGE")" || exit 1
    [ "$path" != "<no value>" ] || path=""
    if [ -z "$path" ]; then
        return 0
    elif [[ ! "$path" =~ ^(/.+)/[0-9a-z]{32}-$name$ ]]; then
        STORE_NOTES+=("$engine: the image's $label label is not a <store>/<hash>-$package path (got '$path')")
    elif [ "${BASH_REMATCH[1]}" = "$OPENROAD_STORE" ] && [ ! -e "$path/$holds" ]; then
        STORE_NOTES+=("no $engine $kind: $path is not on this host, and the store mounted for OpenROAD hides the image's copy (nix build .#$package in the adk-tools flake)")
    fi
}
cosim_path "$COSIM_DEVSHELL_LABEL" 'cosim-devshell' bin/python3 cosim-devshell
cosim_path "$COSIM_PYTHON_LABEL" 'python3-[.0-9]*-env' bin/python3 cosim-python
cosim_path "$COSIM_IVERILOG_LABEL" 'iverilog-[-.+_0-9A-Za-z]*' bin/vvp cosim-iverilog
cosim_path "$MIXSIM_NGSPICE_LABEL" 'ngspice-[.0-9]+' bin/ngspice mixsim-ngspice mixed-signal
cosim_path "$MIXSIM_IVERILOG_LABEL" 'iverilog-libvvp-[-.+_0-9A-Za-z]*' lib/libvvp.so mixsim-iverilog mixed-signal
cosim_path "$SG13G2_OSDI_LABEL" 'sg13g2-osdi-[0-9a-f]{7}' osdi sg13g2-osdi mixed-signal
cosim_path "$GTKWAVE_LABEL" 'gtkwave-[.0-9]+' bin/gtkwave gtkwave gtkwave viewer
cosim_path "$GAW_LABEL" 'gaw3-[0-9]{8}' bin/gaw gaw gaw viewer
cosim_path "$XSCHEM_LABEL" 'xschem-[.0-9]+(RC)?-[0-9a-f]{7}' bin/xschem xschem xschem viewer
cosim_path "$VAMS_DEVSHELL_LABEL" 'vams-devshell' bin/gnucap vams-devshell Verilog-AMS
cosim_path "$VAMS_GNUCAP_LABEL" 'gnucap-[0-9a-f]{7}-modelgen-verilog-[0-9a-f]{7}' bin/gnucap \
    vams-gnucap Verilog-AMS

# The LibreLane launcher a die build runs (build.sh writes it with
# nix/build-env.sh): the store LibreLane above with the image's staged
# chiplet-flow and chiplet-spec on its import path, at a fixed
# path in the image, and its sha256 in the image's label. It goes in as
# CHIPLET_FLOW_LIBRELANE_LAUNCHER and CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256,
# the pair chiplet-flow binds and records for each build, only with
# ADK_NIX_LIBRELANE and only when this host's bin/librelane, the wrapper the
# launcher runs, has the sha256 the image's other label recorded when it was
# built. Otherwise neither goes in, and that is a note: a page that builds a die
# refuses.
LIBRELANE_LAUNCHER=/opt/adk-tools/librelane-launcher/librelane
LAUNCHER_SHA256_LABEL=org.ihp.adk-tools.librelane-launcher-sha256
LIBRELANE_WRAPPER_SHA256_LABEL=org.ihp.adk-tools.nix-librelane-wrapper-sha256
if [ -n "${STORE_PASSED[ADK_NIX_LIBRELANE]:-}" ]; then
    launcher_sha="$(docker image inspect --format "{{index .Config.Labels \"$LAUNCHER_SHA256_LABEL\"}}" "$IMAGE")" || exit 1
    wrapper_sha="$(docker image inspect --format "{{index .Config.Labels \"$LIBRELANE_WRAPPER_SHA256_LABEL\"}}" "$IMAGE")" || exit 1
    [ "$launcher_sha" != "<no value>" ] || launcher_sha=""
    [ "$wrapper_sha" != "<no value>" ] || wrapper_sha=""
    wrapper="${STORE_PASSED[ADK_NIX_LIBRELANE]}/bin/librelane"
    missing=()
    [ -n "$launcher_sha" ] || missing+=("$LAUNCHER_SHA256_LABEL")
    [ -n "$wrapper_sha" ] || missing+=("$LIBRELANE_WRAPPER_SHA256_LABEL")
    if [ "${#missing[@]}" -gt 0 ]; then
        STORE_NOTES+=("no CHIPLET_FLOW_LIBRELANE_LAUNCHER: the image has no ${missing[0]} label${missing[1]:+ and no ${missing[1]} label}; a page that builds a die will refuse")
    elif [[ ! "$launcher_sha" =~ ^[0-9a-f]{64}$ ]] || [[ ! "$wrapper_sha" =~ ^[0-9a-f]{64}$ ]]; then
        STORE_NOTES+=("no CHIPLET_FLOW_LIBRELANE_LAUNCHER: the image's $LAUNCHER_SHA256_LABEL and $LIBRELANE_WRAPPER_SHA256_LABEL labels are not both a sha256; a page that builds a die will refuse")
    elif [ -L "$wrapper" ] || [ "$(sha256sum < "$wrapper" | cut -d ' ' -f 1)" != "$wrapper_sha" ]; then
        STORE_NOTES+=("no CHIPLET_FLOW_LIBRELANE_LAUNCHER: $wrapper on this host is not the wrapper the image was built against (its $LIBRELANE_WRAPPER_SHA256_LABEL label, $wrapper_sha); a page that builds a die will refuse")
    else
        STORE_ARGS+=(-e "CHIPLET_FLOW_LIBRELANE_LAUNCHER=$LIBRELANE_LAUNCHER"
                     -e "CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=$launcher_sha")
        STORE_NOTES+=("CHIPLET_FLOW_LIBRELANE_LAUNCHER $LIBRELANE_LAUNCHER (sha256 from the image's $LAUNCHER_SHA256_LABEL label)")
    fi
fi

# The X display: ADK_DISPLAY (:N, or none), else DISPLAY when it names a local
# :N, else the newest ThinLinc session (its `last` link). The display has to
# have its socket. The one found through ThinLinc also has to be ours: a stale
# session directory keeps its number after the session ends, and that number
# can be another user's display by now. A display the caller names is not held
# to that, because a local Xorg's socket is commonly root's. ADK_X11_DIR and
# ADK_THINLINC_SESSIONS move the two roots for tests.
X11_DIR="${ADK_X11_DIR:-/tmp/.X11-unix}"
SESSIONS="${ADK_THINLINC_SESSIONS:-/var/opt/thinlinc/sessions/$(id -un 2>/dev/null || true)}"
DISPLAY_N=""
DISPLAY_FROM=""
DISPLAY_OURS=""
# --batch takes no display, and ADK_DISPLAY is then not read at all, unless
# --display asks for one.
DISPLAY_CHOICE="${ADK_DISPLAY:-}"
[ -z "$BATCH" ] || [ -n "$BATCH_DISPLAY" ] || DISPLAY_CHOICE=none
case "$DISPLAY_CHOICE" in
    none)
        if [ -n "$BATCH" ]; then
            DISPLAY_NOTE="no display: --batch"
        else
            DISPLAY_NOTE="no display: ADK_DISPLAY=none"
        fi
        ;;
    "")
        if [[ "${DISPLAY:-}" =~ ^:([0-9]+)(\.[0-9]+)?$ ]]; then
            DISPLAY_N="${BASH_REMATCH[1]}"
            DISPLAY_FROM=DISPLAY
        else
            last="$(readlink "$SESSIONS/last" 2>/dev/null || true)"
            last="${last##*/}"
            if [[ "$last" =~ ^[0-9]+$ ]]; then
                DISPLAY_N="$last"
                DISPLAY_FROM="ThinLinc session $last"
                DISPLAY_OURS=1
            fi
        fi
        ;;
    *)
        if [[ ! "$ADK_DISPLAY" =~ ^:([0-9]+)$ ]]; then
            echo "error: ADK_DISPLAY must be :N or none (got '$ADK_DISPLAY')" >&2
            exit 1
        fi
        DISPLAY_N="${BASH_REMATCH[1]}"
        DISPLAY_FROM=ADK_DISPLAY
        ;;
esac
SOCKET=""
if [ -n "$DISPLAY_N" ]; then
    SOCKET="$X11_DIR/X$DISPLAY_N"
    if [ ! -S "$SOCKET" ]; then
        DISPLAY_NOTE="no display: $SOCKET (from $DISPLAY_FROM) is not an X socket"
        DISPLAY_N=""
    elif [ -n "$DISPLAY_OURS" ] && [ "$(stat -c %u "$SOCKET")" != "$(id -u)" ]; then
        DISPLAY_NOTE="no display: $SOCKET (from $DISPLAY_FROM) is another user's X socket"
        DISPLAY_N=""
    fi
elif [ "$DISPLAY_CHOICE" != none ]; then
    DISPLAY_NOTE="no display: GUI tools will not open (set ADK_DISPLAY=:N)"
fi

# docker takes a bare name in -v as a named volume, not a directory: `work`
# would mount a volume and leave the directory empty. The work dir is bound by
# its absolute path, symlinks resolved, taken before anything is made so that a
# refusal below leaves nothing behind.
ADK_WORK="$(realpath -m -- "${ADK_WORK:-$HOME/adk-work}")"
# Tool and work stay apart both ways. With the clone in the work dir the
# container sees, and can write, the launcher and the pins it was built from;
# with the work dir in the clone, the user's runs sit in its git tree, where a
# git clean deletes them.
CLONE="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
case "$CLONE/" in
    "${ADK_WORK%/}/"*)
        echo "error: ADK_WORK ($ADK_WORK) holds this adk-tools clone ($CLONE)" >&2
        echo "       Keep the clone outside the work dir, or point ADK_WORK elsewhere." >&2
        exit 1
        ;;
esac
case "$ADK_WORK/" in
    "$CLONE/"*)
        echo "error: ADK_WORK ($ADK_WORK) is inside this adk-tools clone ($CLONE)" >&2
        echo "       A git clean in the clone would delete it; point ADK_WORK outside the clone." >&2
        exit 1
        ;;
esac

# --cwd: the command starts in /work/REL. REL has to name, physically, a
# directory of ADK_WORK: an absolute REL or a ".." in it would leave /work, a
# symlink on the way could resolve elsewhere inside the container, and a
# missing one would be made by docker, as root.
WORKDIR=/work
if [ -n "$CWD_REL" ]; then
    case "$CWD_REL" in
        /*)
            echo "error: --cwd $CWD_REL is absolute; it has to be relative to /work" >&2
            exit 1
            ;;
    esac
    REL="${CWD_REL%/}"
    [ "$REL" != "." ] || REL=""
    case "/$REL/" in
        */../*)
            echo "error: --cwd $CWD_REL has a '..' in it; it has to stay below /work" >&2
            exit 1
            ;;
    esac
    if [ -n "$REL" ]; then
        if [ ! -d "$ADK_WORK/$REL" ] \
                || [ "$(realpath -e -- "$ADK_WORK/$REL")" != "$ADK_WORK/$REL" ]; then
            echo "error: --cwd $CWD_REL: $ADK_WORK/$REL is not a directory of ADK_WORK" \
                "with no symlink on the way" >&2
            exit 1
        fi
        WORKDIR="/work/$REL"
    fi
fi
[ -n "$DRY_RUN" ] || mkdir -p "$ADK_WORK"

# Hard boundaries: cap the container (and thus every process inside it,
# collectively) so a runaway tool cannot exhaust host RAM or CPUs on a shared
# host. --memory-swap set equal to --memory disables swap for the container: at
# the limit the kernel OOM-kills a process inside the container instead of
# spilling to host swap. cgroup v2 accounts swap, so the zero-swap cap is
# honored. 8g covers the examples, whose measured peaks are 3.6 and 4.3 GiB
# (bin/flow caps a call at 5g); KiCad, Chiplet Studio
# and the assembly DRC have no measured peak, so raise ADK_MEM_LIMIT (e.g. 16g)
# when one of them is killed.
ADK_MEM_LIMIT="${ADK_MEM_LIMIT:-8g}"
ADK_CPUS="${ADK_CPUS:-4}"

# Minimal passwd/group so the container user has a name (avoids
# "I have no name!" prompts and user-lookup failures). A private mktemp dir
# (0700, unguessable name) instead of a predictable /tmp/...-$(id -u) path that
# a co-located user could pre-create to DoS the launch or, via a planted passwd
# symlink, redirect our write; cleaned up after the container exits. The X
# cookie below is written here too.
ETC_DIR="$(mktemp -d "${TMPDIR:-/tmp}/adk-tools-etc.XXXXXX")"
trap 'rm -rf "$ETC_DIR"' EXIT
printf 'root:x:0:0:root:/root:/bin/bash\nadk:x:%s:%s:adk:/tmp:/bin/bash\n' \
    "$(id -u)" "$(id -g)" > "$ETC_DIR/passwd"
printf 'root:x:0:\nadk:x:%s:\n' "$(id -g)" > "$ETC_DIR/group"

ARGS=(
    --rm
    --memory "$ADK_MEM_LIMIT"
    --memory-swap "$ADK_MEM_LIMIT"
    --cpus "$ADK_CPUS"
    "${NETWORK_ARGS[@]}"
    --user "$(id -u):$(id -g)"
    -e HOME=/tmp
    -e "ADK_TOOLS_IMAGE=$IMAGE"
    -v "$ETC_DIR/passwd:/etc/passwd:ro"
    -v "$ETC_DIR/group:/etc/group:ro"
    -v "$ADK_WORK:/work"
    -w "$WORKDIR"
)
# Outside --batch, stdin reaches the container, so `./run.sh bash < script.sh`
# runs the script; without -i docker hands the command an empty stdin. With a
# TTY, docker writes the container's ID to the cidfile, so that a hangup can
# kill the container (at the end).
if [ "$STDIN" = caller ]; then
    ARGS+=(-i)
    [ -z "$TTY" ] || ARGS+=(-t)
fi
[ -z "$TTY" ] || ARGS=(--cidfile "$ETC_DIR/cid" "${ARGS[@]}")
# Under hostslot the container carries its slot's label, so hostslot can kill
# it when the slot ends: a killed docker client leaves its container running.
[ -z "${HOSTSLOT_ID:-}" ] || ARGS+=(--label "org.ihp.hostslot=$HOSTSLOT_ID")
# The caller's variables the container sees, by name, each only when set:
# unset, chiplet-flow keeps the shared-host floor and an entry naming
# ${CHIPLET_FLOW_DIE_PROJECT} gets no project, and nothing here gives either a
# default. A name joins this list only when a run needs it from the host.
for forward in CHIPLET_FLOW_HOST CHIPLET_FLOW_DIE_PROJECT; do
    [ -z "${!forward+x}" ] || ARGS+=(-e "$forward=${!forward}")
done
ARGS+=("${OPENROAD_ARGS[@]}")
ARGS+=("${STORE_ARGS[@]}")
ARGS+=("${EXAMPLES_ARGS[@]}")
ARGS+=("${OVERLAY_ARGS[@]}")

# X11 (GUI tools need it, CLI does not). Only the display's own socket goes in,
# read-only (connecting to a socket does not write the mount). The cookie is
# the first entry for :N found in XAUTHORITY, the ThinLinc session's own
# Xauthority, then ~/.Xauthority, which can hold stale cookies of other
# displays. It is rewritten as a wildcard-host entry (family ffff) into the
# private dir: the file's entries are bound to the host name, which the
# container does not share. With no entry found the display is still passed,
# for a server that admits the caller's uid on its socket (SI:localuser). A host
# without xauth gets the first readable file mounted as it is.
if [ -n "$DISPLAY_N" ]; then
    ARGS+=(-e "DISPLAY=:$DISPLAY_N"
           -v "$SOCKET:/tmp/.X11-unix/X$DISPLAY_N:ro")
    COOKIE_FROM=""
    sources=()
    [ -n "${XAUTHORITY:-}" ] && sources+=("$XAUTHORITY")
    sources+=("$SESSIONS/$DISPLAY_N/Xauthority" "$HOME/.Xauthority")
    for src in "${sources[@]}"; do
        [ -f "$src" ] && [ -r "$src" ] || continue
        if command -v xauth >/dev/null 2>&1; then
            entries="$(xauth -q -f "$src" nlist ":$DISPLAY_N" 2>/dev/null || true)"
            [ -n "$entries" ] || continue
            if ! { (umask 077 && : > "$ETC_DIR/Xauthority") &&
                   printf '%s\n' "$entries" | sed 's/^..../ffff/' \
                   | xauth -q -f "$ETC_DIR/Xauthority" nmerge -; }; then
                echo "error: could not write the cookie for :$DISPLAY_N from $src into run.sh's private dir" >&2
                exit 1
            fi
            ARGS+=(-v "$ETC_DIR/Xauthority:/tmp/.Xauthority:ro")
        else
            ARGS+=(-v "$src:/tmp/.Xauthority:ro")
        fi
        ARGS+=(-e XAUTHORITY=/tmp/.Xauthority)
        COOKIE_FROM="$src"
        break
    done
    if [ -n "$COOKIE_FROM" ]; then
        DISPLAY_NOTE="display :$DISPLAY_N (from $DISPLAY_FROM), cookie from $COOKIE_FROM"
    else
        DISPLAY_NOTE="display :$DISPLAY_N (from $DISPLAY_FROM), no cookie for :$DISPLAY_N found; the server has to admit your uid"
    fi
fi

# The first three notes are fixed (docs/RUN_SH.md): a launcher reads them.
note "/work is $ADK_WORK"
note "image $IMAGE, as ADK_TOOLS_IMAGE in the container"
note "$DISPLAY_NOTE"
note "memory cap $ADK_MEM_LIMIT (swap off), $ADK_CPUS CPUs (ADK_MEM_LIMIT, ADK_CPUS)"
if [ -n "$CLOSURE_INSIDE" ]; then
    note "OpenROAD ${INSIDE_ENV[OPENROAD]} (the image's own, in its ENV)"
elif [ -n "$OPENROAD_PATH" ]; then
    note "OpenROAD $OPENROAD_PATH (from $OPENROAD_FROM)"
else
    note "no OpenROAD: route stages will refuse (set ADK_OPENROAD)"
fi
for n in "${STORE_NOTES[@]}"; do note "$n"; done
[ -z "${ADK_NETWORK:-}" ] || note "no network (ADK_NETWORK=none)"
[ -z "$LOCK_NOTE" ] || note "$LOCK_NOTE"
[ -z "$REFERENCE_NOTE" ] || note "$REFERENCE_NOTE"
for n in "${OVERLAY_LINES[@]}"; do note "$n"; done

# Not exec'd: the passwd/group and cookie bind-mounts must stay live for the
# container's lifetime, so the EXIT trap can only clean up the private etc dir
# once docker run returns. The container's exit code is run.sh's, or 129, 130
# or 143 when a hangup, an interrupt or a TERM ended a TTY run (below).
if [ -n "$DRY_RUN" ]; then
    # One line, quoted so a shell reads it back as the same argv. The passwd
    # and group files it names are removed when run.sh exits.
    printf '%q ' docker run "${ARGS[@]}" "$IMAGE" "$@" | sed 's/ $//'
    echo
    exit 0
fi

rc=0
if [ -n "$TTY" ]; then
    # A closed terminal kills the docker client, and with a TTY the container
    # stays up with --rm, waiting on it. So docker run runs in the background,
    # where a signal to run.sh is handled at once rather than after it, with the
    # terminal kept as its stdin by <&0. A HUP, INT or TERM kills the client
    # first, so it cannot start the container once this has exited, then the
    # container by its cidfile, and waits for the client. A killed client
    # leaves the terminal raw, so its settings from before go back (after a
    # hangup there is no terminal, and stty fails quietly).
    CLIENT=""
    TERMINAL="$(stty -g 2> /dev/null || true)"
    end_tty_run() {
        [ -z "$CLIENT" ] || kill -KILL "$CLIENT" 2> /dev/null || true
        [ -z "$TERMINAL" ] || stty "$TERMINAL" 2> /dev/null || true
        docker kill "$(cat "$ETC_DIR/cid" 2> /dev/null)" > /dev/null 2>&1 || true
        [ -z "$CLIENT" ] || wait "$CLIENT" 2> /dev/null || true
        exit "$1"
    }
    trap 'end_tty_run 129' HUP
    trap 'end_tty_run 130' INT
    trap 'end_tty_run 143' TERM
    docker run "${ARGS[@]}" "$IMAGE" "$@" <&0 &
    CLIENT=$!
    wait "$CLIENT" || rc=$?
    trap - HUP INT TERM
elif [ "$STDIN" = none ]; then
    docker run "${ARGS[@]}" "$IMAGE" "$@" < /dev/null || rc=$?
else
    docker run "${ARGS[@]}" "$IMAGE" "$@" || rc=$?
fi
if [ "$rc" -eq 137 ]; then
    note "exit 137: the container was killed (SIGKILL); if the memory cap ($ADK_MEM_LIMIT) did it, raise ADK_MEM_LIMIT"
fi
exit "$rc"
