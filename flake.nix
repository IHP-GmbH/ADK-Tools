# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
#
# Host-side Nix environment for the heterogeneous-integration flow. It does not
# replace the Docker image.
#
# What lives here:
#
#   packages.klayout, packages.klayout-python, devShells.default
#       KLayout at the image's version (binary + pya, and its Python module
#       for this flake's python3), so a consumer flake can follow this one
#       and get the same store path. The CLI shell below uses this same
#       KLayout.
#
#   packages.cli-env, packages.openroad, devShells.cli
#       The environment for chiplet-flow's command line up to the signoff gate
#       on a ready-die spec: the interpreter, the third-party Python it imports
#       (klayout.db among it: the flow's start-of-run check demands a klayout.db
#       interpreter for some chains), the klayout binary, git and OpenROAD. The flow's own packages (chiplet-flow, chiplet-system,
#       interposer-pnr, chiplet-format-io, assembly-lvs, chiplet-cosim) are NOT built into the store. They
#       stay git checkouts installed editable, as in the image, because the
#       flow records each package's identity with `git rev-parse` on the
#       directory it imports from; a store copy has no repository and verify
#       refuses the full tier on it. That is a transition, not the packaging
#       target: moving them into the store needs a Flow-owned identity change
#       first. See docs/NIX.md.
#
#   packages.librelane, packages.librelane-src, packages.sg13g2-pdk,
#   devShells.build
#       The build variant (docs/E2E_BUILD_VARIANT.md): LibreLane as the die
#       builds ran it (staging.pin's LIBRELANE_REV and LIBRELANE_PATCHES) and
#       the sg13g2 PDK at the Dockerfile's SG13G2_REF, copied into one tree,
#       added to the cli shell. The cli shell and its pins are untouched.
#
#   packages.kicad-fork-base, packages.tail-export-python,
#   packages.tail-wrappers, devShells.tail
#       The KiCad fork's base build (the tail needs its pcbnew and Python
#       module), from the fork at the tools/kicad gitlink; the interpreter
#       chiplet-export runs under, which reaches the fork's pcbnew; and the
#       image's three tail commands by the image's names. devShells.tail is
#       the cli shell plus these and the sg13g2 PDK (docs/NIX.md, "The tail
#       shell"). The cli and build shells are untouched by them.
#
#   packages.tools-python
#       The image's tools Python: python3 with pcbnew from
#       kicad-fork-base, wx, a Qt-free KLayout module, PyQt6, pytest with
#       xdist and the packages the staged trees need. The image's worker venv
#       is made from it.
#
#   packages.cosim-iverilog, packages.cosim-cocotb, packages.cosim-python,
#   packages.cosim-devshell
#       The co-simulation engine the image ships, for chiplet-cosim: Icarus,
#       cocotb 2.0.0 and a python3.13 with cocotb in its site-packages, at the
#       store paths chiplet-cosim's capability entry was shown on, and the
#       devshell COSIM_DEVSHELL names: three links into them. The image
#       carries their closure (staging.pin, nix/cosim-closure.txt); the cli,
#       build and tail shells carry the devshell and set COSIM_DEVSHELL to it,
#       and none puts them on PATH.
#
#   packages.mixsim-iverilog, packages.mixsim-ngspice,
#   packages.openvaf-reloaded, packages.openvaf, packages.sg13g2-osdi,
#   checks.sg13g2-osdi-generic, checks.sg13g2-osdi-ngspice,
#   checks.mixsim-explicit-paths
#       The mixed-signal engine, apart from the co-simulation one: an Icarus
#       with libvvp at the same rev, nixpkgs' ngspice 45, and the six sg13g2
#       OSDI models compiled at the PDK input for generic x86-64 by
#       OpenVAF-Reloaded (a build tool, pinned by sha256, shipped nowhere;
#       OpenVAF 23.5.0 only builds the scan's native control). The checks read
#       the OSDI files' instructions and version, load them in that ngspice,
#       and run Icarus under it by explicit library paths. The image carries
#       ngspice, that Icarus and the OSDI models (staging.pin,
#       nix/mixsim-closure.txt, nix/sg13g2-osdi.sha256); no shell here puts
#       them on PATH.
#
#   packages.gtkwave, packages.gaw
#       The waveform viewers the image ships: GTKWave 3.3.121 (VCD and FST,
#       with vcd2fst and fst2vcd) and GAW, which reads ngspice's raw files,
#       both nixpkgs' at the plain nixpkgs this lock names. The image carries
#       their closure (staging.pin, nix/viewer-closure.txt) and puts gtkwave
#       and gaw on PATH; no shell here does.
#
#   packages.xschem
#       Xschem, which draws a mixed run's analog waves from a sheet that loads
#       its raw file: nixpkgs' expression with its source moved to master
#       8d2088ed (3.4.8RC, schematic file_version 1.3; the 3.4.7 tag writes
#       1.2), unpatched, so share/xschem/xschem_library is the source tree's.
#       The image carries it in the viewers' closure and puts xschem on PATH.
#
#   packages.vams-gnucap, packages.vams-devshell
#       The Verilog-AMS engine the image ships: gnucap and its Verilog-AMS
#       model compiler (gnucap-modelgen-verilog) built from source into one
#       prefix at plain nixpkgs, so the compiler's plugins land where gnucap
#       looks for them, and a devshell of links to their commands and to the
#       stdenv's g++, which compiles a model against the same libstdc++. Not
#       in the binary cache. The image carries their closure (staging.pin,
#       nix/vams-closure.txt); no shell here puts them on PATH.
#
#   packages.opensta, checks.opensta
#       The standalone `sta` that LibreLane runs, with the timing-model guard
#       (patches/opensta-timing-model-clocked-output-exists.patch), given to
#       LibreLane through its package argument only; OpenROAD is unchanged.
#       The check proves the wrapper resolves it and runs the guard's test.
#
# This follows LibreLane's nix-eda (6.4.0), NOT the newest. It used to pin
# 7.8.0 / KLayout 0.30.12, which was deliberate at the time and is deliberately
# reversed here: once the image ships LibreLane, 0.30.12 is a fourth KLayout in
# play with nobody who needs it. Bump this only together with LIBRELANE_REV in
# staging.pin, which is what decides the image's side. The librelane input
# below is that same commit, and follows this nix-eda, so its OpenROAD is built
# against the nixpkgs this lock already names.
#
# nix-eda 6.4.0 ships KLayout 0.30.6; the image runs KLAYOUT_PIP (0.30.5) and
# Chiplet Studio builds extern/klayout at v0.30.5. packages.klayout overrides
# nix-eda's KLayout to that version, from the release tarball whose tree is
# that tag's commit (dacb3230), so DRC and LVS run on the same KLayout here and
# in the image. The override is used only by this flake's own outputs and
# shells, never as an overlay, so LibreLane's KLayout and every staging.pin
# outpath but NIX_PYTHON_OUTPATH stay as they are. The shells still read
# ADK_NIX_KLAYOUT_IMAGE_VERSION from the Dockerfile, and evaluation stops when
# it is not this KLayout's version, so a KLAYOUT_PIP bump has to move this
# override in the same change.

