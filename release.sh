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

docker tag adk-tools:dev "$IMAGE:latest"
docker push "$IMAGE:latest"
if [ -n "$CAL_TAG" ]; then
    docker tag adk-tools:dev "$IMAGE:$CAL_TAG"
    docker push "$IMAGE:$CAL_TAG"
fi

docker logout ghcr.io

# Prune untagged versions (superseded digests).
for id in $(gh api orgs/IHP-GmbH/packages/container/adk-tools/versions \
        --jq '.[] | select(.metadata.container.tags | length == 0) | .id'); do
    echo "pruning untagged version $id"
    gh api -X DELETE "/orgs/IHP-GmbH/packages/container/adk-tools/versions/$id"
done

echo "Done:"
gh api orgs/IHP-GmbH/packages/container/adk-tools/versions \
    --jq '.[] | {tags: .metadata.container.tags, updated: .updated_at}'
