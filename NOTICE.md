# Third-party notices: adk-tools distribution image

The `adk-tools` repository's own content (the `Dockerfile`, `build.sh`, `run.sh`,
helper scripts and this documentation) is licensed **GPL-3.0-or-later** (see
`LICENSE`).

The Docker image it builds is a **mere aggregation** of independently developed and
independently licensed tools and data. Each bundled component **retains its own
license**; packaging them together in one image does not relicense any of them, and
the top-level `LICENSE` of this repo does **not** apply to the bundled components.

## Redistribution obligation

The image contains **GPL-licensed programs** (the KiCad fork, Chiplet Studio, the
KiCad plugin, gds-to-kicad, and KLayout). If you redistribute the image, or any
binary built from it, you must, for those components, also make their
**corresponding source** available to recipients under the terms of their respective
GPL licenses. Each component is pinned to a public upstream (table below); pointing
recipients at the exact pinned commit, or shipping the source alongside the image,
satisfies this. The permissive (Apache-2.0) and CC-licensed components carry their
own, lighter, attribution/notice obligations.

## Bundled components

### Ecosystem tools (pinned as submodules under `tools/`)

| Component | License (SPDX) | Source |
|-----------|----------------|--------|
| KiCad fork | `GPL-3.0-or-later` | https://github.com/IHP-GmbH/KiCad-ADK-MOD |
| Chiplet Studio | `GPL-3.0-or-later` | https://github.com/IHP-GmbH/chiplet-studio |
| Chiplets KiCad Plugin | `GPL-2.0-or-later` | https://github.com/IHP-GmbH/Chiplets-KiCad-Plugin |
| gds-to-kicad | `GPL-3.0-or-later` | https://github.com/IHP-GmbH/gds2kicad |
| ADK (tooling + assembly DRC) | `Apache-2.0` | https://github.com/IHP-GmbH/ADK |
| OpenIntM4TM2 (interposer PDK) | `Apache-2.0` | https://github.com/IHP-GmbH/OpenIntM4TM2 |
| IHP-Interconnect-IntM4TM2 (interconnect PDK) | `Apache-2.0` | https://github.com/IHP-GmbH/IHP-Interconnect-IntM4TM2 |

### External components built or fetched by the `Dockerfile`

| Component | License (SPDX) | Source |
|-----------|----------------|--------|
| KLayout (built in-tree under Chiplet Studio) | `GPL-3.0-or-later` | https://github.com/KLayout/klayout |
| KiCad official symbol library (`kicad-symbols`, tag `9.0.9.1`) | `CC-BY-SA-4.0` with the KiCad library exception | https://gitlab.com/kicad/libraries/kicad-symbols |
| KiCad official footprint library (`kicad-footprints`, tag `9.0.9.1`) | `CC-BY-SA-4.0` with the KiCad library exception | https://gitlab.com/kicad/libraries/kicad-footprints |
| IHP-Open-PDK SG13G2 KLayout slice (`sg13g2_pycell_lib`, `.lyt`/`.lyp`/`.map`) | `Apache-2.0` | https://github.com/IHP-GmbH/IHP-Open-PDK |
| pycell4klayout-api | see upstream repository | https://github.com/IHP-GmbH/pycell4klayout-api |
| pypreprocessor | see upstream repository | https://github.com/IHP-GmbH/pypreprocessor |
| Base OS image | various (Ubuntu 24.04) | https://hub.docker.com/_/ubuntu |

> The KiCad library exception lets the symbol/footprint libraries be used in your own
> designs without your design output inheriting CC-BY-SA-4.0; the libraries themselves
> remain CC-BY-SA-4.0.

> Note: `gds-to-kicad` moved to `IHP-GmbH/gds2kicad`; the old `Mauricio-xx/gds-to-kicad`
> repository is archived. The submodule and this notice point at the live source.

For the authoritative, version-exact license texts, see each component's own
`LICENSE`/`COPYING` file at the pinned commit. Chiplet Studio additionally ships a
detailed `THIRD-PARTY-LICENSES.md` covering the libraries linked into its binary.
