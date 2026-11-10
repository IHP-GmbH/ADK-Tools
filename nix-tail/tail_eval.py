# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""The reader behind nix-tail/check-tail-eval.sh. Standard library only.

The check evaluates two checkouts, a base and a new one, and hands this
script what `nix eval` and `nix derivation show` printed; nothing here runs
Nix. Four subcommands:

  drvs <attrs.json>            the derivations to show: every drvPath in it.
  closure-infos <show.json>    the closure-info derivations the shells name,
                               to be shown in a second call.
  wrapper-sources <attrs.json> <show.json>
                               the store files tail-wrappers' build command
                               names among its sources (the templates and the
                               carrier DRC script), for the check to copy.
  compare <dir>                the verdict, from <dir>/{base,new}.attrs.json,
                               <dir>/{base,new}.show.json (the first call),
                               <dir>/{base,new}.show-ci.json (the second),
                               <dir>/revs.json, <dir>/new.sources/ (those
                               store files, by store name) and
                               <dir>/new.carrier-drc (the new checkout's
                               bin/adk-verify-carrier-drc).

attrs.json is {"shells": {name: drvPath}, "packages": {name: drvPath}};
each show file is one `nix derivation show` object, keyed by .drv path, in
the format of Nix 2.30 or wrapped in "derivations" as later versions print
it. revs.json is {"base": <rev>, "new": <rev>}: HEAD of each
checkout, which the flake records as ADK_NIX_FLAKE_REV.

compare exits 0 only when:

  unchanged   the new cli and build shells differ from the base's in nothing
              but the revision they record (ADK_NIX_FLAKE_REV, each side's
              HEAD) and the output path that follows from it;
  tail        the tail shell is the new cli shell plus tail-wrappers,
              tail-export-python, kicad-fork-base and sg13g2-pdk: the same
              builder and arguments, every variable of the cli shell with its
              value except the four that name what was added (out,
              nativeBuildInputs with tail-wrappers first, shellHook with
              nix-tail/tail-env.sh after the cli hook, ADK_NIX_CLOSURE_INFO),
              the five ADK_NIX_ variables that name the additions, input
              derivations that are the cli shell's with those four and its own
              closure-info in place of the cli's, sources that are the cli
              shell's plus nix-tail/, and a closure-info whose roots are the
              cli shell's plus those four outputs;
  reused      kicad-fork-base and sg13g2-pdk are the base's derivations, and
              the build shell's ADK_NIX_SG13G2_PDK is the same PDK: a tail
              shell built from here builds neither again;
  chain       tail-export-python is built from kicad-fork-base, and
              tail-wrappers from tail-export-python;
  wrappers    the tail-wrappers derivation the tail shell takes, whatever
              form the flake binds it in, has the shape the binding
              nix-tail/check-tail-wrappers.sh reads gives it: the cli
              shell's builder, arguments and stdenv, the variables
              runCommandLocal gives with nothing added (RUN_COMMAND_LOCAL),
              the build command BUILD_COMMAND with a store path in each of its
              twelve open places, and as sources only the three templates, the
              carrier DRC script and the store paths among the builder's
              arguments. Each
              wrapper, rendered from its kept template with the paths that
              command fills in, is printable ASCII, starts `#!<store
              path>/bin/bash -p` on a first line Linux reads whole (at most
              255 bytes since Linux 5.1), and apart from comments is exactly its
              guards, the ADK_PYTHON export where the command needs one, and
              its exec line (RENDERED): every path it names is a store path
              (the bash, tail-export-python, the carrier script) or lies
              under ADK_NIX_VENV, ADK_ROOT or ADK_NIX_PLUGIN_ROOT. The
              carrier script is the new checkout's bin/adk-verify-carrier-drc,
              byte for byte.

