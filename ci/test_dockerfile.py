#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 IHP GmbH
"""ci/dockerfile.py and ci/shell_reader.py: what a Dockerfile and the shell of
its RUNs are read as, case by case.

Usage: python3 -B ci/test_dockerfile.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dockerfile import Dockerfile, Unmodelled, copy_step  # noqa: E402
from shell_reader import Shell  # noqa: E402


def main():
    cases = []

    def case(name):
        def register(fn):
            cases.append((name, fn))
            return fn
        return register

    def run(text, cwd="/"):
        return [(c.cwd, c.raw) for c in Shell(lambda n: None).commands(text, cwd)]

    @case("ARG, ENV, WORKDIR and FROM read as docker reads them")
    def _():
        df = Dockerfile("ARG G=/g\nARG H\nFROM x AS a\nARG G\nARG L=$G/l\n"
                        "ENV E='$G' F=\"$G\" G=/e M=${G:-y}\nWORKDIR \"w d\"\nRUN true\n"
                        "FROM a AS b\nRUN true\nFROM b AS b2\nFROM ubuntu AS ubuntu\nRUN true\n",
                        ["H=/h"])
        a, b = df.stages["a"].steps[-1], df.stages["b"].steps[-1]
        got = [a.lookup(n) for n in ("G", "L", "E", "F", "M", "H")] + [a.cwd]
        got += [b.lookup(n) for n in ("G", "L", "F")] + [df.stages["b2"].parent.name]
        got.append(df.stages["ubuntu"].parent)
        try:
            Dockerfile("# escape=`\nFROM x\n")
            escape = False
        except Unmodelled:
            escape = True
        blank = [s.word for s in Dockerfile("FROM x\nRUN a \\\n\n    b\n").stages["#0"].steps]
        return got == ["/e", "/g/l", "$G", "/g", None, None, "/w d",
                       "/e", None, "/g", "b", None] and escape and blank == ["RUN"]

    @case("a Dockerfile with a heredoc, an escape directive or a bare FROM is not read")
    def _():
        out = []
        for text in ("FROM x\nRUN cat <<EOF >/x\nENV A=1\nEOF\n", "\ufeff# escape=`\nFROM x\n",
                     "FROM\n", "FROM x\nWORKDIR /a$1\n"):
            try:
                df = Dockerfile(text)
                out.append(text.startswith("FROM x\nW") and df.stages["#0"].cwd is None)
            except Unmodelled:
                out.append(True)
        return all(out)

    @case("a relative WORKDIR or path after an unsettled one is unsettled, an absolute one is not")
    def _():
        df = Dockerfile("FROM x AS a\nWORKDIR $NOPE\nWORKDIR w\nRUN true\nWORKDIR /w\nRUN true\n")
        steps = df.stages["a"].steps
        try:
            steps[2].path("x")
            relative = False
        except Unmodelled:
            relative = True
        return [steps[2].cwd, steps[4].cwd, steps[2].path("/x")] == [None, "/w", "/x"] and relative

    @case("the shell reader keeps quotes, substitutions and loop variables")
    def _():
        got = [(c.raw, [str(w) for w in c.words]) for c in Shell(lambda n: None).commands(
            'a "x && y" && b $(c && d) ; for f in 1; do e "$f" \'$g\'; done', "/")]
        return got == [('a "x && y"', ["a", "x && y"]), ("c", ["c"]), ("d", ["d"]),
                       ("b $(c && d)", ["b", "...", "..."]),
                       ("do e \"$f\" '$g'", ["e", "...", "$g"])]

    @case("a cd lasts past its list only when it cannot have failed")
    def _():
        got = [run(t)[-1][0] for t in (
            "cd /a && x; y",
            "set -e; cd /a; y",
            "set -e; cd /a && x; y",
            "set -e; if [ -d /a ]; then cd /a; fi; y",
            "set -e; ! cd /a; y",
            "(set -e); cd /a; y",
            "x; set -e; cd /a; y",
            "set -o errexit; cd /a; y",
            "set -e; cd /a & y",
            "cd /a || exit 1; y",
            "for f in 1; do cd /a; done; y",
            "(cd /a; x); y",
            "cd \"$X\"; y",
            "cd \"$X\"; cd /b && y",
            "cd /a | true; y",
            "cd -P /a && y",
            "cd -- /a && y",
            "cd -x /a && y",
            "CDPATH=/opt; cd a && y",
            "cd ./a && y",
        )]
        return got == [None, "/a", None, None, None, None, None, None, "/", None, None, "/",
                       None, "/b", "/", None, "/a", None, None, "/a"]

    @case("a variable a command may not set, or sets by its own logic, is unsettled")
    def _():
        def value(text):
            return [str(c.words[-1]) for c in Shell(lambda n: None).commands(text, "/")
                    if c.words][-1]
        return [value(t + "; echo \"$P\"") for t in (
            "P=/a", "P=/a & true", "if x; then P=/a; fi", "true && P=/a", "read P",
            "P=/a; P+=/b", "P=~/a", "printf -v P x", "P=/a; P=/b true", "export P=/a",
        )] == ["/a", "...", "...", "...", "...", "...", "...", "...", "/a", "/a"]

    @case("a shell or wrapper option this reader does not know is not read past")
    def _():
        def errors(text):
            return [bool(c.error) for c in Shell(lambda n: None).commands(text, "/")]
        return ([errors(t) for t in (
            "sh -O extglob -c x", "bash --rcfile /r -c x", "sh -i x", "env -iC/a rm x",
            "env --ch=/a rm x", "nice --adj 5 rm x", "timeout --sig KILL 9 rm x",
            "/bin/sh -s", "trap \"$X\" EXIT", "sh -c -- 'rm -rf /a'", "trap 'rm /a' EXIT",
            "builtin rm /a", "time rm /a",
        )] == [[True]] * 13
            and [c.said for c in Shell(lambda n: None).commands(
                "command rm -rf /a; command -p rm /b; /usr/bin/env -u X rm /c; "
                "nice --adjustment 5 rm /d; timeout --signal KILL 9 rm /e", "/")]
            == [["rm", "-rf", "/a"], ["rm", "/b"], ["rm", "/c"], ["rm", "/d"], ["rm", "/e"]])

    @case("a COPY lands a directory source's contents in its destination, a file into it "
          "when it ends in / or there are several sources")
    def _():
        def placed(rest):
            step = Dockerfile("FROM x\nWORKDIR /w\nCOPY %s\n" % rest).stages["#0"].steps[-1]
            return list(copy_step(step).placements())
        return (placed("a d") == [("a", "/w/d", "/w/d")]
                and placed("a/ b /d/") == [("b", "/d", "/d/b"), ("a/", "/d", "/d/a")]
                and placed("--from=s a /d") == [("a", "/d", "/d")]
                and copy_step(Dockerfile("FROM x\nCOPY --from=s a /d\n")
                              .stages["#0"].steps[-1]).frm == "s")

    failed = []
    for name, fn in cases:
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001 - a crashing case is a failed case
            ok = False
            name = "%s (raised %s: %s)" % (name, type(exc).__name__, exc)
        if not ok:
            failed.append(name)
        print("%s: %s" % ("ok" if ok else "FAIL", name))
    print("%d/%d self-test cases passed" % (len(cases) - len(failed), len(cases)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
