# The Nix CLI shell

`devShells.cli` in `flake.nix` is a host environment for packaging-flow's command line, from
`run` up to the signoff gate, on a spec whose dies are ready (no die build). It exists so a
Python edit to one of the flow's packages costs nothing to try: no image rebuild, no Nix build,
only the edit. The Docker image is unchanged by it. The image is still built from its Dockerfile,
not from Nix, and stays the release reference; the Nix shells are the development path. The
direction is Nix as the single toolchain definition and the image as its delivery format, with
chip designers never seeing Nix; that has not happened yet. Several of the repositories these
shells install from checkouts are pinned at commits no remote serves yet (`ci/pin-set.json` marks
them `"reachable": "local-only"`), so the shells cannot be entered from public sources alone. For now the CLI shell refuses on entry:
its tree table still names a chiplet-system tree, which `staging.pin` no longer stages, and it
works again once that table and the build-context capture follow the rename.

## What it holds

From Nix, at the revisions `flake.lock` names: the Python interpreter, the third-party Python the
flow imports (PyYAML, jsonschema and its dependencies, KLayout's `klayout.db`, z3 for assembly-lvs,
plus setuptools and pip for the install below), the `klayout` binary, git, coreutils, gawk, and
OpenROAD as LibreLane packages it (the `librelane` input is the commit `LIBRELANE_REV` in
`staging.pin` names, and follows this flake's nix-eda). KLayout is nix-eda's, the same store path
as `packages.klayout`.

Not from Nix: packaging-flow, chiplet-flow, interposer-pnr, chiplet-spec (with its system
description), assembly-lvs and chiplet-cosim. They stay git checkouts, installed editable into a venv the shell hook makes, because
the flow records each package's identity with `git rev-parse` on the directory it imports from. A
copy in the store has no repository, the identity comes out `unavailable`, and
`packaging-flow verify` refuses the full tier. This is a transition. Building them in Nix needs a
change to how the flow records identity first, and that change belongs to the flow.

The venv is made from the bare interpreter, not from a `withPackages` environment, and reaches the
Nix packages through one `.pth` file. The flow starts its engine with `sys.executable` when nothing
else names one, and a `withPackages` wrapper would point that back at itself, without the flow's
packages. The install is `--no-index --no-deps --no-build-isolation`, followed by `pip check`:
every third-party package comes from Nix, and the check proves the declared requirements are met by
it. nixpkgs' z3 bindings carry no distribution metadata, so the flake adds a `z3_solver` dist-info
beside them, at the derivation's version, for the check to read. assembly-lvs is installed so that
lvs-verify finds its `assembly-lvs` command; the venv's `bin` is first on `PATH` and
`CHIPLET_FLOW_ASSEMBLY_LVS` is cleared. The venv lives under `ADK_NIX_STATE`, resolved to its real
path first. In development it is reinstalled only when the interpreter, the Nix environment, a
tree's path or a tree's packaging metadata changes.

The interpreter environment is staging.pin's `NIX_PYTHON_OUTPATH` and `COSIM_DEVSHELL` is its
`NIX_COSIM_DEVSHELL_OUTPATH`: the hook refuses a site-packages outside the one or a devshell other
than the other, and a git on `PATH` outside the store. Each of the three must be in the shell's
closure, read from its closureInfo's `store-paths`, with no store query. Every entry then writes the
tool identity table from that closureInfo (`nix/tool_identity.py lane`): a row per pin in the
closure, and git by its realpath and `--version`. It is exported as `ADK_TOOLS_TABLE`, with
`ADK_TOOLS_CLOSURE` set to `sha256:` and the table's closure digest; that pair is how packaging-flow
tells a run in this shell from one in the image. An evidence entry adds chiplet-cosim's row, its
console script and the staged checkout's commit and tracked digest. A development tree can hold any
edit, so a development entry leaves that row out, and what keys chiplet-cosim reads UNRESOLVED.

## What it is not

- Not the launcher. `nix develop` gives no network isolation, no CPU or memory cap, no separate
  user and no timeout. A run here is a development run, never equivalent to a run through the
  image's launcher, and never a source of goldens.
- Not the same tools as the image. OpenROAD here is dcf36133, the store path
  `NIX_OPENROAD_OUTPATH` in `staging.pin` records (checked by `nix/check-openroad-pin.sh`), and
  the image carries that same store path in its own `/nix/store` (`$OPENROAD`); `bin/adk-tools`
  reports whether `$OPENROAD` or PATH has one.
  interposer-pnr's tools manifest still names another build, the one its routes were confirmed on.
  Python is 3.13 and jsonschema 4.25, where the image has 3.12 and 4.26. The flow records these
  as tool identity, and `resume` refuses a change of tool identity, so a run never moves between
  the image and this shell. dcf36133 is a candidate, not a proven equivalent.
- Not the tail. gds, interposer-drc and drc need the KiCad fork's pcbnew, which this shell does
  not carry. A run here stops at GatePending, and reaching it is a functional check, not a claim
  that the run reproduces. `devShells.tail` ("The tail shell", below) is this shell plus the tail.
  Its `chiplet-export` runs under one interpreter, the Nix Python, with the fork's site-packages
  appended last to the import path by a plain path line in a `.pth` file. Appended last, the
  fork's modules cannot shadow anything else the export imports; a `PYTHONPATH` entry would put
  them ahead of everything else, and a second interpreter would be one more thing to read and
  keep. `nix-tail/test_capture_tail_context.py` checks the placement against those ablations, and
  the evidence capture refuses any other. Nothing in this repository compares the tail's export
  with the image's.
- The image's KLayout version, not its build. nix-eda 6.4.0 ships 0.30.6; `packages.klayout`
  overrides it to the Dockerfile's `KLAYOUT_PIP` (0.30.5), from the release tarball whose tree is
  the v0.30.5 commit Chiplet Studio's `extern/klayout` builds (dacb3230). The flake reads the
  image's version from the Dockerfile (`ADK_NIX_KLAYOUT_IMAGE_VERSION`) and stops evaluating when
  it is not this KLayout's, so a `KLAYOUT_PIP` bump moves the override in the same change; every
  evidence context still records both. The builds differ: the image runs PyPI's wheel as
  `klayout.db` and Studio's in-tree build as the `klayout` binary, this shell nix-eda's build of
  the same source for both. KLayout here serves the flow's start-of-run check for a
  `klayout.db` interpreter (a chain holding a terminal-map stage, or a conform or die-view stage
  without declared pad maps or observations); before the gate nothing asks KLayout to do more
  than import, so a run to the gate says nothing of KLayout's behaviour.
- Not the image's z3. The shell has nixpkgs' 4.15.4; the image pins `Z3_SOLVER_PIP` (4.15.8.0).
  Every evidence context records the library's version against its metadata, and the engine's
  report records the z3 it ran. A run here does
  not reach the assembly-lvs stage, which comes after gds; the engine runs here only through
  `lvs-verify` on a bundle made elsewhere, and then it does real `klayout.db` geometry work on
  KLayout 0.30.5 and solves with z3 4.15.4, where the image has 4.15.8.0.
- `devShells.default` is untouched: it is still the KLayout shell.

## Development entry

Name the source trees and the three roots; nothing is guessed. Trees can be named one at a time
(`ADK_NIX_SRC_CHIPLET_FLOW`, `ADK_NIX_SRC_PACKAGING_FLOW`, `ADK_NIX_SRC_CHIPLET_SYSTEM`,
`ADK_NIX_SRC_INTERPOSER_PNR`, `ADK_NIX_SRC_CHIPLET_SPEC`, `ADK_NIX_SRC_ASSEMBLY_LVS`,
`ADK_NIX_SRC_CHIPLET_COSIM`) or all at once with `ADK_NIX_SOURCES`, a directory that holds them
under their repository names. Every tree is required; a sources directory without one refuses and
names both ways to give it.

```sh
ADK_NIX_SOURCES=<dir with the seven checkouts> \
ADK_ROOT=<adk checkout> \
INTERPOSER_PDK_ROOT=<OpenIntM4TM2 checkout> \
INTERCONNECT_PDK_ROOT=<IHP-Interconnect-IntM4TM2 checkout> \
nix develop <adk-tools>#cli
```

The trees are used as they are, edits included; a run records `<commit>-dirty` for an edited tree,
as it would anywhere else.

KLayout sees nothing of the host's: `KLAYOUT_HOME` is a new empty directory for each entry (a host
`~/.klayout` can register a second `SG13_dev`, and `klayout -b` loads every technology in a home),
`KLAYOUT_PATH` is that home followed by `$INTERPOSER_PDK_ROOT/libs.tech/klayout`, the image's
shape, and `LD_LIBRARY_PATH`, `RUBYOPT`, `RUBYLIB` and the other `KLAYOUT_*` variables KLayout reads
are cleared. `CHIPLET_FLOW_PADMAP_PYTHON` names the venv's interpreter, so the flow never picks a
gds_to_kicad `venv/` that happens to sit beside the trees.

