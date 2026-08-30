# adk-tools: single distributable image for the heterogeneous integration flow.
#
# Stages:
#   deps           shared apt layer (build deps + runtime libs, one layer for all stages)
#   kicad-builder  compiles the KiCad fork (wxPython scripting ON -> headless pcbnew)
#   studio-builder builds KLayout in-tree + Chiplet Studio at its final path
#   kicad-libs     official v9 symbol/footprint libraries (pinned tag)
#   sg13g2-pdk     SG13G2 base-PDK KLayout slice (PCell library, pinned refs)
#   runtime        lean image: kicad in /usr, tools under /opt/adk-tools, venv, wrappers
#   verify         FROM runtime: regenerates the demo headless, runs studio ctest,
#                  plugin pytest and adk-smoke. Default target, so a plain
#                  `docker build .` never produces an untested image.
#
# Build through ./build.sh (generates the version manifest and tags the lean
# runtime image after verify passes).

# Parallel compile jobs for the heavy stages (kicad-builder and studio-builder
# can overlap; 2 x nproc jobs on big machines exhausts RAM). Override per
# machine: --build-arg JOBS=N (build.sh: JOBS=N ./build.sh).
ARG JOBS=8

############################################################################
FROM ubuntu:24.04 AS deps
ENV DEBIAN_FRONTEND=noninteractive

