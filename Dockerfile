# adk-tools: single distributable image for the heterogeneous integration flow.
#
# Stages, in two parts. Above the thin-layer line, the tools: everything this
# repository does not write, built only when one of them changes.
#   deps           shared apt layer (build deps + runtime libs, one layer for all stages)
#   kicad-builder  compiles the KiCad fork (wxPython scripting ON -> headless pcbnew)
#   kicad-libs     official v9 symbol/footprint libraries (pinned tag)
#   sg13g2-pdk     SG13G2 base-PDK KLayout slice (PCell library, pinned refs)
#   tools          FROM deps: runtime apt, KiCad, libraries, PDK slices and
#                  models, the Nix engines and viewers, the venv's third-party
#                  packages, and the locks naming every package in it
# Below it, our code, on the tools image:
#   studio-builder builds KLayout in-tree + Chiplet Studio at its final path
#   runtime        thin: Studio, the submodules, the staged trees installed
#                  offline, wrappers, manifest, die views, tool identity
#   verify         FROM runtime: regenerates the demo headless, runs studio ctest,
#                  plugin pytest and adk-smoke. Default target, so a plain
#                  `docker build .` never produces an untested image.
#
# Build through ./build.sh (generates the version manifest, builds the tools
# image only when ci/tools_key.py gives a key no image carries yet, and tags
# the lean runtime image after verify passes).

# Parallel compile jobs for the heavy stages (kicad-builder and studio-builder
# can overlap; 2 x nproc jobs on big machines exhausts RAM). Override per
# machine: --build-arg JOBS=N (build.sh: JOBS=N ./build.sh).
ARG JOBS=8

# The base image every ubuntu stage starts from, pinned by digest: the
# ubuntu:24.04 manifest index whose amd64 image (config 8bf6fbc9) the image
# has been built on. The tag alone moves upstream, and with it every file the
# base carries (coreutils' stdbuf among them) and the apt layers above it.
ARG UBUNTU_IMAGE=ubuntu:24.04@sha256:786a8b558f7be160c6c8c4a54f9a57274f3b4fb1491cf65146521ae77ff1dc54


# The Ubuntu archive every apt step installs from: the snapshot service's
# view of it at this time (docker/apt-snapshot/apt-snapshot.sh), so a rebuild
# on another day installs the same versions. The tools stage writes what it
# installed to /opt/adk-tools/tools/apt.lock. Move it in its own commit; every
# apt layer, and with them the tools image, is rebuilt.
ARG APT_SNAPSHOT=20261012T090000Z

# The image the part below the thin-layer line builds on. The default is the
# tools stage of this file, so a plain `docker build` builds both parts;
# build.sh passes adk-tools:tools-<key>, the image built for this key, so a
# change below the line leaves the tools image as it is.
ARG TOOLS_IMAGE=tools

############################################################################
FROM ${UBUNTU_IMAGE} AS deps
ENV DEBIAN_FRONTEND=noninteractive

# Union of: KiCad fork build deps (see tools/kicad/Dockerfile), Chiplet Studio /
# KLayout build deps (see tools/chiplet-studio/docker/Dockerfile.build) and
# runtime utilities. One fat layer shared by every stage: zero missing-lib
# hunts, maximum cache reuse.
#
# First, every package the base image carries moves to its version in the
# snapshot (--only-upgrade over all of them, which is apt-get upgrade through
# the snapshot helper): the base image is pinned by digest and lags the
# archive's security updates, and nothing else here would bring them in for a
# package no install below depends on. The snapshot time keeps the versions
# fixed, and apt.lock records them.
ARG APT_SNAPSHOT
RUN --mount=type=bind,source=docker/apt-snapshot,target=/run/apt-snapshot \
    --mount=type=cache,id=adk-tools-apt-snapshot,target=/var/cache/apt-snapshot,sharing=locked \
    sh /run/apt-snapshot/apt-snapshot.sh "$APT_SNAPSHOT" --only-upgrade \
        $(dpkg-query -W -f='${binary:Package}\n') \
    && sh /run/apt-snapshot/apt-snapshot.sh "$APT_SNAPSHOT" \
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
    libxcb-xfixes0

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
# Official KiCad symbol/footprint libraries, pinned release tags. The fork
# is 9.99 (v10 dev line) but predates the .kicad_symdir format, so the v9
# libraries are the compatible set (.kicad_sym + ${KICAD9_*_DIR} tables).
# 3D models (multi-GB) are deliberately not shipped.
FROM ${UBUNTU_IMAGE} AS kicad-libs
ARG APT_SNAPSHOT
ARG KICAD_LIBS_TAG=9.0.9.1
RUN --mount=type=bind,source=docker/apt-snapshot,target=/run/apt-snapshot \
    --mount=type=cache,id=adk-tools-apt-snapshot,target=/var/cache/apt-snapshot,sharing=locked \
    sh /run/apt-snapshot/apt-snapshot.sh "$APT_SNAPSHOT" --no-install-recommends git ca-certificates \
    && git clone --depth 1 --branch "${KICAD_LIBS_TAG}" \
        https://gitlab.com/kicad/libraries/kicad-symbols.git /libs/symbols \
    && git clone --depth 1 --branch "${KICAD_LIBS_TAG}" \
        https://gitlab.com/kicad/libraries/kicad-footprints.git /libs/footprints \
    && rm -rf /libs/symbols/.git /libs/footprints/.git

############################################################################
# SG13G2 base-PDK KLayout slice: the SG13_dev PCell library (hyp_to_gds
# instantiates its via_stack for vias) + tech files + the DRC deck tree
# (tech/drc without its docs/, images/ and testing/), which packaging-flow's
# chiplet.drc reads below $PDK_ROOT; verify step 13 holds that the deck still
# finds every file it reads in what is left. Plus sg13cmos5l's layer sheet,
# the one ihp-sg13cmos5l file packaging-flow's technology table names (verify
# step check_tech_files reads every sheet that table names), and the technology
# file beside it, which packaging-flow's tests hold to sit next to each sheet. A few MB, not the multi-GB PDK; the sg13g2-models
# stage adds the ngspice files a mixed co-simulation case reaches. pycell4klayout-api and pypreprocessor are
# submodules of IHP-Open-PDK. pypreprocessor is cloned here at SG13G2_REF's gitlink.
# pycell4klayout-api is pinned on its own at PYCELL_API_REF (99f469aa),
# behind that PDK's gitlink (2ffdf79f), until a PDK pin change aligns the
# two with an A/B. The PDK's Apache-2.0 LICENSE ships with the slice.
FROM ${UBUNTU_IMAGE} AS sg13g2-pdk
ARG APT_SNAPSHOT
# IHP-Open-PDK dev 2026-09-07 + its two submodules, pinned as said above.
#
# Was efe8364 (dev 2026-05-22). Bumped because the image's PDK has to be the
# demo's PDK: the DRC decks, the sg13g2_io LEF/GDS and the stdcells all come
# from here, so a run in this image would not be reproducing that demo no
# matter how well every tool matched. 586 commits and about four months apart,
# a clean fast-forward (efe8364 is an ancestor).
#
# A sha, deliberately, and not the tip of a branch. The reproduction target is
# one lineage, and "latest remote" is not a pin: it names something different
# every day and silently makes two images that claim to be the same one.
# Verified reachable from the remote fetched below (on origin/dev), because a
# shallow fetch of an unreachable commit fails an hour into the build.
#
# This is a big bump and it is not yet known to be safe: the verify stage runs
# the ADK suite, the demo regen and the assembly DRC against these decks. If
# something moved in those 586 commits, the build is where it shows.
ARG SG13G2_REF=22f43352dd8219f9007eb659e422e0d5fe28c5fb
ARG PYCELL_API_REF=99f469aa348201536b2f3b55c69e58969bd4847b
ARG PYPREPROCESSOR_REF=cf1ff9bad0fb5338cf1c5b990b2b816b1ea01a64
RUN --mount=type=bind,source=docker/apt-snapshot,target=/run/apt-snapshot \
    --mount=type=cache,id=adk-tools-apt-snapshot,target=/var/cache/apt-snapshot,sharing=locked \
    sh /run/apt-snapshot/apt-snapshot.sh "$APT_SNAPSHOT" --no-install-recommends git ca-certificates \
    && git init -q /pdk \
    && git -C /pdk remote add origin https://github.com/IHP-GmbH/IHP-Open-PDK.git \
    && git -C /pdk fetch -q --depth 1 origin "${SG13G2_REF}" \
    && git -C /pdk checkout -q FETCH_HEAD -- \
        LICENSE \
        ihp-sg13g2/libs.tech/klayout/python/sg13g2_pycell_lib \
        ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyt \
        ihp-sg13g2/libs.tech/klayout/tech/sg13g2.lyp \
        ihp-sg13g2/libs.tech/klayout/tech/sg13g2.map \
        ihp-sg13cmos5l/libs.tech/klayout/tech/sg13cmos5l.lyp \
        ihp-sg13cmos5l/libs.tech/klayout/tech/sg13cmos5l.lyt \
        ihp-sg13g2/libs.tech/klayout/tech/drc \
    && rm -r /pdk/ihp-sg13g2/libs.tech/klayout/tech/drc/docs \
        /pdk/ihp-sg13g2/libs.tech/klayout/tech/drc/images \
        /pdk/ihp-sg13g2/libs.tech/klayout/tech/drc/testing \
    && git clone -q https://github.com/IHP-GmbH/pycell4klayout-api.git \
        /pdk/ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api \
    && git -C /pdk/ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api \
        checkout -q "${PYCELL_API_REF}" \
    && git clone -q https://github.com/IHP-GmbH/pypreprocessor.git \
        /pdk/ihp-sg13g2/libs.tech/klayout/python/pypreprocessor \
    && git -C /pdk/ihp-sg13g2/libs.tech/klayout/python/pypreprocessor \
        checkout -q "${PYPREPROCESSOR_REF}" \
# The .git directories go, so record what actually landed before they do, read
# back from each tree rather than echoed from the ARG. build.sh's manifest
# cannot see this PDK at all: it is fetched here, not a submodule, so without
# these files the image cannot say which PDK it carries and `adk-tools` prints
# a dash. Writing the ARG instead would read the same whether the fetch
# resolved to it or not.
    && git -C /pdk rev-parse FETCH_HEAD > /pdk/.shipped-ref \
    && git -C /pdk/ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api \
        rev-parse HEAD \
        > /pdk/ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api/.shipped-ref \
    && git -C /pdk/ihp-sg13g2/libs.tech/klayout/python/pypreprocessor \
        rev-parse HEAD \
        > /pdk/ihp-sg13g2/libs.tech/klayout/python/pypreprocessor/.shipped-ref \
    && test "$(cat /pdk/.shipped-ref)" = "${SG13G2_REF}" \
    && rm -rf /pdk/.git \
        /pdk/ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api/.git \
        /pdk/ihp-sg13g2/libs.tech/klayout/python/pypreprocessor/.git

############################################################################
# SG13G2 ngspice models: the files a mixed co-simulation case reaches for a
# transistor-level ihp-sg13g2 die, listed with their digests in
# sg13g2-models.sha256 (every file the named corner libraries include, in any
# section and at any depth, since ngspice reads a library whole; and the
# stdcell spice; ci/check_sg13g2_models.py recomputes that closure at
# SG13G2_REF). Fetched at the ref the slice stage shipped, so SG13G2_REF
# stays the one pin; exactly the listed files are fetched, and each is checked.
# A stage of their own for the GF180 models' reason: the tools stage takes them in
# a late layer.
FROM sg13g2-pdk AS sg13g2-models
COPY sg13g2-models.sha256 /sg13g2-models.sha256
RUN ref="$(cat /pdk/.shipped-ref)" \
    && git init -q /models \
    && git -C /models remote add origin https://github.com/IHP-GmbH/IHP-Open-PDK.git \
    && git -C /models fetch -q --depth 1 --filter=blob:none origin "${ref}" \
    && test "$(git -C /models rev-parse FETCH_HEAD)" = "${ref}" \
    && grep -v '^#' /sg13g2-models.sha256 | awk '{print $2}' \
        | xargs -r git -C /models checkout -q FETCH_HEAD -- \
    && grep -v '^#' /sg13g2-models.sha256 | (cd /models && sha256sum -c --quiet) \
    && rm -rf /models/.git

