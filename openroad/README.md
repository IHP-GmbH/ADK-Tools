# OpenROAD 3Dblox sidecar

Host-side tooling to lint an assembly with OpenROAD's `read_3dbx` and
`check_3dblox`. OpenROAD lives in its own pinned image; nothing here is part
of `adk-tools:<tag>`.

## Why a sidecar and not a bundled tool

The exporter already ships in the image (`chiplet2dbx`, from the ADK
submodule). The linter does not, because OpenROAD is a second multi-gigabyte
toolchain and the payoff is narrow (see Scope). Keeping it outside means the
distribution image stays the size it is, and the OpenROAD version is pinned
independently of the suite's release cadence.

`openroad.pin` is the lockfile: upstream repository, exact commit, image tag.
It plays the same role for OpenROAD that the `tools/` submodules play for the
rest of the suite.

## Use

```bash
./openroad/build-image.sh                    # once; JOBS=8 by default
./openroad/check-3dblox.sh path/to/assembly.chiplet
./openroad/verify-live.sh                    # regression gate
```

`build-image.sh` clones OpenROAD at the pinned commit into
`openroad/.openroad-src` (git-ignored, around 3 GB) and builds it with
upstream's own Dockerfile. Set `OPENROAD_SRC` to reuse an existing checkout;
it must already be at the pinned commit, the script will not move someone
else's tree. The build is capped at `JOBS` compile jobs because upstream's
Dockerfile otherwise takes every core on the machine.

`check-3dblox.sh` runs the exporter from the adk-tools image and the linter
from the OpenROAD image, so what gets exercised is the exporter that actually
ships. It exits non-zero on any linter warning, not only on a crash. Pass
`--pins REF=pins.json` (the `gds_to_kicad` artifact) once per die to make the
export bump-aware and activate the bump-alignment check.

`verify-live.sh` runs the ADK's `test_chiplet2dbx.py` suite against the pinned
image in a local virtualenv (`openroad/.venv`, git-ignored). The suite's live
tests skip themselves when the image is missing, which would make a green run
meaningless, so this script requires the image up front and fails if a live
test skipped anyway. The `chiplet-spec` drift guard skips here by design:
that repository is not a submodule of adk-tools.

## Scope: what a clean check_3dblox does and does not mean

`check_3dblox` lints the **declared 3Dblox model**, not the artwork. It is a
structural consistency check, not verification.

Caught, with the geometry this exporter emits:

| finding | code |
|---|---|
| floating chip set | ODB-0151 |
| overlapping chips | ODB-0156 |
| invalid connection region / face pairing | ODB-0207, ODB-0273 |
| bump outside its parent region (needs `--pins`) | ODB-0463 |

Four of the linter's seven checks are reachable from files. Logical
connectivity would need a netlist-to-verilog path, internal-external does not
apply to this geometry, and alignment-markers cannot be fed from files at all
(the rules are only creatable through a Tcl command).

Not caught, measured on a real two-die assembly: a die displaced from its
intended position, two dies swapped, a die flipped face-up, and a physical
open cut into the interposer artwork all pass **clean**. Use the assembly
DRC and LVS stages for those; this linter cannot see them.

The export is also lossy by declaration: metallurgy per layer, connection
method identity, fab DRC parameters, GDS artwork, polygonal boundaries and
wire-bond `io_pads` have no 3Dblox target and are dropped. The reverse
direction (3Dblox to `.chiplet`) is deliberately not provided.

## Related

Chiplet Studio ships an equivalent two-step flow as an in-GUI example
(`examples/openroad_3dblox/` in that repository) for running the same check
from the Flow Pipeline dock. The scripts here are the host-side CLI
equivalent; the Tcl they generate is intentionally identical.

## Known upstream issue

A pure black-box `ChipletDef` with no `APR_tech_file` segfaults `read_3dbv` on
this build: `dbBlock::create` dereferences the chip's technology without a
null check. The exporter works around it by emitting a units-only technology
LEF per `.chiplet` technology. Not reported upstream yet.