## Evidence entry

Every tree comes from one clean adk-tools checkout at the commit the flake is evaluated from: the
flow's packages from `staging/`, at their `staging.pin` refs, and the roots from the `tools/`
submodules, at their gitlinks in `HEAD`. The hook refuses if anything differs from its pin or has
uncommitted changes, before the install and again after it. Clean is asked of git with nothing
able to hide a change: the caller's `GIT_*` variables dropped, no global or system configuration,
no fsmonitor or untracked cache, every untracked file listed. What git would still not show
refuses on its own: an index entry marked assume-unchanged or skip-worktree, a content filter in
the repository's configuration, `core.worktree` pointing elsewhere, and any ignored file in a
tree or root (Python imports an ignored `__pycache__` or `.so` all the same). The one ignored
thing allowed is the `.egg-info` an editable install may leave in a source tree. The adk-tools
checkout itself may hold ignored files (`staging/` is one); nothing in it is on the run's import
path.

The venv is emptied and rebuilt on every evidence entry, so the install and `pip check` always
run and nothing a previous shell left behind can be imported. Its path stays the same, because the
flow records the interpreter's path in a run's identity and `resume` refuses a changed one. It is
keyed by each tree's name and commit, not by where the checkout is, so two clones of one commit
under one `ADK_NIX_STATE` get the same venv path and the same console scripts. A console script's
bytes carry that path, so a tracked tool's `binary_sha256` repeats only under one `ADK_NIX_STATE`;
nothing is claimed across state directories or hosts. The shell holds the
venv's lock (`<venv>.lock`, flock) for its life, and a second evidence entry on the same venv
refuses rather than empty it; evidence entries are therefore serial. The hook then enters `ADK_NIX_CONTEXT_OUT`, which must be an absolute
path to a new or empty directory, and writes `nix-context.json` there before the command runs. The
command runs from that directory: `python -m` puts the working directory first on the import path,
so it must hold nothing but what the flow writes. Every path given to the command must be absolute.

