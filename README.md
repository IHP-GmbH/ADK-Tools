# adk-tools

Single Docker image for the heterogeneous integration flow: every tool of the
ecosystem, pre-wired, callable by name. Versions are pinned three ways: the
submodules under `tools/`, the staged sources and engines in `staging.pin`, and
the Nix inputs in `flake.lock`.

## Status

> [!WARNING]
> adk-tools is an alpha. What is checked is the build: `./build.sh` runs its
> verify gate on the image it makes. The flow's results are not: no assembly
> this image exports or checks is qualified, and the path that builds dies
> from RTL has not run end to end (`docs/E2E_BUILD_VARIANT.md`).

## Quickstart

A clone of this repository alone can neither build nor run anything today.
No image is published, to a registry or as a download: you build it locally
(see Build) and run the image you built. Building needs sources that are not
public yet.

### Build

Building needs Docker and Nix on the host, checkouts of the six sibling
repositories `staging.pin` lists at the commits it pins (beside this
repository, or in the directory `STAGE_SIBLINGS` names), and the `die-views/`
folder of an adk-examples checkout (not public yet) whose records match
`die-views.sha256` (`STAGE_DIE_VIEWS_DIR` names that folder). None of the six
pinned commits can be fetched from a public remote yet (`ci/pin-set.json` marks
them `"reachable": "local-only"`). The two `--check` calls below find a missing
piece in seconds; run them first.

`build.sh` compiles the pinned toolchain into one Docker image and copies
OpenROAD, LibreLane, its interpreter, the sg13g2 PDK and the simulation
engines and viewers from the host's Nix store into the image's own store (see
Notes). At the commit you build, realise them once before the build and keep
the result links (they keep the paths from garbage collection). Most come from
the binary cache; vams-devshell and Xschem build locally, and mixsim-iverilog
builds locally and runs Icarus Verilog's regression suite.

```bash
# the clone lives at ADK_TOOLS_ROOT, outside ADK_WORK (see below); the
# die-views/ folder of the adk-examples checkout at ADK_EXAMPLES_ROOT has to
# match die-views.sha256
export ADK_TOOLS_ROOT=~/adk-tools ADK_EXAMPLES_ROOT=~/adk-examples
export STAGE_DIE_VIEWS_DIR="$ADK_EXAMPLES_ROOT/die-views"
git clone --branch vYYYY.MM --recurse-submodules https://github.com/IHP-GmbH/ADK-Tools.git \
    "$ADK_TOOLS_ROOT"
cd "$ADK_TOOLS_ROOT"
./stage-sources.sh --check && ./stage-die-views.sh --check
nix build .#openroad .#librelane .#librelane-python .#sg13g2-pdk \
    .#cosim-devshell .#mixsim-ngspice .#mixsim-iverilog .#sg13g2-osdi \
    .#gtkwave .#gaw .#xschem .#vams-devshell
# ~1 h the first time, cached afterwards; JOBS=N overrides the default 8
# parallel compile jobs
./build.sh

# run.sh takes the image by its ID (sha256:<64 hex>), never a tag, and has no
# default: read the ID of the image you just built once
export ADK_TOOLS_IMAGE="$(docker image inspect --format '{{.Id}}' adk-tools:dev)"
"$ADK_TOOLS_ROOT/run.sh" --examples "$ADK_EXAMPLES_ROOT"
```

The examples' `release-lock.json` names the reference image they were checked
on and the pin set it was built from. Your build has its own ID: `run.sh`
names both IDs in one note line and runs it, and refuses an image whose
`org.ihp.adk-tools.pin-set` label, which `build.sh` writes, is not the lock's
pin set.

### Running it

With `ADK_TOOLS_IMAGE` set:

```bash
# X11 passthrough for the GUIs; ~/adk-work mounted at /work; capped at 8g and
# 4 CPUs, change with ADK_MEM_LIMIT and ADK_CPUS; ADK_NETWORK=none runs it
# with no network
./run.sh                              # interactive shell
./run.sh --examples ~/adk-examples    # the examples, from an adk-examples checkout,
                                      # held to its lock (an image
                                      # without the closure label needs the
                                      # host's Nix store, see Notes)
./run.sh bash < script.sh             # run a script piped in, no terminal needed
```