############################################################################
# GF180MCU layer sheet, so a technology entry can name it and Chiplet Studio
# colours a GF180 die's layers. Not the PDK: no PCells or libraries, and no
# KLayout technology is registered from it; besides the sheet the image carries
# only the two ngspice files and the DRC and LVS decks the next stages add.
# Kept in the PDK's own
# layout (gf180mcuD/libs.tech/...) so GF180MCU_PDK_ROOT can point at a full
# checkout on a host instead.
FROM ${UBUNTU_IMAGE} AS gf180mcu-sheet
ARG APT_SNAPSHOT
# wafer-space/gf180mcu at the commit the example's GF180 die was built against
# (the pdk_version its build record names), reachable from main. A blobless
# fetch: the checkout pulls the two blobs it names, not the PDK. The sheet's
# digest is checked as well as the ref, since the ref alone would also accept a
# rewritten history.
ARG GF180MCU_REF=77cb86086e6bcded39e5e3f2d25ab234b6cdee65
ARG GF180MCU_LYP_SHA256=4d0e42a66b16f598bc4c401c193e60b8aeabc45bba7ff96d2f93e84cf6acb31f
RUN --mount=type=bind,source=docker/apt-snapshot,target=/run/apt-snapshot \
    --mount=type=cache,id=adk-tools-apt-snapshot,target=/var/cache/apt-snapshot,sharing=locked \
    sh /run/apt-snapshot/apt-snapshot.sh "$APT_SNAPSHOT" --no-install-recommends git ca-certificates \
    && git init -q /gf180mcu \
    && git -C /gf180mcu remote add origin https://github.com/wafer-space/gf180mcu.git \
    && git -C /gf180mcu fetch -q --depth 1 --filter=blob:none origin "${GF180MCU_REF}" \
    && git -C /gf180mcu checkout -q FETCH_HEAD -- \
        LICENSE \
        gf180mcuD/libs.tech/klayout/tech/gf180mcu.lyp \
    && git -C /gf180mcu rev-parse FETCH_HEAD > /gf180mcu/.shipped-ref \
    && test "$(cat /gf180mcu/.shipped-ref)" = "${GF180MCU_REF}" \
    && echo "${GF180MCU_LYP_SHA256}  /gf180mcu/gf180mcuD/libs.tech/klayout/tech/gf180mcu.lyp" \
        | sha256sum -c --quiet \
    && rm -rf /gf180mcu/.git

############################################################################
# GF180MCU ngspice models: the two files a mixed co-simulation case loads for a
# gf180mcuD die, design.ngspice and sm141064.ngspice (its typical section
# reaches nothing outside the file). Fetched at the ref the sheet stage
# shipped, so GF180MCU_REF stays the one pin, and checked by digest like the
# sheet. A stage of their own, so the tools stage takes them in a late layer and
# the layers it builds before that stay cached. No .git is kept: chiplet-cosim
# then records the models entry's revision as declared, unverified, and binds
# the files by sha256.
FROM gf180mcu-sheet AS gf180mcu-models
ARG GF180MCU_DESIGN_NGSPICE_SHA256=091cb530bf85160a1f07878fb81f789ca367d018991c8ab41a584cd1a85c6692
ARG GF180MCU_SM141064_NGSPICE_SHA256=6edba54d23b38a2c60f8ffdbaa3eb75a7568c615e4820808969bd1121b77b1aa
RUN ref="$(cat /gf180mcu/.shipped-ref)" \
    && git init -q /gf180mcu \
    && git -C /gf180mcu remote add origin https://github.com/wafer-space/gf180mcu.git \
    && git -C /gf180mcu fetch -q --depth 1 --filter=blob:none origin "${ref}" \
    && test "$(git -C /gf180mcu rev-parse FETCH_HEAD)" = "${ref}" \
    && git -C /gf180mcu checkout -q FETCH_HEAD -- \
        gf180mcuD/libs.tech/ngspice/design.ngspice \
        gf180mcuD/libs.tech/ngspice/sm141064.ngspice \
    && printf '%s  %s\n' \
        "${GF180MCU_DESIGN_NGSPICE_SHA256}" /gf180mcu/gf180mcuD/libs.tech/ngspice/design.ngspice \
        "${GF180MCU_SM141064_NGSPICE_SHA256}" /gf180mcu/gf180mcuD/libs.tech/ngspice/sm141064.ngspice \
        | sha256sum -c --quiet \
    && rm -rf /gf180mcu/.git

############################################################################
# GF180MCU DRC and LVS decks: gf180mcuD/libs.tech/klayout/tech/drc and
# tech/lvs at the ref the sheet stage shipped, so GF180MCU_REF stays the one
# pin. Each folder's testing/ is left out, as the sg13g2 deck's is (it is
# 251M of regression layouts the decks never read), so the shipped LVS tree is
# not the full tree a die's build record digests. Only the listed blobs are
# fetched. Each folder is checked by the digest of its shipped tree: the
# sha256 of `sha256sum` over its files, sorted by path, as written below. No
# file in either folder is a link, since a deck kind refuses one below its tree.
FROM gf180mcu-sheet AS gf180mcu-decks
ARG GF180MCU_DRC_TREE_SHA256=23044fc3905f9eca7f5e25d51b0deab231ef653265c6be79d9e78d7952288d4f
ARG GF180MCU_LVS_TREE_SHA256=56f55fda31bc915102d5d463ae8d6ceb79110d5abee0b3afd379e81835ee838d
RUN ref="$(cat /gf180mcu/.shipped-ref)" \
    && tech=gf180mcuD/libs.tech/klayout/tech \
    && git init -q /gf180mcu \
    && git -C /gf180mcu remote add origin https://github.com/wafer-space/gf180mcu.git \
    && git -C /gf180mcu fetch -q --depth 1 --filter=blob:none origin "${ref}" \
    && test "$(git -C /gf180mcu rev-parse FETCH_HEAD)" = "${ref}" \
    && git -C /gf180mcu ls-tree -r --name-only FETCH_HEAD -- "$tech/drc" "$tech/lvs" \
        | grep -v -e "^$tech/drc/testing/" -e "^$tech/lvs/testing/" \
        | xargs -r -d '\n' git -C /gf180mcu checkout -q FETCH_HEAD -- \
    && rm -rf /gf180mcu/.git \
    && test -z "$(find /gf180mcu/$tech/drc /gf180mcu/$tech/lvs ! -type d ! -type f)" \
    && for pair in "drc ${GF180MCU_DRC_TREE_SHA256}" "lvs ${GF180MCU_LVS_TREE_SHA256}"; do \
        set -- $pair; \
        got="$(cd "/gf180mcu/$tech/$1" && find . -type f -print0 | LC_ALL=C sort -z \
            | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"; \
        [ "$got" = "$2" ] || { echo "gf180mcu-decks: tech/$1 digests to $got, not $2" >&2; exit 1; }; \
    done

############################################################################
# The gf180mcuD pieces a LibreLane build of a gf180mcuD die reads: the
# gf180mcu_fd_sc_mcu7t5v0 views and its three 3v30 liberty corners, the
# gf180mcu_fd_io views the PDK's librelane config globs (a glob with no match
# stops the config load, so they ship though no PDK pad is placed), the
# librelane, magic and netgen tech folders, the KLayout lyt and map, and
# SOURCES. Not the whole gf180mcuD: no 9t5v0, SRAM macros, 5 V liberty or
# deck test data. gf180mcu-pdk.sha256 lists every file by its sha256 at the
# ref the sheet stage shipped; only those blobs are fetched, and the stage
# fails on a missing file, other bytes or one more file. Read-only, so a run
# cannot write a file the list lacks into the tree. A fresh folder, so no
# file of the sheet's lands in it.
FROM gf180mcu-sheet AS gf180mcu-pdk
COPY gf180mcu-pdk.sha256 /gf180mcu-pdk.sha256
RUN ref="$(cat /gf180mcu/.shipped-ref)" \
    && git init -q /gf180mcu-pdk \
    && git -C /gf180mcu-pdk remote add origin https://github.com/wafer-space/gf180mcu.git \
    && git -C /gf180mcu-pdk fetch -q --depth 1 --filter=blob:none origin "${ref}" \
    && test "$(git -C /gf180mcu-pdk rev-parse FETCH_HEAD)" = "${ref}" \
    && cut -c67- /gf180mcu-pdk.sha256 \
        | xargs -r -d '\n' git -C /gf180mcu-pdk checkout -q FETCH_HEAD -- \
    && rm -rf /gf180mcu-pdk/.git \
    && cd /gf180mcu-pdk \
    && sha256sum -c --strict --quiet /gf180mcu-pdk.sha256 \
    && [ "$(find gf180mcuD ! -type d | LC_ALL=C sort)" = "$(cut -c67- /gf180mcu-pdk.sha256)" ] \
    && test -z "$(find gf180mcuD ! -type d ! -type f)" \
    && chmod -R a-w gf180mcuD

############################################################################
# The tools: everything below /opt/adk-tools and /usr this repository does not
# write. build.sh tags it adk-tools:tools-<key>, the key ci/tools_key.py
# computes from the text above the thin-layer line and every file it reads,
# and builds it only when no image carries that key.
FROM deps AS tools

# Runtime-only apt, kept out of `deps` so the heavy builder caches survive:
#   python3-tk  the SG13G2 cni PCell API imports tkinter
#   vim         terminal editor for quick edits inside the container
#   featherpad  lightweight GUI text editor (Qt) for editing files under /work
# Editors are a deliberate runtime convenience: /work is the host-shared area, so
# users edit project files in-container without a second terminal on the host.
ARG APT_SNAPSHOT
RUN --mount=type=bind,source=docker/apt-snapshot,target=/run/apt-snapshot \
    --mount=type=cache,id=adk-tools-apt-snapshot,target=/var/cache/apt-snapshot,sharing=locked \
    sh /run/apt-snapshot/apt-snapshot.sh "$APT_SNAPSHOT" --no-install-recommends \
        python3-tk vim featherpad

# Ecosystem discovery roots (env is the first link of every tool's discovery
# chain: env -> textvar -> sibling walk -> loud). PDK dirs carry their IHP
# repository names. Kept out of `deps` so env changes never invalidate the
# builder caches; the runtime and `verify` inherit them from the tools image.
# The last three are the flows' own: the staged chiplet-spec (the one the
# installed reader comes from), the interpreter packaging-flow runs the
# interposer_pnr engine legs under, and the klayout.db interpreter chiplet-flow's
# die-view observation runs under. Named rather than probed, as the Nix CLI shell
# does, so a run needs no -e for them.
ENV ADK_TOOLS=/opt/adk-tools \
    ADK_ROOT=/opt/adk-tools/adk \
    INTERPOSER_PDK_ROOT=/opt/adk-tools/OpenIntM4TM2 \
    INTERCONNECT_PDK_ROOT=/opt/adk-tools/IHP-Interconnect-IntM4TM2 \
    GDS_TO_KICAD_ROOT=/opt/adk-tools/gds_to_kicad \
    CHIPLET_KICAD_PLUGIN_ROOT=/opt/adk-tools/chiplet_kicad_plugin \
    PDK_ROOT=/opt/adk-tools/IHP-Open-PDK \
    KICAD_CHIPLET_PYTHON=/opt/adk-tools/venv/bin/python3 \
    CHIPLET_SPEC_ROOT=/opt/adk-tools/staging/chiplet-spec \
    CHIPLET_FLOW_ENGINE_PYTHON=/opt/adk-tools/venv/bin/python3 \
    CHIPLET_FLOW_PADMAP_PYTHON=/opt/adk-tools/venv/bin/python3

# Register the interposer KLayout technology (intm4tm2.lyt + .lyp under
# tech/) so `klayout` opens generated interposer/assembly GDS with named,
# colored layers. First path component = writable config home (run.sh sets
# HOME=/tmp, so this matches KLayout's default location). The interposer
# tree carries two macros: the TopMetal filler (autorun=false) and
# tech/pymacros/autorun.lym (autorun=true), which imports the intm4tm2 pycell
# library. Any run without -rx loads it; -b implies -rx and skips it. The
# sg13g2 PDK slice deliberately stays OFF this path -- its pycell autorun would need
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

# SG13G2 base-PDK slice (PDK_ROOT): hyp_to_gds self-registers the SG13_dev
# PCell library from here, so vias are real via_stack PCells instead of the
# rectangle fallback.
COPY --from=sg13g2-pdk /pdk /opt/adk-tools/IHP-Open-PDK

# Layer sheets and stackups a technology entry names as ${VAR} paths. The
# SG13G2, interposer and interconnect sheets are in the PDK trees above, under
# the roots set earlier; these two roots are for what lives elsewhere: the GF180
# sheet, and the stackups Chiplet Studio ships under configs/stackups. Set here,
# not with the roots above, so the tools layers before this point stay cached.
COPY --from=gf180mcu-sheet /gf180mcu /opt/adk-tools/gf180mcu
ENV GF180MCU_PDK_ROOT=/opt/adk-tools/gf180mcu \
    CHIPLET_STUDIO_ROOT=/opt/adk-tools/chiplet-studio

