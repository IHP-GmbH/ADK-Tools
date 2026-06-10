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

# Links the ghcr package to the repo: access is then managed in one place
# (people with ADK-Tools access get the image). Keep both private.
LABEL org.opencontainers.image.source=https://github.com/IHP-GmbH/ADK-Tools \
      org.opencontainers.image.description="Heterogeneous integration flow: KiCad fork, Chiplet Studio, PDKs, assembly DRC -- pre-wired"

# SG13G2 PCell runtime dep, installed here (not in deps) so the heavy
# builder caches survive: the PDK's cni PCell API imports tkinter.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-tk \
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
RUN cp /usr/share/kicad/symbols/sym-lib-table /usr/share/kicad/template/ \
    && cp /usr/share/kicad/footprints/fp-lib-table /usr/share/kicad/template/

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

# SG13G2 base-PDK slice (PDK_ROOT): hyp_to_gds self-registers the SG13_dev
# PCell library from here, so vias are real via_stack PCells instead of the
# rectangle fallback.
COPY --from=sg13g2-pdk /pdk /opt/adk-tools/IHP-Open-PDK

# Worker venv. --system-site-packages on purpose: the venv python then also
# sees pcbnew (kicad install) and wx (python3-wxgtk4.0), so one interpreter
# can drive the whole pipeline.
ARG KLAYOUT_PIP=0.30.5
RUN python3 -m venv --system-site-packages /opt/adk-tools/venv \
    && /opt/adk-tools/venv/bin/pip install --no-cache-dir \
        "klayout==${KLAYOUT_PIP}" \
        "PyYAML>=6.0" \
        "PyQt6>=6.6" \
        jinja2 \
        psutil \
        pytest

# Plugin visible to the KiCad GUI for every user
RUN mkdir -p /usr/share/kicad/scripting/plugins \
    && ln -s /opt/adk-tools/chiplet_kicad_plugin /usr/share/kicad/scripting/plugins/chiplet_kicad_plugin

# Command wrappers + interactive-shell banner
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/* \
    && echo '[ -n "$PS1" ] && echo "adk-tools: heterogeneous integration flow image. Run \`adk-tools\` for the tool list."' >> /etc/bash.bashrc

# Version manifest (generated by build.sh, passed base64 to survive quoting)
ARG MANIFEST_B64=e30K
RUN echo "${MANIFEST_B64}" | base64 -d > /opt/adk-tools/manifest.json

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

# 3. Interposer PDK suite (bump mirror PCell regressions).
RUN cd /opt/adk-tools/OpenIntM4TM2/libs.tech/klayout \
    && /opt/adk-tools/venv/bin/python3 -m pytest intm4tm2_tests -q

# 4. Regenerate the wire-bond demo headless (pcbnew + worker venv + ADK DRC).
#    Output lands inside the demo dir, exactly where the studio gated tests
#    expect the sibling layout to provide it. --require-drc: a combo that
#    breaks assembly DRC fails the image build.
RUN python3 /opt/adk-tools/chiplet_kicad_plugin/tests/regenerate_wirebond_demo.py \
        --require-drc \
        --board /opt/adk-tools/examples/interposer_wire_bonding_demo/interposer_wire_bonding_demo.kicad_pcb \
        --output-dir /opt/adk-tools/examples/interposer_wire_bonding_demo

# 5. Chiplet Studio full suite (gated tests resolve the PDK/tool roots via the
#    env baked in deps).
RUN cd /opt/adk-tools/chiplet-studio/build \
    && QT_QPA_PLATFORM=offscreen \
       LD_LIBRARY_PATH=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release \
       WIREBOND_DEMO_CHIPLET=/opt/adk-tools/examples/interposer_wire_bonding_demo/interposer_wire_bonding_demo.chiplet \
       ctest --output-on-failure

# 6. Plugin suite (pcbnew available here, so the env-gated tests run too).
RUN cd /opt/adk-tools/chiplet_kicad_plugin \
    && /opt/adk-tools/venv/bin/python3 -m pytest tests -q

# 7. End-to-end smoke exactly as a user would run it.
RUN adk-smoke