Inside the shell, `adk-tools` prints this table with the pinned versions of
the running image.

The examples live in their own repository, adk-examples, which is not public
yet. Its `release-lock.json` names the reference image they were checked on
and its pin set, and `run.sh --examples <checkout>` starts an image of that pin
set, with the checkout mounted read-only at `/examples`. Each example's
`README.md` is its page: what to run, and what the run should show.
`examples/two_die_interposer` in this repository is not one of them: it is the
test fixture of the image's own checks.

## Working in the shared folder

`run.sh` mounts a host directory (`~/adk-work`, override with `ADK_WORK=...`) at
`/work` inside the container, so files written on either side are immediately
visible on the other. A relative `ADK_WORK` is taken from the directory you call
`run.sh` from; either way `run.sh` binds it by its absolute path and names it on
its first stderr line, `run.sh: /work is <path>`. What a program calling
`run.sh` can rely on (its notes, exit codes and refusals) is in
[docs/RUN_SH.md](docs/RUN_SH.md).

Keep the adk-tools clone apart from that directory: clone it once to
`ADK_TOOLS_ROOT` (`~/adk-tools` above), outside `ADK_WORK`, leave it at the
version of the image you run (the release tag, or the commit you built), and
call `run.sh` by its path from wherever you work. `run.sh` refuses to start when `ADK_WORK` holds its clone, which the
container would then see and could write, or lies inside it, where a git clean
in the clone would delete your runs.

```bash
export ADK_TOOLS_ROOT=~/adk-tools ADK_WORK=~/adk-work ADK_EXAMPLES_ROOT=~/adk-examples
"$ADK_TOOLS_ROOT/run.sh" --examples "$ADK_EXAMPLES_ROOT"
```

The work directory is the recommended place to keep your designs: edit
with the bundled GUIs/editors in the container, keep the files (and your git
history) on the host. The image writes nothing there unless you ask; two
subdirs are the usual layout:

```
/work/<example>            an example copied in from /examples, as its page says
/work/heterogenic-designs  your persistent designs (survives image rebuilds)
```

The runtime image carries no examples. Start it with an adk-examples checkout
(`run.sh --examples`); inside the container, `adk-example` prints that line.

Scaffold a new design instead of starting from a blank dir:

```bash
adk-new-project my_design          # -> /work/heterogenic-designs/my_design
```

It lays out the same structure as the bundled example, a clean split between
source and generated artifacts:

```
my_design/
  kicad/       KiCad source you author (schematic, PCB, .pretty, fp-lib-table)
  chiplets/    die GDS inputs (+ pin lists) for the gds-to-kicad prior steps
  outputs/     export products: <board>.chiplet + MANIFEST.md at the root; the
               GDS layouts + DRC sidecars under outputs/layout/; the DRC
               reports under outputs/reports/
```

Author the board under `kicad/`, run the Chiplet Export plugin in pcbnew with
its output directory set to the sibling `outputs/`, then open the result with
`chiplet-studio outputs/<board>.chiplet`. The scaffolded `.gitignore` already
keeps the heavy/scratch files (the full `_complete.gds`, the intermediate
`.hyp`, the DRC working dir, logs and KiCad caches) out of git while tracking
the light deliverables, so `git init` in the project is ready to use.

## Tools

| Command          | Tool                                  | Kind |
|------------------|---------------------------------------|------|
| `kicad`          | KiCad fork (+ `pcbnew`, `kicad-cli`, chiplet export plugin preloaded) | GUI |
| `chiplet-studio` | 3D assembly viewer / `.chiplet` editor | GUI |
| `gds-to-kicad`   | GDS -> KiCad symbol + footprint generator (unified GUI) | GUI |
| `hyp-to-gds`     | HyperLynx -> GDS converter             | CLI |
| `adk-drc`        | ADK assembly DRC runner                | CLI |
| `klayout`        | KLayout (version pinned by the studio submodule) | GUI/CLI |
| `adk-smoke`      | Board export + dual DRC, asserts green  | CLI |

