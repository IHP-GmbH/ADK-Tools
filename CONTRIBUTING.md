# Contributing to adk-tools

`adk-tools` is a **version lockfile plus a build recipe**: the tools live in
their own repositories and are pinned here as git submodules. One commit of this
repo pins one exact, verified combination of every tool.

## Branching model

- **`main`** -- stable. Every commit is release-quality and passed the verify
  gate. Release tags (`vYYYY.MM`) live here. Do not push tool bumps here
  directly.
- **`dev`** -- integration. Submodule bumps and new tools land here first and get
  verified, then promote to `main` at a release.
- **`feature/*`** -- short-lived branches cut from `dev`, merged back into `dev`.

## What enforces the model

A ruleset covers `main` and `dev`, so the section above is a condition on
merging rather than a convention. Both lines take changes only through a pull
request, neither can be deleted or force-pushed, and two status checks are
required to merge. Release tags (`v*`) are protected against deletion and
against being moved onto a different commit.

**`ci-gate`** collects the jobs in `.github/workflows/ci.yml`, which check that
every submodule pin is publicly reachable on the branch it declares, that every
path the image build reaches for exists in the pinned tree, and that the tracked
ruleset payloads are well formed.

**`integration`** collects `.github/workflows/integration.yml`, the cross-repo
run. It checks out the pinned combination and asserts the facts that span two
repositories and that therefore no per-repo gate can see: the hand-synced
schemas and both vendored `.chiplet` readers still byte-identical to
chiplet-spec, every pin naming this superproject's track, every repository
publishing a `ci-gate` that is a real gate, one KLayout across the ecosystem,
and every pinned `*_PIP` satisfying what the repositories declare. Its refs come
from the gitlinks and from `ci/integration-refs.json`, so it goes red because
something changed here, never because somebody pushed elsewhere while a pull
request was open. The run that follows the branch heads is
`integration-floating.yml`, weekly, which never blocks anything and reports
through one issue.

Both compile nothing and both finish in about a minute.

The payloads for every repository in the ecosystem live in `ci/rulesets/` and
are applied with `ci/rulesets/apply_rulesets.py`, never through the GitHub web
UI. `ci/rulesets/README.md` gives the reason, which is sharper than a style
preference. Repository admins hold a permanent bypass, so the ruleset is binding
for contributors and advisory for a maintainer who needs a force push on the day
a history rewrite is genuinely required.

## The verify gate

`build.sh` always builds the `verify` stage first (regenerates the demo
headless and runs every tool's test suite inside the image). A combination that
does not pass verify must never land on `main`. `--skip-verify` is for local
iteration only.

## Where to look

- **`docs/MAINTAINING.md`** -- how to bump a tool, add a new tool as a submodule,
  remove one, and cut a release. Read it before touching submodules or the
  `Dockerfile`.
- **`docs/RELEASE_CHECKLIST.md`** -- the ordered release checklist, and the
  record of the one-time work done for the first public release.
- **`CHANGELOG.md`** -- release history.
- **`NOTICE.md`** -- per-component licenses and the GPL corresponding-source
  obligation. Update it whenever you add or change a bundled component.

## Commit conventions

- English, concise, imperative. Match the existing history, e.g.
  `Bump <tool> pin to <ref> (<summary>)`.
- One logical change per commit; keep each tool bump in its own commit so
  regressions bisect cleanly.
- Do not hand-edit `manifest.json`; `build.sh` generates it (and it is
  gitignored).