The record holds what the flow does not record: the lock and its inputs, every store path of the
realised closure with its hash, the file and version of every module imported on this path, every
distribution Python can see, the setuptools and pip that did the install, the `klayout` on `PATH`
with its `-b -v`, the file of `klayout.db` as imported and where it resolves, its version, the
declared skew against the image, the version z3 reports and the one its metadata claims, the
`assembly-lvs` on `PATH`, `openroad -version`, the executable of the bash that ran the hook (read
from `/proc`; `$SHELL` there is the shell derivation's build shell), the working directory, and
each tree's commit. It also checks where each of those came from: third-party modules from inside
the realised closure, the flow's own from the tree verified for it, nothing on `sys.path` (as the
command starts with it) outside the closure, the venv and those trees, no distribution twice except
an editable install's own pair. A missing lock, closure, tool answer or commit counts the same, and
so does a tree that is not the clean top of its checkout or an adk-tools checkout at another commit
than the flake, a `klayout` on `PATH` other than the flake's, a binary and a `klayout.db` of
different versions, a variable the hook cleared for KLayout that is set again, a z3 whose version
is not its metadata's, and an `assembly-lvs` on `PATH` other than the venv's. Any of these is
listed under `problems` in the record, which is written anyway, and the shell refuses. The record
states that nothing was enforced (`kind: context-capture`), so it cannot be taken for a launch
record.

```sh
cd <clean adk-tools checkout>
STAGE_SIBLINGS=<dir holding the sibling checkouts> ./stage-sources.sh
git submodule update --init tools/adk tools/OpenIntM4TM2 tools/IHP-Interconnect-IntM4TM2

out=<absolute path of a new directory>
ADK_NIX_MODE=evidence ADK_NIX_ADK_TOOLS="$PWD" ADK_NIX_STATE=<venv dir> ADK_NIX_CONTEXT_OUT="$out" \
CHIPLET_FLOW_READY_DIES=<ready dies> \
nix develop --ignore-environment \
  --keep ADK_NIX_MODE --keep ADK_NIX_ADK_TOOLS --keep ADK_NIX_STATE \
  --keep ADK_NIX_CONTEXT_OUT --keep CHIPLET_FLOW_READY_DIES \
  --max-jobs 1 --cores 2 .#cli \
  --command python -B -m packaging_flow.cli run <absolute spec path> --workdir "$out/run" --tier full
```