Data roots baked in (and exported as env), named after their IHP
repositories: `INTERPOSER_PDK_ROOT=/opt/adk-tools/OpenIntM4TM2`,
`INTERCONNECT_PDK_ROOT=/opt/adk-tools/IHP-Interconnect-IntM4TM2`,
`PDK_ROOT=/opt/adk-tools/IHP-Open-PDK` (SG13G2 KLayout slice: the
SG13_dev PCell library + tech, pinned in the Dockerfile, so
`hyp-to-gds` builds vias from real `via_stack` PCells, not the
rectangle fallback), plus `ADK_ROOT` and `GDS_TO_KICAD_ROOT`. The
two-die interposer test fixture lives in this repo under `examples/` (not in the
runtime image; the verify build puts it at `/opt/adk-tools/examples`,
regenerates it and DRC-gates it); it follows the same `kicad/` + `chiplets/` + `outputs/` template
as a scaffolded project. Python worker venv: `/opt/adk-tools/venv`
(`KICAD_CHIPLET_PYTHON` already points at it).

Dies on an IntM4TM2 interposer mount on the plane the interposer PDK states,
`beol.attachment_surface_z_um` in `libs.tech/intm4tm2_stack.json`.
`ci/check_attach_plane.py` holds every IntM4TM2 `.chiplet` in the plugin, in
Studio and in `examples/` to it, and the consumers' copies of the PDK's stack
files to the PDK; it runs in CI and in every verify build, and its docstring
states exactly which files it walks.
`ci/check_kicad_templates.py` does the same for the staged interposer-pnr and
packaging-flow copies of the PDK's KiCad rule templates, in every verify build.

`openroad/` holds a host-side sidecar that lints an assembly with OpenROAD's
`read_3dbx` / `check_3dblox`, using a separately pinned OpenROAD image. It is
not part of the distribution image; see `openroad/README.md`, which also
states plainly what that linter does and does not catch.

The interposer KLayout technology is pre-registered (`KLAYOUT_PATH`
includes the OpenIntM4TM2 tree), so exported interposer/assembly GDS
files open with named, colored layers: `klayout -n intm4tm2 <file>.gds`,
or pick `intm4tm2` in the technology selector.

The PDK's `IntM4TM2 Interposer > Verification` menu (`Interposer DRC…`,
`Interposer LVS…`) needs a KLayout with Qt bindings, and the image's KLayout
is built without them: the entries are listed, and choosing one reports
`AttributeError: module 'pya' has no attribute 'QDialog'`. KLayout itself
starts normally, and the PDK's other menu entries do not use Qt bindings. The
runners behind that menu are
unaffected; call them directly from `libs.tech/klayout/tech/drc/run_drc.py`
and `libs.tech/klayout/tech/lvs/run_lvs.py` under `$INTERPOSER_PDK_ROOT`.

## Updating an existing clone

This section and the two after it are for building (Quickstart, Build). To run
a newer release, check out its tag and build it.

```bash
# the Quickstart's exports, again in a fresh shell
export ADK_TOOLS_ROOT=~/adk-tools ADK_EXAMPLES_ROOT=~/adk-examples
export STAGE_DIE_VIEWS_DIR="$ADK_EXAMPLES_ROOT/die-views"
cd "$ADK_TOOLS_ROOT"
git fetch --tags
git checkout vYYYY.MM    # a release clone sits on a tag, so there is nothing to pull
git submodule update --init --recursive
./stage-sources.sh --check && ./stage-die-views.sh --check
./build.sh    # cached stages rebuild only what changed
```

The sibling checkouts have to hold the new pins (a `git fetch` in each), and
the adk-examples checkout has to hold die-views records that match the new
`die-views.sha256` (update it until they do); the two `--check` calls say
which one does not.

One-time cleanups, depending on how old the clone is:

- Clones from before the demo moved into `examples/` still carry the
  removed `kicad_designs` submodule as leftovers:
  `rm -rf tools/kicad_designs .git/modules/tools/kicad_designs`
- If the default job count changed since your last build (it keys the
  compile layers), the heavy stages rebuild once (~40 min), then cache
  normally again.
- A `/work/example` from an older image stays as it is. It was the demo copied
  from this clone; the examples now come from adk-examples.

## Branch model

`main` is frozen per release and pins each tool's `main`. `dev` is where the
suite is integrated and tested, and pins each tool's `dev` branch instead, so
`git submodule update --remote` follows the right side on each branch. Tools
without ongoing work keep `branch = main` in `.gitmodules` on both sides until
they need a `dev`; check that file for which is which.

