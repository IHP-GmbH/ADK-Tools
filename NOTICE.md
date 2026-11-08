# Third-party notices: adk-tools distribution image

The `adk-tools` repository's own content (the `Dockerfile`, `build.sh`, `run.sh`,
helper scripts and this documentation) is licensed **GPL-3.0-or-later** (see
`LICENSE`), except where a file's own SPDX line says otherwise.

The record format schemas under `formats/`, and the files of ours whose SPDX line says
Apache-2.0 (most of `ci/`, `nix/` and `nix-tail/`, with their tests), are licensed
**Apache-2.0** (text in `formats/LICENSE`). The image contains one of the
schemas, `formats/adk-tools-tool-identity.v1.schema.json`, at
`/opt/adk-tools/formats/`.

The Docker image it builds is a **mere aggregation** of independently developed and
independently licensed tools and data. Each bundled component **retains its own
license**; packaging them together in one image does not relicense any of them, and
the top-level `LICENSE` of this repo does **not** apply to the bundled components.

## Third-party files in this repository

These files are not ours and keep their own license. They carry no header of ours
because each one's bytes are pinned: a header would change a Nix store path or the
tools image key.

| File | Upstream | License (SPDX) | What changed |
|------|----------|----------------|--------------|
| `patches/opensta-timing-model-clocked-output-exists.patch` | OpenSTA (https://github.com/parallaxsw/OpenSTA), at `857316ff` as LibreLane's `nix/opensta.nix` packages it | `GPL-3.0-or-later`, as OpenSTA | a patch of ours to `search/MakeTimingModel.cc`: skips an output transition no path reaches |
| `patches/librelane-ys_common.patch` | LibreLane (https://github.com/librelane/librelane), at `LIBRELANE_REV` in `staging.pin` | `Apache-2.0`, as LibreLane | a patch of ours to `librelane/scripts/pyosys/ys_common.py`: imports `libyosys` the way the Nix Yosys provides it |
| `docker/apt-snapshot/isrg-root-x1.pem` | ISRG Root X1 (https://letsencrypt.org/certs/isrgrootx1.pem) | public certificate, no license | unchanged |

`REUSE.toml` gives the same licences in a form licence tools read. The recorded
fixtures under `tests/nix-tail/nix-2.30.1/` are output of an earlier tree kept
byte for byte: five of them carry the SPDX line they were recorded with
(`Apache-2.0` or `GPL-3.0-or-later`), and those lines stay.

## Redistribution obligation

The image contains **GPL-licensed programs**: the KiCad fork, Chiplet Studio,
the KiCad plugin, gds-to-kicad, the staged assembly-lvs, chiplet-cosim,
chiplet-flow, interposer-pnr and packaging-flow, KLayout (the in-tree build,
the `klayout` PyPI wheel in the worker venv and nix-eda's KLayout), the SG13G2
`pycell4klayout-api` PCell API and PyQt6 (GPLv3) in the worker venv. From the
Nix closures copied into the image it also contains Icarus Verilog, GTKWave,
GAW, Xschem, gnucap and modelgen-verilog, and the OpenSTA inside OpenROAD. The
runtime image additionally apt-installs the `featherpad` (GPL-3.0) and `vim`
editors from Ubuntu. This enumeration covers the primary bundled tools;
OS-level packages installed from Ubuntu are also conveyed under Ubuntu's own
distribution terms.

If you redistribute the image, or any binary built from it, you must, for those
components, also make their **corresponding source** available to recipients
under the terms of their respective GPL licenses:

- Each submodule and external component is pinned to a public upstream (tables
  below); pointing recipients at the exact pinned commit or version satisfies
  this.
- The Nix closure programs are built from the revisions `flake.lock` and
  `flake.nix` pin; `nix/*-closure.txt` list the store paths the image carries.
- The staged components are pinned by commit in `staging.pin`, and those
  commits cannot yet be fetched from a public remote. The image carries each
  staged tree, as a git checkout at its pinned commit, under
  `/opt/adk-tools/staging/<tree>`; whoever redistributes the image passes that
  source on with it.

The permissive (Apache-2.0, MIT, BSD) and CC-licensed components carry their
own, lighter, attribution/notice obligations.

## Bundled components

### Ecosystem tools (pinned as submodules under `tools/`)

| Component | License (SPDX) | Source |
|-----------|----------------|--------|
| KiCad fork | `GPL-3.0-or-later` | https://github.com/IHP-GmbH/KiCad-ADK-MOD |
| Chiplet Studio | `GPL-3.0-or-later` | https://github.com/IHP-GmbH/chiplet-studio |
| Chiplets KiCad Plugin | `GPL-3.0-or-later` | https://github.com/IHP-GmbH/Chiplets-KiCad-Plugin |
| gds-to-kicad | `GPL-3.0-or-later` | https://github.com/IHP-GmbH/gds2kicad |
| IHP-Open-ADK (tooling + assembly DRC) | `Apache-2.0` | https://github.com/IHP-GmbH/IHP-Open-ADK |
| OpenIntM4TM2 (interposer PDK) | `Apache-2.0` | https://github.com/IHP-GmbH/OpenIntM4TM2 |
| IHP-Interconnect-IntM4TM2 (interconnect PDK) | `Apache-2.0` | https://github.com/IHP-GmbH/IHP-Interconnect-IntM4TM2 |

### Staged components (installed from local checkouts at the commits `staging.pin` pins)

A tree's license is its own files' SPDX lines, together with its REUSE file
(`REUSE.toml`, or the older `.reuse/dep5`) and its `.license` sidecars for
files that carry no line of their own; a license file below a tree's root
covers its directory. The exceptions name each file or directory whose
license differs.

| Component (tree) | License (SPDX) | Exceptions | Source |
|------------------|----------------|------------|--------|
| assembly-lvs | `GPL-3.0-or-later` | none | not public yet |
| chiplet-cosim | `GPL-3.0-or-later` | `Apache-2.0`: `src/chiplet_cosim/cosim_cocotb_observer.py`, `src/chiplet_cosim/mixed_run.py`, and the test copies from IHP__CMP7345, IHP__OTA1336 and IHP-Open-PDK: `tests/fixtures/cx_mixed_two_die/die_b_cmp7345/views/cdl/3_CMP7345.cdl`, `tests/fixtures/cx_mixed_two_die/die_b_cmp7345/views/spice/1_CMP7345.spice`, `tests/fixtures/sigma_delta/b/CMP7345-final.spice`, `tests/fixtures/sigma_delta/b/OTA1336-final.spice`, `tests/fixtures/hv_gate/models/`, `tests/fixtures/hv_gate/cdl/`, `tests/fixtures/xschem_rows/sg13g2_pr/`, `tests/fixtures/xschem_rows/cdl/1_cells.cdl` (`.reuse/dep5` lists each); `Apache-2.0`, versioned formats: `src/chiplet_cosim/cosim_setup_symbols.json`, `src/chiplet_cosim/cosim_readings.json` | not public yet |
| chiplet-flow | `GPL-3.0-or-later` | `Apache-2.0`, vendored upstream pads: `ip/bondpad_70x70_novias_cf/`, `vehicle/ip/bondpad_70x70/`, the pad files of `ip/bondpad_70x70_5L_cf/` (`gds/` and the derived access LEF under `lef/`), and the pad files of `ip/gf180mcu_bondpad_5lm_cf/` (`gds/`, `lef/`, `gf180mcu_bondpad_5lm.artwork.json.gz`; its `extract_wrapper.py` is under the row licence); `Apache-2.0`, byte copy of the picorv32a-cmos5l-librelane example: `vehicle/bondpad_only/picorv32a_cmos5l/core/chip_core.sv`; `ISC`, PicoRV32 (its header carries the notice): `vehicle/bondpad_only/picorv32a_cmos5l/core/picorv32a.v`; `Apache-2.0`, IHP-Open-PDK's track file with two lines changed: `vehicle/bondpad_only/picorv32a_cmos5l/tracks.info` (`vehicle/bondpad_only/picorv32a_cmos5l/core/PROVENANCE.md` names each source; the tree's `REUSE.toml` does not annotate them yet) | not public yet |
| interposer-pnr | `GPL-3.0-or-later` | `Apache-2.0`, a format: `src/interposer_pnr/schema/interposer_pnr_signoff_check.schema.json`; `Apache-2.0`, routing descriptors transcribed from OpenIntM4TM2 and lambdapdk: `src/interposer_pnr/descriptors/ihp_sg13g2_intm4tm2.routing_tech.json`, `src/interposer_pnr/descriptors/ihp_sg13g2_intm4tm2_rdl2.routing_tech.json`, `src/interposer_pnr/descriptors/lambdapdk_interposer_5ml_0800.routing_tech.json`; `Apache-2.0`, a copy from chiplet-spec: `tests/fixtures/top_level_blocks_cases.json` (these four in `REUSE.toml`); `Apache-2.0`, a copy of chiplet-spec's reference reader, each file with its own SPDX line: `src/interposer_pnr/_vendor/chiplet_format_io/`; test copies under `tests/data/frozen/`, named in its `docs/DEVELOPMENT.md` and `MANIFEST.json`: `Apache-2.0` from OpenIntM4TM2, `tests/data/frozen/interposer_pdk/`; `GPL-3.0-or-later` from ADK-Tools, `tests/data/frozen/adk_tools_two_die_interposer/` | not public yet |
| packaging-flow | `GPL-3.0-or-later` | `Apache-2.0`, format copies: `src/packaging_flow/messages/messages.v1.schema.json`, `src/packaging_flow/verification/schema/adk-tools-tool-identity.v1.schema.json`, `tests/fixtures/signoff_check/interposer_pnr_signoff_check.schema.json`; `Apache-2.0`, upstream copies of IHP-Interconnect-IntM4TM2, each with its `LICENSE`: `examples/<example>/inputs/registry/interconnect_pdk/`, for each of `mixed_gf180`, `two_die_echo_gf180`, `two_die_echo_shifted_gf180` and `two_die_sigma_delta_gf180`; `Apache-2.0`, copy from chiplet-flow: `vehicle/ip/bondpad_70x70/` (from ihp-sg13g2-librelane-template, its `LICENSE.upstream`); `Apache-2.0`, fixtures from OpenIntM4TM2: `tests/fixtures/interposer_pdk/`, `tests/fixtures/verification/interposer_drc/`; `Apache-2.0`, fixture from chiplet-cosim: `tests/fixtures/verification/chiplet_cosim/` (each fixture's `PROVENANCE.md` names its source) | not public yet |
| chiplet-spec (including the system description, `description/`) | `Apache-2.0` | none | https://github.com/IHP-GmbH/chiplet-system-spec (the pinned commit is not pushed yet) |

### External components built or fetched by the `Dockerfile`

| Component | License (SPDX) | Source |
|-----------|----------------|--------|
| KLayout (built in-tree under Chiplet Studio) | `GPL-2.0-or-later` | https://github.com/KLayout/klayout |
| KiCad official symbol library (`kicad-symbols`, tag `9.0.9.1`) | `CC-BY-SA-4.0` with the KiCad library exception | https://gitlab.com/kicad/libraries/kicad-symbols |
| KiCad official footprint library (`kicad-footprints`, tag `9.0.9.1`) | `CC-BY-SA-4.0` with the KiCad library exception | https://gitlab.com/kicad/libraries/kicad-footprints |
| IHP-Open-PDK SG13G2 KLayout slice (`sg13g2_pycell_lib`, `.lyt`/`.lyp`/`.map`) | `Apache-2.0` | https://github.com/IHP-GmbH/IHP-Open-PDK |
| pycell4klayout-api (SG13G2 PCell API; PDK slice) | `GPL-3.0-only` | https://github.com/IHP-GmbH/pycell4klayout-api |
| pypreprocessor (SG13G2 PCell preprocessor; PDK slice) | `MIT` | https://github.com/IHP-GmbH/pypreprocessor |
| KLayout Python module (`klayout` PyPI wheel, pinned `KLAYOUT_PIP`; worker venv) | `GPL-2.0-or-later` | https://github.com/KLayout/klayout |
| PyQt6 (worker venv; gds-to-kicad GUI) | `GPL-3.0-only` or commercial | https://www.riverbankcomputing.com/software/pyqt/ |
| PyYAML, Jinja2, jsonschema, psutil, pytest (worker venv) | permissive (MIT / BSD-3-Clause) | PyPI |
| featherpad (GUI text editor; apt) | `GPL-3.0-or-later` | Ubuntu 24.04 package (https://github.com/tsujan/FeatherPad) |
| vim (terminal editor; apt) | Vim license (GPL-compatible, charityware) | Ubuntu 24.04 package (https://www.vim.org) |
| Base OS image | various (Ubuntu 24.04) | https://hub.docker.com/_/ubuntu |

> The KiCad library exception lets the symbol/footprint libraries be used in your own
> designs without your design output inheriting CC-BY-SA-4.0; the libraries themselves
> remain CC-BY-SA-4.0.

> Note: `gds-to-kicad` moved to `IHP-GmbH/gds2kicad`; its old repository is no
> longer reachable. The submodule and this notice point at the live source.

For the authoritative, version-exact license texts, see each component's own
`LICENSE`/`COPYING` file at the pinned commit. Chiplet Studio additionally ships a
detailed `THIRD-PARTY-LICENSES.md` covering the libraries linked into its binary.
