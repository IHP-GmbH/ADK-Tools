#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""Checks the tool identity table against late writes: none after the runtime write, and verify checks it first.

The runtime stage writes /opt/adk-tools/tool-identity.json with
nix/tool-identity-image.sh, and the table names trees by their NAR hash, the
venv's among them. Python writes a module's bytecode next to its source the
first time it imports it, unless it runs with -B or under
PYTHONDONTWRITEBYTECODE=1, and the image sets that only after the table is
written (its ENV PYTHONDONTWRITEBYTECODE=1 follows the write). A step after the
write that imports from a named tree without either would leave the table
naming bytes the image no longer holds.

So, in the runtime stage, every command after the one that writes the table,
in its own RUN and in each RUN after it:

  - starts Python only with -B, or with PYTHONDONTWRITEBYTECODE=1 set for that
    command (an assignment before it, `env`, an `export` earlier in the RUN, or
    an ENV earlier in the stage);
  - runs no other command this check cannot see into (a console script, a
    wrapper, `bash -c`, a command substitution) unless PYTHONDONTWRITEBYTECODE=1
    is set for it; the commands it can see into are a closed list that start no
    Python;
  - runs no pip, which writes into the venv whatever else it does.

The runtime stage must have exactly one RUN that writes the table. Steps
before it are not this check's.

The verify stage's first RUN is the table's verify, so it rebuilds the table on
the runtime filesystem as shipped, which shows that nothing after the write
moved a tree. Only COPY may come before it, and no COPY there may touch a path
the table names: a binary or tree nix/tool-identity-image.sh hands it, or
/nix/store, where the closure paths build.sh's facts name are. A path the
script reads at build time (klayout-python's, from the venv's klayout module)
cannot be resolved here; the check names it as unchecked, and the verify RUN
in the build is what holds it.

Usage:
    check_late_writes.py [DOCKERFILE] [--identity-script FILE]
        (defaults: the Dockerfile and nix/tool-identity-image.sh next to ci/)
    check_late_writes.py --self-test