# Union of: KiCad fork build deps (see tools/kicad/Dockerfile), Chiplet Studio /
# KLayout build deps (see tools/chiplet-studio/docker/Dockerfile.build) and
# runtime utilities. One fat layer shared by every stage: zero missing-lib
# hunts, maximum cache reuse.
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    ninja-build \
    gettext \
    git \
    curl \
    wget \
    file \
    ca-certificates \
    pkg-config \
    \
    libglm-dev \
    zlib1g-dev \
    libzstd-dev \
    libcurl4-openssl-dev \
    libspnav-dev \
    libcairo2-dev \
    libpixman-1-dev \
    libgit2-dev \
    libboost-all-dev \
    libfreetype-dev \
    libharfbuzz-dev \
    libfontconfig1-dev \
    libngspice0-dev \
    libocct-data-exchange-dev \
    libocct-draw-dev \
    libocct-foundation-dev \
    libocct-modeling-algorithms-dev \
    libocct-modeling-data-dev \
    libocct-ocaf-dev \
    libocct-visualization-dev \
    protobuf-compiler \
    libprotobuf-dev \
    swig \
    python3 \
    python3-dev \
    python3-venv \
    python3-pip \
    python3-wxgtk4.0 \
    libwxgtk3.2-dev \
    libwxgtk-webview3.2-dev \
    libglew-dev \
    libgl1-mesa-dev \
    libglu1-mesa-dev \
    libnng-dev \
    unixodbc-dev \
    libgtk-3-dev \
    libsecret-1-dev \
    libpoppler-dev \
    libpoppler-glib-dev \
    libpoppler-cpp-dev \
    \
    qt6-base-dev \
    qt6-base-dev-tools \
    libqt6opengl6-dev \
    libqt6core5compat6-dev \
    libyaml-cpp-dev \
    ruby \
    ruby-dev \
    \
    xvfb \
    xauth \
    libxkbcommon-x11-0 \
    libxcb-cursor0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-xinerama0 \
    libxcb-xfixes0 \
    && rm -rf /var/lib/apt/lists/*

# KLayout's qmake-based build expects plain `qmake`
RUN ln -sf /usr/bin/qmake6 /usr/bin/qmake

############################################################################
FROM deps AS kicad-builder
ARG JOBS

COPY tools/kicad /src/kicad

RUN cmake -G Ninja -S /src/kicad -B /build/kicad \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr \
        -DKICAD_SCRIPTING_WXPYTHON=ON \
        -DKICAD_USE_OCC=ON \
        -DKICAD_SPICE=ON \
        -DKICAD_BUILD_QA_TESTS=OFF \
    && ninja -C /build/kicad -j"${JOBS}" \
    && DESTDIR=/install ninja -C /build/kicad -j"${JOBS}" install

############################################################################
FROM deps AS studio-builder
ARG JOBS

# Build at the final runtime path: CMake bakes CONFIGS_DIR (absolute) into the
# binary, and ctest metadata in build/ keeps working in the verify stage.
COPY tools/chiplet-studio /opt/adk-tools/chiplet-studio
WORKDIR /opt/adk-tools/chiplet-studio

# gtest_discover_tests executes the test binary at build time; make the
# in-tree KLayout libs resolvable for it.
ENV LD_LIBRARY_PATH=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release

RUN cd extern/klayout && ./build.sh -j"${JOBS}" -without-qtbinding

RUN mkdir -p build && cd build \
    && cmake .. -DKLAYOUT_BUILD_DIR=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release \
    && make -j"${JOBS}"

############################################################################
# Official KiCad symbol/footprint libraries, pinned release tags. The fork
# is 9.99 (v10 dev line) but predates the .kicad_symdir format, so the v9
# libraries are the compatible set (.kicad_sym + ${KICAD9_*_DIR} tables).
# 3D models (multi-GB) are deliberately not shipped.
FROM ubuntu:24.04 AS kicad-libs
ARG KICAD_LIBS_TAG=9.0.9.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --branch "${KICAD_LIBS_TAG}" \
        https://gitlab.com/kicad/libraries/kicad-symbols.git /libs/symbols \
    && git clone --depth 1 --branch "${KICAD_LIBS_TAG}" \
        https://gitlab.com/kicad/libraries/kicad-footprints.git /libs/footprints \
    && rm -rf /libs/symbols/.git /libs/footprints/.git

############################################################################
# SG13G2 base-PDK KLayout slice: the SG13_dev PCell library (hyp_to_gds
# instantiates its via_stack for vias) + tech files. A few MB, not the
# multi-GB PDK. pycell4klayout-api and pypreprocessor are submodules of
# IHP-Open-PDK, cloned here at the gitlink pins of SG13G2_REF. The PDK's
# Apache-2.0 LICENSE ships with the slice.
FROM ubuntu:24.04 AS sg13g2-pdk
# IHP-Open-PDK dev 2026-05-22 + its submodule gitlinks
ARG SG13G2_REF=efe8364456a5c5d0042c15429ac88f2ae9f41b4f
ARG PYCELL_API_REF=99f469aa348201536b2f3b55c69e58969bd4847b
ARG PYPREPROCESSOR_REF=cf1ff9bad0fb5338cf1c5b990b2b816b1ea01a64
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git init -q /pdk \
    && git -C /pdk remote add origin https://github.com/IHP-GmbH/IHP-Open-PDK.git \
    && git -C /pdk fetch -q --depth 1 origin "${SG13G2_REF}" \
    && git -C /pdk checkout -q FETCH_HEAD -- \
        LICENSE \
        ihp-sg13g2/libs.tech/klayout/python/sg13g2_pycell_lib \
        ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyt \
        ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyp \
        ihp-sg13g2/libs.tech/klayout/tech/sg13g2.map \
    && git clone -q https://github.com/IHP-GmbH/pycell4klayout-api.git \
        /pdk/ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api \
    && git -C /pdk/ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api \
        checkout -q "${PYCELL_API_REF}" \
    && git clone -q https://github.com/IHP-GmbH/pypreprocessor.git \
        /pdk/ihp-sg13g2/libs.tech/klayout/python/pypreprocessor \
    && git -C /pdk/ihp-sg13g2/libs.tech/klayout/python/pypreprocessor \
        checkout -q "${PYPREPROCESSOR_REF}" \
    && rm -rf /pdk/.git \
        /pdk/ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api/.git \
        /pdk/ihp-sg13g2/libs.tech/klayout/python/pypreprocessor/.git

############################################################################
FROM deps AS runtime

# Point the built image at its source repo (org.opencontainers.image.source) so
# a locally-built image links back to the code it was built from.
LABEL org.opencontainers.image.source=https://github.com/IHP-GmbH/ADK-Tools \
      org.opencontainers.image.description="Heterogeneous integration flow: KiCad fork, Chiplet Studio, PDKs, assembly DRC -- pre-wired"

# Runtime-only apt, kept out of `deps` so the heavy builder caches survive:
#   python3-tk  the SG13G2 cni PCell API imports tkinter
#   vim         terminal editor for quick edits inside the container
#   featherpad  lightweight GUI text editor (Qt) for editing files under /work
# Editors are a deliberate runtime convenience: /work is the host-shared area, so
# users edit project files in-container without a second terminal on the host.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-tk vim featherpad \
    && rm -rf /var/lib/apt/lists/*

# Ecosystem discovery roots (env is the first link of every tool's discovery
# chain: env -> textvar -> sibling walk -> loud). PDK dirs carry their IHP
# repository names. Kept out of `deps` so env changes never invalidate the
# builder caches; `verify` inherits them via FROM runtime.
ENV ADK_TOOLS=/opt/adk-tools \
    ADK_ROOT=/opt/adk-tools/adk \
    INTERPOSER_PDK_ROOT=/opt/adk-tools/OpenIntM4TM2 \
    INTERCONNECT_PDK_ROOT=/opt/adk-tools/IHP-Interconnect-IntM4TM2 \
    GDS_TO_KICAD_ROOT=/opt/adk-tools/gds_to_kicad \
    PDK_ROOT=/opt/adk-tools/IHP-Open-PDK \
    KICAD_CHIPLET_PYTHON=/opt/adk-tools/venv/bin/python3

# Register the interposer KLayout technology (intm4tm2.lyt + .lyp under
# tech/) so `klayout` opens generated interposer/assembly GDS with named,
# colored layers. First path component = writable config home (run.sh sets
# HOME=/tmp, so this matches KLayout's default location). The interposer
# tree is safe on the path: its only macro is autorun=false. The sg13g2 PDK
# slice deliberately stays OFF this path -- its pycell autorun would need
# system-level psutil/tkinter and pops error dialogs in the GUI.
ENV KLAYOUT_PATH=/tmp/.klayout:/opt/adk-tools/OpenIntM4TM2/libs.tech/klayout

# Force Mesa's software renderer (llvmpipe) for every GUI. The container is run
# without a GPU device (run.sh does not pass /dev/dri), and remote X servers
# such as ThinLinc advertise GLX but cannot provide a DRI3 device. KiCad's GAL
# then probes the hardware path, storms pixman with invalid rectangles and
# segfaults the instant a GL canvas is built (the symbol-chooser preview is the
# usual trigger). llvmpipe is always available here (libgl1-mesa-dri) and gives
# every tool -- KiCad, Chiplet Studio, gds-to-kicad, KLayout -- a stable context.
# Override with `-e LIBGL_ALWAYS_SOFTWARE=0` if you pass a real GPU. NO_AT_BRIDGE
# silences the harmless at-spi accessibility-bus warning in headless sessions.
ENV LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe \
    NO_AT_BRIDGE=1

# KiCad fork (kicad, pcbnew, kicad-cli in /usr/bin; pcbnew python module in
# /usr/lib/python3/dist-packages -> headless plugin pipeline works)
COPY --from=kicad-builder /install/ /

# Standard libraries + their default global tables (the repos ship them);
# the env pins resolution regardless of KiCad's built-in defaults.
COPY --from=kicad-libs /libs/symbols/ /usr/share/kicad/symbols/
COPY --from=kicad-libs /libs/footprints/ /usr/share/kicad/footprints/
ENV KICAD9_SYMBOL_DIR=/usr/share/kicad/symbols \
    KICAD9_FOOTPRINT_DIR=/usr/share/kicad/footprints
# Keep the full SYMBOL table; seed a small CURATED global FOOTPRINT table. The
# startup stall was footprint enumeration: the stock 155 libraries hold ~15k
# .kicad_mod re-read on every launch (HOME=/tmp keeps no fp-info-cache between
# runs). Symbols are cheap (223 files, ~40ms) so all stock symbol libraries
# (power: GND/VCC/VDD/..., Device, Connector, etc.) stay available. Footprints
# get only a useful basic set (mechanical + SMD discretes + a common header,
# ~1.5k footprints) so startup stays fast; the chiplet flow resolves interposer
# footprints from each project's own ${KIPRJMOD} library, and the full footprint
# libraries remain on disk under /usr/share/kicad/footprints (re-addable from
# Preferences > Manage Footprint Libraries).
RUN mkdir -p /usr/share/kicad/template \
    && cp /usr/share/kicad/symbols/sym-lib-table /usr/share/kicad/template/ \
    && { echo '(fp_lib_table'; echo '  (version 7)'; \
         for lib in MountingHole TestPoint Fiducial Resistor_SMD Capacitor_SMD Inductor_SMD LED_SMD Diode_SMD Connector_PinHeader_2.54mm; do \
           printf '  (lib (name "%s")(type "KiCad")(uri "${KICAD9_FOOTPRINT_DIR}/%s.pretty")(options "")(descr ""))\n' "$lib" "$lib"; \
         done; \
         echo ')'; } > /usr/share/kicad/template/fp-lib-table

# Also seed KiCad's common config so the "starting for the first time" setup
# wizard never appears. HOME=/tmp keeps no config between --rm runs. The fork's
# start wizard (common/startwizard) fires if ANY of three providers needs input:
#   settings  -> satisfied once kicad_common.json exists;
#   libraries -> needs the sym, fp AND design-block global tables to be valid;
#   privacy   -> needs update_check_prompt + data_collection_prompt marked "seen".
# kicad-cli writes a portable kicad_common.json + kicad.json; flip the two privacy
# flags and add the (empty) design-block table, then bake all of it as templates
# for the entrypoint to copy into the runtime HOME alongside the lib tables.
RUN HOME=/tmp QT_QPA_PLATFORM=offscreen kicad-cli version >/dev/null 2>&1 \
    && sed -i 's/"update_check_prompt": false/"update_check_prompt": true/; \
               s/"data_collection_prompt": false/"data_collection_prompt": true/' \
           /tmp/.config/kicad/9.99/kicad_common.json \
    && cp /tmp/.config/kicad/9.99/kicad_common.json \
          /tmp/.config/kicad/9.99/kicad.json \
          /usr/share/kicad/template/ \
    && printf '(design_block_lib_table\n  (version 7)\n)\n' \
          > /usr/share/kicad/template/design-block-lib-table \
    && rm -rf /tmp/.config /tmp/.local /tmp/.cache

# Chiplet Studio: binary + configs + embedded-python module (PYTHON_MODULE_DIR
# is baked as build/python) + the in-tree KLayout (libs + klayout CLI)
COPY --from=studio-builder /opt/adk-tools/chiplet-studio/build/chiplet-studio /opt/adk-tools/chiplet-studio/build/chiplet-studio
COPY --from=studio-builder /opt/adk-tools/chiplet-studio/build/python /opt/adk-tools/chiplet-studio/build/python
COPY --from=studio-builder /opt/adk-tools/chiplet-studio/configs /opt/adk-tools/chiplet-studio/configs
COPY --from=studio-builder /opt/adk-tools/chiplet-studio/extern/klayout/bin-release /opt/adk-tools/chiplet-studio/extern/klayout/bin-release

# Python tools + PDKs (data) as siblings under /opt/adk-tools, mirroring the
# development layout the discovery walk expects.
COPY tools/chiplet_kicad_plugin /opt/adk-tools/chiplet_kicad_plugin
COPY tools/gds_to_kicad /opt/adk-tools/gds_to_kicad
COPY tools/adk /opt/adk-tools/adk
COPY tools/OpenIntM4TM2 /opt/adk-tools/OpenIntM4TM2
COPY tools/IHP-Interconnect-IntM4TM2 /opt/adk-tools/IHP-Interconnect-IntM4TM2
COPY examples /opt/adk-tools/examples

# Version-stamp the seeded demo. adk-entrypoint compares this against the copy
# in /work/example and refreshes it when a newer image ships a changed demo
# (the plain "seed if absent" rule otherwise lets the first-ever seed shadow
# every later image). Content hash over the committed tree: stable across
# rebuilds, changes only when the demo actually changes.
RUN find /opt/adk-tools/examples/two_die_interposer -type f \
        -not -name .seed-version | LC_ALL=C sort \
    | xargs -r sha256sum | sha256sum | cut -d' ' -f1 \
    > /opt/adk-tools/examples/two_die_interposer/.seed-version

# SG13G2 base-PDK slice (PDK_ROOT): hyp_to_gds self-registers the SG13_dev
# PCell library from here, so vias are real via_stack PCells instead of the
# rectangle fallback.
COPY --from=sg13g2-pdk /pdk /opt/adk-tools/IHP-Open-PDK

# Worker venv. --system-site-packages on purpose: the venv python then also
# sees pcbnew (kicad install) and wx (python3-wxgtk4.0), so one interpreter
# can drive the whole pipeline.
# The direct deps are exact-pinned to the known-good versions resolved by the
# prior green build (transitive deps still follow pip's resolver; not locked).
# Pinning only klayout left the behaviour-defining packages free to drift against
# live PyPI on a cache-cold rebuild, which could silently flip a verify-stage
# suite; bump a pin deliberately, in its own commit.
#
# KLAYOUT_PIP is the ecosystem's one KLayout version, not just this venv's.
# Testing a deck against a different KLayout than the one the image runs proves
# the deck works somewhere other than where it ships, so these agree with it and
# have to be bumped in the same change:
#   chiplet-studio  extern/klayout submodule tag       (the in-tree build)
#   IHP-Open-ADK    .github/workflows/tests.yml        (env KLAYOUT_VERSION)
#   IHP-Open-ADK-docs  docs/requirements.txt           (klayout==)
#   IHP-Open-ADK-docs  docs/install/02_host.rst        (the worker venv block)
#   Chiplets-KiCad-Plugin  plugins/chiplet_export/requirements.txt  (the floor)
ARG KLAYOUT_PIP=0.30.5
ARG PYYAML_PIP=6.0.3
ARG PYQT6_PIP=6.11.0
ARG JINJA2_PIP=3.1.6
ARG JSONSCHEMA_PIP=4.26.0
ARG PSUTIL_PIP=7.2.2
ARG PYTEST_PIP=9.1.0
RUN python3 -m venv --system-site-packages /opt/adk-tools/venv \
    && /opt/adk-tools/venv/bin/pip install --no-cache-dir \
        "klayout==${KLAYOUT_PIP}" \
        "PyYAML==${PYYAML_PIP}" \
        "PyQt6==${PYQT6_PIP}" \
        "jinja2==${JINJA2_PIP}" \
        "jsonschema==${JSONSCHEMA_PIP}" \
        "psutil==${PSUTIL_PIP}" \
        "pytest==${PYTEST_PIP}"

# Plugins visible to the KiCad GUI for every user. The repository is a monorepo
# of plugins, so its root is not a plugin package: KiCad scans this folder for
# packages that register an ActionPlugin, and linking the root would give it a
# directory with no __init__.py and no plugin at all. Link each package, which
# is also what the plugin README prescribes ("the specific plugin directory,
# not the whole repository").
RUN mkdir -p /usr/share/kicad/scripting/plugins \
    && ln -s /opt/adk-tools/chiplet_kicad_plugin/plugins/chiplet_export \
             /usr/share/kicad/scripting/plugins/chiplet_export \
    && ln -s /opt/adk-tools/chiplet_kicad_plugin/plugins/resizer_passive_elements \
             /usr/share/kicad/scripting/plugins/resizer_passive_elements

# Command wrappers + interactive-shell banner
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/* \
    && echo '[ -n "$PS1" ] && echo "adk-tools: heterogeneous integration flow image. Run \`adk-tools\` for the tool list."' >> /etc/bash.bashrc

# Version manifest (generated by build.sh, passed base64 to survive quoting)
ARG MANIFEST_B64=e30K
RUN echo "${MANIFEST_B64}" | base64 -d > /opt/adk-tools/manifest.json

# License + third-party notices travel inside the image: it conveys GPL binaries
# (KiCad fork, Chiplet Studio, KLayout, the plugin, gds-to-kicad), and GPL
# conveyance requires the license text accompany the program. manifest.json names
# each component's pinned upstream for the corresponding-source obligation.
COPY LICENSE NOTICE.md /opt/adk-tools/

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/adk-entrypoint"]
CMD ["bash"]

############################################################################
FROM runtime AS verify

# Full studio tree (sources, fixtures, ctest metadata, test binaries) layered
# over the lean copy, same absolute paths as studio-builder.
COPY --from=studio-builder /opt/adk-tools/chiplet-studio /opt/adk-tools/chiplet-studio

# 1. ADK suite (boundary manifest validation, DRC regressions via the klayout
#    CLI wrapper, DRU generator). Cheap and fail-fast, so it runs first.
RUN cd /opt/adk-tools/adk \
    && /opt/adk-tools/venv/bin/python3 -m pytest tests -q

# 2. gds_to_kicad suite (footprint/symbol writers, blackbox chiplets, GUI
#    pieces under the offscreen Qt platform).
RUN cd /opt/adk-tools/gds_to_kicad \
    && QT_QPA_PLATFORM=offscreen /opt/adk-tools/venv/bin/python3 -m pytest tests -q

# 2b. gds2kicad "prior steps" of the two_die_interposer example: its footprints
#     and symbols are produced from the shipped chiplet GDS + pin list, upstream
#     of any board work. Rerun the reproducible steps and diff against the
#     committed library (symbol + I/O pad reproduced, pin-list fidelity, converter
#     liveness on the die GDS), so an upstream gds2kicad change that silently
#     alters the demo artifacts fails here, before step 4 places them on the board.
RUN QT_QPA_PLATFORM=offscreen /opt/adk-tools/venv/bin/python3 \
        /usr/local/bin/adk-verify-chiplet-artifacts

# 3. Interposer PDK suite (bump mirror PCell regressions).
RUN cd /opt/adk-tools/OpenIntM4TM2/libs.tech/klayout \
    && /opt/adk-tools/venv/bin/python3 -m pytest intm4tm2_tests -q

# 3b. Interconnect PDK suite (manifest contract, schema, 3D body generator).
#     jsonschema is in the verify venv, so the schema check actually runs here.
RUN cd /opt/adk-tools/IHP-Interconnect-IntM4TM2/libs.tech/klayout \
    && /opt/adk-tools/venv/bin/python3 -m pytest interconnect_tests -q

# 4a. Snapshot the tracked (COPY'd) demo outputs -- the exact set the runtime
#     image ships -- then CLEAR the tree so step 4 regenerates into an empty
#     dir. Without the clear, step 4 overwrites in place and any tracked file a
#     fresh regen no longer emits survives untouched, so 4b's staleness check
#     (a committed deliverable the regen does not reproduce) can never fire.
#     The `../chiplets/` die-GDS sibling lives outside outputs/, so it survives
#     the clear; the recorded `../chiplets/Metal_Test.gds` layout still resolves
#     from the emptied output dir, so the die GDS is not bundled and the
#     regenerated .chiplet stays byte-identical to the tracked one for 4b.
RUN cp -a /opt/adk-tools/examples/two_die_interposer/outputs \
        /opt/adk-tools/.demo-tracked-snapshot \
    && find /opt/adk-tools/examples/two_die_interposer/outputs \
        -mindepth 1 -delete

# 4. Regenerate the two-die interposer demo headless (pcbnew + worker venv + ADK
#    DRC). Source board lives under the project `kicad/` dir; all products land in
#    the sibling `outputs/` dir, where the studio gated tests pick up the .chiplet
#    + its co-located interposer/complete GDS. --require-drc: a combo that breaks
#    assembly DRC fails the image build.
RUN /opt/adk-tools/venv/bin/python3 /opt/adk-tools/chiplet_kicad_plugin/plugins/chiplet_export/tests/regenerate_wirebond_demo.py \
        --require-drc \
        --board /opt/adk-tools/examples/two_die_interposer/kicad/two_die_interposer.kicad_pcb \
        --output-dir /opt/adk-tools/examples/two_die_interposer/outputs

# 4b. Reproducibility gate: the shipped (COPY'd) demo deliverables must equal a
#     fresh headless regen -- same die geometry, pillar manifests and boundary
#     geometry. The runtime image bakes the tracked tree, not this regen, so
#     without this a drifted/stale demo (old die bbox, missing *.pillars.json)
#     ships on a green build. Guards the GUI-vs-headless courtyard determinism
#     too: the tracked set is committed from a headless regen, so a headless
#     mismatch here means the writer's die geometry has gone context-dependent
#     again.
RUN adk-verify-demo-reproducible \
        /opt/adk-tools/.demo-tracked-snapshot \
        /opt/adk-tools/examples/two_die_interposer/outputs

# 4c. Carrier foundry DRC. Step 4 gates the ASSEMBLY (dies do not overlap, the
#     interconnect method is respected); it says nothing about whether the
#     interposer we just drew can be made. Run the interposer PDK's own deck on
#     the regenerated carrier and compare against the committed baseline, so an
#     export change that puts geometry off the 5 nm grid, off 0/45/90, or under
#     a width or space minimum fails the image build. The baseline is a debt
#     list with per-rule notes, not a waiver; see the file.
RUN /opt/adk-tools/venv/bin/python3 /usr/local/bin/adk-verify-carrier-drc

# 5. Chiplet Studio full suite (gated tests resolve the PDK/tool roots via the
#    env baked in deps).
RUN cd /opt/adk-tools/chiplet-studio/build \
    && QT_QPA_PLATFORM=offscreen \
       LD_LIBRARY_PATH=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release \
       WIREBOND_DEMO_CHIPLET=/opt/adk-tools/examples/two_die_interposer/outputs/two_die_interposer.chiplet \
       ctest --output-on-failure

# 6. Plugin suites (pcbnew available here, so the env-gated tests run too).
#    The writer fixtures discover the demo board via CHIPLET_WRITER_BOARD /
#    HYPERLYNX_WRITER_BOARD before falling back to hardcoded paths; point them
#    at the board's new kicad/ location so the byte-exact regression tests run
#    instead of self-skipping.
#    One run per plugin package, from inside it, which is what each package's
#    own pytest.ini (testpaths = tests) is written for and what the repository's
#    CI matrix does. A single run from the monorepo root would pick up no ini at
#    all and collect both suites into one rootdir.
RUN for plugin in chiplet_export resizer_passive_elements; do \
        cd "/opt/adk-tools/chiplet_kicad_plugin/plugins/$plugin" \
        && echo "=== plugin suite: $plugin" \
        && CHIPLET_WRITER_BOARD=/opt/adk-tools/examples/two_die_interposer/kicad/two_die_interposer.kicad_pcb \
           HYPERLYNX_WRITER_BOARD=/opt/adk-tools/examples/two_die_interposer/kicad/two_die_interposer.kicad_pcb \
           /opt/adk-tools/venv/bin/python3 -m pytest tests -q \
        || exit 1; \
    done

# 7. End-to-end smoke exactly as a user would run it.
RUN adk-smoke
