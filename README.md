# adk-tools

Single Docker image for the heterogeneous integration flow: every tool of the
ecosystem, pre-wired, callable by name. The pinned submodules under `tools/`
are the version lockfile; one commit of this repo = one tested combination.

## Quickstart

Primary distribution channel: clone + local build (internal use).

```bash
# build locally (~1 h first time, cached afterwards; JOBS=N overrides
# the default 8 parallel compile jobs)
git clone --recurse-submodules git@github.com:IHP-GmbH/ADK-Tools.git
cd ADK-Tools && ./build.sh

# alternatively, best-effort registry copy (private; small org quota,
# may lag behind main): docker pull ghcr.io/ihp-gmbh/adk-tools:latest

# run (X11 passthrough for the GUIs; ~/adk-work mounted at /work)
./run.sh                              # interactive shell
./run.sh kicad example/kicad/two_die_interposer.kicad_pro
./run.sh chiplet-studio example/outputs/two_die_interposer.chiplet
./run.sh adk-smoke                    # end-to-end self test
```

Inside the shell, `adk-tools` prints this table with the pinned versions of
the running image.

## Working in the shared folder

`run.sh` mounts a host directory (`~/adk-work`, override with `ADK_WORK=...`) at
`/work` inside the container, so files written on either side are immediately
visible on the other. This is the recommended place to keep your designs: edit
with the bundled GUIs/editors in the container, keep the files (and your git
history) on the host. Two subdirs are seeded on first start:

```
/work/example              two-die interposer demo, copied here on start (disposable)
/work/heterogenic-designs  your persistent designs (survives image rebuilds)
```

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
| `adk-smoke`      | Demo export + dual DRC, asserts green  | CLI |

Data roots baked in (and exported as env), named after their IHP
repositories: `INTERPOSER_PDK_ROOT=/opt/adk-tools/OpenIntM4TM2`,
`INTERCONNECT_PDK_ROOT=/opt/adk-tools/IHP-Interconnect-IntM4TM2`,
`PDK_ROOT=/opt/adk-tools/IHP-Open-PDK` (SG13G2 KLayout slice: the
SG13_dev PCell library + tech, pinned in the Dockerfile, so
`hyp-to-gds` builds vias from real `via_stack` PCells, not the
rectangle fallback), plus `ADK_ROOT` and `GDS_TO_KICAD_ROOT`. The
two-die interposer demo lives in this repo under `examples/` (baked at
`/opt/adk-tools/examples`, regenerated and DRC-gated by every verify
build); it follows the same `kicad/` + `chiplets/` + `outputs/` template
as a scaffolded project. Python worker venv: `/opt/adk-tools/venv`
(`KICAD_CHIPLET_PYTHON` already points at it).

The interposer KLayout technology is pre-registered (`KLAYOUT_PATH`
includes the OpenIntM4TM2 tree), so exported interposer/assembly GDS
files open with named, colored layers: `klayout -n intm4tm2 <file>.gds`,
or pick `intm4tm2` in the technology selector.

## Updating an existing clone

```bash
cd ADK-Tools
git pull
git submodule update --init --recursive
./build.sh    # cached stages rebuild only what changed
```

One-time cleanups, depending on how old the clone is:

- Clones from before the demo moved into `examples/` still carry the
  removed `kicad_designs` submodule as leftovers:
  `rm -rf tools/kicad_designs .git/modules/tools/kicad_designs`
- If the default job count changed since your last build (it keys the
  compile layers), the heavy stages rebuild once (~40 min), then cache
  normally again.
- A `/work/example` seeded by an older image stays as-is (seeding only
  happens when absent); `rm -rf ~/adk-work/example` to get the current
  one on next start.

## Updating / testing a tool release

```bash
# bump one tool to its latest upstream branch
git submodule update --remote tools/adk
./build.sh && git commit -am "Bump adk to <ref>"

# try an arbitrary branch of one tool against the rest of the pinned stack
cd tools/chiplet-studio && git fetch && git checkout feature/x && cd ../..
./build.sh
```

`build.sh` always builds the `verify` stage first: it regenerates the
two-die interposer demo headless through the full pipeline (pcbnew -> writers ->
hyp_to_gds -> assembly DRC), runs the Chiplet Studio test suite and the
plugin test suite inside the image. A broken combination does not produce a
tagged image. `--skip-verify` exists for quick iteration only.

To develop a Python tool against the pinned stack, mount your checkout over
the baked-in copy:

```bash
docker run --rm -it -v ~/git/.../chiplet_kicad_plugin:/opt/adk-tools/chiplet_kicad_plugin ... adk-tools:dev
```

## Notes

- GUIs need an X server; `run.sh` wires `DISPLAY`/Xauthority automatically
  (local X, ThinLinc and WSLg all work). The image itself forces Mesa's
  software path (`LIBGL_ALWAYS_SOFTWARE=1`, `GALLIUM_DRIVER=llvmpipe`, set in
  the Dockerfile) so the GUIs stay stable over remote X without a GPU;
  otherwise KiCad's GL canvas crashes when the host can't provide a DRI3
  device. Pass a real GPU and `-e LIBGL_ALWAYS_SOFTWARE=0` for hardware
  acceleration.
- The image embeds private-repo code: keep it on the private registry.
- KiCad ships the official v9 symbol/footprint libraries (pinned tag,
  `KICAD_LIBS_TAG` build arg). 3D model packages are not included.
- Text editors are bundled for in-container edits to files under `/work`:
  `vim` (terminal) and `featherpad` (a light Qt GUI editor; needs X, like the
  other GUIs).

## License & notices

This repo's own glue (Dockerfile, scripts, docs) is **GPL-3.0-or-later** (`LICENSE`).
The image is a **mere aggregation** of independently licensed tools; each keeps its
own license, and the bundled set spans GPL, Apache-2.0, CC-BY-SA-4.0 and permissive
(MIT/BSD) licenses. See
[`NOTICE.md`](NOTICE.md) for the per-component licenses, source URLs, and the GPL
corresponding-source obligation that applies when you redistribute the image.