"""

import argparse
import os
import pathlib
import re
import shlex
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAGE = "runtime"
WRITE = re.compile(r"tool-identity-image\.sh\s+image\b")
VERIFY_STAGE = "verify"
VERIFY = re.compile(r"tool-identity-image\.sh\s+verify\b")
STORE = "/nix/store"
ENV_VAR = "PYTHONDONTWRITEBYTECODE"
PYTHON = re.compile(r"python[0-9.]*")
PIP = re.compile(r"pip[0-9.]*")
# Commands that start no Python, by basename. A command not here and not a
# Python start needs PYTHONDONTWRITEBYTECODE=1.
NO_PYTHON = frozenset({
    ":", "[", "base64", "cat", "cd", "chmod", "chown", "cp", "echo", "exit", "false",
    "grep", "install", "ln", "ls", "mkdir", "mv", "printf", "rm", "rmdir", "sed", "set",
    "tar", "test", "touch", "true",
})
# Shell words that lead into a command or close a compound one.
LEADERS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{"})
CLOSERS = frozenset({"done", "fi", "}"})
OPERATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def instructions(text):
    """(line number, keyword, body) per instruction, continuations joined and
    comment lines inside them dropped, as Docker reads them."""
    out = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        start = i + 1
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [line]
        while parts[-1].rstrip().endswith("\\") and i < len(lines):
            parts[-1] = parts[-1].rstrip()[:-1]
            nxt = lines[i]
            i += 1
            if nxt.lstrip().startswith("#"):
                parts.append("\\")
                continue
            parts.append(nxt)
        joined = " ".join(p for p in parts if p != "\\")
        keyword, _, body = joined.strip().partition(" ")
        out.append((start, keyword.upper(), body.strip()))
    return out


def simple_commands(body):
    """The words of each simple command in a RUN body, or None where this
    check cannot read the body (a command substitution, a heredoc, bad quotes)."""
    if "$(" in body or "`" in body or "<<" in body:
        return None
    lexer = shlex.shlex(body, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    commands, words = [], []
    for tok in tokens:
        if tok in OPERATORS:
            if words:
                commands.append(words)
            words = []
        else:
            words.append(tok)
    if words:
        commands.append(words)
    return commands


def judge(words, exported):
    """Why one simple command breaks the rule, or None."""
    words = list(words)
    while words and words[0] in LEADERS:
        words.pop(0)
    if not words or words[0] in CLOSERS:
        return None
    if words[0] in ("for", "export", "unset"):
        return None  # `for NAME in WORDS` (the body is the next command), or the RUN's state
    set_here = exported
    while words and ASSIGNMENT.match(words[0]):
        if words[0].startswith(ENV_VAR + "="):
            set_here = words[0] == ENV_VAR + "=1"
        words.pop(0)
    if words and words[0] == "env":
        words.pop(0)
        while words and (words[0].startswith("-") or ASSIGNMENT.match(words[0])):
            word = words.pop(0)
            if word in ("-i", "-", "--ignore-environment"):
                set_here = False
            elif word == "-u" and words:
                if words.pop(0) == ENV_VAR:
                    set_here = False
            elif word.startswith(ENV_VAR + "="):
                set_here = word == ENV_VAR + "=1"
    if not words:
        return None
    command = os.path.basename(words[0])
    if PIP.fullmatch(command):
        return "runs pip, which writes into the venv"
    if PYTHON.fullmatch(command):
        options = []
        for word in words[1:]:
            if not word.startswith("-") or word in ("-", "--"):
                break
            options.append(word)
            if word.startswith(("-m", "-c")):
                break
        rest = words[1 + len(options):]
        if "-m" in options and rest and PIP.fullmatch(rest[0]):
            return "runs pip, which writes into the venv"
        if set_here or any(re.fullmatch(r"-[A-Za-z]*B[A-Za-z]*", o) for o in options):
            return None
        return "starts Python without -B or %s=1" % ENV_VAR
    if command in NO_PYTHON or set_here:
        return None
    return ("runs %s, which this check cannot see into, without %s=1"
            % (words[0], ENV_VAR))


def stage_name(body):
    m = re.search(r"\s+AS\s+(\S+)\s*$", body, re.IGNORECASE)
    return m.group(1) if m else None


def check_runtime(text):
    """Failures of the runtime stage's rule; empty when it holds."""
    failures = []
    stage = None
    seen_write = 0
    exported = False
    for line, keyword, body in instructions(text):
        if keyword == "FROM":
            stage = stage_name(body)
            exported = False
            continue
        if stage != STAGE:
            continue
        if keyword == "ENV":
            m = re.search(r"(?:^|\s)%s=(\S*)" % ENV_VAR, body)
            if m:
                exported = m.group(1) == "1"
            continue
        if keyword != "RUN":
            continue
        writes = bool(WRITE.search(body))
        if writes:
            seen_write += 1
            if seen_write > 1:
                failures.append("Dockerfile:%d: a second RUN writes the tool identity table"
                                % line)
                continue
        elif not seen_write:
            continue
        commands = simple_commands(re.sub(r"^(--\S+\s+)+", "", body))
        if commands is None:
            if not exported:
                failures.append(
                    "Dockerfile:%d: a RUN from the tool identity table write on that this "
                    "check cannot read (a command substitution, a heredoc or bad quoting), "
                    "without %s=1" % (line, ENV_VAR))
            continue
        if writes:
            first = next((i for i, w in enumerate(commands) if WRITE.search(" ".join(w))), -1)
            commands = commands[first + 1:]
        here = exported
        for words in commands:
            if words[0] == "export" and any(w.startswith(ENV_VAR + "=") for w in words[1:]):
                here = ENV_VAR + "=1" in words[1:]
            elif words[0] == "unset" and ENV_VAR in words[1:]:
                here = False
            why = judge(words, here)
            if why:
                failures.append("Dockerfile:%d: a RUN after the tool identity table write %s: %s"
                                % (line, why, " ".join(words)))
    if not seen_write:
        failures.append("the %s stage has no RUN that writes the tool identity table, so "
                        "nothing here holds the steps after it" % STAGE)
    return failures


def named_paths(script):
    """(resolved, unresolved) from the identity script's text: each a list of
    (description, path) for the binaries and trees its --tool and
    --tracked-tool specs hand the table, plus the store."""
    values = {}
    for name in ("ADK", "PY"):
        m = re.search(r'^%s="?([^"\s]+)"?$' % name, script, re.MULTILINE)
        if m:
            values[name] = m.group(1)

    def expand(value):
        for _ in range(3):
            for name, v in values.items():
                value = value.replace('"$%s"' % name, v).replace("${%s}" % name, v)
                value = re.sub(r"\$%s\b" % name, lambda _m: v, value)
        return value

    resolved, unresolved = [(STORE + " (the closure paths the facts name)", STORE)], []
    specs = re.findall(r'^\s*--(tool|tracked-tool) "(.*)"\s*\\?\s*$', script, re.MULTILINE)
    for kind, spec in specs:
        name, _, rest = spec.partition("=")
        rest = re.sub(r"\$\((?:[^()]|\([^()]*\))*\)", "V", rest)
        parts = rest.split(":")
        fields = parts[1:] if kind == "tool" else parts
        for role, value in zip(("binary", "tree"), fields):
            path = expand(value)
            what = "%s's %s" % (name, role)
            (unresolved if "$" in path else resolved).append((what, path))
    if len(specs) == 0:
        resolved = []
    return resolved, unresolved