Not read here: what the programs these paths name do. @bash@ is checked by
shape only: one store path, the same in every wrapper's first line, ending
/bin/bash. Which bash that is, is read by neither this check nor the static
one, which reads only how its one form of binding spells it; so a program
there that drops -p, such as a shim that runs bash without it, is caught by
neither. Likewise tail-export-python's bin/python is read only as the path
@exportPython@ names, and ADK_NIX_TAIL_LIB only as a store path named
nix-tail: what either holds is not read.

Anything it cannot read is a problem, never a pass.
"""

import json
import os
import re
import sys

SHELLS = ("cli", "build")
TAIL_PACKAGES = ("tail-wrappers", "tail-export-python", "kicad-fork-base", "sg13g2-pdk")
REUSED = ("kicad-fork-base", "sg13g2-pdk")
TAIL_VARS = {
    "ADK_NIX_TAIL_WRAPPERS": "tail-wrappers",
    "ADK_NIX_TAIL_EXPORT_PYTHON": "tail-export-python",
    "ADK_NIX_KICAD_FORK_BASE": "kicad-fork-base",
    "ADK_NIX_SG13G2_PDK": "sg13g2-pdk",
}
# Variables of the cli shell the tail shell sets otherwise, by design.
TAIL_OWN = {"out", "nativeBuildInputs", "shellHook", "ADK_NIX_CLOSURE_INFO"}
TAIL_HOOK = 'source "$ADK_NIX_TAIL_LIB/tail-env.sh"'
# What the revision may change in an unchanged shell.
REVISION_ONLY = {("env", "ADK_NIX_FLAKE_REV"), ("env", "out"), ("outputs", "out", "path")}
STORE = re.compile(r"^/nix/store/[0-9a-z]{32}-[^/\s]+$")
# A store path inside a text: the hash part and the characters Nix allows
# in a store name.
STORE_WORD = re.compile(r"/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+")

WRAPPERS = ("chiplet-export", "adk-verify-carrier-drc", "adk-drc")
# The build command the flake's tail-wrappers binding evaluates to, each store
# path in it written <store>: one substitute call per wrapper, in this order,
# then the placeholder check and the mode.
BUILD_COMMAND = 'mkdir -p "$out/bin"\n' + "".join(
    f'substitute <store> "$out/bin/{name}" \\\n'
    "  --subst-var-by bash <store>/bin/bash \\\n"
    "  --subst-var-by exportPython <store> \\\n"
    "  --subst-var-by carrierDrc <store>\n" for name in WRAPPERS) + (
    "\n"
    "if grep -n '@[A-Za-z][A-Za-z]*@' \"$out\"/bin/*; then\n"
    '  echo "a placeholder is left in a wrapper" >&2\n'
    "  exit 1\n"
    "fi\n"
    'chmod 0555 "$out"/bin/*\n')
# The rest of the tail-wrappers derivation's variables: what runCommandLocal
# gives with nothing added. name, out, buildCommand, builder, stdenv and
# system are compared on their own.
RUN_COMMAND_LOCAL = {
    "__structuredAttrs": "", "allowSubstitutes": "", "buildInputs": "", "cmakeFlags": "",
    "configureFlags": "", "depsBuildBuild": "", "depsBuildBuildPropagated": "", "depsBuildTarget": "",
    "depsBuildTargetPropagated": "", "depsHostHost": "", "depsHostHostPropagated": "",
    "depsTargetTarget": "", "depsTargetTargetPropagated": "", "doCheck": "", "doInstallCheck": "",
    "enableParallelBuilding": "1", "enableParallelChecking": "1", "enableParallelInstalling": "1",
    "mesonFlags": "", "nativeBuildInputs": "", "outputs": "out", "passAsFile": "buildCommand",
    "patches": "", "preferLocalBuild": "1", "propagatedBuildInputs": "", "propagatedNativeBuildInputs": "",
    "strictDeps": "",
}
# Linux reads the first 256 bytes of a script (since 5.1; 128 before): a
# longer first line loses its end, -p first.
SHEBANG_MAX = 255
GUARD = ': "${{{}:?enter through devShells.tail}}"'
EXPORT = 'export ADK_PYTHON="$ADK_NIX_VENV/bin/python"'
# Per wrapper, what bash runs apart from comments: the variables it guards, in
# order, whether it exports ADK_PYTHON (where the image's script starts a
# second Python), and its exec line, {python} being tail-export-python and
# {carrier} the store copy of bin/adk-verify-carrier-drc.
RENDERED = {
    "chiplet-export": (("ADK_NIX_PLUGIN_ROOT", "PDK_ROOT", "KICAD_CHIPLET_PYTHON", "GDS_TO_KICAD_ROOT"),
                       False,
                       "exec '{python}/bin/python' "
                       '"$ADK_NIX_PLUGIN_ROOT/plugins/chiplet_export/chiplet_export_cli.py" "$@"'),
    "adk-verify-carrier-drc": (("ADK_NIX_VENV",), True, 'exec "$ADK_NIX_VENV/bin/python" \'{carrier}\' "$@"'),
    "adk-drc": (("ADK_NIX_VENV", "ADK_ROOT"), False,
                'exec "$ADK_NIX_VENV/bin/python" "$ADK_ROOT/klayout/drc/run_drc.py" "$@"'),
}


class Unreadable(Exception):
    pass


def load(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise Unreadable(f"{path}: {exc}") from None


def derivations(show):
    """{drv path: derivation} from what `nix derivation show` printed."""
    if isinstance(show, dict) and isinstance(show.get("derivations"), dict):
        show = show["derivations"]
    if not isinstance(show, dict):
        raise Unreadable("a derivation show that is not an object")
    out = {}
    for key, drv in show.items():
        path = key if key.startswith("/") else "/nix/store/" + key
        if not isinstance(drv, dict) or not isinstance(drv.get("env"), dict):
            raise Unreadable(f"{key}: no env")
        out[path] = drv
    return out


def merged(paths):
    out = {}
    for path in paths:
        out.update(derivations(load(path)))
    return out


def get(drvs, path, what):
    drv = drvs.get(path)
    if drv is None:
        raise Unreadable(f"{what} ({path}) is not in the derivation show")
    return drv


def out_path(drv, what):
    path = ((drv.get("outputs") or {}).get("out") or {}).get("path")
    if not isinstance(path, str) or not STORE.match(path):
        raise Unreadable(f"{what} has no out path")
    return path


def input_drvs(drv):
    got = drv.get("inputDrvs")
    if not isinstance(got, dict):
        raise Unreadable("inputDrvs is not an object")
    return {k if k.startswith("/") else "/nix/store/" + k for k in got}


def closure_info(drv, what):
    """The one closure-info input derivation of a shell."""
    found = [d for d in input_drvs(drv) if d.endswith("-closure-info.drv")]
    if len(found) != 1:
        raise Unreadable(f"{what} has {len(found)} closure-info inputs, expected 1")
    return found[0]


def closure_roots(drv, what):
    """The root paths a closureInfo derivation was given. closureInfo uses
    structured attributes: `nix derivation show` prints them as
    "structuredAttrs", the .drv file keeps them in env as "__json"."""
    env = drv.get("env")
    if not isinstance(env, dict):
        raise Unreadable(f"{what}: env is not an object")
    attrs = drv.get("structuredAttrs")
    if attrs is not None and "__json" in env:
        raise Unreadable(f"{what}: both structuredAttrs and env __json")
    if attrs is None and "__json" in env:
        try:
            attrs = json.loads(env["__json"])
        except ValueError:
            raise Unreadable(f"{what}: __json is not JSON") from None
    if attrs is not None:
        graph = attrs.get("exportReferencesGraph") if isinstance(attrs, dict) else None
        roots = graph.get("closure") if isinstance(graph, dict) else None
    else:
        words = (env.get("exportReferencesGraph") or "").split()
        roots = words[1:] if words[:1] == ["closure"] else None
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise Unreadable(f"{what}: no exportReferencesGraph closure")
    return roots


def flat(value, prefix=()):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out.update(flat(item, prefix + (key,)))
        return out
    return {prefix: value}


def unchanged(name, base, new, revs, problems):
    b, n = flat(base), flat(new)
    differ = {k for k in b.keys() | n.keys() if b.get(k) != n.get(k)}
    extra = sorted(".".join(k) for k in differ - REVISION_ONLY)
    if extra:
        problems.append(f"{name}: differs from the base in {', '.join(extra)}")
    for side, drv in (("base", base), ("new", new)):
        got = drv["env"].get("ADK_NIX_FLAKE_REV")
        if got != revs[side]:
            problems.append(f"{name}: the {side} shell records revision {got}, "
                            f"its checkout is at {revs[side]}")


def tail_is_cli_plus(drvs, attrs, problems):
    cli = get(drvs, attrs["shells"]["cli"], "the cli shell")
    tail = get(drvs, attrs["shells"]["tail"], "the tail shell")
    pkgs = {name: get(drvs, attrs["packages"][name], name) for name in TAIL_PACKAGES}
    outs = {name: out_path(drv, name) for name, drv in pkgs.items()}
    c_env, t_env = cli["env"], tail["env"]

    for field in ("builder", "args", "system", "name"):
        if tail.get(field) != cli.get(field):
            problems.append(f"tail: {field} is not the cli shell's")
    for key in sorted(c_env.keys() - TAIL_OWN):
        if key not in t_env:
            problems.append(f"tail: does not set the cli shell's {key}")
        elif t_env[key] != c_env[key]:
            problems.append(f"tail: sets {key} otherwise than the cli shell")
    added = sorted(t_env.keys() - c_env.keys())
    want_added = sorted(set(TAIL_VARS) | {"ADK_NIX_TAIL_LIB"})
    if added != want_added:
        problems.append(f"tail: adds {added}, expected {want_added}")
    for var, name in TAIL_VARS.items():
        if t_env.get(var) != outs[name]:
            problems.append(f"tail: {var} is {t_env.get(var)}, not {name} {outs[name]}")

    words = (t_env.get("nativeBuildInputs") or "").split()
    if words[:1] != [outs["tail-wrappers"]] or words[1:] != (c_env.get("nativeBuildInputs") or "").split():
        problems.append("tail: nativeBuildInputs is not tail-wrappers followed by the cli shell's")
    hook, c_hook = t_env.get("shellHook") or "", c_env.get("shellHook") or ""
    if not hook.startswith(c_hook + "\n") or hook[len(c_hook) + 1:].strip() != TAIL_HOOK:
        problems.append(f"tail: shellHook is not the cli shell's followed by {TAIL_HOOK}")

    lib = t_env.get("ADK_NIX_TAIL_LIB")
    if not isinstance(lib, str) or not STORE.match(lib) or os.path.basename(lib)[33:] != "nix-tail":
        problems.append(f"tail: ADK_NIX_TAIL_LIB is {lib}, not a store path named nix-tail")
    srcs, c_srcs = set(tail.get("inputSrcs") or ()), set(cli.get("inputSrcs") or ())
    if not isinstance(lib, str) or not STORE.match(lib) or srcs != c_srcs | {lib}:
        problems.append(f"tail: inputSrcs are not the cli shell's plus ADK_NIX_TAIL_LIB "
                        f"(extra {sorted(srcs - c_srcs)}, missing {sorted(c_srcs - srcs)})")

    c_ci, t_ci = closure_info(cli, "the cli shell"), closure_info(tail, "the tail shell")
    want = (input_drvs(cli) - {c_ci}) | {attrs["packages"][n] for n in TAIL_PACKAGES} | {t_ci}
    got = input_drvs(tail)
    if got != want:
        problems.append(f"tail: input derivations are not the cli shell's plus {', '.join(TAIL_PACKAGES)} "
                        f"(extra {sorted(got - want)}, missing {sorted(want - got)})")
    c_ci_drv, t_ci_drv = get(drvs, c_ci, "the cli closure-info"), get(drvs, t_ci, "the tail closure-info")
    if c_env.get("ADK_NIX_CLOSURE_INFO") != out_path(c_ci_drv, "the cli closure-info"):
        problems.append("cli: ADK_NIX_CLOSURE_INFO is not its closure-info input")
    if t_env.get("ADK_NIX_CLOSURE_INFO") != out_path(t_ci_drv, "the tail closure-info"):
        problems.append("tail: ADK_NIX_CLOSURE_INFO is not its closure-info input")
    c_roots = closure_roots(c_ci_drv, "the cli closure-info")
    t_roots = closure_roots(t_ci_drv, "the tail closure-info")
    if len(t_roots) != len(set(t_roots)) or set(t_roots) != set(c_roots) | set(outs.values()) \
            or len(t_roots) != len(set(c_roots)) + len(outs):
        problems.append(f"tail: closure roots are not the cli shell's plus {', '.join(TAIL_PACKAGES)} "
                        f"(extra {sorted(set(t_roots) - set(c_roots) - set(outs.values()))})")
    return outs


def kept(where, name, what):
    try:
        with open(os.path.join(where, name), "rb") as handle:
            return handle.read()
    except OSError:
        raise Unreadable(f"{what} was not kept ({name})") from None


def first_difference(got, want):
    n = next((k for k, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
    return (n + 1, got[n] if n < len(got) else "(the end)", want[n] if n < len(want) else "(the end)")


def wrapper(name, text, bash, python, carrier, problems):
    """One wrapper as its template renders with the paths the build command
    fills in: printable ASCII, the shebang, and what bash runs (RENDERED)."""
    what = f"tail-wrappers: {name}"
    odd = sorted({c for c in text if c not in "\t\n" and not " " <= c <= "~"})
    if odd:
        problems.append(f"{what}: holds {odd!r}, which bash and this check would split differently")
        return
    lines = text.split("\n")
    shebang = f"#!{bash}/bin/bash -p"
    if lines[0] != shebang:
        problems.append(f"{what}: starts {lines[0]!r}, not {shebang!r}")
    elif len(lines[0]) > SHEBANG_MAX:
        problems.append(f"{what}: its first line is {len(lines[0])} bytes, more than the {SHEBANG_MAX} Linux "
                        f"(since 5.1) reads before the newline")
    guards, exports, exec_line = RENDERED[name]
    want = [GUARD.format(v) for v in guards] + ([EXPORT] if exports else []) + \
        [exec_line.format(python=python, carrier=carrier)]
    runs = [line for line in lines[1:] if line.strip(" \t") and not line.lstrip(" \t").startswith("#")]
    if runs != want:
        n, got, should = first_difference(runs, want)
        problems.append(f"{what}: command {n} is {got!r}, not {should!r}")


def wrappers(where, drvs, attrs, outs, problems):
    """The tail-wrappers derivation Nix evaluated, and the wrappers it renders."""
    cli = get(drvs, attrs["shells"]["cli"], "the cli shell")
    drv = get(drvs, attrs["packages"]["tail-wrappers"], "tail-wrappers")
    env = drv["env"]
    for field in ("builder", "args", "system"):
        if drv.get(field) != cli.get(field):
            problems.append(f"tail-wrappers: {field} is not the cli shell's")
    if "structuredAttrs" in drv:
        problems.append("tail-wrappers: has structured attributes")
    for key, want in (("name", "adk-tail-wrappers"), ("out", outs["tail-wrappers"]),
                      ("builder", cli["env"].get("builder")), ("stdenv", cli["env"].get("stdenv")),
                      ("system", cli.get("system"))):
        if env.get(key) != want:
            problems.append(f"tail-wrappers: {key} is {env.get(key)!r}, not {want!r}")
    rest = {k: v for k, v in env.items() if k not in ("name", "out", "buildCommand", "builder", "stdenv", "system")}
    differ = sorted(k for k in rest.keys() | RUN_COMMAND_LOCAL.keys() if rest.get(k) != RUN_COMMAND_LOCAL.get(k))
    if differ:
        problems.append(f"tail-wrappers: sets {', '.join(differ)} otherwise than runCommandLocal with nothing added")

    command = env.get("buildCommand")
    if not isinstance(command, str):
        problems.append("tail-wrappers: has no build command")
        return
    skeleton = STORE_WORD.sub("<store>", command)
    if skeleton != BUILD_COMMAND:
        n, got, want = first_difference(skeleton.split("\n"), BUILD_COMMAND.split("\n"))
        problems.append(f"tail-wrappers: the build command's line {n} is {got!r}, not {want!r}")
        return
    paths = STORE_WORD.findall(command)
    if len(paths) != 4 * len(WRAPPERS):
        problems.append(f"tail-wrappers: the build command names {len(paths)} store paths, not {4 * len(WRAPPERS)}")
        return
    calls = [paths[k:k + 4] for k in range(0, len(paths), 4)]
    fills = []
    for k, placeholder in ((1, "bash"), (2, "exportPython"), (3, "carrierDrc")):
        got = sorted({call[k] for call in calls})
        if len(got) != 1:
            problems.append(f"tail-wrappers: fills @{placeholder}@ with {got}, not one path")
        fills.append(got[0])
    bash, python, carrier = fills
    if python != outs["tail-export-python"]:
        problems.append(f"tail-wrappers: fills @exportPython@ with {python}, not tail-export-python "
                        f"{outs['tail-export-python']}")
    if not carrier.endswith("-adk-verify-carrier-drc"):
        problems.append(f"tail-wrappers: fills @carrierDrc@ with {carrier}, not a copy of bin/adk-verify-carrier-drc")
    templates = [call[0] for call in calls]
    stdenv_srcs = {a for a in drv.get("args") or () if isinstance(a, str) and STORE.match(a)}
    srcs, want = set(drv.get("inputSrcs") or ()), set(templates) | {carrier} | stdenv_srcs
    if srcs != want:
        problems.append(f"tail-wrappers: its sources are not the templates, the carrier script and the "
                        f"stdenv's (extra {sorted(srcs - want)}, missing {sorted(want - srcs)})")
    for name, template in zip(WRAPPERS, templates):
        if not template.endswith(f"-{name}.in"):
            problems.append(f"tail-wrappers: {name} is made from {template}, not a {name}.in")
            continue
        text = kept(where, os.path.join("new.sources", template[len("/nix/store/"):]), f"the template {template}")
        text = text.decode("utf-8", "surrogateescape")
        for placeholder, value in (("@bash@", bash + "/bin/bash"), ("@exportPython@", python),
                                   ("@carrierDrc@", carrier)):
            text = text.replace(placeholder, value)
        wrapper(name, text, bash, python, carrier, problems)
    store_copy = kept(where, os.path.join("new.sources", carrier[len("/nix/store/"):]), f"the carrier script {carrier}")
    if store_copy != kept(where, "new.carrier-drc", "the checkout's bin/adk-verify-carrier-drc"):
        problems.append(f"tail-wrappers: {carrier} is not the checkout's bin/adk-verify-carrier-drc")


def compare(where):
    revs = load(f"{where}/revs.json")
    attrs = {side: load(f"{where}/{side}.attrs.json") for side in ("base", "new")}
    drvs = {side: merged([f"{where}/{side}.show.json", f"{where}/{side}.show-ci.json"])
            for side in ("base", "new")}
    problems = []
    for side in ("base", "new"):
        for name in SHELLS:
            if name not in attrs[side].get("shells", {}):
                raise Unreadable(f"the {side} flake has no {name} shell")
        for name in REUSED:
            if name not in attrs[side].get("packages", {}):
                raise Unreadable(f"the {side} flake has no package {name}")
    if "tail" not in attrs["new"]["shells"]:
        raise Unreadable("the new flake has no tail shell")
    for name in TAIL_PACKAGES:
        if name not in attrs["new"]["packages"]:
            raise Unreadable(f"the new flake has no package {name}")

    for name in SHELLS:
        unchanged(name, get(drvs["base"], attrs["base"]["shells"][name], f"the base {name} shell"),
                  get(drvs["new"], attrs["new"]["shells"][name], f"the new {name} shell"), revs, problems)
    outs = tail_is_cli_plus(drvs["new"], attrs["new"], problems)
    for name in REUSED:
        if attrs["new"]["packages"][name] != attrs["base"]["packages"][name]:
            problems.append(f"{name}: {attrs['new']['packages'][name]} is not the base's "
                            f"{attrs['base']['packages'][name]}")
    build = get(drvs["new"], attrs["new"]["shells"]["build"], "the new build shell")
    if build["env"].get("ADK_NIX_SG13G2_PDK") != outs["sg13g2-pdk"]:
        problems.append("build: ADK_NIX_SG13G2_PDK is not the tail shell's sg13g2-pdk")
    chain = (("tail-export-python", "kicad-fork-base"), ("tail-wrappers", "tail-export-python"))
    for name, needs in chain:
        drv = get(drvs["new"], attrs["new"]["packages"][name], name)
        if attrs["new"]["packages"][needs] not in input_drvs(drv):
            problems.append(f"{name}: is not built from {needs}")
    wrappers(where, drvs["new"], attrs["new"], outs, problems)

    for p in problems:
        print(f"adk-nix: tail eval: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"adk-nix: tail eval: cli and build differ from {revs['base']} only in the revision they "
          f"record; tail = cli + tail-wrappers + tail-export-python + kicad-fork-base + sg13g2-pdk; "
          f"kicad-fork-base {attrs['new']['packages']['kicad-fork-base']} and sg13g2-pdk "
          f"{attrs['new']['packages']['sg13g2-pdk']} are the base's; the wrappers are the image's commands, "
          f"naming only store paths and the venv and tools/ roots", file=sys.stderr)
    return 0


def main(argv):
    try:
        if len(argv) == 2 and argv[0] == "drvs":
            attrs = load(argv[1])
            paths = list(attrs.get("shells", {}).values()) + list(attrs.get("packages", {}).values())
            if not paths or not all(isinstance(p, str) and p.endswith(".drv") and STORE.match(p) for p in paths):
                raise Unreadable(f"{argv[1]}: not a list of .drv paths")
            print(" ".join(paths))
            return 0
        if len(argv) == 2 and argv[0] == "closure-infos":
            drvs = derivations(load(argv[1]))
            found = sorted({d for drv in drvs.values() for d in input_drvs(drv)
                            if d.endswith("-closure-info.drv")})
            if not found:
                raise Unreadable(f"{argv[1]}: no closure-info inputs")
            print(" ".join(found))
            return 0
        if len(argv) == 3 and argv[0] == "wrapper-sources":
            attrs = load(argv[1])
            drv = get(derivations(load(argv[2])), (attrs.get("packages") or {}).get("tail-wrappers"),
                      "tail-wrappers")
            command, srcs = drv["env"].get("buildCommand"), set(drv.get("inputSrcs") or ())
            print(" ".join(sorted({p for p in STORE_WORD.findall(command if isinstance(command, str) else "")
                                   if p in srcs})))
            return 0
        if len(argv) == 2 and argv[0] == "compare":
            return compare(argv[1])
    except Unreadable as exc:
        print(f"adk-nix: tail eval: unreadable: {exc}", file=sys.stderr)
        return 1
    print("usage: tail_eval.py drvs <attrs.json> | closure-infos <show.json> | "
          "wrapper-sources <attrs.json> <show.json> | compare <dir>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