`STAGE_SIBLINGS` is needed only when the checkout does not sit beside its siblings (a worktree,
for instance). `--max-jobs 1 --cores 2` caps a local build if one happens; on a shared host, check
first with `nix build --dry-run .#devShells.x86_64-linux.cli` that everything heavy is substituted,
and stop if OpenROAD or Python would be built locally.

## The build shell

`devShells.build` is the `cli` shell plus what a die build needs (`docs/E2E_BUILD_VARIANT.md`). It
adds these packages:

- `packages.librelane`: LibreLane built from `packages.librelane-src`, which is the `librelane`
  input with `staging.pin`'s `LIBRELANE_PATCHES` applied in order. The evaluation reads
  `LIBRELANE_PATCHES` from `staging.pin` and refuses a lock whose `librelane` revision is not
  `LIBRELANE_REV`. Its `opensta` argument is `packages.opensta`.
- `packages.opensta`: LibreLane's standalone OpenSTA (its `nix/opensta.nix`, OpenSTA `857316ff`)
  with `patches/opensta-timing-model-clocked-output-exists.patch`, which keeps
  `write_timing_model` from writing a delay table for an output transition no path reaches (a
  forwarded clock reaches its output with one transition per edge; the other table was read from
  memory nothing set). LibreLane's STA steps run this `sta` by name, first on the wrapper's `PATH`,
  and write the cores' timing models with it. It is given to LibreLane through that package's
  argument only, never through an overlay: LibreLane's OpenROAD links the unpatched
  `opensta.dev`, so an overlay would rebuild the router. OpenROAD stays the path
  `NIX_OPENROAD_OUTPATH` names, and the unpatched OpenSTA stays in the closure through it.
- `packages.sg13g2-pdk`: IHP-Open-PDK at the Dockerfile's `SG13G2_REF`, plus the
  `pycell4klayout-api` and `pypreprocessor` submodules at that commit's gitlinks, copied into one
  tree with the repository's layout, and `pdk-source.json` at its root stating those three inputs
  (docs/E2E_BUILD_VARIANT.md, "Store-PDK identity"). The evaluation refuses a parent revision
  other than `SG13G2_REF`.
- `packages.librelane-python`: LibreLane's own Python with that LibreLane package and the Python
  packages it depends on, no other, the interpreter chiplet-flow's core preparation takes beside
  `$ADK_NIX_LIBRELANE/bin/librelane`. In the build shell's closure, not on its `PATH`.
- `devShells.build` itself: everything the `cli` shell sets, plus `ADK_NIX_LIBRELANE`,
  `ADK_NIX_SG13G2_PDK` (that tree) and `ADK_NIX_LIBRELANE_PYTHON` (the env's `.interpreter`). The
  `cli` hook clears `PDK_ROOT` and `PDK` with the other host variables, so the build hook sets them
  after it: `PDK_ROOT` to that tree, `PDK` to `ihp-sg13g2`.
  Its closure record also covers LibreLane, the cores' interpreter env and the PDK.

Identities, for the context record:

- LibreLane: `passthru.adkIdentity`, exported by the build shell as JSON
  (`ADK_NIX_LIBRELANE_IDENTITY`): the input's revision and narHash, each patch's sha256, and
  `LIBRELANE_PATCHED_REV`. That last one is `staging.pin`'s claim, not something the evaluation
  checks: the patched tree has no commit of its own. Under `opensta`, the store path of the `sta`
  package LibreLane was given and each of its patches' sha256; `checks.opensta` proves the wrapper
  resolves that path.
- The PDK: the tree states its own sources in `pdk-source.json` at its root, which the context
  record digests and checks against the lock. No variable carries it.
- The cores' interpreter: `librelane.interpreter`, bound to its store path, that path's narHash
  and the one LibreLane package it serves, asked of the interpreter itself as a core launch runs
  it; the shell's own Python and the generated launcher are refused.

Two comparisons need git and belong to the pins check: the patched tree against
`LIBRELANE_PATCHED_REV`'s tree, and the submodule revisions against the parent's gitlinks.

The three PDK inputs enter `flake.lock` the first time it is locked with them, which fetches all
three, about 0.9 GB: that step is a lock and a fetch, not an evaluation. Until then, an evaluation
with `--no-update-lock-file` refuses on this flake, and one that may lock fetches them first. Once
the lock holds them, only `packages.sg13g2-pdk` and the shells that hold it (build and tail) read
them, and Nix fetches an input only when an output forces it, so evaluating the `cli` shell does not
fetch them.

## The tail shell

`devShells.tail` is the `cli` shell plus what packaging-flow's tail stages (gds, interposer-drc,
drc) run. The `cli` and build shells are unchanged by it. It adds these packages:

