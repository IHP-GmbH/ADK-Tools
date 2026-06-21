#!/bin/bash
# Push the current adk-tools:dev image to ghcr and prune old versions.
#
# Usage: ./release.sh [CALENDAR_TAG]
#
#   CALENDAR_TAG   optional extra tag (e.g. 2026.06). `latest` always moves.
#
# Storage policy: the registry keeps exactly ONE image version (tags are
# free pointers onto the same digest; old untagged digests are deleted --
# the org plan has a small private-package quota). Requires `gh` auth with
# write:packages + delete:packages.
set -euo pipefail

IMAGE=ghcr.io/ihp-gmbh/adk-tools
CAL_TAG="${1:-}"

gh auth token | docker login ghcr.io -u "$(gh api user --jq .login)" --password-stdin
# Guarantee the ghcr credential is dropped from ~/.docker/config.json on every
# exit path, including a failed push (set -e would otherwise skip the logout and
# leave the token persisted on a shared host).
trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT

docker tag adk-tools:dev "$IMAGE:latest"
docker push "$IMAGE:latest"
if [ -n "$CAL_TAG" ]; then
    docker tag adk-tools:dev "$IMAGE:$CAL_TAG"
    docker push "$IMAGE:$CAL_TAG"
fi

# Prune untagged versions (superseded digests). Capture the list in a variable
# first: a command substitution consumed directly by `for` has its exit status
# ignored, so a failed `gh api` (lost auth, transient 5xx, rate limit) would
# silently iterate nothing and let superseded digests pile up against the small
# package quota. A bare assignment IS checked by set -e, so this aborts loudly.
untagged="$(gh api orgs/IHP-GmbH/packages/container/adk-tools/versions \
        --jq '.[] | select(.metadata.container.tags | length == 0) | .id')"
for id in $untagged; do
    echo "pruning untagged version $id"
    gh api -X DELETE "/orgs/IHP-GmbH/packages/container/adk-tools/versions/$id"
done

echo "Done:"
gh api orgs/IHP-GmbH/packages/container/adk-tools/versions \
    --jq '.[] | {tags: .metadata.container.tags, updated: .updated_at}'
