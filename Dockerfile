# adk-tools: single distributable image for the heterogeneous integration flow.
#
# Stages:
#   deps           shared apt layer (build deps + runtime libs, one layer for all stages)
#   kicad-builder  compiles the KiCad fork (wxPython scripting ON -> headless pcbnew)
#   studio-builder builds KLayout in-tree + Chiplet Studio at its final path
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
ARG JOBS=16

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

# Ecosystem discovery roots (env is the first link of every tool's discovery
# chain: env -> textvar -> sibling walk -> loud). Set here so all stages,
# including verify, resolve identically.
ENV ADK_TOOLS=/opt/adk-tools \
    ADK_ROOT=/opt/adk-tools/adk \
    INTERPOSER_PDK_ROOT=/opt/adk-tools/interposer \
    INTERCONNECT_PDK_ROOT=/opt/adk-tools/interconnect_pdk \
    GDS_TO_KICAD_ROOT=/opt/adk-tools/gds_to_kicad \
    KICAD_CHIPLET_PYTHON=/opt/adk-tools/venv/bin/python3

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
FROM deps AS runtime

# KiCad fork (kicad, pcbnew, kicad-cli in /usr/bin; pcbnew python module in
# /usr/lib/python3/dist-packages -> headless plugin pipeline works)
COPY --from=kicad-builder /install/ /

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
COPY tools/interposer /opt/adk-tools/interposer
COPY tools/interconnect_pdk /opt/adk-tools/interconnect_pdk
COPY tools/kicad_designs /opt/adk-tools/kicad_designs

# Worker venv. --system-site-packages on purpose: the venv python then also
# sees pcbnew (kicad install) and wx (python3-wxgtk4.0), so one interpreter
# can drive the whole pipeline.
ARG KLAYOUT_PIP=0.30.5
RUN python3 -m venv --system-site-packages /opt/adk-tools/venv \
    && /opt/adk-tools/venv/bin/pip install --no-cache-dir \
        "klayout==${KLAYOUT_PIP}" \
        "PyYAML>=6.0" \
        "PyQt6>=6.6" \
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
CMD ["bash"]

############################################################################
FROM runtime AS verify

# Full studio tree (sources, fixtures, ctest metadata, test binaries) layered
# over the lean copy, same absolute paths as studio-builder.
COPY --from=studio-builder /opt/adk-tools/chiplet-studio /opt/adk-tools/chiplet-studio

# 1. Regenerate the wire-bond demo headless (pcbnew + worker venv + ADK DRC).
#    Output lands inside the demo dir, exactly where the studio gated tests
#    expect the sibling layout to provide it. --require-drc: a combo that
#    breaks assembly DRC fails the image build.
RUN python3 /opt/adk-tools/chiplet_kicad_plugin/tests/regenerate_wirebond_demo.py \
        --require-drc \
        --output-dir /opt/adk-tools/kicad_designs/interposer_wire_bonding_demo

# 2. Chiplet Studio full suite (gated tests resolve the PDK/tool roots via the
#    env baked in deps).
RUN cd /opt/adk-tools/chiplet-studio/build \
    && QT_QPA_PLATFORM=offscreen \
       LD_LIBRARY_PATH=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release \
       WIREBOND_DEMO_CHIPLET=/opt/adk-tools/kicad_designs/interposer_wire_bonding_demo/interposer_wire_bonding_demo.chiplet \
       ctest --output-on-failure

# 3. Plugin suite (pcbnew available here, so the env-gated tests run too).
RUN cd /opt/adk-tools/chiplet_kicad_plugin \
    && /opt/adk-tools/venv/bin/python3 -m pytest tests -q

# 4. End-to-end smoke exactly as a user would run it.
RUN adk-smoke
