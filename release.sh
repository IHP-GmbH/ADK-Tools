#!/bin/bash
# Publish the current adk-tools:dev image to ghcr under an immutable version tag.
#
# Usage: [PRUNE_UNTAGGED=1] ./release.sh vYYYY.MM
#
#   vYYYY.MM   REQUIRED immutable version tag (must match ^v[0-9]{4}\.[0-9]{2}$).
#              Pushed as ghcr.io/ihp-gmbh/adk-tools:vYYYY.MM; `latest` also moves
#              onto the same digest. The tag must not already be published --
#              released digests are immutable.
#
# Provenance gate: the image is published only if it was built from a clean,
# tagged commit. This refuses a dirty working tree, dirty/unpinned submodules, a
# HEAD that is not exactly at vYYYY.MM, or an image whose baked manifest `meta`
# is not exactly vYYYY.MM (i.e. built from a dirty/untagged tree). Rebuild from
# the tagged commit with ./build.sh if any check fails.
#
# Storage: published digests are immutable and are NEVER pruned by default. The
# org plan has a small private-package quota; to reclaim it, run a deliberate
# `PRUNE_UNTAGGED=1 ./release.sh vYYYY.MM`, which additionally deletes untagged
# (superseded) digests. Never prune on the public-release path.
#
# Requires `gh` auth with write:packages (+ delete:packages only for pruning).
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=ghcr.io/ihp-gmbh/adk-tools
VERSION_TAG="${1:?usage: [PRUNE_UNTAGGED=1] ./release.sh vYYYY.MM  (an immutable version tag is required)}"
[[ "$VERSION_TAG" =~ ^v[0-9]{4}\.[0-9]{2}$ ]] || {
    echo "error: VERSION_TAG must look like vYYYY.MM (got '$VERSION_TAG')" >&2
    exit 1
}

# --- Provenance gate: only publish a clean image built from the tagged commit ---
[ -z "$(git status --porcelain)" ] || {
    echo "error: working tree is dirty; commit or stash before releasing" >&2
    exit 1
}
if git submodule status --recursive | grep -qE '^[+-U]'; then
    echo "error: submodules are dirty or not at their pinned commit:" >&2
    git submodule status --recursive | grep -E '^[+-U]' >&2
    exit 1
fi
git describe --exact-match --tags HEAD 2>/dev/null | grep -qx "$VERSION_TAG" || {
    echo "error: HEAD is not exactly at tag $VERSION_TAG; tag the release commit first" >&2
    exit 1
}
# Definitive check: cross-verify the baked image against the tag. build.sh sets
# manifest `meta` from `git describe --tags --always --dirty`, so a clean tagged
# build yields exactly vYYYY.MM; any -dirty / -N-gHASH suffix fails here.
built_meta="$(docker run --rm adk-tools:dev \
        cat /opt/adk-tools/manifest.json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["meta"])')"
[ "$built_meta" = "$VERSION_TAG" ] || {
    echo "error: adk-tools:dev was built from '$built_meta', not the clean tag '$VERSION_TAG';" >&2
    echo "       rebuild from the tagged commit with ./build.sh" >&2
    exit 1
}

gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin
# Guarantee the ghcr credential is dropped from ~/.docker/config.json on every
# exit path, including a failed push (set -e would otherwise skip the logout and
# leave the token persisted on a shared host).
trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT

# Immutability: never overwrite an already-published version tag. On the very
# first release the package does not exist yet -- the `gh api` 404 makes the
# pipeline non-zero, the `if` is false, and we correctly proceed to publish.
if gh api orgs/IHP-GmbH/packages/container/adk-tools/versions \
        --jq '.[].metadata.container.tags[]' 2>/dev/null | grep -qx "$VERSION_TAG"; then
    echo "error: $VERSION_TAG is already published and digests are immutable; cut a new version" >&2
    exit 1
fi

docker tag adk-tools:dev "$IMAGE:$VERSION_TAG"
docker push "$IMAGE:$VERSION_TAG"
docker tag adk-tools:dev "$IMAGE:latest"
docker push "$IMAGE:latest"

# Pruning is OFF by default so a published digest is never deleted. Opt in only
# to reclaim the private-package quota, never on the public-release path. Capture
# the list first: a command substitution consumed directly by `for` has its exit
# status ignored, so a failed `gh api` (lost auth, transient 5xx, rate limit)
# would silently iterate nothing; a bare assignment IS checked by set -e, so it
# aborts loudly.
if [ "${PRUNE_UNTAGGED:-0}" = 1 ]; then
    untagged="$(gh api orgs/IHP-GmbH/packages/container/adk-tools/versions \
            --jq '.[] | select(.metadata.container.tags | length == 0) | .id')"
    for id in $untagged; do
        echo "pruning untagged version $id"
        gh api -X DELETE "/orgs/IHP-GmbH/packages/container/adk-tools/versions/$id"
    done
fi

echo "Done:"
gh api orgs/IHP-GmbH/packages/container/adk-tools/versions \
    --jq '.[] | {tags: .metadata.container.tags, updated: .updated_at}'