Feature work happens on a branch of the tool's `dev`, and is evaluated by
checking it out in the submodule and rebuilding (see below). A release folds
each tool's `dev` into its `main`, then re-pins adk-tools `main`.

## Updating / testing a tool release

```bash
# bump one tool to its latest upstream branch (dev on this branch, main on main)
git submodule update --remote tools/adk
git add tools/adk
python3 -B ci/pin_set.py    # the pin set records every staged gitlink
git commit -am "Bump adk to <ref>"
./build.sh

# try an arbitrary branch of one tool against the rest of the pinned stack
cd tools/chiplet-studio && git fetch && git checkout feature/x && cd ../..
./build.sh
```

The pin set takes each tool's commit from the gitlink git has staged, not from
the submodule's checkout, so stage the bump before you regenerate it. The
image is labelled with the committed pin set: `build.sh` refuses a
`ci/pin-set.json` that differs from the commit or from what the staged pins
give, so commit the regenerated file before you build. In the second case
nothing is staged: the image carries `feature/x` while its
`org.ihp.adk-tools.pin-set` label still names the pinned commit, so do not run
such an image under an examples checkout's `release-lock.json`.

`build.sh` always builds the `verify` stage first: it regenerates the
two-die interposer demo headless through the full pipeline (pcbnew -> writers ->
hyp_to_gds -> assembly DRC), runs the Chiplet Studio test suite and the
plugin test suite inside the image. A broken combination does not produce a
tagged image. `--skip-verify` exists for quick iteration only.

To develop a Python tool against the pinned stack, mount your checkout over
the baked-in copy:

```bash
docker run --rm -it -v "$PWD/chiplet_kicad_plugin":/opt/adk-tools/chiplet_kicad_plugin ... adk-tools:dev
```

## Notes

- The image's Magic is 8.3.610, from LibreLane's Nix closure, and the PDK's Magic
  techfiles require 8.3.617 (ihp-sg13g2) and 8.3.657 (ihp-sg13cmos5l), so a LibreLane
  Classic flow run in the image fails at `Magic.StreamOut`. Build dies with
  chiplet-flow's Chiplet flow (`meta.flow: Chiplet`), which runs no Magic step.
- The image carries OpenROAD, LibreLane, its interpreter, the sg13g2 PDK and
  the LibreLane launcher in its own `/nix/store`: the closure
  `nix/host-closure.txt` lists, each path pinned with its narHash in
  `nix/host-closure.lock`. `build.sh` refuses to pack unless every path is in
  the build host's store with that narHash, and the verify stage recomputes
  each one in the image. The image sets `OPENROAD`, `ADK_NIX_LIBRELANE`,
  `ADK_NIX_LIBRELANE_PYTHON`, `ADK_NIX_SG13G2_PDK` and the launcher pair in its
  ENV, and carries the `org.ihp.adk-tools.nix-closure-inside` label (the lock's
  sha256). For such an image `run.sh` mounts nothing of the host's store, passes
  none of those variables, ignores `ADK_OPENROAD` with a note, and needs no Nix
  on the host. It refuses an image whose label is there but is not a sha256.
- An image without that label needs the host's
  Nix store: `run.sh` takes, read-only, the OpenROAD the image's
  `org.ihp.adk-tools.nix-openroad` label names (staging.pin's
  `NIX_OPENROAD_OUTPATH`), or `ADK_OPENROAD` when set, and sets `OPENROAD` to
  it. It refuses to start when the host has no Nix store or that path is not in
  it, with a line starting `needs Nix on the host:`; realise it with
  `nix build .#openroad`. An image without the `nix-openroad` label needs
  `ADK_OPENROAD`.
- What goes in from the host's store, for such an image, is the closure of OpenROAD, LibreLane,
  its interpreter and the sg13g2 PDK, listed in `nix/host-closure.txt`.
  `build.sh` refuses unless that file is the store's closure of the four, and
  writes it on the image as the `org.ihp.adk-tools.nix-host-closure` label (the
  paths joined by single spaces) with its sha256 in
  `org.ihp.adk-tools.nix-host-closure-sha256`. `run.sh` checks the list against
  its sha256 and binds each listed path on its own, read-only, at its own path,
  so the image's own `/nix/store` stays visible beside them; a listed path the
  host lacks is not bound and is counted in a note. No launch calls Nix. An
  image without the two labels, or an `ADK_OPENROAD` the list does not name,
  gets the whole host store instead, which hides the image's own store paths.
  A die run recorded under another sg13g2-pdk path names a PDK outside the
  list; rebuild that die under this image's PDK.
