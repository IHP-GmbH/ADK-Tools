# ADK-Tools

One Docker image that carries every tool of the IHP Open ADK flow, pinned
together and callable by name: KiCad with the chiplet export plugin, KLayout,
the ADK assembly checks, Chiplet Studio, the interposer and die flows, OpenROAD
and LibreLane, and the simulation engines. `build.sh` builds it and checks the
combination before it tags anything; `run.sh` starts it.

## Use

### Build

Building needs Docker and Nix on the host, checkouts of the sibling
repositories `staging.pin` lists at the commits it pins (beside this
repository, or in the directory `STAGE_SIBLINGS` names), and the `die-views/`
folder of an adk-examples checkout (`STAGE_DIE_VIEWS_DIR`). The two `--check`
calls find a missing piece in seconds.

```bash
# the clone lives at ADK_TOOLS_ROOT, outside ADK_WORK; the
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

## Updating an existing clone

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

## Documentation

The full documentation is at
https://ihp-open-adk-docs.readthedocs.io/en/latest/tools/07_adk_tools_image.html,
and how to build and run the image at
https://ihp-open-adk-docs.readthedocs.io/en/latest/install/01_container.html.
Maintaining this repository: `docs/MAINTAINING.md`.

## Release

Released as part of the IHP Open ADK. The release page lists the commit of every
repository in each release:
https://ihp-open-adk-docs.readthedocs.io/en/latest/install/00_release.html

## License

GPL-3.0-or-later. See `LICENSE`. A file's own `SPDX-License-Identifier` line
decides its licence, and `NOTICE.md` lists the licences of the tools the image
bundles and the source obligation when you redistribute it.