- `packages.kicad-fork-base`: the KiCad fork's base build from the `tools/kicad` gitlink
  (nixpkgs' KiCad expression with the Dockerfile's flags; `flake.nix` lists where it departs from
  the image). Its site-packages holds `pcbnew.py` and `_pcbnew.so`, a link to the fork's
  `_pcbnew.kiface`, which is linked against the Python 3.13 the `cli` shell runs.
- `packages.tail-export-python`: the interpreter `chiplet-export` runs under, a venv of that
  Python made with `--without-pip`, with the fork's site-packages in its one `.pth` file ("Not the
  tail" above). The build refuses when the fork has no
  `pcbnew.py` or `_pcbnew.so`, or when `_pcbnew.so`'s RUNPATH does not name that Python's `lib`.
- `packages.tail-wrappers`: `chiplet-export`, `adk-verify-carrier-drc` and `adk-drc`, the image's
  names, made from `nix-tail/<name>.in` with the store paths filled in. packaging-flow finds them on
  `PATH` by name, as it finds the image's, and is not changed for the tail; what differs is behind
  those names and in the variables the hook sets. Each is the shape of the image's `bin/<name>`
  and passes its arguments on whole:
  - `chiplet-export`: the plugin's `plugins/chiplet_export/chiplet_export_cli.py` under
    `tail-export-python`;
  - `adk-drc`: `$ADK_ROOT/klayout/drc/run_drc.py` under the `cli` shell's venv interpreter;
  - `adk-verify-carrier-drc`: the store copy of this revision's `bin/adk-verify-carrier-drc` under
    that interpreter, with `ADK_PYTHON` naming the same one for the deck runner it starts. Its
    defaults for an argument not given are the image's `/opt/adk-tools` paths; packaging-flow gives
    every path the stage needs.

  Each runs under `bash -p` and refuses to start when a variable it needs is unset, that is,
  outside the shell; `chiplet-export` also needs `PDK_ROOT`, `KICAD_CHIPLET_PYTHON` and
  `GDS_TO_KICAD_ROOT`, which the plugin or its worker would otherwise look for elsewhere.
- `packages.sg13g2-pdk`: the build shell's PDK. The export's worker places the SG13G2 `via_stack`
  PCells from `PDK_ROOT` and draws plain via rectangles when it cannot, so the tail needs the PDK
  as much as it needs pcbnew. The image's PDK tree and this one differ in one input today: the
  image clones `pycell4klayout-api` at the Dockerfile's `PYCELL_API_REF`, an ancestor of the
  commit `SG13G2_REF` pins, and this tree follows the pinned commit.

The hook, `nix-tail/tail-env.sh`, runs after the `cli` hook, which clears `PDK_ROOT`,
`KICAD_CHIPLET_PYTHON`, `GDS_TO_KICAD_ROOT` and `CHIPLET_KICAD_PLUGIN_ROOT` with the host's other
variables. It sets:

- `ADK_NIX_PLUGIN_ROOT`: the chiplet_kicad_plugin tree `chiplet-export` runs. Evidence entry:
  `tools/chiplet_kicad_plugin` of the adk-tools checkout, clean at its gitlink in `HEAD`; the hook
  refuses first, by name, when it holds no `plugins/chiplet_export/chiplet_export_cli.py`.
  Development entry: the caller's `ADK_NIX_PLUGIN_ROOT`, used as it is; nothing is guessed.
- `PDK_ROOT`: `packages.sg13g2-pdk`. `PDK` stays unset, as in the image.
- `KICAD_CHIPLET_PYTHON`: the `cli` shell's venv interpreter, the worker `chiplet-export` starts
  for `hyp_to_gds.py` (`klayout.db` 0.30.5, see "The image's KLayout version, not its build").