- Such an image ships no LibreLane, no LibreLane interpreter and no sg13g2 PDK
  either; a page that builds a die in the container takes them from the same
  store. `run.sh` passes the paths the image's `org.ihp.adk-tools.nix-librelane`,
  `org.ihp.adk-tools.nix-librelane-python` and `org.ihp.adk-tools.nix-sg13g2-pdk`
  labels name (staging.pin's `NIX_LIBRELANE_OUTPATH`, the `bin/python3.<minor>`
  of `NIX_LIBRELANE_PYTHON_OUTPATH`, and `NIX_SG13G2_PDK_OUTPATH`) as
  `ADK_NIX_LIBRELANE`, `ADK_NIX_LIBRELANE_PYTHON` and `ADK_NIX_SG13G2_PDK`, the
  names the Nix build shell sets, when they are on the host and in the list
  bound. When one is not, it says so and starts anyway, and that page refuses;
  realise them with
  `nix build .#librelane .#librelane-python .#sg13g2-pdk`.
- The image does carry the co-simulation engine chiplet-cosim runs: Icarus and
  a python3.13 with cocotb 2.0.0 in its own site-packages, at the Nix store
  paths staging.pin's `NIX_COSIM_*` lines name, under the image's `/nix/store`.
  `COSIM_DEVSHELL` links to the flake's `cosim-devshell`, a store path whose
  `bin/` holds only `python3`, `iverilog` and `vvp`, linked into them; none is
  on `PATH`. Only when `run.sh` mounts the whole host store (see above) does
  that hide the image's; then the engine comes from the host's store, and
  `run.sh` says when it is not there; realise it with
  `nix build .#cosim-devshell`.
- It carries a mixed-signal engine the same way: ngspice 45, an Icarus with
  libvvp, and the six sg13g2 OSDI models compiled for generic x86-64, at the
  paths staging.pin's `NIX_MIXSIM_*` and `NIX_SG13G2_OSDI_OUTPATH` lines name.
  `MIXSIM_DEVSHELL` holds `bin/ngspice`, `bin/iverilog`, `bin/vvp`,
  `lib/libvvp.so`, `lib/ngspice` (with the ivlng shim d_cosim loads) and
  `osdi/`; none is on `PATH`. Give d_cosim `lib/libvvp.so` and
  `lib/ngspice/ivlng` by absolute path, and load the models with `pre_osdi`.
  Under a whole-store mount, realise it with
  `nix build .#mixsim-ngspice .#mixsim-iverilog .#sg13g2-osdi`.
- For a gf180mcuD die in a mixed case it carries that PDK's `design.ngspice`
  and `sm141064.ngspice` and no cell library, at the GF180 layer sheet's
  commit. `COSIM_GF180_ROOT` points at them, and it is the only place
  chiplet-cosim reads a gf180mcuD models entry's root from.
- For a transistor-level ihp-sg13g2 die it carries, under
  `$PDK_ROOT/ihp-sg13g2`, every ngspice model file the MOS, diode, resistor
  and capacitor corner libraries include, in any section and at any depth, and
  the stdcell spice (`sg13g2-models.sha256` lists them, and
  `ci/check_sg13g2_models.py` recomputes that list), at the slice's commit, and
  copies of the OSDI files `psp103`, `psp103_nqs`, `r3_cmc` and `mosvar` in
  `libs.tech/ngspice/osdi`. A link would not do there: chiplet-cosim refuses
  an OSDI file that resolves outside the PDK.
- It carries two waveform viewers and a schematic editor the same way, and
  these are on `PATH`: `gtkwave` (GTKWave 3.3.121, for VCD and FST), `gaw`
  (GAW, for ngspice's raw files, binary or ASCII) and `xschem` (Xschem
  3.4.8RC, upstream commit 8d2088e, with its device symbols under
  `XSCHEM_SHAREDIR`), at the paths staging.pin's `NIX_GTKWAVE_OUTPATH`,
  `NIX_GAW_OUTPATH` and `NIX_XSCHEM_OUTPATH` name. All three need a display
  (`ADK_DISPLAY`); `xschem -x -q -n -s` netlists without one. Under a
  whole-store mount, realise them with `nix build .#gtkwave .#gaw .#xschem`.