# Worker venv. --system-site-packages on purpose: the venv python then also
# sees pcbnew (kicad install) and wx (python3-wxgtk4.0), so one interpreter
# can drive the whole pipeline.
# The direct deps are exact-pinned to the known-good versions resolved by the
# prior green build, and docker/venv-constraints.txt holds what they pull in.
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
#   assembly-lvs    pyproject.toml at the staged ref   (klayout>=0.30,<0.31)
ARG KLAYOUT_PIP=0.30.5
ARG PYYAML_PIP=6.0.3
ARG PYQT6_PIP=6.11.0
ARG JINJA2_PIP=3.1.6
ARG JSONSCHEMA_PIP=4.26.0
ARG PSUTIL_PIP=7.2.2
ARG PYTEST_PIP=9.1.0
# pytest's plugin manager, pinned with it: left free, a rebuild of this layer
# took a newer pluggy than the image before it ran.
ARG PLUGGY_PIP=1.6.0
# pytest -n N for the suites that run in this image; execnet, its one
# requirement, is in docker/venv-constraints.txt.
ARG PYTEST_XDIST_PIP=3.8.0
# assembly-lvs's one third-party requirement the pins above leave out. The
# staged trees install below the thin-layer line with no index, so every
# package they need has to be here already.
ARG Z3_SOLVER_PIP=4.15.8.0
# packaging-flow's console extra: with rich installed its run console draws
# progress and tables on stderr, and its console tests import it. The staged
# trees install below with no index, so it has to be here already.
ARG RICH_PIP=14.3.4
# docker/venv-constraints.txt pins the rest: every package the ones above
# pull in, at the version the image resolved before this file pinned them.
RUN --mount=type=bind,source=docker/venv-constraints.txt,target=/run/venv-constraints.txt \
    python3 -m venv --system-site-packages /opt/adk-tools/venv \
    && /opt/adk-tools/venv/bin/pip install --no-cache-dir \
        -c /run/venv-constraints.txt \
        "klayout==${KLAYOUT_PIP}" \
        "PyYAML==${PYYAML_PIP}" \
        "PyQt6==${PYQT6_PIP}" \
        "jinja2==${JINJA2_PIP}" \
        "jsonschema==${JSONSCHEMA_PIP}" \
        "psutil==${PSUTIL_PIP}" \
        "pytest==${PYTEST_PIP}" \
        "pluggy==${PLUGGY_PIP}" \
        "pytest-xdist==${PYTEST_XDIST_PIP}" \
        "z3-solver==${Z3_SOLVER_PIP}" \
        "rich==${RICH_PIP}"

# The build backend the staged trees install with, below the thin-layer line,
# where nothing reaches the network: a wheelhouse pip finds it in, outside the
# venv, so the venv's own setuptools stays the system's. One version for
# every staged tree (assembly-lvs asks for >=77, the system has 68.1.2),
# checked by digest.
ARG BUILD_SETUPTOOLS=84.0.0
ARG BUILD_SETUPTOOLS_SHA256=51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670
RUN mkdir -p /opt/adk-tools/tools/wheels \
    && /opt/adk-tools/venv/bin/pip download --no-cache-dir --no-deps --only-binary=:all: \
        --dest /opt/adk-tools/tools/wheels "setuptools==${BUILD_SETUPTOOLS}" \
    && echo "${BUILD_SETUPTOOLS_SHA256}  /opt/adk-tools/tools/wheels/setuptools-${BUILD_SETUPTOOLS}-py3-none-any.whl" \
        | sha256sum -c --quiet \
    && test "$(ls /opt/adk-tools/tools/wheels)" = "setuptools-${BUILD_SETUPTOOLS}-py3-none-any.whl"

# The co-simulation engine chiplet-cosim runs (staging.pin, "Co-simulation
# engine"): Icarus, cocotb 2.0.0 and a python3.13 with cocotb in its own
# site-packages, the Nix store paths its capability entry was shown on.
# build.sh checks nix/cosim-closure.txt against the host store, packs the
# listed paths into cosim-closure.tar and names the four pins as build
# arguments; ADD unpacks the archive under /nix/store. Nothing in the image
# runs Nix. COSIM_DEVSHELL is where chiplet-cosim finds the engine: the
# devshell's own store path (packages.cosim-devshell), whose bin/ links
# python3, iverilog and vvp into the env and Icarus, so a lock that pins the
# devshell by path replays here as on the host. /opt/adk-tools/cosim-devshell
# is a link to it; chiplet-cosim takes the realpath. None of them is on PATH;
# the venv stays the image's Python, and chiplet-cosim starts this one itself.
# A run.sh launch that mounts the host's store for OpenROAD mounts it over this
# /nix/store, and the engine then has to be in the host's store too (run.sh
# says when it is not). The verify stage's last leg holds the devshell to the
# entry.
ARG NIX_COSIM_IVERILOG_OUTPATH
ARG NIX_COSIM_COCOTB_OUTPATH
ARG NIX_COSIM_PYTHON_OUTPATH
ARG NIX_COSIM_DEVSHELL_OUTPATH
ADD cosim-closure.tar /
COPY nix/cosim-closure.txt /opt/adk-tools/nix/cosim-closure.txt
RUN for pin in "$NIX_COSIM_IVERILOG_OUTPATH" "$NIX_COSIM_COCOTB_OUTPATH" "$NIX_COSIM_PYTHON_OUTPATH" \
            "$NIX_COSIM_DEVSHELL_OUTPATH"; do \
        grep -qxF -- "$pin" /opt/adk-tools/nix/cosim-closure.txt \
            || { echo "co-simulation engine: build argument '$pin' is not a path nix/cosim-closure.txt lists (build with build.sh)" >&2; exit 1; }; \
    done \
    && ln -sT "$NIX_COSIM_DEVSHELL_OUTPATH" /opt/adk-tools/cosim-devshell
ENV COSIM_DEVSHELL=/opt/adk-tools/cosim-devshell

# The mixed-signal engine (staging.pin, "Mixed-signal engine"): ngspice 45, an
# Icarus built with libvvp, and the six sg13g2 OSDI models compiled for generic
# x86-64. build.sh checks nix/mixsim-closure.txt against the host store (the
# engine's closure less what cosim-closure.tar already holds), checks the OSDI
# files' bytes against nix/sg13g2-osdi.sha256, packs the listed paths into
# mixsim-closure.tar and names the three pins as build arguments. MIXSIM_DEVSHELL
# is where a run finds the engine: bin/ngspice, bin/iverilog, bin/vvp,
# lib/libvvp.so, lib/ngspice (ngspice's own: the code models, and the ivlng shim
# and VPI module d_cosim loads) and osdi/, linked into those paths. None of it is
# on PATH. A run gives d_cosim libvvp.so and ivlng by these absolute paths, so no
# loader search is involved. The verify stage's mixed-signal leg runs both.
ARG NIX_MIXSIM_NGSPICE_OUTPATH
ARG NIX_MIXSIM_IVERILOG_OUTPATH
ARG NIX_SG13G2_OSDI_OUTPATH
ADD mixsim-closure.tar /
COPY nix/mixsim-closure.txt /opt/adk-tools/nix/mixsim-closure.txt
COPY nix/sg13g2-osdi.sha256 /opt/adk-tools/nix/sg13g2-osdi.sha256
RUN for pin in "$NIX_MIXSIM_NGSPICE_OUTPATH" "$NIX_MIXSIM_IVERILOG_OUTPATH" "$NIX_SG13G2_OSDI_OUTPATH"; do \
        grep -qxF -- "$pin" /opt/adk-tools/nix/mixsim-closure.txt \
            || { echo "mixed-signal engine: build argument '$pin' is not a path nix/mixsim-closure.txt lists (build with build.sh)" >&2; exit 1; }; \
    done \
    && mkdir -p /opt/adk-tools/mixsim-devshell/bin /opt/adk-tools/mixsim-devshell/lib \
    && ln -s "$NIX_MIXSIM_NGSPICE_OUTPATH/bin/ngspice" /opt/adk-tools/mixsim-devshell/bin/ngspice \
    && ln -s "$NIX_MIXSIM_IVERILOG_OUTPATH/bin/iverilog" /opt/adk-tools/mixsim-devshell/bin/iverilog \
    && ln -s "$NIX_MIXSIM_IVERILOG_OUTPATH/bin/vvp" /opt/adk-tools/mixsim-devshell/bin/vvp \
    && ln -s "$NIX_MIXSIM_IVERILOG_OUTPATH/lib/libvvp.so" /opt/adk-tools/mixsim-devshell/lib/libvvp.so \
    && ln -s "$NIX_MIXSIM_NGSPICE_OUTPATH/lib/ngspice" /opt/adk-tools/mixsim-devshell/lib/ngspice \
    && ln -s "$NIX_SG13G2_OSDI_OUTPATH/osdi" /opt/adk-tools/mixsim-devshell/osdi
ENV MIXSIM_DEVSHELL=/opt/adk-tools/mixsim-devshell

# The gf180mcuD models a mixed case loads (the gf180mcu-models stage), beside
# the GF180 sheet in the PDK's own layout. chiplet-cosim reads a gf180mcuD
# models entry's root from COSIM_GF180_ROOT and from nothing else; unset, such
# an entry reads NOT_RUN tool_missing. Copied here, not with the sheet, so the
# tools layers before this point stay cached.
COPY --from=gf180mcu-models /gf180mcu/gf180mcuD/libs.tech/ngspice /opt/adk-tools/gf180mcu/gf180mcuD/libs.tech/ngspice
ENV COSIM_GF180_ROOT=/opt/adk-tools/gf180mcu/gf180mcuD

# The ihp-sg13g2 models a mixed case reaches (the sg13g2-models stage), into
# the slice, since chiplet-cosim reads an ihp-sg13g2 models entry's root as
# $PDK_ROOT/ihp-sg13g2. Such an entry names its OSDI files under that root too
# (libs.tech/ngspice/osdi), and the model walk refuses one that resolves
# outside the PDK installation, so a link into the mixed-signal engine's store
# path would not do: the four the case loads are copied from there and checked
# against nix/sg13g2-osdi.sha256 again. Late, for the GF180 models' reason.
COPY --from=sg13g2-models /models/ihp-sg13g2 /opt/adk-tools/IHP-Open-PDK/ihp-sg13g2
RUN cd /opt/adk-tools/IHP-Open-PDK/ihp-sg13g2/libs.tech/ngspice \
    && mkdir osdi \
    && grep -E '  (psp103|psp103_nqs|r3_cmc|mosvar)\.osdi$' /opt/adk-tools/nix/sg13g2-osdi.sha256 \
        | sed 's|  |  osdi/|' > /tmp/pdk-osdi.sha256 \
    && test "$(wc -l < /tmp/pdk-osdi.sha256)" -eq 4 \
    && for name in psp103 psp103_nqs r3_cmc mosvar; do \
        cp -L "/opt/adk-tools/mixsim-devshell/osdi/${name}.osdi" osdi/ || exit 1; \
    done \
    && sha256sum -c --quiet /tmp/pdk-osdi.sha256 \
    && rm /tmp/pdk-osdi.sha256

# The waveform viewers (staging.pin, "Waveform viewers"): GTKWave 3.3.121 for VCD
# and FST, GAW for ngspice's raw files, and Xschem, which draws a mixed run's
# analog waves from a sheet that loads its raw file. build.sh checks
# nix/viewer-closure.txt against the host store (the viewers' closure less what
# the engines' archives already hold), packs the listed paths into
# viewer-closure.tar and names the three pins as build arguments. Unlike the
# engines they are on PATH: /usr/local/bin/gtkwave, gaw and xschem link into
# the pins. Xschem reads its rcfile and symbol library from XSCHEM_SHAREDIR,
# its own share/xschem through /opt/adk-tools/xschem. A run.sh launch that mounts the host's store for OpenROAD hides
# this copy, and run.sh says when the host's store lacks them. The verify
# stage's viewer leg opens a waveform in each, headless.
ARG NIX_GTKWAVE_OUTPATH
ARG NIX_GAW_OUTPATH
ARG NIX_XSCHEM_OUTPATH
ADD viewer-closure.tar /
COPY nix/viewer-closure.txt /opt/adk-tools/nix/viewer-closure.txt
RUN for pin in "$NIX_GTKWAVE_OUTPATH" "$NIX_GAW_OUTPATH" "$NIX_XSCHEM_OUTPATH"; do \
        grep -qxF -- "$pin" /opt/adk-tools/nix/viewer-closure.txt \
            || { echo "viewers: build argument '$pin' is not a path nix/viewer-closure.txt lists (build with build.sh)" >&2; exit 1; }; \
    done \
    && ln -s "$NIX_GTKWAVE_OUTPATH/bin/gtkwave" /usr/local/bin/gtkwave \
    && ln -s "$NIX_GAW_OUTPATH/bin/gaw" /usr/local/bin/gaw \
    && ln -s "$NIX_XSCHEM_OUTPATH/bin/xschem" /usr/local/bin/xschem \
    && ln -sT "$NIX_XSCHEM_OUTPATH" /opt/adk-tools/xschem
ENV XSCHEM_SHAREDIR=/opt/adk-tools/xschem/share/xschem

