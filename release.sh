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
#   <image ID>    sha256:<64 hex>, the release's reference image (the ID the
#                 adk-examples release lock names); a tag is refused, since the
#                 daemon is shared and a tag can be moved.
#
# Before anything is pushed: this checkout is the tagged release (clean, every
# submodule at its pinned commit, HEAD exactly at the tag), and the image's baked
# manifest `meta` is exactly the tag. build.sh sets `meta` from
# `git describe --tags --dirty`, so `meta` is what ties the image to a clean
# build of the tag; the checkout checks only make sure the tag is the one this
# repository holds.
#
# Private only. The package's visibility is read before the push (absent, on the
# first release, or `private`) and again after it, and an anonymous read of the
# pushed tag must be refused by the registry. Anything else stops the script
# with exit 3 and says what it read; nothing here changes a visibility. On the
# first release the push creates the package with the visibility the
# organization's package settings give it, so read those settings before it.
# The Dockerfile's org.opencontainers.image.source label links the package to
# the ADK-Tools repository, so the package takes its access from that
# repository's roles; access for anyone else is granted on the package by hand.
#
# Exit codes: 0 pushed and private; 1 refused before any push; 3 the package is
# not provably private, or its state could not be read. Nothing is ever deleted:
# the push tag is removed from the daemon at the end only when the image keeps
# another tag, so the image itself never goes.
#
# Requires `gh` auth with write:packages.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=ghcr.io/ihp-gmbh/adk-tools
PACKAGE_API=orgs/IHP-GmbH/packages/container/adk-tools
USAGE="usage: ./release.sh vYYYY.MM[.N] sha256:<64 hex>"

refuse() { echo "error: $*" >&2; exit 1; }
stop() { echo "error: $*" >&2; exit 3; }

VERSION_TAG="${1:-}"
[[ "$VERSION_TAG" =~ ^v[0-9]{4}\.[0-9]{2}(\.[1-9][0-9]*)?$ ]] \
    || refuse "VERSION_TAG must look like vYYYY.MM or vYYYY.MM.N (got '$VERSION_TAG'); $USAGE"
IMAGE_ID="${2:-}"
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || refuse "the image must be given by its ID, sha256:<64 hex> (got '$IMAGE_ID'); $USAGE"
[ $# -eq 2 ] || refuse "$USAGE"
PUSH_REF="$IMAGE:$VERSION_TAG"

# --- This checkout is the tagged release, and the image was built from it ---
[ -z "$(git status --porcelain)" ] || refuse "working tree is dirty; commit before releasing"
if git submodule status --recursive | grep -qE '^[+-U]'; then
    git submodule status --recursive | grep -E '^[+-U]' >&2
    refuse "submodules are dirty or not at their pinned commit"
fi
git describe --exact-match --tags --exclude '*/*' HEAD 2>/dev/null | grep -qxF "$VERSION_TAG" \
    || refuse "HEAD is not exactly at tag $VERSION_TAG; tag the release commit first"
got_id="$(docker image inspect --format '{{.Id}}' "$IMAGE_ID" 2>/dev/null)" \
    || refuse "the daemon has no image $IMAGE_ID"
[ "$got_id" = "$IMAGE_ID" ] || refuse "$IMAGE_ID resolves to $got_id on this daemon"
built_meta="$(docker run --rm "$IMAGE_ID" cat /opt/adk-tools/manifest.json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["meta"])')"
[ "$built_meta" = "$VERSION_TAG" ] \
    || refuse "$IMAGE_ID was built from '$built_meta', not the clean tag '$VERSION_TAG'; rebuild from the tagged commit with ./build.sh"
# The push tag is made here and only here, so it cannot be someone else's.
if docker image inspect "$PUSH_REF" >/dev/null 2>&1; then
    refuse "$PUSH_REF is already a tag on this daemon; it would be moved"
fi

# The package's visibility: `absent` on a 404, else the API's word. Any other
# failure of the call stops the script, so a lost token never reads as absent.
visibility() {
    local out err rc=0
    err="$(mktemp)"
    out="$(gh api "$PACKAGE_API" --jq .visibility 2>"$err")" || rc=$?
    if [ "$rc" -eq 0 ]; then
        printf '%s\n' "$out"
    elif grep -q 'HTTP 404' "$err"; then
        echo absent
    else
        echo "error: cannot read the package's visibility: $(cat "$err")" >&2
        rm -f "$err"
        exit 3
    fi
    rm -f "$err"
}

before="$(visibility)" || exit 3
case "$before" in
    absent|private) echo "package visibility before the push: $before" ;;
    *) stop "the package is '$before', not private; nothing pushed" ;;
esac

# Immutability: never overwrite an already-published version tag.
if [ "$before" = private ]; then
    tags="$(gh api --paginate "$PACKAGE_API/versions" --jq '.[].metadata.container.tags[]')" \
        || stop "cannot read the package's published tags"
    if grep -qxF "$VERSION_TAG" <<<"$tags"; then
        refuse "$VERSION_TAG is already published and digests are immutable; cut a new version"
    fi
fi

work="$(mktemp -d)"
tagged=0
cleanup() {
    docker logout ghcr.io >/dev/null 2>&1 || true
    if [ "$tagged" = 1 ]; then
        others="$(docker image inspect --format '{{len .RepoTags}}' "$IMAGE_ID" 2>/dev/null || echo 0)"
        if [ "$others" -gt 1 ]; then
            docker rmi "$PUSH_REF" >/dev/null 2>&1 || true
        else
            echo "note: $PUSH_REF stays on this daemon; it is the image's only tag" >&2
        fi
    fi
    rm -rf "$work"
}
trap cleanup EXIT

gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin
docker tag "$IMAGE_ID" "$PUSH_REF"
tagged=1
docker push "$PUSH_REF" | tee "$work/push.out"
digest="$(sed -n 's/.*digest: \(sha256:[0-9a-f]\{64\}\).*/\1/p' "$work/push.out" | tail -n 1)"
[ -n "$digest" ] || stop "the push printed no digest; look at the package now"
docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE_ID" \
    | grep -qxF "$IMAGE@$digest" || stop "the pushed digest $digest is not one of $IMAGE_ID's"

after="$(visibility)" || exit 3
[ "$after" = private ] || stop "after the push the package is '$after', not private; look at it now"
# An anonymous read of the tag must be refused: a fresh docker config holds no login.
mkdir "$work/anon"
if anon="$(DOCKER_CONFIG="$work/anon" docker manifest inspect "$PUSH_REF" 2>&1)"; then
    stop "$PUSH_REF reads without a login; look at the package now"
fi
# The registry's refusal reaches docker in one of three wordings; anything else
# (network, proxy, TLS) is a failure to look into, not a refusal.
refusals=(
    'unauthorized: authentication required'
    'denied: requested access to the resource is denied'
    '^Get "https://ghcr\.io/v2/[^"]*": unauthorized$'
)
grep -qE "$(IFS='|'; echo "${refusals[*]}")" <<<"$anon" \
    || stop "the anonymous read failed, but not as the registry's refusal: $anon"

echo "Done: $IMAGE@$digest ($PUSH_REF), image $IMAGE_ID"
echo "package visibility after the push: $after; anonymous read refused"