- It carries a Verilog-AMS engine the same way: gnucap and its Verilog-AMS
  model compiler in one prefix, at the paths staging.pin's
  `NIX_VAMS_GNUCAP_OUTPATH` and `NIX_VAMS_DEVSHELL_OUTPATH` name.
  `VAMS_DEVSHELL` holds `bin/gnucap`, `bin/gnucap-conf`, `bin/gnucap-mg-vams`,
  `bin/gnucap-modelgen`, `bin/g++` and `bin/c++`; none is on `PATH`. Compile a
  model with `gnucap-mg-vams` and that `g++` (flags from
  `gnucap-conf --cppflags`), then `load` the `.so` in gnucap. Under a
  whole-store mount, realise it with `nix build .#vams-devshell`; it is not
  in the binary cache, so that builds it.
- It carries the EDA views of the two built bondpad dies, their gate-level
  netlists and CDL among them, under `ADK_DIE_VIEWS`
  (`/opt/adk-tools/die-views`), one directory per die named by the layout
  sha256 its contract binds. A die with no directory there has no
  views. Copy a record out before using it; the image's copy is the reference.
  `die-views.sha256` pins every byte, and `staging.pin` pins that listing as
  `STAGE_DIE_VIEWS_SHA256`. Building the image needs the records:
  set `STAGE_DIE_VIEWS_DIR` to the directory holding their `records/`.
- GUIs need an X server. `run.sh` takes the display from `ADK_DISPLAY` (`:N`,
  or `none`), else a local `DISPLAY`, else the newest ThinLinc session whose
  socket is yours, and passes its cookie from `XAUTHORITY`, the ThinLinc
  session or `~/.Xauthority`, whichever holds one for that display. It prints
  the display it chose. The image itself forces Mesa's
  software path (`LIBGL_ALWAYS_SOFTWARE=1`, `GALLIUM_DRIVER=llvmpipe`, set in
  the Dockerfile) so the GUIs stay stable over remote X without a GPU;
  otherwise KiCad's GL canvas crashes when the host can't provide a DRI3
  device. Pass a real GPU and `-e LIBGL_ALWAYS_SOFTWARE=0` for hardware
  acceleration.
- The image bundles GPL-licensed tools: the pinned submodules under `tools/`,
  public in the IHP-GmbH org; the staged trees `staging.pin` pins, not public
  yet, whose source the image carries as git checkouts under
  `/opt/adk-tools/staging/<tree>`; and programs from the Nix closures copied
  into the image (Icarus Verilog, GTKWave, GAW, Xschem, gnucap and
  modelgen-verilog, nix-eda's KLayout, and the OpenSTA inside OpenROAD), whose
  source is at the revisions `flake.lock` and `flake.nix` pin. `NOTICE.md`
  lists them all.
- KiCad ships the official v9 symbol/footprint libraries (pinned tag,
  `KICAD_LIBS_TAG` build arg). 3D model packages are not included.
- Text editors are bundled for in-container edits to files under `/work`:
  `vim` (terminal) and `featherpad` (a light Qt GUI editor; needs X, like the
  other GUIs).

## License & notices

This repository is **GPL-3.0-or-later** (`LICENSE`), with one rule on top: a
file's own `SPDX-License-Identifier` line decides its license. The record format
schemas in [`formats/`](formats/) and the files of ours whose SPDX line says
Apache-2.0 (most of `ci/`, `nix/`, `nix-tail/`, their tests and `flake.nix`) are
**Apache-2.0**; its text is in
[`formats/LICENSE`](formats/LICENSE). A file with no SPDX line is under `LICENSE`,
and the third-party files `NOTICE.md` lists keep their own license.
The image is a **mere aggregation** of independently licensed tools; each keeps its
own license, and the bundled set spans GPL, Apache-2.0, CC-BY-SA-4.0 and permissive
(MIT/BSD) licenses. See
[`NOTICE.md`](NOTICE.md) for the per-component licenses, source URLs, and the GPL
corresponding-source obligation that applies when you redistribute the image.