# The Verilog-AMS engine (staging.pin, "Verilog-AMS engine"): gnucap and
# modelgen-verilog in one prefix, and the stdenv's GCC that builds a run's
# modelgen output into a plugin. build.sh checks nix/vams-closure.txt against
# the host store (the devshell's closure less what the other archives already
# hold), packs the listed paths into vams-closure.tar and names the two pins as
# build arguments. VAMS_DEVSHELL is where a run finds the engine: the
# devshell's own store path (packages.vams-devshell), whose bin/ links gnucap,
# gnucap-conf, gnucap-mg-vams, gnucap-modelgen, g++ and c++, so a lock that
# pins the devshell by path replays here as on the host.
# /opt/adk-tools/vams-devshell is a link to it. None of it is on PATH. gnucap
# finds libgnucap.so by its rpath and its plugins on the path it was built with,
# so a run sets neither LD_LIBRARY_PATH nor GNUCAP_PLUGPATH. The verify stage's
# Verilog-AMS leg builds a model with that g++ and solves a deck with it.
ARG NIX_VAMS_GNUCAP_OUTPATH
ARG NIX_VAMS_DEVSHELL_OUTPATH
ADD vams-closure.tar /
COPY nix/vams-closure.txt /opt/adk-tools/nix/vams-closure.txt
RUN for pin in "$NIX_VAMS_GNUCAP_OUTPATH" "$NIX_VAMS_DEVSHELL_OUTPATH"; do \
        grep -qxF -- "$pin" /opt/adk-tools/nix/vams-closure.txt \
            || { echo "Verilog-AMS engine: build argument '$pin' is not a path nix/vams-closure.txt lists (build with build.sh)" >&2; exit 1; }; \
    done \
    && ln -sT "$NIX_VAMS_DEVSHELL_OUTPATH" /opt/adk-tools/vams-devshell
ENV VAMS_DEVSHELL=/opt/adk-tools/vams-devshell

# The gf180mcuD DRC and LVS decks (the gf180mcu-decks stage), beside the sheet
# and the models in the one GF180 tree. A deck kind finds a die's deck at
# $PDK_ROOT/<pdk>/libs.tech/klayout, so $PDK_ROOT/gf180mcuD is a link to that
# tree: no root moves, and the kind refuses links only below the deck's tree.
# The verify stage's GF180 deck leg holds that. Late in the stage, so the tools
# layers before this point stay cached.
COPY --from=gf180mcu-decks /gf180mcu/gf180mcuD/libs.tech/klayout/tech/drc /opt/adk-tools/gf180mcu/gf180mcuD/libs.tech/klayout/tech/drc
COPY --from=gf180mcu-decks /gf180mcu/gf180mcuD/libs.tech/klayout/tech/lvs /opt/adk-tools/gf180mcu/gf180mcuD/libs.tech/klayout/tech/lvs
# The gf180mcuD pieces a LibreLane build reads (the gf180mcu-pdk stage), in the
# same tree, so GF180MCU_PDK_ROOT is the root a gf180mcuD die build passes with
# --manual-pdk. One COPY per folder or file of its own: none writes a path the
# sheet, the models or the decks ship. The verify stage's GF180 PDK leg holds
# the tree to gf180mcu-pdk.sha256.
COPY --from=gf180mcu-pdk /gf180mcu-pdk/gf180mcuD/SOURCES /opt/adk-tools/gf180mcu/gf180mcuD/SOURCES
COPY --from=gf180mcu-pdk /gf180mcu-pdk/gf180mcuD/libs.ref /opt/adk-tools/gf180mcu/gf180mcuD/libs.ref
COPY --from=gf180mcu-pdk /gf180mcu-pdk/gf180mcuD/libs.tech/librelane /opt/adk-tools/gf180mcu/gf180mcuD/libs.tech/librelane
COPY --from=gf180mcu-pdk /gf180mcu-pdk/gf180mcuD/libs.tech/magic /opt/adk-tools/gf180mcu/gf180mcuD/libs.tech/magic
COPY --from=gf180mcu-pdk /gf180mcu-pdk/gf180mcuD/libs.tech/netgen /opt/adk-tools/gf180mcu/gf180mcuD/libs.tech/netgen
COPY --from=gf180mcu-pdk /gf180mcu-pdk/gf180mcuD/libs.tech/klayout/tech/gf180mcu.lyt /opt/adk-tools/gf180mcu/gf180mcuD/libs.tech/klayout/tech/gf180mcu.lyt
COPY --from=gf180mcu-pdk /gf180mcu-pdk/gf180mcuD/libs.tech/klayout/tech/gf180mcu.map /opt/adk-tools/gf180mcu/gf180mcuD/libs.tech/klayout/tech/gf180mcu.map
RUN ln -sT /opt/adk-tools/gf180mcu/gf180mcuD "$PDK_ROOT/gf180mcuD"

# What the tools image holds, by version: every dpkg package and every Python
# distribution the venv sees. Last in the stage, so they name what shipped.
RUN mkdir -p /opt/adk-tools/tools \
    && dpkg-query -W -f='${binary:Package}=${Version}\n' | LC_ALL=C sort \
        > /opt/adk-tools/tools/apt.lock \
    && /opt/adk-tools/venv/bin/python3 -m pip freeze --all --disable-pip-version-check \
        | LC_ALL=C sort > /opt/adk-tools/tools/pip.lock

#### thin layer: below this line, our code on the tools image (ci/check_thin_layer.py) ####
# Nothing below reaches the network but studio-builder (its CMake fetches
# its pinned dependencies) and nothing installs from apt; every RUN of the
# runtime stage carries --network=none. ci/check_thin_layer.py holds both.

############################################################################
# On the tools image, which carries deps' build packages, so a build below the
# line needs nothing but that image. A new tools image rebuilds it.
FROM ${TOOLS_IMAGE} AS studio-builder
ARG JOBS

# Build at the final runtime path: CMake bakes CONFIGS_DIR (absolute) into the
# binary, and ctest metadata in build/ keeps working in the verify stage.
#
# KLayout first, from its own tree alone, so a chiplet-studio commit that leaves
# extern/klayout as it was reuses this layer instead of rebuilding it. The full
# tree is copied over it below; .dockerignore keeps bin-release/ and
# build-release/ out of the context, so that copy leaves the build in place.
COPY tools/chiplet-studio/extern/klayout /opt/adk-tools/chiplet-studio/extern/klayout
WORKDIR /opt/adk-tools/chiplet-studio

# gtest_discover_tests executes the test binary at build time; make the
# in-tree KLayout libs resolvable for it.
ENV LD_LIBRARY_PATH=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release

# -rpath names the folder the binary and its libraries ship in, so the klayout
# binary loads its own libraries with no LD_LIBRARY_PATH: the tool identity
# table names that binary, and packaging-flow runs it from there in an
# environment it builds from nothing. build.sh's own default (-bin) is unset
# when it picks RPATH, so without the flag the binaries carry none.
#
# The same sources give the same bytes. KLayout's version.sh stamps the build
# day into the binaries (`date "+%Y-%m-%d"`, its prg_date), and the GNU build
# id hashes the output, so a rebuild on another day moved both and with them
# the sha256 the tool identity table keys on. KLAYOUT_SOURCE_DATE_EPOCH is the
# KLayout commit's time, which build.sh reads from the submodule. A `date`
# placed first on PATH for this RUN answers exactly the call version.sh makes
# from that time and refuses any other call, so a build step that starts
# calling date differently fails here instead of getting a frozen value. rcc
# takes the same time in place of the files' mtimes. Not named
# SOURCE_DATE_EPOCH as a build argument: BuildKit would also stamp the image's
# created time with it.
ARG KLAYOUT_SOURCE_DATE_EPOCH
RUN case "$KLAYOUT_SOURCE_DATE_EPOCH" in \
        ''|*[!0-9]*) echo "KLAYOUT_SOURCE_DATE_EPOCH is '$KLAYOUT_SOURCE_DATE_EPOCH'; build.sh passes the KLayout commit's time" >&2; exit 1 ;; \
    esac \
    && mkdir /tmp/klayout-date \
    && printf '%s\n' '#!/bin/sh' \
        'if [ "$#" -ne 1 ] || [ "$1" != "+%Y-%m-%d" ]; then' \
        '    echo "date: the KLayout build answers only version.sh (date +%Y-%m-%d), not: date $*" >&2' \
        '    exit 1' \
        'fi' \
        'exec /bin/date -u -d "@$SOURCE_DATE_EPOCH" "+%Y-%m-%d"' > /tmp/klayout-date/date \
    && chmod 0755 /tmp/klayout-date/date \
    && cd extern/klayout \
    && PATH=/tmp/klayout-date:$PATH \
       SOURCE_DATE_EPOCH="$KLAYOUT_SOURCE_DATE_EPOCH" \
       QT_RCC_SOURCE_DATE_OVERRIDE="$KLAYOUT_SOURCE_DATE_EPOCH" \
       ./build.sh -j"${JOBS}" -without-qtbinding \
        -rpath /opt/adk-tools/chiplet-studio/extern/klayout/bin-release \
    && rm -r /tmp/klayout-date

COPY tools/chiplet-studio /opt/adk-tools/chiplet-studio

RUN mkdir -p build && cd build \
    && cmake .. -DKLAYOUT_BUILD_DIR=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release \
    && make -j"${JOBS}"

############################################################################
FROM ${TOOLS_IMAGE} AS runtime

# Point the built image at its source repo (org.opencontainers.image.source) so
# a locally-built image links back to the code it was built from.
LABEL org.opencontainers.image.source=https://github.com/IHP-GmbH/ADK-Tools \
      org.opencontainers.image.description="Heterogeneous integration flow: KiCad fork, Chiplet Studio, PDKs, assembly DRC -- pre-wired"

# The tools image is the one docker/apt.lock and docker/pip.lock record: its
# own locks (the tools stage's last step) against the tracked ones, first, so
# a tools image built on another day, or for another key, stops here. A new
# tools image moves both files in the commit that moves its key.
RUN --network=none \
    --mount=type=bind,source=docker/apt.lock,target=/run/locks/apt.lock \
    --mount=type=bind,source=docker/pip.lock,target=/run/locks/pip.lock \
    diff -u /run/locks/apt.lock /opt/adk-tools/tools/apt.lock \
    && diff -u /run/locks/pip.lock /opt/adk-tools/tools/pip.lock

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

# The interposer PDK's label commit: stamped from the gitlink and read back by
# the PDK's own reader (ci/check_interposer_label.py says why the ARG is the
# copied tree's commit). No default: a build that does not pass it fails.
ARG INTERPOSER_PIN
RUN --network=none \
    --mount=type=bind,source=ci/check_interposer_label.py,target=/run/check_interposer_label.py \
    /opt/adk-tools/venv/bin/python3 -B /run/check_interposer_label.py \
        --tree /opt/adk-tools/OpenIntM4TM2 --pin "${INTERPOSER_PIN}"
# No examples here: the runtime image carries no demo. The person copies it
# from the adk-tools clone they ran run.sh from (adk-example says how), and the
# verify stage COPYs examples/ for its own checks.

# Sources that cannot be submodules, staged by commit into staging/ by
# stage-sources.sh. Their shas are in manifest.json under "staged", kept apart
# from "tools" because a submodule entry is reproducible from this repository
# alone and one of these is not.
#
# The .git directories come along deliberately; .dockerignore carries the
# exception that keeps them, with the reason.
#
# They arrive owned by root, and a container run as the host user
# (--user uid:gid, which is how a package acceptance runs) then has git refuse
# every staged checkout as "dubious ownership". packaging-flow's identity probe
# reads that as the package's commit being unavailable, and `packaging-flow
# verify` fails its full tier on it. So each tree is declared safe system-wide.
# One entry per tree, not a glob: the git in this image (2.43) does not honour a
# trailing `/opt/adk-tools/staging/*` (measured in this recipe's image). Written
# ahead of the COPY lines so a staging bump never rebuilds it; a tree added
# there needs its entry here, and the verify stage refuses a staged tree that
# does not read back as a non-owner.
RUN --network=none for tree in assembly-lvs interposer-pnr packaging-flow chiplet-flow chiplet-cosim chiplet-spec; do \
        git config --system --add safe.directory "/opt/adk-tools/staging/$tree" || exit 1; \
    done
COPY staging/assembly-lvs /opt/adk-tools/staging/assembly-lvs
COPY staging/interposer-pnr /opt/adk-tools/staging/interposer-pnr
COPY staging/packaging-flow /opt/adk-tools/staging/packaging-flow
COPY staging/chiplet-flow /opt/adk-tools/staging/chiplet-flow
COPY staging/chiplet-cosim /opt/adk-tools/staging/chiplet-cosim
COPY staging/chiplet-spec /opt/adk-tools/staging/chiplet-spec

# EDITABLE, and that is a requirement rather than a preference. packaging-flow
# records the identity of every package it dispatches by running
# `git rev-parse HEAD` on the directory find_spec resolves to. A regular install
# resolves to site-packages, where there is no repository, and the identity is
# then recorded as "unavailable: not a git checkout" -- which `packaging-flow
# verify` refuses at its full tier. The image would reproduce a run and be
# unable to demonstrate it with the flow's own verifier.
#
# --no-deps on the three that have already-satisfied requirements would be the
# wrong economy: their pins are what the venv has to agree with, and a silent
# disagreement is exactly what pip refuses to install for. Let it resolve, and
# let it fail here if a pin above stops satisfying one of them.
#
# With no index: every requirement has to be in the tools image's venv
# already, and the build backend comes from its wheelhouse. A staged tree that
# starts needing a package the tools do not carry fails here, by name; the
# package goes into the tools stage, pinned.
#
# chiplet-spec installs from reference/python, not from its root, and the whole
# repository is staged anyway because the package needs what is ABOVE that
# directory: the canonical schemas/ at the repository root. setup.py copies them
# into the package while building a wheel, and in a checkout the reader reads
# them from that same root directory instead. Editable keeps the checkout, so
# the root copy is the one that answers -- which is another reason the tree
# staged for this entry cannot be trimmed to the Python subdirectory. The
# system description is a second project in the same checkout, installed from
# description/.
RUN --network=none /opt/adk-tools/venv/bin/pip install --no-cache-dir --no-index \
        --find-links /opt/adk-tools/tools/wheels --disable-pip-version-check \
        -e /opt/adk-tools/staging/chiplet-spec/description \
        -e /opt/adk-tools/staging/chiplet-flow \
        -e /opt/adk-tools/staging/packaging-flow \
        -e /opt/adk-tools/staging/chiplet-cosim \
        -e /opt/adk-tools/staging/interposer-pnr \
        -e /opt/adk-tools/staging/assembly-lvs \
        -e "/opt/adk-tools/staging/chiplet-spec/reference/python[strict]" \
    && /opt/adk-tools/venv/bin/pip check

