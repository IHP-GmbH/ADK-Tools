<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 IHP GmbH
-->

# Rulesets

One JSON file per ruleset, applied by `apply_rulesets.py`. Twelve files across
the ten repositories of the ecosystem. This directory is the source of truth:
if a ruleset on GitHub does not match what is here, that is drift, and
`apply_rulesets.py --check` says so and exits non-zero.

Filenames are `<repository>[.<qualifier>].json`. The repository is everything up
to the first dot, so one repository can carry more than one ruleset. The owner
is `IHP-GmbH` for all ten.

## Why not the web UI

The UI is convenient and it is also how this gets quietly broken. When a
required status check is picked from its dropdown, the UI records the app that
last posted that check as `integration_id` on the context. A context pinned to
an app can only ever be satisfied by that app, so a status posted with a user
token no longer counts. That is precisely how the local heavy gate reports its
verdict, since a self-hosted runner is not an option on public repositories.

Nothing in this directory sets `integration_id`, and `apply_rulesets.py` fails
if a read-back shows one. The failure it prevents is invisible in the UI and
permanent.

## What is required, and what is deliberately not

`ci-gate` is required in every repository. It is one collector job per repo that
depends on every gating job in that repo and compares their results explicitly,
so the ruleset never has to be edited when a workflow grows or loses a job.

ADK-Tools requires a second context, `integration`, the cross-repo run in
`.github/workflows/integration.yml`. It is only here because this is the only
repository where more than one repository exists at once.

One context named in the original plan is still absent on purpose: `local/verify`,
the verdict the local heavy gate posts, which is not built yet. Requiring a
context that nothing publishes does not make a repository stricter, it makes
every pull request unmergeable forever, so it lands in this JSON in the same
change that makes its producer real and not before. That is not a hypothetical:
adding `ci-gate` blocked a colleague's open pull request whose branch predated
the CI, because the merge ref carried no workflow and the context could never
appear on it.

## Settled parameters

`required_approving_review_count` is 0. GitHub forbids approving your own pull
request, so on a project with one maintainer any higher number leaves two
outcomes, permanent bypass or nothing ever merging, and both teach the reflex
that gets rulesets deleted. Zero still buys the pull request path, which is the
only thing that makes a required status check bind at all. It is one field to
change when a second maintainer joins.

Bypass is repository admin, `always`. This was verified rather than assumed:
with `{"actor_id": 5, "actor_type": "RepositoryRole"}` in place, a direct push
to a branch carrying a `pull_request` rule reported `Bypassed rule violations`
and succeeded; with the bypass list emptied, the same push was refused with
`GH013`. So the number is right and the rule genuinely binds for everyone else.

Not enabled anywhere: `required_linear_history`, because it forbids the reviewed
merge that the release fold uses, and `required_signatures`, because no signing
key is configured and it would be a day-one lockout.

## Two exceptions

`KiCad-ADK-MOD` carries two rulesets rather than one. Its `main` is a fork line,
and re-syncing a fork against upstream KiCad legitimately rewrites history, so
`main` gets `deletion` and nothing else until `FORK_NOTES.md` states how that
sync happens. `dev`, where all the fork's own work lands, gets the full set.

`ADK-Tools` carries a tag ruleset for `refs/tags/v*` alongside its branch
ruleset. A release tag is what users pin, so moving or deleting one changes what
a published version means after the fact.

## Applying

```bash
python3 ci/rulesets/apply_rulesets.py --self-test   # prove the checks can fail
python3 ci/rulesets/apply_rulesets.py --check       # read-only drift report
python3 ci/rulesets/apply_rulesets.py --apply
python3 ci/rulesets/apply_rulesets.py --apply --enforcement evaluate --repo OpenIntM4TM2
```

`--apply` creates a ruleset that does not exist and updates in place the one
whose `name` matches, so it is idempotent and safe to re-run. Every write is
read back and compared against the declared payload, because GitHub accepts a
payload containing a rule type it does not recognise and stores it without that
rule. An apply that reports success without reading back cannot tell a ruleset
from a typo.

`--enforcement` overrides the value in the JSON without editing it, which is for
the staged rollout only. The tracked value is what the repository should
converge to.

## Rolling back

In increasing severity: edit the JSON here and re-apply; apply with
`--enforcement disabled`; delete the ruleset through the API; or use the admin
bypass, which exists for the incident where none of the first three is fast
enough.