- `GDS_TO_KICAD_ROOT`: the gds_to_kicad tree the worker imports `PinList` from for every die with
  pin lists (the interposer PDK's `bump_mirror.py`), as the image's `/opt/adk-tools/gds_to_kicad`.
  Evidence entry: `tools/gds_to_kicad` of the adk-tools checkout, clean at its gitlink in `HEAD`;
  the hook refuses first, by name, when it holds no `pin_list.py` (the submodule was not
  initialised). Development entry: the caller's `ADK_NIX_GDS_TO_KICAD_ROOT`, used as it is. It is
  always set: without it `bump_mirror.py` walks up from its own file for a `gds_to_kicad`
  directory and imports whichever checkout it finds above.
- `PATH`: the fork's `bin` directory appended, for `kicad-cli`, which gds_to_kicad's
  `chiplet_to_kicad_sch.py --check` (packaging-flow's schematic row) finds on `PATH` as it finds the
  image's `/usr/bin/kicad-cli`. Every name in that directory is KiCad's own (its programs and
  their `.kiface` modules), so appending it shadows nothing found before it.

It also clears host variables read on the export's path that the `cli` hook leaves:
`KLAYOUT_LYP_FILE` (the SG13G2 library's layer table), `NIX_PYTHONPATH` (site directories the
store Python's `sitecustomize.py` adds), the two `IHP_PYCELL_LIB_PRINT_` variables, and any
variable named `KLAYOUT` in any case, which switches on the PCell libraries' `KLAYOUT` define.
`NIX_PYTHONPATH` is read by every interpreter of the `cli` venv, not only the export's: a
development entry of the `cli` shell passes the host's value on, and a development entry of this
shell clears it only after the `cli` hook has set up the venv.

`CHIPLET_KICAD_PLUGIN_ROOT` stays cleared: packaging-flow reads it only to find its board observer,
which imports pcbnew in the flow's own interpreter, and that interpreter has none here.

The hook refuses when `PATH` resolves any of the three names to another file, or `kicad-cli` to any
file but the fork's. On evidence entry it then runs `nix-tail/capture_tail_context.py`, which writes
`tail-context.json` beside `nix-context.json`: each wrapper as `PATH` resolves it, with the store
paths its text names; `kicad-cli` as `PATH` resolves it and the file it resolves to; the
export interpreter asked to import pcbnew (it must run as that venv, with the fork's site-packages
last on its import path and nowhere else, and load code and map shared objects only from the
realised closure); the worker asked to register the PCell library from `PDK_ROOT` as
`hyp_to_gds.py` does and place one `via_stack`; `PDK_ROOT`'s `pdk-source.json` against the lock;
the worker asked to import `PinList` from `GDS_TO_KICAD_ROOT` as `bump_mirror.py` does; the plugin
and gds_to_kicad trees' commits and state; and that none of the variables the hook clears is set.
Any problem is listed in the record, which is written anyway, and the shell then refuses to run the
command. Like `nix-context.json`, the record is a capture (`kind: context-capture`), not a launch
record: it states what was found when the shell was entered and holds nothing to the run that
follows.

Entry is the `cli` shell's with `#tail` in place of `#cli`. Development entry also names the
plugin tree with `ADK_NIX_PLUGIN_ROOT` and the gds_to_kicad tree with
`ADK_NIX_GDS_TO_KICAD_ROOT`; evidence entry also needs two more submodules:

```sh
git submodule update --init tools/chiplet_kicad_plugin tools/gds_to_kicad
```

Unlike the `cli` shell, the first entry compiles the KiCad fork locally: no binary cache holds
`kicad-fork-base`, and it is a large C++ build. Check first with `nix build --dry-run
.#devShells.x86_64-linux.tail`, which lists `kicad-base-` followed by the first ten characters of
the `tools/kicad` gitlink among the derivations to build until it is in the store, and give that one
build `--max-jobs 1` and a `--cores` the host can spare; the cli guidance to stop when something
heavy would be built does not apply to it. The dry run itself fetches what it evaluates and the
store lacks: the fork's source (the `kicad-fork` input, about 680 MiB unpacked) and, as the build
shell's evaluation does, the three PDK inputs (about 0.9 GB).

It is not the image's tail. The export runs Python 3.13 where the image runs 3.12; the fork is
built by nixpkgs' expression with the departures `flake.nix` lists (the EGL flag, nixpkgs' two
patches, the version string); the worker runs nix-eda's build of KLayout 0.30.5 where the image runs PyPI's wheel; and
the PDK differs as above. Nothing in this repository compares a tail run with the image's.

The tail's files live in `nix-tail/`, not `nix/`: the `cli` and build shells carry `nix/` whole,
so a file added there would change both. The tail still uses `nix/` through `ADK_NIX_LIB`: the
build hook's functions, and the context readers `capture_tail_context.py` imports. The `nix-tail/`
tests run the hook on those functions and check that every name `capture_tail_context.py` takes
from the readers exists, so run them after any change under `nix/` as well.

## Tests

These run without Nix and write only under a temporary directory.

```sh
nix/test-cli-env.sh                 # the hook's refusals, against throwaway git repositories
nix/test-build-env.sh               # the build hook's launcher, run against a stand-in store
nix/test-check-build-pins.sh        # the build pins check, against a stand-in nix and throwaway repositories
nix/test-check-kicad-pin.sh         # the KiCad fork's pin check, against throwaway repositories
python3 nix/test_capture_context.py # the context record's reading of closureInfo and the lock
python3 nix/test_capture_build_context.py  # the build shell's launcher probe, run against a stand-in store
tests/nix/test-read-opensta-guard-outputs.sh  # checks.opensta's reader, on a recorded ablation and one-change copies
nix-tail/test-tail-env.sh           # the tail hook's refusals, and the wrappers run for real, against a stand-in store
nix-tail/test-check-tail-wrappers.sh  # the wrappers check, against throwaway repositories
nix-tail/test-check-tail-eval.sh    # the tail evaluation check, against a stand-in nix
python3 nix-tail/test_tail_eval.py  # that check's reader, on a fixture and on what Nix 2.30.1 printed
python3 nix-tail/test_capture_tail_context.py  # the tail context record; pcbnew's probe run for real on a stand-in fork
```

One needs Nix but fetches nothing: `nix/test-pdk-source.sh` evaluates and builds the PDK's
`pdk-source.json` on stub inputs, with the nixpkgs the lock names, and refuses to run when that
nixpkgs or its stdenv is not already in the store.

One runs inside the shell, with its real `klayout`: that a host home registering `SG13_dev` does
not reach `klayout -b` or `-zz` there, with controls that reach it outside the isolation, and that
the interposer technology `KLAYOUT_PATH` names does.

```sh
nix develop <adk-tools>#cli --command nix/test-klayout-home.sh
```

One check needs Nix, and evaluates only (import-from-derivation off and `--max-jobs 0`, so no
derivation is built, though flake inputs missing from the store are still fetched;
`--no-update-lock-file`, so the committed lock is used): that `staging.pin`'s
`NIX_OPENROAD_OUTPATH`, `NIX_LIBRELANE_OUTPATH`, `NIX_LIBRELANE_PYTHON_OUTPATH` and
`NIX_SG13G2_PDK_OUTPATH` are the store paths the flake's `packages.x86_64-linux.openroad`,
`librelane`, `librelane-python` and `sg13g2-pdk` evaluate to, and its
`NIX_COSIM_IVERILOG_OUTPATH`, `NIX_COSIM_COCOTB_OUTPATH`, `NIX_COSIM_PYTHON_OUTPATH` and
`NIX_COSIM_DEVSHELL_OUTPATH` the ones `cosim-iverilog`, `cosim-cocotb`, `cosim-python` and
`cosim-devshell` do. The image carries the first four as labels for `run.sh` (the interpreter's
as the env's `bin/python3.<minor>`), and `build.sh` checks only their shape. The next four are the
co-simulation engine the image itself carries (and labels the devshell, the env and Icarus): `build.sh` packs their closure from the host's store, so
they have to be built there first (`nix build .#cosim-devshell`), and it refuses when that closure
is not exactly `nix/cosim-closure.txt`. `COSIM_DEVSHELL` links to the devshell's store path, whose
`bin/` holds exactly `python3`, `iverilog` and `vvp`, so the host and the image run the same one. The
mixed-signal engine's `NIX_MIXSIM_NGSPICE_OUTPATH`, `NIX_MIXSIM_IVERILOG_OUTPATH` and
`NIX_SG13G2_OSDI_OUTPATH` are held to `mixsim-ngspice`, `mixsim-iverilog` and `sg13g2-osdi` the
same way; `build.sh` refuses unless their closure, less the co-simulation list, is exactly
`nix/mixsim-closure.txt`, and unless the OSDI files' bytes are the ones `nix/sg13g2-osdi.sha256`
names (`nix/check-osdi-sha.sh`), since their store path does not tell one build from another. The
viewers' `NIX_GTKWAVE_OUTPATH`, `NIX_GAW_OUTPATH` and `NIX_XSCHEM_OUTPATH` are held to `gtkwave`,
`gaw` and `xschem`, and `build.sh` refuses unless their closure, less both engines' lists, is
exactly `nix/viewer-closure.txt`. The
Verilog-AMS engine's `NIX_VAMS_GNUCAP_OUTPATH` and `NIX_VAMS_DEVSHELL_OUTPATH` are held to
`vams-gnucap` and `vams-devshell`, and `build.sh` refuses unless their closure, less the other three
lists, is exactly `nix/vams-closure.txt`; neither is in the binary cache, so
`nix build .#vams-devshell` compiles both first. Run
the check after any move of `flake.lock`, the `librelane` input or the pins the PDK package reads;
it refuses on any difference.

```sh
nix/check-openroad-pin.sh
```

One check needs neither Nix nor the network: that `flake.lock`'s `kicad-fork` input is a GitHub
input at the `tools/kicad` gitlink's commit in `HEAD`, from the repository `.gitmodules` names for
that path, with a narHash. The gitlink stays the owner of the fork's revision. Run it after any move
of the gitlink or the input; it refuses on any difference.

```sh
nix/check-kicad-pin.sh
```

One check needs neither Nix nor the network: that the tail's wrappers keep the image's names and
argument shape. Read from `HEAD`: each of the three is `bin/<name>` in the image and has a template
`nix-tail/<name>.in`, run by `bash -p`, holding only the guards its command needs, the
`ADK_PYTHON` export where the image's script starts a second Python, and one `exec <interpreter>
<script> "$@"`, in that order; the interpreter is the one the tail gives that command; every path
it names is a store path the flake fills in, or lies under the venv, `ADK_ROOT` or the plugin
root; its script is the image's; and `flake.nix` has one line that reads like a binding
`tail-wrappers = ...` (a line starting with `#` is not counted unless it could end a string or a comment),
and from there the binding is printable ASCII and, line for line with only leading spaces and tabs
aside, the one the check holds: no comment, one `substitute` call that fills exactly the
placeholders the templates use, each spelled as the check spells it. It refuses on any difference.
It reads the flake as text, in that one form of binding only: it does not see `tail-wrappers`
bound in another form (quoted, split over lines, inherited from elsewhere, overridden), a copy of
the binding Nix never evaluates, or what the names the binding uses evaluate to (`lib`, `pkgs`,
the filled values). The evaluation check below reads what Nix evaluates.

```sh
nix-tail/check-tail-wrappers.sh
```

One check needs Nix and evaluates only (`--offline`, `--max-jobs 0` and import-from-derivation
off, so no derivation is built; `--no-update-lock-file`), two clean checkouts: a base and this one.
It passes only when the `cli` and build shells differ from the base's in nothing but the revision
they record and the output path that follows from it, when `devShells.tail` is the `cli` shell
plus `tail-wrappers`, `tail-export-python`, `kicad-fork-base` and `sg13g2-pdk` and nothing else
(`nix-tail/tail_eval.py` lists what that means field by field, the closure's roots included),
when `kicad-fork-base` and `sg13g2-pdk` are the base's derivations, so that where the base's are
built the tail shell builds neither again, and when the `tail-wrappers` derivation the tail shell
takes, in whatever form the flake binds it, has the build command and variables the binding the
wrappers check reads gives it, with store paths in the places that binding fills. For that last,
it renders each wrapper from the store template the derivation names, with the store paths its
build command fills in, and requires the image's command, run by `bash -p`, naming only store
paths, the venv and the `tools/` roots, and the checkout's `bin/adk-verify-carrier-drc` as the
carrier script. The bash that fills `@bash@` is checked there by shape only (one store path
ending `/bin/bash`), and the wrappers check reads only how its one form of binding spells it, so a
bash that drops `-p`, such as a shim that runs bash without it, is caught by neither; what
`tail-export-python` and the `nix-tail/` store copy hold is not read either. It reads those store
files, which the evaluation put there, and keeps them with what Nix printed and the store
directory it read them from.

The base must define `kicad-fork-base` and `sg13g2-pdk`; the second checkout defaults to the one
holding the script.

```sh
nix-tail/check-tail-eval.sh [--keep <new dir>] <base checkout> [<checkout>]
```

One check builds: `checks.opensta` evaluates the LibreLane wrapper's own `PATH` lines from an empty
`PATH`, requires `sta` to resolve to `packages.opensta` and to the path `adkIdentity` states, and
runs `tests/nix/test-opensta-timing-model-guard.sh` on it and on LibreLane's unpatched OpenSTA (its
ablation). `tests/nix/read-opensta-guard-outputs.sh` decides: a clean pass on the first; on the
second, the same checks, the three forwarded-clock table-set failures among them and no other
failure but the fill and finite checks. Building it builds the patched OpenSTA and LibreLane on it,
not OpenROAD.

```sh
nix build <adk-tools>#checks.x86_64-linux.opensta
```