# Plugins visible to the KiCad GUI for every user. The repository is a monorepo
# of plugins, so its root is not a plugin package: KiCad scans this folder for
# packages that register an ActionPlugin, and linking the root would give it a
# directory with no __init__.py and no plugin at all. Link each package, which
# is also what the plugin README prescribes ("the specific plugin directory,
# not the whole repository").
RUN --network=none mkdir -p /usr/share/kicad/scripting/plugins \
    && ln -s /opt/adk-tools/chiplet_kicad_plugin/plugins/chiplet_export \
             /usr/share/kicad/scripting/plugins/chiplet_export \
    && ln -s /opt/adk-tools/chiplet_kicad_plugin/plugins/resizer_passive_elements \
             /usr/share/kicad/scripting/plugins/resizer_passive_elements

# Command wrappers + interactive-shell banner
COPY bin/ /usr/local/bin/
RUN --network=none chmod +x /usr/local/bin/* \
    && echo '[ -n "$PS1" ] && echo "adk-tools: heterogeneous integration flow image. Run \`adk-tools\` for the tool list."' >> /etc/bash.bashrc

# Version manifest (generated by build.sh, passed base64 to survive quoting)
ARG MANIFEST_B64=e30K
RUN --network=none echo "${MANIFEST_B64}" | base64 -d > /opt/adk-tools/manifest.json

# The die views (staging.pin, "Die views"): the records of the built
# dies, which stage-die-views.sh copied into die-views/ and held to
# die-views.sha256, whose sha256 build.sh passes as DIE_VIEWS_SHA256. Checked
# here, where users get them: exactly the listed files with the listed bytes and
# no symlink, each record bound to its directory's layout sha256, and the
# manifest's die_views entry naming this listing and these directories. Outside
# staging/ and every example's inputs, so no staged tree's identity moves. A
# run finds a die's views at $ADK_DIE_VIEWS/<layout sha256>/ and copies them;
# a die with no directory there has none.
ARG DIE_VIEWS_SHA256
COPY die-views.sha256 /opt/adk-tools/die-views.sha256
COPY ci/check_die_views.py /opt/adk-tools/ci/check_die_views.py
COPY die-views/ /opt/adk-tools/die-views/
RUN --network=none /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_die_views.py \
        --listing /opt/adk-tools/die-views.sha256 --root /opt/adk-tools/die-views \
        --expect-sha256 "$DIE_VIEWS_SHA256" --manifest /opt/adk-tools/manifest.json
ENV ADK_DIE_VIEWS=/opt/adk-tools/die-views

# The die flow whose plans a system.chiplet builds entry reads is the staged
# chiplet-flow, so an entry that names ${CHIPLET_FLOW_ROOT} for them needs no
# host path; the entry's run folder is the project's own, under /work. Here,
# below the thin layer, so the tools image's key stays.
ENV CHIPLET_FLOW_ROOT=/opt/adk-tools/staging/chiplet-flow

# The pin set this image is built from, where a staged tree's own reader looks
# for it: under the umbrella directory its checkout sits in, as
# adk-tools/ci/pin-set.json. interposer-pnr's tools manifest reads its pinned
# versions there, and packaging-flow's die flow door its die flow commit.
# Read-only, and the bytes build.sh labels as org.ihp.adk-tools.pin-set; the
# verify stage holds the file to that digest. Not a git checkout, so it takes no
# safe.directory entry and no manifest "staged" row.
COPY --chmod=0444 ci/pin-set.json /opt/adk-tools/staging/adk-tools/ci/pin-set.json
# The same pin set, and the resource guard, under the image's own root: a
# reader given ADK_TOOLS_ROOT=/opt/adk-tools finds adk-tools' ci/ there.
# packaging-flow's conftest reads ci/pin-set.json when it is imported and runs
# ci/resource_guard.py, so without them its suite stops at collection. The
# guard is stdlib Python and is run, never imported; leg 0a holds both files.
COPY --chmod=0444 ci/pin-set.json ci/resource_guard.py /opt/adk-tools/ci/

# The die build's tools: OpenROAD, LibreLane, its interpreter and the sg13g2
# PDK, the closure nix/host-closure.txt lists, in this image's own /nix/store,
# so no launch mounts the host's. build.sh refuses unless each path is on the
# build host with the narHash nix/host-closure.lock pins, and packs those the
# four archives in the tools stage do not already hold. Before the tool
# identity table, which then names the four pins as this image's. The verify
# stage recomputes every narHash (leg 20) and runs the tools with no mount (21).
# Below the thin-layer line there is no ADD (ci/check_thin_layer.py), so a RUN
# with the archive bound in unpacks it the way ADD would: root's, with the
# archive's modes and times.
RUN --network=none --mount=type=bind,source=host-closure.tar,target=/run/host-closure.tar \
    tar --extract --file /run/host-closure.tar --directory / \
        --numeric-owner --same-owner --same-permissions
COPY --chmod=0444 nix/host-closure.txt nix/host-closure.lock /opt/adk-tools/nix/

# The tool identity table (formats/adk-tools-tool-identity.v1.schema.json),
# /opt/adk-tools/tool-identity.json: each tool of this image named by its
# content. build.sh wrote tool-identity.facts.json from the build host's store:
# every Nix pin, whether this image carries it, and the narHash of each path it
# needs. Each path this image carries is serialised here and has to be that
# narHash; a pin run.sh mounts from the host keeps the host's values. The six
# tools outside Nix are named by the version each reports (Studio's commit, as
# it prints none), their binary's sha256 and, where the row names a tree, that
# tree's NAR hash. chiplet-cosim and assembly-lvs, each run from its staged
# checkout, are named by its commit, the digest of the files its index tracks
# and its binary's sha256.
# Late in this stage on purpose: nothing after it writes into a tree the table
# names. ci/check_late_writes.py holds that for Python: after this RUN, Python
# starts only with -B or PYTHONDONTWRITEBYTECODE=1, and no pip runs. The table
# holds no image ID; the schema says why.
COPY nix/tool_identity.py nix/tool-identity-image.sh tool-identity.facts.json /opt/adk-tools/nix/
RUN --network=none /opt/adk-tools/nix/tool-identity-image.sh image --out /opt/adk-tools/tool-identity.json

# The LibreLane launcher a die build runs: nix/build-env.sh's, which build.sh
# wrote for the store LibreLane this image carries, with the staged
# chiplet-flow and chiplet-spec sources on its import path.
# The ENV below names it with its sha256, and so does the image's label, which
# an older run.sh reads to pass it in. The verify stage holds it to that sha256
# and its roots to the staged trees. No tree the tool identity table names
# holds it.
COPY --chmod=0555 librelane-launcher /opt/adk-tools/librelane-launcher/librelane

# nix/build-env.sh, which bin/chiplet-die-flow sources for adk_nix_die_build_env:
# the PDK a die build reads, the same function the Nix build shell sets it
# with, so the two are one copy.
COPY --chmod=0444 nix/build-env.sh /opt/adk-tools/nix/build-env.sh

# The tools and the launcher a die build runs, fixed: the names the Nix build
# shell sets (core preparation reads ADK_NIX_*), and the pair chiplet-flow
# binds and records. bin/chiplet-die-flow sets the PDK a build-die reads from
# ADK_NIX_SG13G2_PDK; PDK_ROOT stays the subset the KLayout checks read.
ARG NIX_OPENROAD_OUTPATH
ARG NIX_LIBRELANE_OUTPATH
ARG NIX_LIBRELANE_PYTHON
ARG NIX_SG13G2_PDK_OUTPATH
ARG LAUNCHER_SHA256
ENV OPENROAD=$NIX_OPENROAD_OUTPATH/bin/openroad \
    ADK_NIX_LIBRELANE=$NIX_LIBRELANE_OUTPATH \
    ADK_NIX_LIBRELANE_PYTHON=$NIX_LIBRELANE_PYTHON \
    ADK_NIX_SG13G2_PDK=$NIX_SG13G2_PDK_OUTPATH \
    CHIPLET_FLOW_LIBRELANE_LAUNCHER=/opt/adk-tools/librelane-launcher/librelane \
    CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256=$LAUNCHER_SHA256

# No Python run in the container writes bytecode: the staged trees are root's
# and an overlay is mounted read-only, so those writes fail anyway, and under
# /work they would leave files beside a person's sources. After the tool
# identity table, so its own rule (ci/check_late_writes.py) is unchanged.
ENV PYTHONDONTWRITEBYTECODE=1

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

# 0d. The tool identity table is the one this image gives: rebuilt here from
#     the store, the tools and build.sh's facts, it has to be the written file
#     byte for byte, so each path the image's store carries still serialises to
#     the build host's narHash, and the file has to be valid against its schema.
#     First in this stage on purpose: here the filesystem is the runtime image
#     as shipped, so this is also what shows that nothing after the runtime
#     stage's write moved a tree. The Studio tree layered next adds to build/,
#     which the chiplet-studio row names, and the suites write into trees the
#     table names. ci/check_late_writes.py keeps this RUN first and the schema
#     outside every path the table names.
COPY formats/adk-tools-tool-identity.v1.schema.json /opt/adk-tools/formats/adk-tools-tool-identity.v1.schema.json
RUN /opt/adk-tools/nix/tool-identity-image.sh verify --table /opt/adk-tools/tool-identity.json \
        --schema /opt/adk-tools/formats/adk-tools-tool-identity.v1.schema.json

# Full studio tree (sources, fixtures, ctest metadata, test binaries) layered
# over the lean copy, same absolute paths as studio-builder.
COPY --from=studio-builder /opt/adk-tools/chiplet-studio /opt/adk-tools/chiplet-studio

# 0. Every staged tree reads back its commit as a user that does not own it.
#    RUN steps are root, and git never refuses root a root-owned checkout, so
#    without dropping to nobody this would pass with the safe.directory entries
#    missing. The list and the refs come from the manifest, not from the RUN
#    that wrote the entries, so a tree staged without its entry fails here.
RUN /opt/adk-tools/venv/bin/python3 -c "import json; [print(n, e['ref']) for n, e in sorted(json.load(open('/opt/adk-tools/manifest.json'))['staged'].items())]" > /tmp/staged.txt \
    && test -s /tmp/staged.txt \
    && while read -r name want; do \
           got=$(setpriv --reuid=65534 --regid=65534 --clear-groups env HOME=/tmp git -C "/opt/adk-tools/staging/$name" rev-parse HEAD 2>&1); \
           [ "$got" = "$want" ] || { echo "staged $name read as a non-owner: git says '$got', the manifest recorded $want" >&2; exit 1; }; \
           echo "staged $name: $got"; \
       done < /tmp/staged.txt \
    && rm /tmp/staged.txt

# 0a. Both copies of the pin set are the one the runtime image is labelled
#     with: PIN_SET_SHA256 is the value build.sh puts in
#     org.ihp.adk-tools.pin-set, so a reader inside the image that would find
#     no pin set, or a stale one, fails here. The pin sets and the resource
#     guard are root-owned, mode 0444, and not writable by a user that does not
#     own them.
ARG PIN_SET_SHA256
RUN if [ -z "$PIN_SET_SHA256" ]; then echo "PIN_SET_SHA256 is unset; build.sh passes the label's digest" >&2; exit 1; fi; \
    for f in /opt/adk-tools/staging/adk-tools/ci/pin-set.json /opt/adk-tools/ci/pin-set.json; do \
        echo "$PIN_SET_SHA256  $f" | sha256sum -c --strict || exit 1; \
    done; \
    for f in /opt/adk-tools/staging/adk-tools/ci/pin-set.json /opt/adk-tools/ci/pin-set.json /opt/adk-tools/ci/resource_guard.py; do \
        mode=$(stat -c '%u:%g %a' "$f") || exit 1; \
        if [ "$mode" != "0:0 444" ]; then echo "$f is $mode, expected root-owned 444" >&2; exit 1; fi; \
        if setpriv --reuid=65534 --regid=65534 --clear-groups test -w "$f"; then echo "$f is writable by a user that does not own it" >&2; exit 1; fi; \
    done

# The checkers and lists the legs below read, in two COPYs rather than one
# per leg: each COPY is a layer, and overlay2 refuses a stack deeper than
# 128 (ci/check_layer_depth.py fails a target at 120). After step 0d,
# because ci/check_late_writes.py keeps that RUN first in this stage.
COPY ci/check_vendored_reader.py \
     ci/check_attach_plane.py \
     ci/attach-plane-exemptions.txt \
     ci/check_kicad_templates.py \
     ci/check_required_tests.py \
     ci/tracked-tree-tests.txt \
     ci/image-xor-tests.txt \
     ci/ipnr-route-tests.txt \
     ci/check_cosim_engine.py \
     ci/check_tech_files.py \
     ci/check_mixsim_engine.py \
     ci/check_viewers.py \
     ci/check_vams_engine.py \
     ci/check_kind_contracts.py \
     ci/check_gf180_deck.py \
     gf180mcu-pdk.sha256 \
     ci/check_cosim_mixed_entries.py \
     ci/check_die_build_env.sh \
     ci/check_store_closure.py \
     ci/check_store_tools.sh \
     /opt/adk-tools/ci/
COPY nix/test_tracked_tree.py nix/cli-env.sh /opt/adk-tools/nix/

# 0b. The vendored readers are the staged chiplet-spec's reader, byte for byte:
#     the system description's and interposer-pnr's copies under staging/, and
#     gds_to_kicad's at /opt/adk-tools/gds_to_kicad, outside staging/, hence its
#     --root. The checker's docstring states the rule. Before steps 1 and 2 on
#     purpose: nothing has imported any copy yet in this stage. The one exemption
#     is chiplet-spec's frozen format 1.2 reader, a test fixture that is meant to
#     differ from the reference and is held to its own SHA256SUMS there.
RUN /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_vendored_reader.py /opt/adk-tools/staging \
        --reference chiplet-spec/reference/python/chiplet_format_io \
        --copy chiplet-spec/description/src/chiplet_system_description/_vendor/chiplet_format_io \
        --copy interposer-pnr/src/interposer_pnr/_vendor/chiplet_format_io \
        --root /opt/adk-tools/gds_to_kicad \
        --copy /opt/adk-tools/gds_to_kicad/_vendor/chiplet_format_io \
        --exempt "chiplet-spec/description/tests/fixtures/reader_1_2/src/chiplet_system_description/_vendor/chiplet_format_io=the frozen format 1.2 reader the description's 1.2 test runs, held to its own SHA256SUMS"

# The demo, for the checks below only. The runtime stage ships no examples, so
# this is the first place the tree exists in the build; same path the verify
# tools default to.
COPY examples /opt/adk-tools/examples

# 0c. The attach plane. Every IntM4TM2 .chiplet in the plugin, Studio,
#     gds_to_kicad, the interconnect PDK, the examples and the staged census
#     repositories declares the PDK's beol.attachment_surface_z_um when it seats
#     a die, each written die z is the interposer's z plus the plane plus its
#     connection's height, and the consumers' copies of the PDK's stack files
#     agree with the PDK this image carries. chiplet-system is outside the pin
#     set, so the summary lists it as not walked.
#     --pdk-copy names the staged repositories' own copies of the key file,
#     which CI cannot see (they are not submodules). interposer-pnr's is the
#     frozen copy its answer keys are measured against; its own suite reports
#     a frozen file that drifted as a skip, so this step is what fails on it.
#     Before step 4a on purpose: the examples are still the tracked bytes here.
#     The checker states which files it walks and what it leaves out.
#     The same RUN holds the staged copies of the PDK's KiCad rule templates to
#     the PDK's bytes: interposer-pnr's export writes its .kicad_dru as that
#     template plus its own rules, and both suites measure against their copy.
#     A refusal ends with the name of the checker that made it. The two take
#     the same consumers differently: the attach plane compares each stack
#     file (--pdk-copy), the templates a copy's whole PDK root (--copy).
RUN /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_attach_plane.py \
        --pdk /opt/adk-tools/OpenIntM4TM2 \
        --interconnect /opt/adk-tools/IHP-Interconnect-IntM4TM2 \
        --tree tools/chiplet_kicad_plugin=/opt/adk-tools/chiplet_kicad_plugin \
        --tree tools/chiplet-studio=/opt/adk-tools/chiplet-studio \
        --tree examples=/opt/adk-tools/examples \
        --tree tools/gds_to_kicad=/opt/adk-tools/gds_to_kicad \
        --tree tools/IHP-Interconnect-IntM4TM2=/opt/adk-tools/IHP-Interconnect-IntM4TM2 \
        --tree chiplet-cosim=/opt/adk-tools/staging/chiplet-cosim \
        --tree chiplet-spec=/opt/adk-tools/staging/chiplet-spec \
        --tree interposer-pnr=/opt/adk-tools/staging/interposer-pnr \
        --tree packaging-flow=/opt/adk-tools/staging/packaging-flow \
        --exemptions /opt/adk-tools/ci/attach-plane-exemptions.txt \
        --pdk-copy chiplet-spec/description=/opt/adk-tools/staging/chiplet-spec/description/tests/fixtures/interposer_pdk/libs.tech/intm4tm2_stack.json \
        --pdk-copy packaging-flow=/opt/adk-tools/staging/packaging-flow/tests/fixtures/interposer_pdk/libs.tech/intm4tm2_stack.json \
        --pdk-copy interposer-pnr=/opt/adk-tools/staging/interposer-pnr/tests/data/frozen/interposer_pdk/libs.tech/intm4tm2_stack.json \
        || { echo "step 0c: check_attach_plane.py refused (above)" >&2; exit 1; } \
    && /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_kicad_templates.py \
        --pdk /opt/adk-tools/OpenIntM4TM2 \
        --copy interposer-pnr=/opt/adk-tools/staging/interposer-pnr/tests/data/frozen/interposer_pdk \
        --copy packaging-flow=/opt/adk-tools/staging/packaging-flow/tests/fixtures/interposer_pdk \
        || { echo "step 0c: check_kicad_templates.py refused (above)" >&2; exit 1; }

# 0e. The tracked digest in the table's chiplet-cosim row is packaging-flow's:
#     nix/test_tracked_tree.py runs tool_identity.py's port and packaging-flow's
#     own tracked_tree_sha256 on every fixture tree and requires the same
#     digest or the same refusal. Its live test skips where packaging_flow does
#     not import, and a skip exits 0, so every id in ci/tracked-tree-tests.txt
#     has to be present and passed in the junit (xunit1, whose `file` attribute
#     the checker keys on). The fixtures are made in a scratch folder.
RUN cd /opt/adk-tools \
    && out=$(mktemp -d) \
    && TMPDIR="$out" PYTHONDONTWRITEBYTECODE=1 /opt/adk-tools/venv/bin/python3 -m pytest \
           nix/test_tracked_tree.py --rootdir /opt/adk-tools -p no:cacheprovider -q -rfEsxX \
           -o junit_family=xunit1 --junitxml="$out/tracked.xml" \
    && /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_required_tests.py \
           /opt/adk-tools/ci/tracked-tree-tests.txt --junit adk-tools="$out/tracked.xml" \
    && rm -rf "$out"

# 0f. KLayout carries its commit's day, not the day it was built (studio-builder,
#     the KLayout RUN), so a rebuild from the same sources gives the bytes the
#     tool identity table keyed before. Without the stamp fixed, the binary
#     carries the build day, and every chiplet-studio commit that rebuilt
#     KLayout moved its sha256 with no change to its sources. The day it is
#     checked on is refused too, unless that is the commit's.
ARG KLAYOUT_SOURCE_DATE_EPOCH
RUN k=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release/klayout; \
    case "$KLAYOUT_SOURCE_DATE_EPOCH" in \
        ''|*[!0-9]*) echo "KLAYOUT_SOURCE_DATE_EPOCH is '$KLAYOUT_SOURCE_DATE_EPOCH'; build.sh passes the KLayout commit's time" >&2; exit 1 ;; \
    esac; \
    want=$(date -u -d "@$KLAYOUT_SOURCE_DATE_EPOCH" +%Y-%m-%d); \
    if ! grep -qaF "$want" "$k"; then echo "$k does not carry $want, the KLayout commit's day" >&2; exit 1; fi; \
    today=$(date -u +%Y-%m-%d); \
    if [ "$today" != "$want" ] && grep -qaF "$today" "$k"; then echo "$k carries today's date $today, a build day" >&2; exit 1; fi; \
    echo "klayout carries $want, its commit's day"

# 0g. interposer-pnr's single-layer route on this image's OpenROAD. Nothing else
#     in this stage runs interposer-pnr, so an OpenROAD whose rdl_route drops
#     nets given by name (dcf36133 does, see interposer-pnr's router.py) shipped
#     unnoticed. Three live cases route the two-die fixture with the staged
#     interposer-pnr and $OPENROAD; a skip (no binary found) is a failure, via
#     the required-tests list. The cases take under a minute, so the timeout
#     ends a hang (rdl_route has spun for minutes on another build) in five.
RUN cd /opt/adk-tools/staging/interposer-pnr \
    && out=$(mktemp -d) \
    && TMPDIR="$out" PYTHONDONTWRITEBYTECODE=1 timeout -k 10 300 \
           /opt/adk-tools/venv/bin/python3 -m pytest tests/test_router.py \
           -m live_openroad -p no:cacheprovider -q -rfEsxX \
           -o junit_family=xunit1 --junitxml="$out/ipnr-route.xml" \
    && /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_required_tests.py \
           /opt/adk-tools/ci/ipnr-route-tests.txt --junit interposer-pnr="$out/ipnr-route.xml" \
    && rm -rf "$out"

# 1. ADK suite (boundary manifest validation, DRC regressions via the klayout
#    CLI wrapper, DRU generator). Cheap and fail-fast, so it runs first.
#
# 1a. What the suite needs in order to have run at all, asserted before it
#     rather than read off its result. pytest does not call a skip a pass: it
#     prints it as its own outcome and then exits 0, and a RUN step reads the
#     exit status and nothing else. So a missing or incompatible reader and a
#     demo pin list the fixtures cannot find both leave this stage green with
#     the checks that matter never executed -- which is how the
#     assembly-admission suite came to report 17 skips here and pass the step
#     anyway. Both facts are the suite's own:
#     load_admission() is the checks package's existing API, and DEMO_PINS is
#     the constant the demo fixtures gate on. Nothing new is validated; this
#     only refuses to let an absence pass as an answer.
#
#     DEMO_PINS was written here as a known failure: the suite computed it as
#     ADK_ROOT.parents[1]/examples, which is the repository root in a `tools/adk`
#     checkout and /opt in this image, where the verify stage puts the examples
#     at /opt/adk-tools/examples (verified in a verify image of that time, where
#     /opt/examples does not exist). The ADK binds it now, as of the tools/adk
#     gitlink: the layouts are listed, ADK_ROOT.parent is one of them, and that
#     is /opt/adk-tools here, so the demo fixtures resolve in this image with no
#     variable set. An explicit ADK_DEMO_PINS still outranks every base if a
#     caller wants to name the file.
#
#     What was NOT done from this side, and should stay not done: a symlink at
#     /opt/examples. It would have made the path resolve while leaving the ADK's
#     assumption wrong and silently satisfied, which is the ambient-path
#     resolution the schema loader in chiplet-spec was itself corrected for.
#     Each check is one physical line on purpose: a backslash-continued Python
#     string reaches the shell with the next line's indentation in it, and an
#     `unexpected indent` here would read as a broken gate rather than a broken
#     line break.
#
#     CHIPLET_SPEC_ROOT names the STAGED chiplet-spec rather than leaving the
#     ADK's vendored-reader guard to its own fallback, ADK_ROOT.parent/
#     chiplet-spec, which is /opt/chiplet-spec here and does not exist. Left
#     alone the guard skips, and the one check that would notice the vendored
#     copy drifting from the spec it declares reports nothing. Pointing it at
#     the staged tree is also the point: a package verification has to resolve
#     the sibling this image SELECTED, at the commit staging.pin names, not
#     whatever checkout happens to be reachable. The guard resolves the commit
#     the vendored copy declares inside that tree, which is why staging.pin
#     stages chiplet-spec at that commit.
#
#     The third line closes a join nothing else checks. Step 0b compares the
#     vendored copy against chiplet-spec's SOURCE tree, which leaves the
#     installed distribution uncovered: if the image ever installed a
#     chiplet_format_io built from a different ref than the one it stages as
#     the source, step 0b stays green and the image carries two
#     versions of the same code with nothing saying so. Today the two are the
#     same directory, not merely the same pin, because the install is editable
#     off the staged tree -- so this asserts exactly that, and reads the commit
#     back from the manifest rather than from the pin file, since the manifest
#     is what records what actually shipped.
RUN cd /opt/adk-tools/adk \
    && /opt/adk-tools/venv/bin/python3 -c "import sys; sys.path.insert(0, 'checks'); import assembly_admission as adm; adm.load_admission()" \
    && /opt/adk-tools/venv/bin/python3 -c "import chiplet_format_io as c, pathlib, subprocess, json; d=pathlib.Path(c.__file__).resolve().parent; s=pathlib.Path('/opt/adk-tools/staging/chiplet-spec'); assert s in d.parents, 'the installed reader resolves to %s, outside the staged chiplet-spec at %s, so nothing ties it to the commit this image staged' % (d, s); h=subprocess.check_output(['git','-C',str(s),'rev-parse','HEAD'],text=True).strip(); m=json.loads(pathlib.Path('/opt/adk-tools/manifest.json').read_text())['staged']['chiplet-spec']['ref']; assert h==m, 'the installed reader is at %s and the manifest says this image staged %s' % (h, m)" \
    && /opt/adk-tools/venv/bin/python3 -c "import sys; sys.path.insert(0, 'tests'); import test_assembly_admission as t; assert t.DEMO_PINS.is_file(), 'the demo pin list is not where the suite looks (%s), so its two strict demo controls would skip and this stage would still be green' % t.DEMO_PINS" \
    && CHIPLET_SPEC_ROOT=/opt/adk-tools/staging/chiplet-spec \
       /opt/adk-tools/venv/bin/python3 -m pytest tests -q

# 2. gds_to_kicad suite (footprint/symbol writers, blackbox chiplets, GUI
#    pieces under the offscreen Qt platform). Its pyproject.toml deselects the
#    cases that run kicad-cli (-m "not kicad_cli"), because a bare run on a host
#    must not start it; kicad-cli is on PATH here, and a command-line -m
#    replaces that one, so -m "" runs every case.
RUN cd /opt/adk-tools/gds_to_kicad \
    && QT_QPA_PLATFORM=offscreen /opt/adk-tools/venv/bin/python3 -m pytest tests -q -m ""

# 2b. gds2kicad "prior steps" of the two_die_interposer example: its footprints
#     and symbols are produced from the shipped chiplet GDS + pin list, upstream
#     of any board work. Rerun the reproducible steps and diff against the
#     committed library (symbol + I/O pad reproduced, pin-list fidelity, converter
#     liveness on the die GDS), so an upstream gds2kicad change that silently
#     alters the demo artifacts fails here, before step 4 places them on the board.
RUN QT_QPA_PLATFORM=offscreen /opt/adk-tools/venv/bin/python3 \
        /usr/local/bin/adk-verify-chiplet-artifacts

# 3. Interposer PDK suite (bump mirror PCell regressions).
#    One node is deselected, by its exact id: the GUI smoke of the PDK's
#    Verification menu. That dialog subclasses pya.QDialog, and the image's
#    KLayout is built without Qt bindings (-without-qtbinding, studio-builder
#    above), so the test hangs on the AttributeError until its own timeout.
#    The menu's DRC/LVS runners (run_drc.py, run_lvs.py) are covered by the rest
#    of the suite. Drop the deselect when the image's KLayout gains Qt bindings.
#    pytest matches --deselect as a prefix of the node id, which is relative to
#    the rootdir (the PDK root, where its pyproject.toml is), not to this cd.
#    That pyproject.toml deselects the cases that start the klayout binary,
#    ngspice or xschem (live_tool) and the long field solves (slow); the image
#    has the tools, and -m "" replaces that selection, so every case runs, as
#    the PDK's own full-suite job runs them.
RUN cd /opt/adk-tools/OpenIntM4TM2/libs.tech/klayout \
    && /opt/adk-tools/venv/bin/python3 -m pytest intm4tm2_tests -q -m "" \
        --deselect libs.tech/klayout/intm4tm2_tests/test_verification_gui.py::test_configure_run_and_open_reports_in_real_klayout

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

# 4d. chiplet-flow's view XOR under this image's KLayout. An emitted die whose
#     LVS read a fill-expanded view records the verdict of an XOR of that view
#     against the shipped GDS, run with the klayout on PATH. Each control in
#     tests/test_view_xor.py asserts the one value a known change must read.
#     They skip without klayout, and a skip exits 0, so PASS is three readings:
#     pytest under HCDP_STRICT_LIVE=1 (chiplet-flow's conftest fails any item
#     skip); every id in ci/image-xor-tests.txt present and passed in the junit
#     (xunit1, whose `file` attribute the checker keys on); and `klayout -v`
#     naming the version of the venv's klayout module, which KLAYOUT_PIP pins.
#     The Studio build's submodule tag follows KLAYOUT_PIP by hand only, so
#     that reading is also the one check that the binary agrees with the pin.
RUN cd /opt/adk-tools/staging/chiplet-flow \
    && bin=$(command -v klayout) \
    && real=$(realpath "$bin") \
    && version=$(klayout -v) \
    && module=$(/opt/adk-tools/venv/bin/python3 -c 'import klayout; print(klayout.__version__)') \
    && echo "klayout: $bin -> $real, '$version', module $module" \
    && test "$version" = "KLayout $module" \
    && home=$(mktemp -d) && out=$(mktemp -d) \
    && CHIPLET_FLOW_KLAYOUT_REALPATH="$real" CHIPLET_FLOW_KLAYOUT_VERSION="$version" \
       KLAYOUT_HOME="$home" HCDP_STRICT_LIVE=1 PYTHONDONTWRITEBYTECODE=1 \
       /opt/adk-tools/venv/bin/python3 -m pytest tests/test_view_xor.py -m klayout \
           -p no:cacheprovider -q -rfEsxX -o junit_family=xunit1 \
           --basetemp="$out/bt" --junitxml="$out/xor.xml" \
    && /opt/adk-tools/venv/bin/python3 /opt/adk-tools/ci/check_required_tests.py \
           /opt/adk-tools/ci/image-xor-tests.txt --junit chiplet-flow="$out/xor.xml"

# 5. Chiplet Studio full suite (gated tests resolve the PDK/tool roots via the
#    env baked in deps), run the way Studio's own gate runs it: as a user, on
#    an X display.
#    - As nobody. RUN steps are root, and root gets past the permission checks
#      the save and import cases exist to exercise: the suite skips those
#      cases as root, and the ones that cannot skip fail. nobody owns only
#      what ctest and the cases write, build/tests (the working directory,
#      where the test list is discovered) and build/Testing, plus a HOME and
#      TMPDIR of its own. The sources and the rest of build/ stay root's, so
#      a stray write there fails its case instead of passing.
#    - Under xvfb-run with the xcb platform. The 3D mesh cases need a native
#      OpenGL context and do not skip without one; offscreen with no display
#      has none. Xvfb's GLX runs on the image's llvmpipe. No -logfile: this
#      Xvfb does not take it and would not start. Its stderr is kept and
#      printed if the suite fails (nobody cannot reopen root's /dev/stderr).
#    - CROSS_READER_PYTHON: the cross-reader check reads the writer's output
#      with PyYAML, which the image venv carries at Studio's own pin.
RUN cd /opt/adk-tools/chiplet-studio/build \
    && mkdir -p Testing /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix \
    && chown -R 65534:65534 tests Testing \
    && home=$(mktemp -d) && tmp=$(mktemp -d) \
    && chown 65534:65534 "$home" "$tmp" \
    && { setpriv --reuid=65534 --regid=65534 --clear-groups \
       env HOME="$home" TMPDIR="$tmp" QT_QPA_PLATFORM=xcb \
       LD_LIBRARY_PATH=/opt/adk-tools/chiplet-studio/extern/klayout/bin-release \
       WIREBOND_DEMO_CHIPLET=/opt/adk-tools/examples/two_die_interposer/outputs/two_die_interposer.chiplet \
       CROSS_READER_PYTHON=/opt/adk-tools/venv/bin/python3 \
       xvfb-run -a -e "$tmp/xvfb-run.err" \
           --server-args="-noreset -screen 0 1920x1080x24" \
       ctest --output-on-failure \
       || { rc=$?; echo "xvfb-run stderr:" >&2; cat "$tmp/xvfb-run.err" >&2; exit "$rc"; }; } \
    && rm -rf "$home" "$tmp"

# 6. Plugin suites (pcbnew available here, so the env-gated tests run too).
#    The writer fixtures discover the demo board via CHIPLET_WRITER_BOARD /
#    HYPERLYNX_WRITER_BOARD before falling back to hardcoded paths; point them
#    at the board's new kicad/ location so the byte-exact regression tests run
#    instead of self-skipping.
#    One run per plugin package, from the repository root, which is what the
#    root pyproject.toml (importlib import mode, both suites' conftest) is
#    written for and what the repository's CI matrix does. That file deselects
#    the cases that start the klayout binary (-m "not klayout_binary"); the
#    binary is on PATH here, and -m "" replaces that selection.
RUN cd /opt/adk-tools/chiplet_kicad_plugin \
    && for plugin in chiplet_export resizer_passive_elements; do \
        echo "=== plugin suite: $plugin" \
        && CHIPLET_WRITER_BOARD=/opt/adk-tools/examples/two_die_interposer/kicad/two_die_interposer.kicad_pcb \
           HYPERLYNX_WRITER_BOARD=/opt/adk-tools/examples/two_die_interposer/kicad/two_die_interposer.kicad_pcb \
           /opt/adk-tools/venv/bin/python3 -m pytest "plugins/$plugin/tests" -q -m "" \
        || exit 1; \
    done

# 7. End-to-end smoke exactly as a user would run it.
RUN adk-smoke /opt/adk-tools/examples/two_die_interposer/kicad/two_die_interposer.kicad_pcb

# 8. The co-simulation engine is the one chiplet-cosim's capability entry
#    sg13g2.stdcell.icarus13 was shown on: the store holds exactly the listed
#    closure (with the mixed-signal engine's, the viewers' and the Verilog-AMS
#    engine's lists, legs 10, 11 and 12, and nix/host-closure.txt, the die
#    build's tools the image carries, which may share paths with those lists;
#    legs 20 and 21 check those tools), COSIM_DEVSHELL
#    resolves to the devshell pin and holds exactly its three links into the
#    pins, chiplet-cosim's own harness check finds nothing missing, cocotb
#    sits in the env's own site-packages, and the iverilog, vvp and VPI
#    sha256, cocotb version, observer sha256 and runner flags equal the
#    entry's. Then live: the env's interpreter imports cocotb 2.0.0 and finds
#    its libpython, and a small design runs under cocotb's Icarus runner.
#    The checker states what it does not check: the half of the context a
#    run's own inputs give.
ARG NIX_COSIM_IVERILOG_OUTPATH
ARG NIX_COSIM_COCOTB_OUTPATH
ARG NIX_COSIM_PYTHON_OUTPATH
ARG NIX_COSIM_DEVSHELL_OUTPATH
RUN /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_cosim_engine.py \
        --entry sg13g2.stdcell.icarus13 \
        --closure /opt/adk-tools/nix/cosim-closure.txt --store /nix/store \
        --also-closure /opt/adk-tools/nix/mixsim-closure.txt \
        --also-closure /opt/adk-tools/nix/viewer-closure.txt \
        --also-closure /opt/adk-tools/nix/vams-closure.txt \
        --carried /opt/adk-tools/nix/host-closure.txt \
        --iverilog "$NIX_COSIM_IVERILOG_OUTPATH" \
        --cocotb "$NIX_COSIM_COCOTB_OUTPATH" \
        --python "$NIX_COSIM_PYTHON_OUTPATH" \
        --devshell "$NIX_COSIM_DEVSHELL_OUTPATH"

# 9. Every layer sheet and stackup the staged packaging-flow declares resolves
#    here and reads: packaging_flow.technologies is imported from the staged
#    tree's src/ (a copy from anywhere else is refused) and its TECHNOLOGIES
#    read, so the strings checked are packaging-flow's own. Every
#    ${VAR} is a name in Studio's path table, read from the source this image's
#    Studio was built from, and is set to a root holding that table's marker;
#    every file is there, a sheet names numbered layers and has its .lyt
#    beside it, and a stackup holds index/type layers. A stackup layer its
#    sheet does not name is said, not failed. The checker defaults to these paths;
#    ci/check_image_tech_files.sh runs the same invocation in a built verify
#    image against another packaging-flow tree, without a build.
COPY --from=studio-builder /opt/adk-tools/chiplet-studio/src/formats/ChipletFormat.cpp \
        /opt/adk-tools/ci/studio-ChipletFormat.cpp
RUN /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_tech_files.py

# 10. The mixed-signal engine is the one staging.pin names, and it runs: the
#     store holds every path nix/mixsim-closure.txt lists and the three pins
#     are among them, MIXSIM_DEVSHELL links into the pins, and its osdi holds
#     exactly the files nix/sg13g2-osdi.sha256 names, by their bytes. Then live:
#     ngspice loads the six with pre_osdi and solves (its ablation, an absent
#     file, must print the open error), and d_cosim runs Icarus given libvvp.so
#     and the VPI module by absolute path, unmoved by a non-VVP libvvp.so on
#     LD_LIBRARY_PATH that the name search does take.
ARG NIX_MIXSIM_NGSPICE_OUTPATH
ARG NIX_MIXSIM_IVERILOG_OUTPATH
ARG NIX_SG13G2_OSDI_OUTPATH
RUN /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_mixsim_engine.py \
        --closure /opt/adk-tools/nix/mixsim-closure.txt --store /nix/store \
        --osdi-sha256 /opt/adk-tools/nix/sg13g2-osdi.sha256 \
        --ngspice "$NIX_MIXSIM_NGSPICE_OUTPATH" \
        --iverilog "$NIX_MIXSIM_IVERILOG_OUTPATH" \
        --osdi "$NIX_SG13G2_OSDI_OUTPATH"

# 11. The viewers are the ones staging.pin names, and each opens what it is
#     for, headless: the store holds every path nix/viewer-closure.txt lists
#     and the three pins are among them, /usr/local/bin/gtkwave, gaw and xschem
#     link into the pins, and XSCHEM_SHAREDIR is the Xschem pin's share
#     directory with the device symbols. Then live, under Xvfb: GTKWave runs
#     a Tcl script on a VCD and on the FST its own vcd2fst made of it, and must
#     read the clock's name, the time range and its value before and after the
#     edge; GAW, driven over its command socket, must refuse a variable before
#     any load and one the file lacks, load the binary raw file the mixed-signal
#     engine's ngspice wrote, and export the waves it shows equal to the file's
#     own values; Xschem must print the version its pin's name carries,
#     netlist a small RC schematic to exactly its three elements with no X,
#     and open it in a mapped window that reads back its instances.
ARG NIX_GTKWAVE_OUTPATH
ARG NIX_GAW_OUTPATH
ARG NIX_XSCHEM_OUTPATH
RUN /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_viewers.py \
        --closure /opt/adk-tools/nix/viewer-closure.txt --store /nix/store \
        --gtkwave "$NIX_GTKWAVE_OUTPATH" \
        --gaw "$NIX_GAW_OUTPATH" \
        --xschem "$NIX_XSCHEM_OUTPATH" \
        --ngspice /opt/adk-tools/mixsim-devshell/bin/ngspice

# 12. The Verilog-AMS engine is the one staging.pin names, and it builds and
#     solves a model: the store holds every path nix/vams-closure.txt lists
#     and the two pins are among them, VAMS_DEVSHELL resolves to the devshell
#     pin and holds exactly its six links, into gnucap's prefix and the
#     compiler, and no startup file gnucap would read exists. Then live, with
#     neither LD_LIBRARY_PATH nor GNUCAP_PLUGPATH set and gnucap by absolute
#     path, a deck on stdin: gnucap-mg-vams writes C++ for a small resistor
#     module, the devshell's g++ builds it into a plugin with gnucap-conf's
#     flags, and a divider of two instances solves to the ratio their
#     parameters give, for two ratios. Both version lines and the default
#     plugins' must read the version below, and every plugin the run loaded
#     must be the model or one under the gnucap pin.
ARG NIX_VAMS_GNUCAP_OUTPATH
ARG NIX_VAMS_DEVSHELL_OUTPATH
RUN /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_vams_engine.py \
        --closure /opt/adk-tools/nix/vams-closure.txt --store /nix/store \
        --gnucap "$NIX_VAMS_GNUCAP_OUTPATH" \
        --devshell "$NIX_VAMS_DEVSHELL_OUTPATH" \
        --version "trace_adv 2026.09.25"

# 13. The kinds packaging-flow ships can run in this image, three frames read
#     from packaging-flow's own kinds.shipped(), table reader and deck readers:
#     every tool a kind names has a row that keys as content; the argv[0] each
#     run_here kind's own argv builder makes from that table, as verify-run
#     makes it, answers its version flag with no environment at all (env -i);
#     and every deck a kind names resolves to its file set below the PDK root
#     variable the kind reads. As nobody, so nothing it starts writes into a
#     tree. After leg 12 on purpose: no earlier step's numbering moved with it.
RUN setpriv --reuid=65534 --regid=65534 --clear-groups \
        /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_kind_contracts.py

# 14. `chiplet-system-description` on PATH is bin/chiplet-system-description,
#     and the console script it execs exists: as nobody, the description's own
#     test helper, loaded by its staged path (description/tests/source_dies.py,
#     write_source), writes a source system.chiplet and its two die folders
#     into a temp dir, and `chiplet-system-description check --json` on it, by
#     name and with no PYTHONPATH,
#     exits 0 (no refusing finding) and writes one chiplet_system.check report.
#     A missing script, a wrapper off PATH or a changed helper fails here.
#     After leg 13, so no earlier step's numbering moves.
RUN set -e; as_nobody="setpriv --reuid=65534 --regid=65534 --clear-groups"; \
    dir="$($as_nobody mktemp -d)"; \
    src="$($as_nobody /opt/adk-tools/venv/bin/python3 -B -c 'import importlib.util, pathlib, sys; \
spec = importlib.util.spec_from_file_location("source_dies", sys.argv[1]); \
helper = importlib.util.module_from_spec(spec); \
spec.loader.exec_module(helper); \
print(helper.write_source(pathlib.Path(sys.argv[2])))' \
        /opt/adk-tools/staging/chiplet-spec/description/tests/source_dies.py "$dir")"; \
    rc=0; out="$(env -u PYTHONPATH $as_nobody chiplet-system-description check --json "$src")" || rc=$?; \
    rm -rf "$dir"; \
    [ "$rc" = 0 ] || { printf '%s\n' "$out"; echo "chiplet-system-description check: rc $rc on the helper's source"; exit 1; }; \
    printf '%s\n' "$out" | /opt/adk-tools/venv/bin/python3 -B -c 'import json, sys; \
r = json.load(sys.stdin); \
assert r["format"] == "chiplet_system.check", r; \
print("chiplet-system-description check: rc 0,", len(r["findings"]), "findings")'

# 15. The gf180mcuD DRC deck resolves where chiplet.drc looks for it: as
#     nobody, packaging-flow's own deck_files resolves tech/drc/gf180mcu.drc
#     below $PDK_ROOT/gf180mcuD/libs.tech/klayout (through the link to the GF180
#     tree), to 65 files, and nothing below that tree is a link. After leg 14,
#     so no earlier step's numbering moves.
RUN setpriv --reuid=65534 --regid=65534 --clear-groups \
        /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_gf180_deck.py --files 65

# 16. The gf180mcuD pieces a LibreLane build reads are the ones
#     gf180mcu-pdk.sha256 lists, byte for byte and no file more, below
#     GF180MCU_PDK_ROOT; every path is root's, no file is writable, and no
#     folder is writable by anyone but root (a folder a COPY makes is 0755),
#     and the PDK's librelane config resolves through $PDK_ROOT/gf180mcuD too.
#     Each refusal says which of these failed. Modes hold the tree read-only for
#     every uid but 0; root writes it whatever the modes say. run.sh runs the
#     container as the caller's uid (--user), and bin/flow runs through run.sh.
RUN set -e; cd "$GF180MCU_PDK_ROOT"; \
    list=/opt/adk-tools/ci/gf180mcu-pdk.sha256; \
    set -- gf180mcuD/SOURCES gf180mcuD/libs.ref gf180mcuD/libs.tech/librelane \
        gf180mcuD/libs.tech/magic gf180mcuD/libs.tech/netgen \
        gf180mcuD/libs.tech/klayout/tech/gf180mcu.lyt gf180mcuD/libs.tech/klayout/tech/gf180mcu.map; \
    sha256sum -c --strict --quiet "$list" \
        || { echo "gf180 pdk: a file's bytes are not the ones $list lists"; exit 1; }; \
    find "$@" ! -type d | LC_ALL=C sort > /tmp/gf180-found; \
    cut -c67- "$list" | diff /tmp/gf180-found - > /tmp/gf180-diff \
        || { head -n 20 /tmp/gf180-diff; echo "gf180 pdk: the files here are not the ones $list lists"; exit 1; }; \
    rm -f /tmp/gf180-found /tmp/gf180-diff; \
    bad="$(find "$@" ! -user root -o -perm /022 -o ! -type d -perm /222)"; \
    [ -z "$bad" ] \
        || { printf '%s\n' "$bad" | head -n 20; echo "gf180 pdk: writable or not root's"; exit 1; }; \
    test -f "$PDK_ROOT/gf180mcuD/libs.tech/librelane/config.tcl" \
        || { echo "gf180 pdk: no librelane config.tcl through \$PDK_ROOT/gf180mcuD"; exit 1; }; \
    echo "gf180 pdk: $(wc -l < "$list") files as listed, read-only"

# 17. The check that every file chiplet-cosim's mixed capability entries pin
#     is here with those bytes (the engine's files, the VPI modules a compiled
#     file loads, the interpreter, libpython and cocotb, and each PDK's reached
#     model and OSDI files) runs its self-test only: fake data, nothing in this
#     image is read, the same self-test the host CI runs. It shows only that the
#     check runs as nobody under the image's venv. The three recorded entries
#     pin stdbuf and libstdbuf.so from coreutils 9.4-3ubuntu6.2 and this image
#     ships 9.4-3ubuntu6.3, so none of them holds here. The full RUN, with the
#     engine's five COSIM_* paths and --min-entries 3, is in this file's history
#     (the oldest commit git log -S'--min-entries 3' -- Dockerfile lists); it
#     comes back once chiplet-cosim's capability table holds mixed entries
#     recorded against coreutils 9.4-3ubuntu6.3 and the current runner
#     (its digest moved with chiplet-cosim's pin; this check does not read it).
#     After leg 16.
RUN setpriv --reuid=65534 --regid=65534 --clear-groups \
        /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_cosim_mixed_entries.py --self-test

# 18. CHIPLET_FLOW_ROOT names the staged chiplet-flow, and the plans a builds
#     entry reads from it are there: vehicle/plans/driver.pins.json and
#     driver.blackbox.json, readable and not writable as nobody. After leg 17,
#     so no earlier step's numbering moves.
RUN set -e; as_nobody="setpriv --reuid=65534 --regid=65534 --clear-groups"; \
    want=/opt/adk-tools/staging/chiplet-flow; \
    [ "$CHIPLET_FLOW_ROOT" = "$want" ] \
        || { echo "CHIPLET_FLOW_ROOT is '$CHIPLET_FLOW_ROOT', not $want"; exit 1; }; \
    [ "$(realpath "$CHIPLET_FLOW_ROOT")" = "$want" ] \
        || { echo "CHIPLET_FLOW_ROOT resolves to $(realpath "$CHIPLET_FLOW_ROOT"), not $want"; exit 1; }; \
    for f in vehicle/plans/driver.pins.json vehicle/plans/driver.blackbox.json; do \
        $as_nobody test -f "$CHIPLET_FLOW_ROOT/$f" -a -r "$CHIPLET_FLOW_ROOT/$f" \
            || { echo "CHIPLET_FLOW_ROOT: no readable $f"; exit 1; }; \
        ! $as_nobody test -w "$CHIPLET_FLOW_ROOT/$f" \
            || { echo "CHIPLET_FLOW_ROOT: $f is writable as nobody"; exit 1; }; \
    done; \
    echo "CHIPLET_FLOW_ROOT: $CHIPLET_FLOW_ROOT, plans driver.pins.json and driver.blackbox.json there"

# 19. The die build environment (ci/check_die_build_env.sh): packaging-flow's
#     own lookup of its die flow, as nobody on the image's PATH, finds the
#     image's wrapper /usr/local/bin/chiplet-die-flow, not the venv's script behind
#     it; the launcher is root's, 0555, not a link, with the sha256 build.sh
#     labels; and its PYTHONPATH is, in order, the roots nix/build-env.sh
#     resolves from the staged trees, each holding its packages and each
#     exactly /opt/adk-tools/staging/<name>/<subdirectory>, the path an overlay
#     of <name> mounts at. After leg 18, so no earlier step's numbering moves.
ARG LAUNCHER_SHA256
RUN setpriv --reuid=65534 --regid=65534 --clear-groups \
        bash /opt/adk-tools/ci/check_die_build_env.sh \
        --launcher /opt/adk-tools/librelane-launcher/librelane --sha256 "$LAUNCHER_SHA256" \
        --python /opt/adk-tools/venv/bin/python3 --nix-lib /opt/adk-tools/nix \
        --staging /opt/adk-tools/staging --die-flow /usr/local/bin/chiplet-die-flow

# 20. The host closure (ci/check_store_closure.py): nix/host-closure.lock lists
#     exactly host-closure.txt's paths, and each path in this image's store has
#     the narHash the lock pins, recomputed here from its NAR serialisation.
RUN /opt/adk-tools/venv/bin/python3 -B /opt/adk-tools/ci/check_store_closure.py \
        --lock /opt/adk-tools/nix/host-closure.lock --list /opt/adk-tools/nix/host-closure.txt \
        --nar-lib /opt/adk-tools/nix

# 21. The die build's tools with no /nix mounted (ci/check_store_tools.sh; a
#     build step mounts nothing from the host): each tool of the fixed ENV lies
#     in a path the closure list names; as nobody, LibreLane, OpenROAD and the
#     launcher start; the PDK tree holds LibreLane's files; the launcher is the
#     file its ENV sha256 names; the wrapper hands a build-die that PDK and the
#     launcher pair unchanged; nothing under /nix/store is writable as nobody or
#     other than root's. It prints each value it checks.
RUN bash /opt/adk-tools/ci/check_store_tools.sh --list /opt/adk-tools/nix/host-closure.txt \
        --nix-lib /opt/adk-tools/nix --die-flow /usr/local/bin/chiplet-die-flow
