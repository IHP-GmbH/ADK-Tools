# The build variant

The flow's package chain (route, package, carrier and assembly DRC, assembly LVS) takes dies whose
views already exist. The build variant starts one step earlier: it builds the dies from RTL with
LibreLane, then hands their views to the same chain. LibreLane, its interpreter and the sg13g2 PDK
are Nix store paths. On the host the flake's `build` shell sets them. An image with the
`org.ihp.adk-tools.nix-closure-inside` label carries the same paths in its own `/nix/store`
(`docs/RUN_SH.md`, "When the image carries its tools"); for an image without it, `run.sh` passes
the host's paths into the container.

This page says how that runtime is pinned and how a die build states what it ran with. What the
build shell holds, package by package, is in `docs/NIX.md`, "The build shell".

## What has been shown

Both example cores have been built from RTL to die views in the build shell. That run stops by
design at the KiCad step, so the package chain has not yet run on dies built this way. No die built
here is qualified: the results have not been checked by hand, and the timing models the cores
write are not qualified. Nothing on this page is a reproducibility claim.

## Pins: one authority each

The flake reads its revisions from the places that already choose them, and its evaluation refuses
a lock that disagrees on LibreLane's revision or the PDK's parent. What evaluation cannot see needs
git or a build, and `nix/check-build-pins.sh` refuses any difference there.

- LibreLane: `staging.pin` (`LIBRELANE_REV`, the ordered `LIBRELANE_PATCHES`, and
  `LIBRELANE_PATCHED_REV`). With `--realize` the check compares the patched source file by file
  with the tree `git apply` makes from `LIBRELANE_REV` and those patches. It compares trees, not
  commits, because the Nix-patched source has no commit of its own; where no clone holds
  `LIBRELANE_PATCHED_REV`, the check's last line names it UNVERIFIED.
- The sg13g2 PDK: the Dockerfile's `ARG SG13G2_REF`, and each copied submodule at the parent's
  gitlink at that commit.
- The store paths: `staging.pin`'s `NIX_OPENROAD_OUTPATH`, `NIX_LIBRELANE_OUTPATH`,
  `NIX_LIBRELANE_PYTHON_OUTPATH` and `NIX_SG13G2_PDK_OUTPATH` are derived, not chosen.
  `nix/check-openroad-pin.sh` holds each to the flake's evaluation, so a lock move that changes
  one of them fails the check rather than a die build hours later. `build.sh` writes all four
  into the image's labels (the interpreter's label names the `bin/python3.<minor>` under
  `NIX_LIBRELANE_PYTHON_OUTPATH`), which is how `run.sh` finds them on the host for an image
  that does not carry them.

Moving any of these is a release decision, made in its authority, never in the flake alone.

## The launcher

chiplet-flow runs LibreLane through a launcher it is given by absolute path with its sha256, and
refuses a symlink, a non-executable or a changed digest. The flow's Python packages live in the
`cli` shell's venv, which the store LibreLane's own interpreter does not see, so
`nix/build-env.sh` writes a short launcher outside the store. It runs the store package's
`bin/librelane` and adds the flow's checked source roots by `PYTHONPATH`, so LibreLane finds its
chiplet plugin; it drops the caller's Python variables and runs under `bash -p`. Every path it
adds or runs is written in its text, so its sha256 binds them, and the flow records path and
sha256 for every die build.

Preparing the die cores is the one part that does not go through the launcher: it takes the
upstream `$ADK_NIX_LIBRELANE/bin/librelane` and the interpreter `ADK_NIX_LIBRELANE_PYTHON`, whose
site-packages resolve to that same LibreLane package.

## The build context record

In evidence mode, entering the build shell writes `build-context.json` beside the `cli` shell's
`nix-context.json`, and refuses the shell when what it finds disagrees with what the shell
declared: the launcher, LibreLane and its patch list, the PDK tree's own statement (below) and the
cores' interpreter. It also runs the launcher once to see which files it loads at start. The rules
are in `nix/capture_build_context.py`. The record covers what the launcher imports when it starts,
not what a build later executes.

The flow's run record names the launcher by path and sha256, and the build context record says
what that launcher runs. The two join on the sha256.

## Store-PDK identity

The die emitter states the PDK's version with `git rev-parse HEAD` under `PDK_ROOT`. A store tree
has no `.git`, and none is invented for it. Instead the tree states its own sources in
`pdk-source.json` at its root: the store path it was materialized at, the parent repository and
each copied submodule by revision and narHash, and the submodules not fetched. The format and
the evaluation's refusals are written once, in `nix/pdk-source.nix`; `nix/test-pdk-source.sh`
checks them on stub inputs, without a fetch.

The statement describes the tree it sits in and authenticates nothing, the same class of assurance
as a git HEAD. The build context record digests the file, and chiplet-flow's build record names
it. No variable carries the PDK's identity.

## Resources

A die build is the heaviest step in the flow, and the flow has no memory cap or estimate for it.
Run die builds one at a time, under a memory watchdog, with the thread cap set explicitly
(`CHIPLET_FLOW_THREAD_CAP`).