def overlaps(a, b):
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def check_verify_head(text, named):
    """Failures of the verify stage's rule; empty when it holds."""
    if not named:
        return ["the identity script hands the table no --tool or --tracked-tool, so "
                "nothing says which paths the table names"]
    failures = []
    stage = None
    seen = False
    for line, keyword, body in instructions(text):
        if keyword == "FROM":
            stage = stage_name(body)
            if stage == VERIFY_STAGE:
                seen = True
            continue
        if stage != VERIFY_STAGE:
            continue
        if keyword == "RUN":
            if not VERIFY.search(body):
                failures.append("Dockerfile:%d: the %s stage's first RUN is not the tool "
                                "identity table's verify" % (line, VERIFY_STAGE))
            return failures
        if keyword != "COPY":
            failures.append("Dockerfile:%d: %s before the tool identity table's verify; only "
                            "COPY may come before it" % (line, keyword))
            continue
        target = shlex.split(body)[-1]
        if not target.startswith("/"):
            failures.append("Dockerfile:%d: a COPY before the tool identity table's verify "
                            "to a relative target, %s, which this check cannot place"
                            % (line, target))
            continue
        for what, path in named:
            if overlaps(target, path):
                failures.append("Dockerfile:%d: a COPY before the tool identity table's verify "
                                "to %s overlaps %s, %s" % (line, target, what, path))
    if not seen:
        failures.append("no %s stage, so nothing checks the tool identity table in the image"
                        % VERIFY_STAGE)
    else:
        failures.append("the %s stage has no RUN, so it never verifies the tool identity "
                        "table" % VERIFY_STAGE)
    return failures


def check(text, script):
    """(failures, unresolved) for a Dockerfile's text and the identity script's."""
    resolved, unresolved = named_paths(script)
    return check_runtime(text) + check_verify_head(text, resolved), unresolved


