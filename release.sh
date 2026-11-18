#!/bin/bash
# Push one release image to ghcr as a private package, under its immutable version tag.
#
# Usage: ./release.sh vYYYY.MM[.N] <image ID>
#
#   vYYYY.MM[.N]  REQUIRED immutable version tag: a release (v2026.11) or a point
#                 release of it (v2026.11.2), ^v[0-9]{4}\.[0-9]{2}(\.[1-9][0-9]*)?$.
#                 Pushed as ghcr.io/ihp-gmbh/adk-tools:<tag> and as nothing else:
#                 no `latest`. The tag must not already be published; released
#                 digests are immutable.
#   <image ID>    sha256:<64 hex>, the ID of the image the release was proven on
#                 (the ID the adk-examples release lock names); a tag is refused,
#                 since the daemon is shared and a tag can be moved.
#
# Provenance gate: the image is pushed only from a clean checkout whose HEAD is
# exactly at the tag, with every submodule at its pinned commit, and only if the
# image's baked manifest `meta` is exactly the tag (build.sh sets it from
# `git describe --tags`, so an image built from a dirty or untagged tree fails).
#
# Private only. The package's visibility is read before the push (absent, on the
# first release, or `private`) and again after it, and an anonymous read of the
# pushed tag must be refused. Anything else stops the script with exit 3 and says
# what it read; the package is then left for a person to look at, and nothing
# here changes a visibility. The Dockerfile's org.opencontainers.image.source
# label links the package to the ADK-Tools repository, so the package takes its
# access from that repository's roles; access for anyone else is granted on the
# package by hand.
#
# Requires `gh` auth with write:packages. Nothing is ever deleted.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=ghcr.io/ihp-gmbh/adk-tools
PACKAGE_API=orgs/IHP-GmbH/packages/container/adk-tools
USAGE="usage: ./release.sh vYYYY.MM[.N] sha256:<64 hex>"
VERSION_TAG="${1:?$USAGE  (an immutable version tag is required)}"
[[ "$VERSION_TAG" =~ ^v[0-9]{4}\.[0-9]{2}(\.[1-9][0-9]*)?$ ]] || {
    echo "error: VERSION_TAG must look like vYYYY.MM or vYYYY.MM.N (got '$VERSION_TAG')" >&2
    exit 1
}
IMAGE_ID="${2:-}"
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "error: the image must be given by its ID, sha256:<64 hex> (got '$IMAGE_ID')" >&2
    echo "$USAGE" >&2
    exit 1
}

# --- Provenance gate: only publish a clean image built from the tagged commit ---
[ -z "$(git status --porcelain)" ] || {
    echo "error: working tree is dirty; commit before releasing" >&2
    exit 1
}
if git submodule status --recursive | grep -qE '^[+-U]'; then
    echo "error: submodules are dirty or not at their pinned commit:" >&2
    git submodule status --recursive | grep -E '^[+-U]' >&2
    exit 1
fi
git describe --exact-match --tags --exclude '*/*' HEAD 2>/dev/null | grep -qxF "$VERSION_TAG" || {
    echo "error: HEAD is not exactly at tag $VERSION_TAG; tag the release commit first" >&2
    exit 1
}
got_id="$(docker image inspect --format '{{.Id}}' "$IMAGE_ID")"
[ "$got_id" = "$IMAGE_ID" ] || {
    echo "error: the daemon has no image $IMAGE_ID (it answered '$got_id')" >&2
    exit 1
}
built_meta="$(docker run --rm "$IMAGE_ID" cat /opt/adk-tools/manifest.json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["meta"])')"
[ "$built_meta" = "$VERSION_TAG" ] || {
    echo "error: $IMAGE_ID was built from '$built_meta', not the clean tag '$VERSION_TAG';" >&2
    echo "       rebuild from the tagged commit with ./build.sh" >&2
    exit 1
}

# The package's visibility: `absent` on a 404, else the API's word. Any other
# failure of the call stops the script, so a lost token never reads as absent.
visibility() {
    local out
    if out="$(gh api "$PACKAGE_API" --jq .visibility 2>&1)"; then
        printf '%s\n' "$out"
    elif grep -q 'HTTP 404' <<<"$out"; then
        echo absent
    else
        echo "error: cannot read the package's visibility: $out" >&2
        exit 3
    fi
}

before="$(visibility)"
case "$before" in
    absent|private) echo "package visibility before the push: $before" ;;
    *) echo "error: the package is '$before', not private; nothing pushed" >&2; exit 3 ;;
esac

# Immutability: never overwrite an already-published version tag.
if [ "$before" = private ]; then
    tags="$(gh api --paginate "$PACKAGE_API/versions" --jq '.[].metadata.container.tags[]')"
    if grep -qxF "$VERSION_TAG" <<<"$tags"; then
        echo "error: $VERSION_TAG is already published and digests are immutable; cut a new version" >&2
        exit 1
    fi
fi

gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin
# Drop the ghcr credential from ~/.docker/config.json and the push tag from the
# shared daemon on every exit path, including a failed push.
trap 'docker logout ghcr.io >/dev/null 2>&1 || true; docker rmi "$IMAGE:$VERSION_TAG" >/dev/null 2>&1 || true' EXIT

docker tag "$IMAGE_ID" "$IMAGE:$VERSION_TAG"
docker push "$IMAGE:$VERSION_TAG"
digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE_ID" \
    | grep -m1 "^$IMAGE@" || true)"

after="$(visibility)"
[ "$after" = private ] || {
    echo "error: after the push the package is '$after', not private; look at it now" >&2
    exit 3
}
# An anonymous read of the tag must be refused: a fresh docker config holds no login.
anon_cfg="$(mktemp -d)"
if anon="$(DOCKER_CONFIG="$anon_cfg" docker manifest inspect "$IMAGE:$VERSION_TAG" 2>&1)"; then
    rm -rf "$anon_cfg"
    echo "error: $IMAGE:$VERSION_TAG reads without a login; look at the package now" >&2
    exit 3
fi
rm -rf "$anon_cfg"
grep -qiE 'unauthorized|denied' <<<"$anon" || {
    echo "error: the anonymous read failed, but not as refused: $anon" >&2
    exit 3
}

echo "Done: $IMAGE:$VERSION_TAG, image $IMAGE_ID, ${digest:-repository digest not reported}"
echo "package visibility after the push: $after; anonymous read refused"
