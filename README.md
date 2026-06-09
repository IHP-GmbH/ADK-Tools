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

# alternatively, best-effort registry copy (private; small org quota --
# may lag behind main): docker pull ghcr.io/ihp-gmbh/adk-tools:latest

# run (X11 passthrough for the GUIs; ~/adk-work mounted at /work)
./run.sh                              # interactive shell
./run.sh kicad example/interposer_wire_bonding_demo.kicad_pro
./run.sh chiplet-studio example/interposer_wire_bonding_demo.chiplet
./run.sh adk-smoke                    # end-to-end self test
```

Inside the shell, `adk-tools` prints this table with the pinned versions of
the running image.

Work directory layout (`~/adk-work` on the host, `/work` in the container;
override with `ADK_WORK=...`):

```
/work/example              wire-bond demo, seeded on start (disposable)
/work/heterogenic-designs  your persistent work area
```

## Tools

| Command          | Tool                                  | Kind |
|------------------|---------------------------------------|------|
| `kicad`          | KiCad fork (+ `pcbnew`, `kicad-cli`, chiplet export plugin preloaded) | GUI |
| `chiplet-studio` | 3D assembly viewer / `.chiplet` editor | GUI |
| `gds-to-kicad`   | GDS -> KiCad footprint generator       | GUI |
| `hyp-to-gds`     | HyperLynx -> GDS converter             | CLI |
| `adk-drc`        | ADK assembly DRC runner                | CLI |
| `klayout`        | KLayout (version pinned by the studio submodule) | GUI/CLI |
| `adk-smoke`      | Demo export + dual DRC, asserts green  | CLI |

Data roots baked in (and exported as env), named after their IHP
repositories: `INTERPOSER_PDK_ROOT=/opt/adk-tools/OpenIntM4TM2`,
`INTERCONNECT_PDK_ROOT=/opt/adk-tools/IHP-Interconnect-IntM4TM2`,
`PDK_ROOT=/opt/adk-tools/IHP-Open-PDK` (SG13G2 KLayout slice: the
SG13_dev PCell library + tech, pinned in the Dockerfile -- so
`hyp-to-gds` builds vias from real `via_stack` PCells, not the
rectangle fallback), plus `ADK_ROOT` and `GDS_TO_KICAD_ROOT`. The
wire-bond demo lives in this repo under `examples/` (baked at
`/opt/adk-tools/examples`, regenerated and DRC-gated by every verify
build). Python worker venv: `/opt/adk-tools/venv`
(`KICAD_CHIPLET_PYTHON` already points at it).

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
wire-bond demo headless through the full pipeline (pcbnew -> writers ->
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
  (local X, ThinLinc and WSLg all work). Rendering is forced to Mesa's
  software path (`llvmpipe`) so the GUIs stay stable over remote X without a
  GPU -- otherwise KiCad's GL canvas crashes when the host can't provide a
  DRI3 device. Pass a real GPU and `-e LIBGL_ALWAYS_SOFTWARE=0` for hardware
  acceleration.
- The image embeds private-repo code: keep it on the private registry.
- KiCad ships the official v9 symbol/footprint libraries (pinned tag,
  `KICAD_LIBS_TAG` build arg). 3D model packages are not included.