def self_test():
    write = ("RUN /opt/adk-tools/nix/tool-identity-image.sh image"
             " --out /opt/adk-tools/tool-identity.json\n")
    head = ("FROM ubuntu:24.04 AS deps\n"
            "RUN apt-get install -y python3\n"
            "FROM deps AS runtime\n"
            "RUN /opt/adk-tools/venv/bin/python3 -m pip install x\n" + write)
    schema = ("COPY formats/adk-tools-tool-identity.v1.schema.json"
              " /opt/adk-tools/formats/adk-tools-tool-identity.v1.schema.json\n")
    verify = ("RUN /opt/adk-tools/nix/tool-identity-image.sh verify"
              " --table /opt/adk-tools/tool-identity.json \\\n"
              "        --schema /opt/adk-tools/formats/adk-tools-tool-identity.v1.schema.json\n")
    studio = ("COPY --from=studio-builder /opt/adk-tools/chiplet-studio"
              " /opt/adk-tools/chiplet-studio\n")
    tail = ("COPY LICENSE NOTICE.md /opt/adk-tools/\n"
            "WORKDIR /work\n"
            "ENTRYPOINT [\"/usr/local/bin/adk-entrypoint\"]\n"
            "FROM runtime AS verify\n" + schema + verify + studio +
            "RUN /opt/adk-tools/venv/bin/python3 /opt/adk-tools/ci/x.py\n")
    # The identity script's shape: two variables, specs one per line.
    script = (
        "ADK=/opt/adk-tools\n"
        'PY="$ADK/venv/bin/python3"\n'
        'read -r kmodule kdir kbinary < <("$PY" -B -c \'...\')\n'
        'exec "$PY" -B "$ADK/nix/tool_identity.py" "$mode" \\\n'
        '    --tool "chiplet-studio=$studio:$ADK/chiplet-studio/build/chiplet-studio:'
        '$ADK/chiplet-studio/build" \\\n'
        '    --tool "kicad-cli=$kicad:/usr/bin/kicad-cli" \\\n'
        '    --tool "klayout-python=$kmodule:$kbinary:$kdir" \\\n'
        '    --tool "python=$("$PY" -V):$PY:$ADK/venv" \\\n'
        '    --tool "git=$(/usr/bin/git --version):/usr/bin/git" \\\n'
        '    --tracked-tool "chiplet-cosim=$ADK/venv/bin/chiplet-cosim:$ADK/staging/chiplet-cosim" \\\n'
        '    "$@"\n')
    py = "/opt/adk-tools/venv/bin/python3"

    def verify_head(text):
        return tail.replace(schema + verify + studio, text)

    def after_write(text):
        return head.replace(write, write[:-1] + " \\\n    && " + text + "\n") + tail

    cases = [
        ("the steps after the write start no Python", head + tail, None),
        ("an import without -B after the write is refused",
         head + 'RUN %s -c "import yaml; print(yaml.__version__)"\n' % py + tail, "without -B"),
        ("an import without -B in the write's own RUN is refused",
         after_write('%s -c "import yaml"' % py), "without -B"),
        ("a shell command in the write's own RUN passes",
         after_write("chmod 0444 /opt/adk-tools/tool-identity.json"), None),
        ("-B passes", head + 'RUN %s -B -c "import yaml"\n' % py + tail, None),
        ("-B among other options passes", head + "RUN %s -I -B /x.py\n" % py + tail, None),
        ("-B in a cluster passes", head + "RUN %s -IB /x.py\n" % py + tail, None),
        ("-B after the script is the script's, and refused",
         head + "RUN %s /x.py -B\n" % py + tail, "without -B"),
        ("the variable on the command passes",
         head + "RUN %s=1 %s /x.py\n" % (ENV_VAR, py) + tail, None),
        ("the variable through env passes",
         head + "RUN env -u PYTHONPATH %s=1 %s /x.py\n" % (ENV_VAR, py) + tail, None),
        ("the variable on another command does not count",
         head + "RUN %s=1 true && %s /x.py\n" % (ENV_VAR, py) + tail, "without -B"),
        ("an export earlier in the RUN passes",
         head + "RUN export %s=1 && %s /x.py\n" % (ENV_VAR, py) + tail, None),
        ("an unset after the export ends it",
         head + "RUN export %s=1 && unset %s && %s /x.py\n" % (ENV_VAR, ENV_VAR, py) + tail,
         "without -B"),
        ("an ENV earlier in the stage passes",
         head + "ENV %s=1\nRUN %s /x.py && adk-smoke\n" % (ENV_VAR, py) + tail, None),
        ("an ENV that empties the variable ends it",
         head + "ENV %s=1\nENV %s=\nRUN %s /x.py\n" % (ENV_VAR, ENV_VAR, py) + tail,
         "without -B"),
        ("env -u drops the variable",
         head + "ENV %s=1\nRUN env -u %s %s /x.py\n" % (ENV_VAR, ENV_VAR, py) + tail,
         "without -B"),
        ("env -i drops the variable",
         head + "ENV %s=1\nRUN env -i %s /x.py\n" % (ENV_VAR, py) + tail, "without -B"),
        ("a console script is refused", head + "RUN adk-smoke\n" + tail, "cannot see into"),
        ("a console script by path is refused",
         head + "RUN /opt/adk-tools/venv/bin/chiplet-cosim --help\n" + tail, "cannot see into"),
        ("a console script under the variable passes",
         head + "RUN %s=1 adk-smoke\n" % ENV_VAR + tail, None),
        ("bash -c is refused", head + "RUN bash -c 'true'\n" + tail, "cannot see into"),
        ("pip is refused", head + "RUN /opt/adk-tools/venv/bin/pip install x\n" + tail,
         "runs pip"),
        ("pip through -m is refused even with -B",
         head + "RUN %s -B -m pip install x\n" % py + tail, "runs pip"),
        ("pip under the variable is still refused",
         head + "RUN %s=1 pip3 install x\n" % ENV_VAR + tail, "runs pip"),
        ("a command substitution is refused",
         head + 'RUN echo "$(%s -V)"\n' % py + tail, "cannot read"),
        ("a heredoc is refused", head + "RUN <<EOF\ntrue\nEOF\n" + tail, "cannot read"),
        ("a shell-only RUN passes",
         head + "RUN mkdir -p /x \\\n    && ln -s /x /y; chmod 0755 /y\n" + tail, None),
        ("a loop whose body starts Python with -B passes",
         head + "RUN for p in a b; do \\\n        %s -B /x.py \"$p\"; \\\n    done\n" % py + tail,
         None),
        ("a loop whose body starts Python without -B is refused",
         head + "RUN for p in a b; do \\\n        %s /x.py \"$p\"; \\\n    done\n" % py + tail,
         "without -B"),
        ("a comment line inside a RUN is dropped",
         head + "RUN true \\\n# %s /x.py\n    && false\n" % py + tail, None),
        ("a Python start before the write is not this check's",
         head.replace("FROM deps AS runtime\n",
                      "FROM deps AS runtime\nRUN %s /x.py\n" % py) + tail, None),
        ("another stage is not this check's", head + tail + "RUN adk-smoke\n", None),
        ("a runtime stage with no write is refused",
         head.replace(write, "RUN true\n") + tail, "no RUN that writes"),
        ("a second write is refused",
         head + "RUN /opt/adk-tools/nix/tool-identity-image.sh image --out /x\n" + tail,
         "a second RUN writes"),
        ("the Studio tree copied before the table's verify is refused",
         head + verify_head(schema + studio + verify), "overlaps chiplet-studio's tree"),
        ("the schema copied into a row's tree is refused",
         head + verify_head(schema.replace(" /opt/adk-tools/formats/", " /opt/adk-tools/venv/")
                            + verify + studio), "overlaps python's tree"),
        ("a COPY over a binary the table names is refused",
         head + verify_head(schema + "COPY git /usr/bin/git\n" + verify + studio),
         "overlaps git's binary"),
        ("a COPY into the store is refused",
         head + verify_head(schema + "COPY x /nix/store/x\n" + verify + studio),
         "overlaps /nix/store"),
        ("a relative COPY target is refused",
         head + verify_head("WORKDIR /opt/adk-tools\nCOPY x venv/x\n" + verify + studio),
         "relative target"),
        ("an instruction other than COPY before the table's verify is refused",
         head + verify_head("ENV X=1\n" + schema + verify + studio), "only COPY"),
        ("a RUN before the table's verify is refused",
         head + verify_head(schema + "RUN true\n" + verify + studio), "first RUN is not"),
        ("a verify stage that never verifies the table is refused",
         head + verify_head(schema + studio), "first RUN is not"),
        ("a verify stage with no RUN is refused",
         head + verify_head(schema).replace(
             "RUN /opt/adk-tools/venv/bin/python3 /opt/adk-tools/ci/x.py\n", ""),
         "has no RUN"),
        ("no verify stage is refused",
         head + tail[:tail.index("FROM runtime AS verify")], "no verify stage"),
        ("an identity script with no specs is refused",
         head + tail, "no --tool"),
    ]
    failed = []
    for name, text, expect_fail in cases:
        failures, _ = check(text, "" if "no specs" in name else script)
        ok = (not failures) if expect_fail is None else (expect_fail in "\n".join(failures))
        if not ok:
            failed.append("%s: expected %s, got %s" % (
                name, "a pass" if expect_fail is None else repr(expect_fail),
                failures or "a pass"))
    # The paths the script hands the table, resolved where the script says them.
    resolved, unresolved = named_paths(script)
    want = [("/nix/store (the closure paths the facts name)", "/nix/store"),
            ("chiplet-studio's binary", "/opt/adk-tools/chiplet-studio/build/chiplet-studio"),
            ("chiplet-studio's tree", "/opt/adk-tools/chiplet-studio/build"),
            ("kicad-cli's binary", "/usr/bin/kicad-cli"),
            ("python's binary", "/opt/adk-tools/venv/bin/python3"),
            ("python's tree", "/opt/adk-tools/venv"),
            ("git's binary", "/usr/bin/git"),
            ("chiplet-cosim's binary", "/opt/adk-tools/venv/bin/chiplet-cosim"),
            ("chiplet-cosim's tree", "/opt/adk-tools/staging/chiplet-cosim")]
    cases.append(("each spec's binary and tree resolve where the script names them", "", None))
    if resolved != want:
        failed.append("each spec's binary and tree resolve where the script names them: "
                      "got %s" % resolved)
    cases.append(("a path read at build time is named as unchecked", "", None))
    if unresolved != [("klayout-python's binary", "$kbinary"), ("klayout-python's tree", "$kdir")]:
        failed.append("a path read at build time is named as unchecked: got %s" % unresolved)
    for f in failed:
        print("FAIL: %s" % f)
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dockerfile", nargs="?", default=str(ROOT / "Dockerfile"))
    ap.add_argument("--identity-script", default=str(ROOT / "nix" / "tool-identity-image.sh"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    failures, unresolved = check(pathlib.Path(args.dockerfile).read_text(),
                                 pathlib.Path(args.identity_script).read_text())
    for f in failures:
        print("FAIL: %s" % f, file=sys.stderr)
    if failures:
        return 1
    print("%s: nothing after the tool identity table write can write bytecode, and the "
          "verify stage checks the table first" % args.dockerfile)
    for what, path in unresolved:
        print("not checked: %s, %s, is read at build time; the verify RUN holds it"
              % (what, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