{
  description = "IHP ADK tools: Nix environment for the heterogeneous-integration flow";

  nixConfig = {
    extra-substituters = [
      "https://nix-cache.fossi-foundation.org"
    ];
    extra-trusted-public-keys = [
      "nix-cache.fossi-foundation.org:3+K59iFwXqKsL7BNu6Guy0v+uTlwsxYQxjspXzqLYQs="
    ];
  };

  inputs = {
    nix-eda.url = "github:fossi-foundation/nix-eda/6.4.0";
    # LIBRELANE_REV in staging.pin. Only its OpenROAD is used here: OpenROAD
    # dcf36133 as LibreLane packages it. The interposer router was validated on
    # the build its tools_manifest.json names, not on this one, so this is a
    # candidate, not an equivalent.
    librelane.url = "github:librelane/librelane/359bedc8aef928648b2fa247b9e845f4ec934276";
    librelane.inputs.nix-eda.follows = "nix-eda";
    # The sg13g2 PDK the die builds read, for packages.sg13g2-pdk, which the
    # build and tail shells hold: IHP-Open-PDK at the Dockerfile's SG13G2_REF
    # (the evaluation refuses any other), and the two submodules those builds
    # had populated, each at that commit's gitlink. The first lock with them
    # fetches all three (about 0.9 GB). After that, only those three outputs
    # read them, and Nix fetches an input only when an output forces it, so
    # evaluating the other outputs does not fetch them; with a lock that lacks
    # them, every evaluation that may lock fetches them first.
    ihp-open-pdk.url = "github:IHP-GmbH/IHP-Open-PDK/22f43352dd8219f9007eb659e422e0d5fe28c5fb";
    ihp-open-pdk.flake = false;
    pycell4klayout-api.url = "github:IHP-GmbH/pycell4klayout-api/2ffdf79fe51a7c35b361cc854369b5d434c8d478";
    pycell4klayout-api.flake = false;
    pypreprocessor.url = "github:IHP-GmbH/pypreprocessor/cf1ff9bad0fb5338cf1c5b990b2b816b1ea01a64";
    pypreprocessor.flake = false;
    # The KiCad fork at this repository's tools/kicad gitlink, for
    # packages.kicad-fork-base only. The gitlink stays the owner:
    # nix/check-kicad-pin.sh refuses a lock whose rev is not the gitlink's or
    # whose repository is not the one .gitmodules names for tools/kicad. The
    # lock fetches it once (about 680 MiB unpacked).
    kicad-fork.url = "github:IHP-GmbH/KiCad-ADK-MOD/5d12a2566750832a5bdd94ebe520f756901762e4";
    kicad-fork.flake = false;
    # Not used by the outputs. `nix develop` takes its interactive bash from
    # the flake's own `nixpkgs` input and, without one, from the global
    # registry's nixpkgs-unstable, which pins nothing. This names the nixpkgs
    # the lock already holds. To build against another nixpkgs, override
    # nix-eda's (`--override-input nix-eda/nixpkgs <ref>`): overriding this
    # input alone changes the dev shell's bash and nothing the outputs use.
    nixpkgs.follows = "nix-eda/nixpkgs";
  };

  outputs =
    {
      self,
      nix-eda,
      librelane,
      ihp-open-pdk,
      pycell4klayout-api,
      pypreprocessor,
      kicad-fork,
      ...
    }:
    let
      nixpkgs = nix-eda.inputs.nixpkgs;
      lib = nixpkgs.lib;
      # The group of the one line of `file` that `pattern` matches whole, read
      # rather than copied so the flake cannot drift from the file that
      # decides it. Not exactly one matching line is an evaluation error.
      readPin =
        file: pattern:
        let
          found = builtins.filter (m: m != null) (
            map (builtins.match pattern) (lib.splitString "\n" (builtins.readFile file))
          );
        in
        if builtins.length found == 1 then
          builtins.head (builtins.head found)
        else
          throw "${baseNameOf file}: ${toString (builtins.length found)} lines match ${pattern}, expected 1";
    in
    {
      # ADK packages only. Apply nix-eda.overlays.default beside this overlay,
      # never compose it into overlays.default (nix-eda flake API).
      overlays.default = _final: _prev: { };

      legacyPackages = nix-eda.forAllSystems (
        system:
        import nixpkgs {
          inherit system;
          overlays = [
            nix-eda.overlays.default
            self.overlays.default
          ];
        }
      );

      packages = nix-eda.forAllSystems (
        system:
        let
          pkgs = self.legacyPackages.${system};
          # KLayout at KLAYOUT_PIP's version (see the header). nix-eda's
          # klayout.nix takes version and sha256 as arguments; the module is
          # built the way nix-eda builds python3Packages.klayout.
          klayout = pkgs.klayout.override {
            version = "0.30.5";
            sha256 = "sha256-85pX1dAD3FQvGtQpXChtaERx+sr3tWUN83fGxKyXaII=";
          };
          klayout-python = pkgs.python3.pkgs.toPythonModule (
            klayout.override { python3 = pkgs.python3; }
          ).python;
          # Third-party Python only. Each entry is something the CLI path
          # imports; the flow's own packages are added editable by the shell.
          # setuptools and pip are here for that editable install, which runs
          # with --no-index --no-build-isolation and so must find its build
          # backend in this environment rather than on the network.
          cli-env = pkgs.python3.withPackages (ps: [
            ps.pyyaml
            ps.jsonschema
            klayout-python
            (z3-solver ps)
            ps.setuptools
            ps.pip
          ]);
          # nixpkgs' z3 bindings have no dist-info; pip check needs it for assembly-lvs.
          z3-solver =
            ps:
            let
              z3 = ps.z3-solver;
              site = ps.python.sitePackages;
              info = "$out/${site}/z3_solver-${z3.version}.dist-info";
            in
            ps.toPythonModule (
              pkgs.runCommand "z3-solver-${z3.version}-dist-info" { } ''
                mkdir -p ${info}
                ln -s ${z3}/${site}/z3 $out/${site}/z3
                printf 'Metadata-Version: 2.1\nName: z3-solver\nVersion: %s\n' ${z3.version} > ${info}/METADATA
                echo nix > ${info}/INSTALLER
                echo z3 > ${info}/top_level.txt
              ''
            );
          openroad = librelane.packages.${system}.openroad;

          # nixpkgs' unstable KiCad expression with the fork as its source, taken
          # from its base output, so no library set or wrapper comes with it. The
          # flags follow the Dockerfile's kicad-builder stage: Release, wxPython
          # scripting and ngspice on, QA tests and translations off. One flag
          # departs from the image: KICAD_USE_EGL stays nixpkgs' ON. The fork
          # turns EGL on by itself when wxWidgets has the EGL canvas, as
          # nixpkgs' wxGTK does, but builds its bundled EGL GLEW only when the
          # option is set from the start; left unset, it falls back to nixpkgs'
          # GLEW, which has no GL/eglew.h, and the compile fails (KICAD_WAYLAND
          # follows it on, as in nixpkgs). The image's KICAD_USE_OCC names no option
          # in the fork, so it is left out. The added inputs are what the fork's
          # CMake requires and base.nix does not list: ZSTD, SPNAV, Pixman,
          # Freetype, HarfBuzz and Fontconfig in the top-level CMakeLists.txt,
          # Poppler in libs/kiplatform. Two departures from the image are kept on
          # purpose: nixpkgs' two patches (a writable copy of project templates
          # and a runtime stock-data path, both for a read-only store; they change
          # where KiCad looks at run time, not what it writes), and the version
          # string, which names the fork's commit where the image's reads
          # 9.99.0-unknown.
          kicad-fork-base =
            (pkgs.kicad-unstable.override {
              srcs.kicad = kicad-fork;
              withI18n = false;
            }).base.overrideAttrs
              (old: {
                buildInputs = old.buildInputs ++ [
                  pkgs.zstd
                  pkgs.libspnav
                  pkgs.pixman
                  pkgs.freetype
                  pkgs.harfbuzz
                  pkgs.fontconfig
                  pkgs.poppler
                ];
                cmakeFlags =
                  builtins.filter (
                    f: !(lib.hasPrefix "-DKICAD_BUILD_QA_TESTS:" f)
                  ) old.cmakeFlags
                  ++ [ (lib.cmakeBool "KICAD_BUILD_QA_TESTS" false) ];
                doInstallCheck = false;
              });

          # The interpreter chiplet-export runs under in devShells.tail. The
          # image runs its system Python, into which its KiCad installs pcbnew;
          # here it is a venv of the Python the fork's base was built against,
          # with nothing installed and the base's site-packages appended last
          # to the import path by a plain path line in a .pth file (docs/NIX.md,
          # "The tail shell"). The build refuses when pcbnew.py or _pcbnew.so is
          # missing from the base, or when _pcbnew.so does not name this
          # Python's lib directory in its RUNPATH.
          tail-export-python =
            let
              site = pkgs.python3.sitePackages;
            in
            pkgs.runCommandLocal "adk-tail-export-python"
              {
                nativeBuildInputs = [ pkgs.patchelf ];
              }
              ''
                fork=${kicad-fork-base}/${site}
                if [ ! -f "$fork/pcbnew.py" ] || [ ! -e "$fork/_pcbnew.so" ]; then
                  echo "no pcbnew.py and _pcbnew.so under $fork" >&2
                  exit 1
                fi
                rpath="$(patchelf --print-rpath "$(realpath "$fork/_pcbnew.so")")"
                case ":$rpath:" in
                  *:${pkgs.python3}/lib:*) ;;
                  *)
                    echo "$fork/_pcbnew.so does not name ${pkgs.python3}/lib in its RUNPATH ($rpath)" >&2
                    exit 1
                    ;;
                esac
                ${pkgs.python3}/bin/python3 -m venv --without-pip "$out"
                [ -d "$out/${site}" ] || { echo "the venv has no ${site}" >&2; exit 1; }
                printf '%s\n' "$fork" > "$out/${site}/_adk_nix_kicad_fork.pth"
              '';

          # The image's tools Python: one python3 env the image's worker
          # venv is made from with --system-site-packages, so pcbnew, wx, the
          # KLayout module and the third-party packages the staged trees need
          # come from here, and the staged trees still install into the venv
          # editable from the setuptools wheelhouse. Held at the image's pins
          # where a version changes behaviour: z3 4.15.8 (a source build;
          # nixpkgs has 4.15.4), pytest 9.1.0 and jsonschema 4.26.0. KLayout's
          # module is upstream's 0.30.5 wheel (below): it links no Qt, where
          # nix-eda's klayout-python (packages.klayout-python, the lane's)
          # links Qt 5, and gds_to_kicad's GUI loads klayout.db beside PyQt6
          # (Qt 6) in one process. pcbnew comes from kicad-fork-base by its
          # site-packages only.
          tools-python =
            let
              site = pkgs.python3.sitePackages;
              z3 =
                (pkgs.z3.override { python3Packages = pkgs.python3.pkgs; }).overrideAttrs
                  (_old: {
                    version = "4.15.8";
                    src = pkgs.fetchFromGitHub {
                      owner = "Z3Prover";
                      repo = "z3";
                      rev = "z3-4.15.8";
                      hash = "sha256-6HCymxICyxGUWD1aq5Vb1t/AYODFQ6ZzoOr3owr+nvY=";
                    };
                  });
              # pip names the PyPI release 4.15.8.0; the dist-info says so,
              # as the image's freeze does.
              z3-solver =
                ps:
                let
                  info = "$out/${site}/z3_solver-4.15.8.0.dist-info";
                in
                ps.toPythonModule (
                  pkgs.runCommand "z3-solver-4.15.8.0-dist-info" { } ''
                    mkdir -p ${info}
                    ln -s ${z3.python}/${site}/z3 $out/${site}/z3
                    printf 'Metadata-Version: 2.1\nName: z3-solver\nVersion: 4.15.8.0\n' > ${info}/METADATA
                    echo nix > ${info}/INSTALLER
                    echo z3 > ${info}/top_level.txt
                  ''
                );
              pytest =
                ps:
                ps.pytest.overridePythonAttrs (_old: rec {
                  version = "9.1.0";
                  src = ps.fetchPypi {
                    pname = "pytest";
                    inherit version;
                    hash = "sha256-Qd2RSMCAckRjlM79PXlwFwEzWp9Mrmm6kuOfbH9cBhw=";
                  };
                });
              jsonschema =
                ps:
                ps.jsonschema.overridePythonAttrs (_old: rec {
                  version = "4.26.0";
                  src = ps.fetchPypi {
                    pname = "jsonschema";
                    inherit version;
                    hash = "sha256-DCZwfi762Kob/Ft84XDz/MwuSRj/hZibqf+p+ssr4yY=";
                  };
                  doCheck = false;
                });
              # KLayout's module is upstream's binary wheel, not a build of
              # ours: klayout-0.30.5-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl
              # from PyPI, the cp313 twin of the cp312 wheel the image's venv
              # installs today. Its PyPI sdist ships no pysetup.toml, so setup.py
              # builds no extension from it. autoPatchelfHook resolves every
              # library outside the wheel's klayout.libs, with nothing ignored,
              # and the install check reads a GDS back through the compiled core.
              klayout-module =
                ps:
                ps.buildPythonPackage {
                  pname = "klayout";
                  version = "0.30.5";
                  format = "wheel";
                  src = pkgs.fetchurl {
                    url = "https://files.pythonhosted.org/packages/d3/0a/df726d14586987968c00c6af10a6694b7fa6b652e316218cff3f8e10607a/klayout-0.30.5-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl";
                    hash = "sha256-vWAbrfZE6h1+k7t+pD3OAjFmHAgsfdJQZEiWlqKC7mM=";
                  };
                  nativeBuildInputs = [ pkgs.autoPatchelfHook ];
                  buildInputs = [
                    pkgs.stdenv.cc.cc.lib
                    pkgs.expat
                    pkgs.zlib
                  ];
                  doInstallCheck = true;
                  installCheckPhase = ''
                    runHook preInstallCheck
                    missing=0
                    for so in $(find "$out/${site}/klayout" "$out/${site}/klayout.libs" -name '*.so*' -type f); do
                      if ${pkgs.lib.getBin pkgs.glibc}/bin/ldd "$so" | grep -q 'not found'; then
                        echo "unresolved: $so" >&2
                        ${pkgs.lib.getBin pkgs.glibc}/bin/ldd "$so" | grep 'not found' >&2
                        missing=1
                      fi
                    done
                    [ "$missing" = 0 ]
                    PYTHONPATH="$out/${site}" ${ps.python.interpreter} -c '
                    import os, klayout.tlcore, klayout.dbcore, klayout.db as db
                    ly = db.Layout()
                    top = ly.create_cell("TOP")
                    top.shapes(ly.layer(1, 0)).insert(db.Box(0, 0, 1000, 1000))
                    path = os.path.join(os.environ["TMPDIR"], "check.gds")
                    ly.write(path)
                    back = db.Layout()
                    back.read(path)
                    cell = back.cell("TOP")
                    assert cell is not None and cell.shapes(back.layer(1, 0)).size() == 1
                    assert klayout.__version__ == "0.30.5", klayout.__version__
                    print("klayout", klayout.__version__, "GDS round trip ok")
                    '
                    runHook postInstallCheck
                  '';
                  pythonImportsCheck = [ "klayout.db" ];
                };
              pcbnew =
                ps:
                ps.toPythonModule (
                  pkgs.runCommandLocal "kicad-fork-pcbnew" { } ''
                    fork=${kicad-fork-base}/${site}
                    if [ ! -f "$fork/pcbnew.py" ] || [ ! -e "$fork/_pcbnew.so" ]; then
                      echo "no pcbnew.py and _pcbnew.so under $fork" >&2
                      exit 1
                    fi
                    mkdir -p "$out/${site}"
                    for f in "$fork"/*; do ln -s "$f" "$out/${site}/"; done
                  ''
                );
            in
            pkgs.python3.withPackages (ps: [
              (pcbnew ps)
              ps.wxpython
              (klayout-module ps)
              ps.pyyaml
              (jsonschema ps)
              ps.jinja2
              ps.psutil
              (pytest ps)
              ps.pluggy
              ps.pytest-xdist
              (z3-solver ps)
              ps.pyqt6
              ps.setuptools
              ps.pip
            ]);

          # The image's three tail commands, by the image's names, for
          # devShells.tail: nix-tail/<name>.in with the store paths filled in.
          # Each passes its arguments on whole, as bin/<name> does in the image
          # (nix-tail/check-tail-wrappers.sh reads the templates). The build
          # refuses a wrapper that keeps a placeholder.
          tail-wrappers = pkgs.runCommandLocal "adk-tail-wrappers" { } ''
            mkdir -p "$out/bin"
            ${lib.concatMapStrings
              (name: ''
                substitute ${./nix-tail + "/${name}.in"} "$out/bin/${name}" \
                  --subst-var-by bash ${pkgs.bash}/bin/bash \
                  --subst-var-by exportPython ${tail-export-python} \
                  --subst-var-by carrierDrc ${./bin/adk-verify-carrier-drc}
              '')
              [
                "chiplet-export"
                "adk-verify-carrier-drc"
                "adk-drc"
              ]
            }
            if grep -n '@[A-Za-z][A-Za-z]*@' "$out"/bin/*; then
              echo "a placeholder is left in a wrapper" >&2
              exit 1
            fi
            chmod 0555 "$out"/bin/*
          '';

          # LibreLane as the die builds ran it. librelane-src is the librelane
          # input (the evaluation refuses a lock whose rev is not staging.pin's
          # LIBRELANE_REV) with staging.pin's LIBRELANE_PATCHES applied in
          # order: a small derivation of its own, so the patched tree can be
          # realised and compared with LIBRELANE_PATCHED_REV's tree, which needs
          # git and is the pins check's job. The package is LibreLane's own
          # built from that source; the builds' own derivation took a patched
          # checkout as its src, so this one is new: a Python rebuild with
          # LibreLane's own tests, on the input's tools, OpenROAD included.
          librelanePatches = map (p: ./. + "/${p}") (
            builtins.filter (p: p != "") (
              lib.splitString " " (readPin ./staging.pin "LIBRELANE_PATCHES=\"([^\"]*)\"")
            )
          );
          librelane-src =
            assert lib.assertMsg (
              librelane.rev == readPin ./staging.pin "LIBRELANE_REV=([0-9a-f]{40})"
            ) "the locked librelane input is not staging.pin's LIBRELANE_REV";
            pkgs.applyPatches {
              name = "librelane-${librelane.shortRev}-patched";
              src = librelane;
              patches = librelanePatches;
            };
          # LibreLane's standalone OpenSTA (its nix/opensta.nix, 857316ff)
          # with the timing-model guard. LibreLane's STA steps run this `sta`
          # by name, first on its wrapper's PATH, and it is what writes the
          # cores' timing models (write_libs in scripts/openroad/sta/corner.tcl).
          # It reaches LibreLane only through that package's `opensta`
          # argument, never through an overlay: LibreLane's OpenROAD links the
          # unpatched opensta.dev (USE_SYSTEM_OPENSTA), so an overlay would
          # rebuild the router, and this way OpenROAD stays the store path
          # staging.pin's NIX_OPENROAD_OUTPATH names. The unpatched OpenSTA
          # therefore stays in the closure through OpenROAD.
          openstaPatches = [ ./patches/opensta-timing-model-clocked-output-exists.patch ];
          opensta = librelane.packages.${system}.opensta.overrideAttrs (old: {
            patches = (old.patches or [ ]) ++ openstaPatches;
          });
          librelane-patched =
            (librelane.packages.${system}.librelane.override { inherit opensta; }).overridePythonAttrs
              (old: {
                src = librelane-src;
                passthru = (old.passthru or { }) // {
                  adkIdentity = {
                    input = {
                      inherit (librelane) rev narHash;
                    };
                    patches = map (p: {
                      file = baseNameOf p;
                      sha256 = builtins.hashFile "sha256" p;
                    }) librelanePatches;
                    patchedRev = readPin ./staging.pin "LIBRELANE_PATCHED_REV=([0-9a-f]{40})";
                    # The `sta` the wrapper runs, as evaluated; checks.opensta
                    # proves the wrapper resolves it.
                    opensta = {
                      path = "${opensta}";
                      patches = map (p: {
                        file = baseNameOf p;
                        sha256 = builtins.hashFile "sha256" p;
                      }) openstaPatches;
                    };
                  };
                };
              });
          # An interpreter that serves exactly that LibreLane package, for
          # the die cores: chiplet-flow's core preparation takes the upstream
          # wrapper, $ADK_NIX_LIBRELANE/bin/librelane, and an interpreter
          # whose site-packages resolve to the same package, and refuses the
          # shell's own Python (no librelane) and the generated launcher (a
          # script, not an interpreter). In the build shell's closure only,
          # never on its PATH.
          librelane-python = librelane-patched.pythonModule.withPackages (_: [ librelane-patched ]);

          # PDK_ROOT for the die builds, with PDK=ihp-sg13g2: the three PDK
          # inputs copied into one tree with the repository's layout, and
          # pdk-source.json at its root stating them (nix/sg13g2-pdk.nix). The
          # evaluation refuses a parent other than the Dockerfile's SG13G2_REF.
          sg13g2-pdk =
            assert lib.assertMsg (
              ihp-open-pdk.rev == readPin ./Dockerfile "ARG SG13G2_REF=([0-9a-f]{40})"
            ) "the locked ihp-open-pdk input is not the Dockerfile's SG13G2_REF";
            pkgs.callPackage ./nix/sg13g2-pdk.nix {
              parent = {
                input = "ihp-open-pdk";
                src = ihp-open-pdk;
              };
              submodules = {
                "ihp-sg13g2/libs.tech/klayout/python/pycell4klayout-api" = {
                  input = "pycell4klayout-api";
                  src = pycell4klayout-api;
                };
                "ihp-sg13g2/libs.tech/klayout/python/pypreprocessor" = {
                  input = "pypreprocessor";
                  src = pypreprocessor;
                };
              };
            };

          # The co-simulation engine the image ships (chiplet-cosim's
          # COSIM_DEVSHELL): Icarus, cocotb 2.0.0 and a python3.13 with cocotb
          # in its own site-packages, which the harness check reads. These are
          # the exact store paths of chiplet-cosim's capability entry for
          # sg13g2 cells under Icarus, which admits a gate run by the bytes of
          # iverilog, vvp and the cocotb VPI library. A cocotb other than this
          # override (nix-eda's default is 2.0.1; nixpkgs' own 2.0.0 is built
          # against another Icarus) is a different path the entry does not
          # cover. staging.pin names all three and the image's closure.
          cosim-iverilog = pkgs.iverilog;
          cosim-cocotb = pkgs.python313Packages.cocotb.override {
            version = "2.0.0";
            sha256 = "sha256-BpshczKA83ZeytGDrHEg6IAbI5FxciAUnzwE10hgPC0=";
          };
          cosim-python = pkgs.python313.withPackages (_: [ cosim-cocotb ]);
          # COSIM_DEVSHELL itself, as one store path the host and the image
          # both carry: a bin/ of exactly three links, python3 into the env
          # and iverilog and vvp into Icarus, and nothing else. A run's lock
          # can then pin the devshell by this path wherever it runs.
          cosim-devshell = pkgs.runCommandLocal "cosim-devshell" { } ''
            mkdir -p "$out/bin"
            ln -s ${cosim-python}/bin/python3 "$out/bin/python3"
            ln -s ${cosim-iverilog}/bin/iverilog "$out/bin/iverilog"
            ln -s ${cosim-iverilog}/bin/vvp "$out/bin/vvp"
          '';

          # The mixed-signal engine: ngspice running Icarus through its ivlng
          # bridge. It is kept apart from the co-simulation engine above, whose
          # store paths a capability entry names and which stay as they are.
          #
          # Icarus at the co-simulation one's rev, built with --enable-libvvp:
          # ivlng runs Icarus as libvvp.so, opened by the path the d_cosim
          # instance gives (checks.mixsim-explicit-paths) or else by name, and
          # the co-simulation Icarus has none. vvp then links that library from
          # its build directory, so the
          # pre-install `make check` needs the directory on the loader path; it
          # is taken off again before the install check, which runs Icarus's
          # regression suite (as nix-eda's does) on the installed vvp, finding
          # libvvp.so through its own rpath.
          mixsim-iverilog = pkgs.iverilog.overrideAttrs (old: {
            pname = "iverilog-libvvp";
            configureFlags = (old.configureFlags or [ ]) ++ [ "--enable-libvvp" ];
            preCheck = (old.preCheck or "") + ''
              adkLdPath="''${LD_LIBRARY_PATH-}"
              export LD_LIBRARY_PATH="$PWD/vvp''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
            '';
            postCheck = (old.postCheck or "") + ''
              if [ -n "$adkLdPath" ]; then export LD_LIBRARY_PATH="$adkLdPath"; else unset LD_LIBRARY_PATH; fi
            '';
            preInstallCheck = (old.preInstallCheck or "") + ''
              [ -f "$out/lib/libvvp.so" ] && [ -f "$out/include/iverilog/libvvp.h" ] || {
                echo "libvvp.so or libvvp.h is not installed"
                exit 1
              }
              patchelf --print-rpath "$out/bin/vvp" | tr : '\n' | grep -qxF "$out/lib" || {
                echo "vvp's rpath does not hold $out/lib"
                exit 1
              }
              "$out/bin/vvp" -V | head -n 1
            '';
          });
          # nixpkgs' ngspice 45 (OSDI, XSPICE and the ivlng bridge built in),
          # from the plain nixpkgs this lock names, so the binary cache's store
          # path. Not nix-eda's, which is 44 and adds KLU.
          mixsim-ngspice = nixpkgs.legacyPackages.${system}.ngspice;

          # OpenVAF 23.5.0: upstream's Linux binary, pinned by the tarball's
          # sha256 and patched to the store's loader. It does not build the
          # OSDI files: its --target_cpu panics, so it can only emit code for
          # the build host's CPU. It is kept as checks.sg13g2-osdi-generic's
          # positive control, which needs exactly such a native build.
          openvaf = pkgs.stdenv.mkDerivation {
            pname = "openvaf";
            version = "23.5.0";
            src = pkgs.fetchurl {
              url = "https://openva.fra1.cdn.digitaloceanspaces.com/openvaf_23_5_0_linux_amd64.tar.gz";
              hash = "sha256-ecDgitlIp6n0YNyHvoiyYbvZm2OkA42zxkaAGJ9E5PA=";
            };
            sourceRoot = ".";
            nativeBuildInputs = [ pkgs.autoPatchelfHook ];
            buildInputs = [ pkgs.stdenv.cc.cc.lib ];
            dontStrip = true;
            installPhase = ''
              runHook preInstall
              install -Dm755 openvaf $out/bin/openvaf
              runHook postInstall
            '';
            doInstallCheck = true;
            installCheckPhase = ''
              [ "$($out/bin/openvaf --version)" = "openvaf 23.5.0" ]
            '';
            meta.platforms = [ "x86_64-linux" ];
          };

          # The OSDI compiler: OpenVAF-Reloaded 20260616 (commit 44cfee3b,
          # GPL-3.0), upstream's Linux release binary from the project's
          # download page. It is pinned twice: by the tarball's sha256 (the
          # fetch) and by the extracted binary's, checked before the binary is
          # patched to the store's loader. A build tool; it ships nowhere.
          openvaf-reloaded = pkgs.stdenv.mkDerivation {
            pname = "openvaf-reloaded";
            version = "20260616";
            src = pkgs.fetchurl {
              url = "https://fides.fe.uni-lj.si/openvaf/download/openvaf-reloaded-20260616-linux_x64.tar.gz";
              hash = "sha256-vUhUpuW1XxxkoLhT2QIW0eOWHS9tOsVyElWX0ouGcwE=";
            };
            sourceRoot = ".";
            nativeBuildInputs = [ pkgs.autoPatchelfHook ];
            buildInputs = [
              pkgs.stdenv.cc.cc.lib
              pkgs.zlib
              pkgs.zstd
              pkgs.ncurses
            ];
            dontStrip = true;
            installPhase = ''
              runHook preInstall
              echo "e99b7fb384524916f1f23649a451d73e77e7ab78b04b62ef730e8a21d7b31a9d  openvaf-r" | sha256sum -c -
              install -Dm755 openvaf-r $out/bin/openvaf-r
              runHook postInstall
            '';
            doInstallCheck = true;
            installCheckPhase = ''
              [ "$($out/bin/openvaf-r --version)" = "OpenVAF-Reloaded 20260616-0-g44cfee3b" ]
            '';
            meta.platforms = [ "x86_64-linux" ];
          };

          # The six OSDI models the sg13g2 ngspice init loads, compiled from
          # the Verilog-A sources at the ihp-open-pdk input (the Dockerfile's
          # SG13G2_REF) by the PDK's own script with its generic-CPU flag. The
          # default target is the build host's CPU. The script takes openvaf-r
          # when it is on PATH, and goes on past a model that fails, and
          # OpenVAF exits 0 on a failed compile, so the build is decided on the
          # six files, each there and not empty, never on an exit code.
          # openvaf-r links each model by running `ld` from PATH, and fails
          # without one, so binutils' own ld is given, not the wrapper that adds
          # the store's flags. No compiler here gives the same bytes twice: the
          # files' identity is the bytes a build shipped, not this recipe.
          sg13g2-osdi = pkgs.runCommand "sg13g2-osdi-${ihp-open-pdk.shortRev}" {
            nativeBuildInputs = [
              openvaf-reloaded
              pkgs.binutils-unwrapped
            ];
          } ''
            cp -r ${ihp-open-pdk}/ihp-sg13g2/libs.tech/verilog-a va
            chmod -R u+w va
            (cd va && bash ./openvaf-compile-va.sh --compile-model-generic)
            want="cap_cmomf.osdi cap_cmomi.osdi mosvar.osdi psp103.osdi psp103_nqs.osdi r3_cmc.osdi"
            got="$(cd ngspice/osdi && ls | sort | tr '\n' ' ' | sed 's/ $//')"
            [ "$got" = "$want" ] || {
              echo "compiled '$got', expected '$want'"
              exit 1
            }
            for f in ngspice/osdi/*.osdi; do
              [ -s "$f" ] || {
                echo "$f is empty"
                exit 1
              }
            done
            mkdir -p $out/osdi
            cp ngspice/osdi/*.osdi $out/osdi/
          '';

          # The waveform viewers, nixpkgs' at the plain nixpkgs this lock names,
          # so the binary cache's store paths: GTKWave for VCD and FST, GAW for
          # ngspice's raw files.
          gtkwave = nixpkgs.legacyPackages.${system}.gtkwave;
          gaw = nixpkgs.legacyPackages.${system}.gaw;

          # Xschem at the master commit the co-simulation's sheets are written
          # for, built by nixpkgs' own expression; nothing else moves.
          xschem = nixpkgs.legacyPackages.${system}.xschem.overrideAttrs (_: {
            version = "3.4.8RC-8d2088e";
            src = nixpkgs.legacyPackages.${system}.fetchFromGitHub {
              owner = "StefanSchippers";
              repo = "xschem";
              rev = "8d2088edd50cb757410a52a02eec2d2c7e197f95";
              hash = "sha256-4KkaZotGW1LmEA+XlRCfBuxoM26QEh73hP62MvdPVEA=";
            };
          });

          # The Verilog-AMS engine: gnucap and its Verilog-AMS model generator,
          # both at develop commits (gnucap's version lines read trace_adv
          # 2026.09.25), installed into one prefix, built by the plain nixpkgs
          # this lock names. gnucap builds in a directory of its own, as its
          # INSTALL says. modelgen-verilog installs under the PREFIX its
          # configure writes and builds against the gnucap-conf it finds on
          # PATH, so it is built after gnucap is installed, with $out/bin first
          # on PATH, and in its own source tree: out of it, it does not find its
          # own headers. Each configure has to have written PREFIX = $out, or
          # the build stops. Without readline: the engine reads decks on stdin.
          # gnucap finds libgnucap.so by its rpath ($out/lib, which stdenv
          # adds), not by LD_LIBRARY_PATH; the install check runs it with
          # neither LD_LIBRARY_PATH nor GNUCAP_PLUGPATH set and reads both
          # version lines. gnucap's tests/Make.test names /bin/bash as make's
          # SHELL, which the sandbox has not; it gets the stdenv's bash.
          # gnucap's configure takes any directory of its source tree holding
          # NEWS, MakeList and configure for a submodule to build out of
          # tree, modelgen-verilog among them, so it runs with
          # --disable-submodules.
          vams-gnucap = nixpkgs.legacyPackages.${system}.stdenv.mkDerivation {
            name = "gnucap-b6678fe-modelgen-verilog-100e746";
            src = nixpkgs.legacyPackages.${system}.fetchgit {
              url = "https://https.git.savannah.gnu.org/git/gnucap.git";
              rev = "b6678fee225495910583b46023f7b5a0ed23fc37";
              hash = "sha256-eHaiOyGnMMWqubAJWTik0+4V5IhinghZ5Uz2gOAvrBc=";
            };
            modelgen = nixpkgs.legacyPackages.${system}.fetchgit {
              url = "https://https.git.savannah.gnu.org/git/gnucap/gnucap-modelgen-verilog.git";
              rev = "100e7469fa2f758b4de0492ec1374266820cbfcd";
              hash = "sha256-yGoGl+C8G2QCpG7EPAPzigEGL9jlKp894Bn3+TrS0M0=";
            };
            noReadline = [ "HAVE_LIBREADLINE=" "LIBREADLINE=" ];
            enableParallelBuilding = true;
            postPatch = ''
              cp -r "$modelgen" modelgen-verilog
              chmod -R u+w modelgen-verilog
              patchShebangs --build .
              substituteInPlace tests/Make.test \
                --replace-fail "SHELL=/bin/bash" "SHELL=${nixpkgs.legacyPackages.${system}.stdenv.shell}"
            '';
            configurePhase = ''
              runHook preConfigure
              mkdir build
              (cd build && ../configure --prefix="$out" --disable-submodules)
              grep -qE "^PREFIX = +$out\$" build/MakeConf || {
                echo "gnucap's configure did not write PREFIX = $out"
                exit 1
              }
              runHook postConfigure
            '';
            buildPhase = ''
              runHook preBuild
              make -C build -j"$NIX_BUILD_CORES" $noReadline
              runHook postBuild
            '';
            installPhase = ''
              runHook preInstall
              make -C build $noReadline install
              patchShebangs --host "$out/bin"
              cd modelgen-verilog
              PATH="$out/bin:$PATH" ./configure --prefix="$out"
              grep -qE "^PREFIX = +$out\$" MakeConf || {
                echo "modelgen-verilog's configure did not write PREFIX = $out"
                exit 1
              }
              PATH="$out/bin:$PATH" make -j"$NIX_BUILD_CORES"
              PATH="$out/bin:$PATH" make install
              runHook postInstall
            '';
            doInstallCheck = true;
            installCheckPhase = ''
              runHook preInstallCheck
              banner="$(cd "$TMPDIR" && echo | env -u LD_LIBRARY_PATH -u GNUCAP_PLUGPATH \
                HOME="$TMPDIR" "$out/bin/gnucap")"
              for line in "main version: trace_adv 2026.09.25" \
                          "core-lib version: trace_adv 2026.09.25"; do
                grep -qxF "$line" <<< "$banner" || {
                  echo "gnucap does not print '$line':"
                  echo "$banner"
                  exit 1
                }
              done
              [ -x "$out/bin/gnucap-mg-vams" ] && [ -f "$out/lib/gnucap/vams/resistor.so" ] || {
                echo "modelgen-verilog is not installed under $out"
                exit 1
              }
              runHook postInstallCheck
            '';
          };
          # VAMS_DEVSHELL, as one store path the host and the image both carry:
          # a bin/ of gnucap's four commands and the C++ compiler a run builds
          # its modelgen output with, the stdenv's own, so the plugin and gnucap
          # link the same libstdc++. Nothing else.
          vams-devshell = pkgs.runCommandLocal "vams-devshell" { } ''
            mkdir -p "$out/bin"
            for tool in gnucap gnucap-conf gnucap-mg-vams gnucap-modelgen; do
              ln -s ${vams-gnucap}/bin/$tool "$out/bin/$tool"
            done
            ln -s ${nixpkgs.legacyPackages.${system}.stdenv.cc}/bin/g++ "$out/bin/g++"
            ln -s ${nixpkgs.legacyPackages.${system}.stdenv.cc}/bin/c++ "$out/bin/c++"
          '';
        in
        {
          default = klayout;
          librelane = librelane-patched;
          # The lane's interpreter under the tool name its pin and table row
          # use (staging.pin's NIX_PYTHON_OUTPATH).
          python = cli-env;
          inherit
            klayout
            klayout-python
            cli-env
            openroad
            kicad-fork-base
            tail-export-python
            tail-wrappers
            tools-python
            opensta
            librelane-src
            librelane-python
            sg13g2-pdk
            cosim-iverilog
            cosim-cocotb
            cosim-python
            cosim-devshell
            mixsim-iverilog
            mixsim-ngspice
            openvaf
            openvaf-reloaded
            sg13g2-osdi
            gtkwave
            gaw
            xschem
            vams-gnucap
            vams-devshell
            ;
        }
      );

      # That the build shell's LibreLane runs the guarded `sta`, and that the
      # guard holds on it. The wrapper's own PATH lines are evaluated as the
      # wrapper evaluates them, from a PATH that holds nothing, and `sta` is
      # resolved on the result: it must be packages.opensta and the path
      # adkIdentity states. The guard's test then runs on it and on LibreLane's
      # unpatched OpenSTA (its ablation), and tests/nix/read-opensta-guard-outputs.sh
      # decides: a clean pass on the first, and on the second exactly the
      # three forwarded-clock table-set failures plus at most the fill and
      # finite checks, with every other check passing. The test and the reader
      # enter as store copies of those files, so editing them rebuilds this
      # check and nothing else.
      checks = nix-eda.forAllSystems (
        system:
        let
          pkgs = self.legacyPackages.${system};
          inherit (self.packages.${system}) opensta;
          wrapped = self.packages.${system}.librelane;
          unpatched = librelane.packages.${system}.opensta;
          test = ./tests/nix/test-opensta-timing-model-guard.sh;
          read = ./tests/nix/read-opensta-guard-outputs.sh;
        in
        {
          opensta = pkgs.runCommand "adk-check-opensta" { } ''
            want=${opensta}/bin/sta
            stated=${wrapped.adkIdentity.opensta.path}/bin/sta
            [ "$stated" = "$want" ] || {
              echo "adkIdentity states $stated, packages.opensta is $want"
              exit 1
            }
            grep -E '^(PATH=|export PATH$)' ${wrapped}/bin/librelane > wrapper-path.sh
            got="$(PATH=/adk-check-empty; . ./wrapper-path.sh; type -P sta)" || true
            [ "$got" = "$want" ] || {
              echo "the LibreLane wrapper resolves sta to '$got', expected $want"
              exit 1
            }
            grc=0
            bash ${test} "$got" > guard.out 2>&1 || grc=$?
            arc=0
            bash ${test} ${unpatched}/bin/sta > ablation.out 2>&1 || arc=$?
            bash ${read} guard.out "$grc" ablation.out "$arc" > read.out || {
              cat guard.out ablation.out read.out
              exit 1
            }
            mkdir -p $out
            cp wrapper-path.sh guard.out ablation.out read.out $out/
            echo "$got" > $out/sta
          '';

          # That ngspice 45 loads each of the six OSDI files: a batch run whose
          # control block loads them with pre_osdi, before the circuit is
          # parsed, then solves an operating point on a resistor so the deck is
          # a whole circuit, must exit 0, print no error and solve. (`osdi`,
          # run after the circuit is parsed, crashes ngspice once a second
          # library is loaded, whichever compiler built them.) Its ablation
          # loads a file that does not exist and must print ngspice's open
          # error, or the first run's silence proves nothing; ngspice keeps
          # exit code 0 on that error, so the text is what is decided on. A
          # third run lists the devices after the loads, kept as a reading,
          # not decided on.
          sg13g2-osdi-ngspice =
            let
              inherit (self.packages.${system}) mixsim-ngspice sg13g2-osdi;
            in
            pkgs.runCommand "adk-check-sg13g2-osdi-ngspice" { } ''
              deck() {
                echo '* OSDI load'
                echo 'V1 1 0 1'
                echo 'R1 1 0 1k'
                echo '.control'
                for f in "$@"; do echo "pre_osdi '$f'"; done
                echo op
                [ -z "''${DEVHELP-}" ] || echo devhelp
                echo '.endc'
                echo '.end'
              }
              run() {
                rc=0
                ${mixsim-ngspice}/bin/ngspice -b -n "$1" > "$2" 2>&1 || rc=$?
                echo "$rc" > "$2.rc"
              }
              deck ${sg13g2-osdi}/osdi/*.osdi > load.cir
              deck /adk-check-absent.osdi > ablation.cir
              DEVHELP=1 deck ${sg13g2-osdi}/osdi/*.osdi > devhelp.cir
              run load.cir load.out
              run ablation.cir ablation.out
              run devhelp.cir devhelp.out
              if [ "$(cat load.out.rc)" != 0 ] || grep -qi error load.out \
                  || ! grep -q 'No. of Data Rows' load.out \
                  || ! grep -q 'Error opening osdi lib' ablation.out; then
                cat load.out ablation.out
                exit 1
              fi
              mkdir -p $out
              cp *.cir *.out *.rc $out/
            '';

          # That the six OSDI files are generic x86-64 code, read from the files
          # rather than assumed from the compiler flag: tests/nix/scan-osdi-isa.sh
          # disassembles each one and counts AVX, AVX2, FMA and AVX-512
          # instructions by a named list, which must come to zero. Its positive
          # control is OpenVAF 23.5.0's native build of cap_cmomf on the build
          # host, which the same scan must flag, or the zeros prove nothing (a
          # build host without AVX would fail it, and says so). The OSDI
          # interface version each file declares is read from its symbols and
          # kept as a reading.
          sg13g2-osdi-generic =
            let
              inherit (self.packages.${system}) openvaf sg13g2-osdi;
              scan = ./tests/nix/scan-osdi-isa.sh;
              osdiVersion = ./tests/nix/osdi-version.c;
              objdump = "${pkgs.binutils-unwrapped}/bin/objdump";
              va = "${ihp-open-pdk}/ihp-sg13g2/libs.tech/verilog-a";
            in
            pkgs.runCommandCC "adk-check-sg13g2-osdi-generic" { } ''
              set -- ${sg13g2-osdi}/osdi/*.osdi
              [ $# = 6 ] || { echo "expected six OSDI files: $*"; exit 1; }
              rc=0
              bash ${scan} ${objdump} "$@" > scan.out 2>&1 || rc=$?
              echo "$rc" > scan.out.rc
              cp ${va}/cap_cmomf/cap_cmomf.va .
              # Like openvaf-r, it links with ld from PATH, and it exits 0 when
              # a compile fails, so the file is what is tested.
              PATH=${pkgs.binutils-unwrapped}/bin:$PATH \
                ${openvaf}/bin/openvaf cap_cmomf.va -o native.osdi > native.log 2>&1 || true
              [ -f native.osdi ] || {
                cat native.log
                exit 1
              }
              crc=0
              bash ${scan} ${objdump} native.osdi > control.out 2>&1 || crc=$?
              echo "$crc" > control.out.rc
              cc -o osdi-version ${osdiVersion} -Wl,--no-as-needed -lm -ldl
              vrc=0
              ./osdi-version "$@" > version.out 2>&1 || vrc=$?
              echo "$vrc" > version.out.rc
              if [ "$rc" != 0 ] || [ "$(wc -l < scan.out)" != 6 ] || [ "$crc" != 1 ] \
                  || [ "$vrc" != 0 ] || [ "$(wc -l < version.out)" != 6 ]; then
                cat scan.out control.out version.out
                exit 1
              fi
              mkdir -p $out
              cp native.log *.out *.rc $out/
            '';

          # That the mixed-signal engine loads the files it is given, whatever
          # the loader path says. ngspice's d_cosim opens the shim it names,
          # and the shim opens lib_args[0] as the libvvp and lib_args[1] as the
          # VPI module; given as absolute store paths, nothing is searched for.
          # A one-bit Verilog output that rises at 10 us, compiled by this
          # Icarus, is run three ways against a libvvp.so on LD_LIBRARY_PATH
          # that is a shared library but not Icarus's (it defines no VVP
          # function): with explicit paths and without that variable (must run
          # and print the output at 1), with explicit paths and with it (must
          # run the same), and with libvvp left to the name search under it
          # (must load that very file and find no VVP function in it, which is
          # what the explicit path takes the loader out of). The shim's open
          # error names only the last path it tried, so it cannot show this.
          mixsim-explicit-paths =
            let
              inherit (self.packages.${system}) mixsim-iverilog mixsim-ngspice;
              ng = "${mixsim-ngspice}/lib/ngspice";
            in
            pkgs.runCommandCC "adk-check-mixsim-explicit-paths" { } ''
              cat > tick.v <<'EOF'
              `timescale 1ns/1ps
              module tick(output reg q);
                initial begin
                  q = 1'b0;
                  #10000 q = 1'b1;
                end
              endmodule
              EOF
              ${mixsim-iverilog}/bin/iverilog -o tick.vvp tick.v
              deck() {
                echo '* d_cosim with Icarus'
                echo 'adut null [ q ] tick'
                echo ".model tick d_cosim simulation=\"${ng}/ivlng.so\""
                echo "+ sim_args=[\"tick.vvp\"] lib_args=[$1]"
                echo '.control'
                echo 'tran 1u 30u'
                echo 'eprint q'
                echo '.endc'
                echo '.end'
              }
              explicit="\"${mixsim-iverilog}/lib/libvvp.so\" \"${ng}/ivlng\""
              deck "$explicit" > explicit.cir
              deck '"libvvp" "${ng}/ivlng"' > by-name.cir
              mkdir poison
              echo 'int adk_check_not_vvp;' > not-vvp.c
              cc -shared -fPIC -o poison/libvvp.so not-vvp.c
              run() {
                rc=0
                env $1 ${mixsim-ngspice}/bin/ngspice -b "$2" > "$3" 2>&1 || rc=$?
                echo "$rc" > "$3.rc"
              }
              run ADK_CHECK=clean explicit.cir clean.out
              run LD_LIBRARY_PATH=$PWD/poison explicit.cir poisoned.out
              run LD_LIBRARY_PATH=$PWD/poison by-name.cir by-name.out
              ran() {
                [ "$(cat "$1.rc")" = 0 ] && ! grep -qiE 'cannot open|failed' "$1" \
                  && grep -qE '^[0-9.eE+-]+[[:space:]]+1[a-z]?[[:space:]]*$' "$1"
              }
              if ! ran clean.out || ! ran poisoned.out \
                  || ! grep -q 'Icarus shim failed to find VVP function' by-name.out; then
                cat clean.out poisoned.out by-name.out
                exit 1
              fi
              mkdir -p $out
              cp tick.v *.cir *.out *.rc $out/
            '';
        }
      );

      devShells = nix-eda.forAllSystems (
        system:
        let
          pkgs = self.legacyPackages.${system};
          inherit (self.packages.${system})
            klayout
            klayout-python
            cli-env
            openroad
            cosim-devshell
            ;
          # The KLayout the image runs: the Dockerfile's one KLAYOUT_PIP line,
          # read rather than copied, so the stated skew cannot drift from it.
          # Not exactly one such line is an evaluation error.
          imageKlayout =
            let
              found = builtins.filter (m: m != null) (
                map (builtins.match "ARG KLAYOUT_PIP=([^ ]+)") (
                  pkgs.lib.splitString "\n" (builtins.readFile ./Dockerfile)
                )
              );
            in
            assert builtins.length found == 1;
            assert builtins.head (builtins.head found) == klayout.version;
            builtins.head (builtins.head found);
        in
        {
          default = pkgs.mkShellNoCC {
            packages = [
              klayout
              (pkgs.python3.withPackages (ps: [ klayout-python ]))
            ];
          };

          cli = pkgs.mkShellNoCC {
            # The interpreter is not on PATH from here: nix/cli-env.sh makes a
            # venv from it and puts that first, so `python` means the one with
            # the flow's packages installed.
            packages = [
              openroad
              klayout
              pkgs.git
              pkgs.coreutils
              pkgs.gawk
            ];
            # Read by nix/cli-env.sh. Store paths, so the shell names exactly
            # what it runs, whatever PATH the caller arrived with.
            ADK_NIX_PYTHON = "${pkgs.python3}/bin/python3";
            ADK_NIX_SITE_PACKAGES = "${cli-env}/${pkgs.python3.sitePackages}";
            ADK_NIX_OPENROAD = "${openroad}";
            ADK_NIX_KLAYOUT = "${klayout}";
            # The KLayout the image runs, which evaluation holds equal to this
            # shell's; the context record states both.
            ADK_NIX_KLAYOUT_IMAGE_VERSION = imageKlayout;
            # COSIM_DEVSHELL, as chiplet-cosim reads it: the devshell the
            # image links, at its store path. The hook checks it is
            # staging.pin's NIX_COSIM_DEVSHELL_OUTPATH.
            ADK_NIX_COSIM_DEVSHELL = "${cosim-devshell}";
            # The closure of what this shell provides, with the narHash of every
            # path, for the context record an evidence run writes and the tool
            # identity table every entry writes.
            ADK_NIX_CLOSURE_INFO = "${
              pkgs.closureInfo {
                rootPaths = [
                  cli-env
                  openroad
                  klayout
                  pkgs.git
                  pkgs.coreutils
                  pkgs.gawk
                  cosim-devshell
                ];
              }
            }";
            # The pins the hook checks the interpreter env and the devshell
            # against, and the tool table's rows come from, as a store copy.
            ADK_NIX_STAGING_PIN = "${./staging.pin}";
            ADK_NIX_FLAKE_REV = self.rev or self.dirtyRev or "unknown";
            # The lock this shell was evaluated with, as a store copy.
            ADK_NIX_FLAKE_LOCK = "${./flake.lock}";
            # The hook and its helpers, copied into the store with the flake.
            ADK_NIX_LIB = "${./nix}";
            shellHook = ''
              source "$ADK_NIX_LIB/cli-env.sh"
            '';
          };

          # The cli shell plus what a die build needs: LibreLane and the PDK.
          # The launcher nix/build-env.sh writes execs
          # $ADK_NIX_LIBRELANE/bin/librelane, whose wrapper puts LibreLane's
          # tools first on PATH. Every variable the cli shell sets is taken
          # from it unchanged; the closure grows by LibreLane, the cores'
          # interpreter env and the PDK.
          # PDK_ROOT and PDK are not set here: the cli hook clears both with
          # the other host variables, so build-env.sh sets them after it,
          # from ADK_NIX_SG13G2_PDK.
          build =
            let
              cli = self.devShells.${system}.cli;
              inherit (self.packages.${system}) librelane librelane-python sg13g2-pdk;
            in
            pkgs.mkShellNoCC {
              inputsFrom = [ cli ];
              inherit (cli)
                ADK_NIX_PYTHON
                ADK_NIX_SITE_PACKAGES
                ADK_NIX_OPENROAD
                ADK_NIX_KLAYOUT
                ADK_NIX_KLAYOUT_IMAGE_VERSION
                ADK_NIX_FLAKE_REV
                ADK_NIX_FLAKE_LOCK
                ADK_NIX_LIB
                ADK_NIX_COSIM_DEVSHELL
                ADK_NIX_STAGING_PIN
                ;
              ADK_NIX_LIBRELANE = "${librelane}";
              ADK_NIX_SG13G2_PDK = "${sg13g2-pdk}";
              # LibreLane's evaluated identity, as the package carries it, so
              # the context record reads it rather than parsing the pins
              # again. patchedRev is staging.pin's claim; the evaluation does
              # not check it against the patched tree. The PDK tree states
              # its own sources (pdk-source.json); no variable carries them.
              ADK_NIX_LIBRELANE_IDENTITY = builtins.toJSON librelane.adkIdentity;
              # The cores' interpreter, as the env names it (the path
              # sys.executable reports). The operator passes it and
              # $ADK_NIX_LIBRELANE/bin/librelane to core preparation; the
              # context record binds it to its store path and narHash and to
              # the package it serves.
              ADK_NIX_LIBRELANE_PYTHON = librelane-python.interpreter;
              ADK_NIX_CLOSURE_INFO = "${
                pkgs.closureInfo {
                  rootPaths = [
                    cli-env
                    openroad
                    klayout
                    pkgs.git
                    pkgs.coreutils
                    pkgs.gawk
                    librelane
                    librelane-python
                    sg13g2-pdk
                    cosim-devshell
                  ];
                }
              }";
              # mkShell runs every inputsFrom shellHook ahead of its own, so
              # the cli hook runs once, before this; naming it here too would
              # run it twice, and evidence mode refuses a second entry.
              shellHook = ''
                source "$ADK_NIX_LIB/build-env.sh"
              '';
            };

          # The cli shell plus what chiplet-flow's tail (gds, interposer-drc,
          # drc) needs: the image's three commands on PATH (tail-wrappers),
          # chiplet-export's interpreter with the KiCad fork's pcbnew
          # (tail-export-python, which brings kicad-fork-base), and the sg13g2
          # PDK the export's worker places its via PCells from. Every variable
          # the cli shell sets is taken from it unchanged; the closure grows by
          # those three. PDK_ROOT and KICAD_CHIPLET_PYTHON are not set here:
          # the cli hook clears both with the other host variables, so
          # nix-tail/tail-env.sh sets them after it. The tail's files live in
          # nix-tail/, not nix/, so that the cli and build shells, which carry
          # nix/ whole, are unchanged by them.
          tail =
            let
              cli = self.devShells.${system}.cli;
              inherit (self.packages.${system})
                kicad-fork-base
                tail-export-python
                tail-wrappers
                sg13g2-pdk
                ;
            in
            pkgs.mkShellNoCC {
              inputsFrom = [ cli ];
              packages = [ tail-wrappers ];
              inherit (cli)
                ADK_NIX_PYTHON
                ADK_NIX_SITE_PACKAGES
                ADK_NIX_OPENROAD
                ADK_NIX_KLAYOUT
                ADK_NIX_KLAYOUT_IMAGE_VERSION
                ADK_NIX_FLAKE_REV
                ADK_NIX_FLAKE_LOCK
                ADK_NIX_LIB
                ADK_NIX_COSIM_DEVSHELL
                ADK_NIX_STAGING_PIN
                ;
              ADK_NIX_TAIL_WRAPPERS = "${tail-wrappers}";
              ADK_NIX_TAIL_EXPORT_PYTHON = "${tail-export-python}";
              ADK_NIX_KICAD_FORK_BASE = "${kicad-fork-base}";
              ADK_NIX_SG13G2_PDK = "${sg13g2-pdk}";
              ADK_NIX_TAIL_LIB = "${./nix-tail}";
              ADK_NIX_CLOSURE_INFO = "${
                pkgs.closureInfo {
                  rootPaths = [
                    cli-env
                    openroad
                    klayout
                    pkgs.git
                    pkgs.coreutils
                    pkgs.gawk
                    tail-wrappers
                    tail-export-python
                    kicad-fork-base
                    sg13g2-pdk
                    cosim-devshell
                  ];
                }
              }";
              # As for the build shell: the cli hook runs once, ahead of this.
              shellHook = ''
                source "$ADK_NIX_TAIL_LIB/tail-env.sh"
              '';
            };
        }
      );
    };
}
