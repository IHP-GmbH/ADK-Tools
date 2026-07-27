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
