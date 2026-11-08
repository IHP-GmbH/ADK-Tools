#!/bin/sh
# sh runs the next two lines: TERM, INT and HUP are ignored until Python has installed its handlers, and
# this same file is handed to Python, which reads both lines as strings that do nothing.
"trap" "" "TERM" "INT" "HUP"
"exec" "python3" "-I" "$0" "$@"
r"""Run one read-only command (packaging-flow status or verify) in a pinned adk-tools image.

Usage:
  ./host-verify.sh --image sha256:<64 hex> --record <new dir> \
      [--mount <host path>:<container path>]... [--env NAME=VALUE]... \
      [--cpus N] [--memory SIZE] [--timeout SECONDS] -- <command> [args...]

The caller supplies the whole execution context: the image by its ID and every host path the
command may read, each mapped to the container path the run recorded. Nothing is inferred from a
manifest or from the working directory. Every mount is a read-only bind declared with --mount,
which refuses a missing source where -v would create it. One container runs one command, with no
shell, no terminal and no stdin. The launcher never reads the output: the verdict stays with
packaging-flow, and the command's exit code is passed through once docker confirms it.

The record directory is created by the launcher, so it must not exist yet, and it may not lie
inside any mounted source. It receives argv.txt (the docker command line, written before the
launch), stdout.txt, stderr.txt and record.json (format adk-tools-host-verify-record/v1).

Exit codes. Every launcher outcome uses a code packaging-flow never returns (its table is 0-11 and
130; the launcher's 64-75 are disjoint from it):
  64  usage error; nothing written
  66  not executed: the context is incomplete or does not match; recorded
  69  interrupted (TERM, INT or HUP) before the command finished; the container, if docker had
      created it, was stopped and removed; recorded, or nothing written if the signal came before
      the record directory existed
  70  launcher failure: docker did not create or start the container, docker itself failed, the
      client died without confirming the result, or its exit disagrees with the container's; recorded
  73  the record directory was refused (exists, parent missing or not writable, inside an input,
      or an input inside it); nothing written, and a record.json already there is not from this run
  74  record.json could not be written; the outcome it would have recorded is printed on stderr
      and the streams are replayed
  75  timed out; the container was stopped and removed; recorded
Any other code is the command's own, passed through only once docker inspect confirms it. A
container that could not be removed is reported on stderr and in the record on every path.

Signals. Once its Python handlers are installed, the launcher only notes the first TERM, INT or
HUP and ignores the rest. Every docker and git call runs in a session of its own, so a terminal's
Ctrl-C or hangup normally reaches the launcher alone. During process creation, before the child
has entered that session, a group signal can reach both. If the launcher has noted the same signal
when it observes the client's signal death, that is an interruption, not a command result. After
a noted signal the launcher stops a still-running container through the daemon, retrying docker kill until
the docker client ends (after 60 s it kills the client), removes the container, records and
exits 69. Once the launcher has seen the docker client end, the outcome is fixed: a later signal
changes nothing and does not cut the replay. If the client was killed, by the launcher or externally,
a container the daemon has not reported by then is recorded as unknown, never as not created: the
daemon may still create it for the request that client sent. A client killed by a signal cannot
confirm a command result, even if its normalized exit matches the container's. A signal that arrives
while Python is still starting, before its handlers exist, is ignored (an interrupted start would otherwise end in
Python's own exit code 1, which packaging-flow also uses). Once the exit code is decided they are
ignored again, so Python's shutdown cannot end the launcher by a signal the record does not show.
SIGKILL cannot be handled: the launcher cannot do any further recording or cleanup. A record
already written can remain. During an active run, a container may keep running or appear later;
inspect the name in argv.txt and, if necessary, remove it by hand (docker rm -f <name>).

Provenance hashes the script file read during startup. Keep that file unchanged while starting
the launcher: concurrent replacement can make that snapshot differ from Python's loaded source.

Needs python3 (3.8 or later) and docker on PATH; the launcher uses the standard library only.
"""
# Signals first, before the other imports: a handler only notes the first one, and the flow decides
# what it means. Until this runs they are ignored, as the sh line above left them.
import signal

SIGNAMES = {signal.SIGTERM: "TERM", signal.SIGINT: "INT", signal.SIGHUP: "HUP"}
STOP = None


def note_signal(signum, frame):
    global STOP
    if STOP is None:
        STOP = SIGNAMES[signum]


for _sig in SIGNAMES:
    signal.signal(_sig, note_signal)

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

EX_USAGE, EX_NOT_EXECUTED, EX_INTERRUPTED, EX_SOFTWARE = 64, 66, 69, 70
EX_RECORD_REFUSED, EX_RECORD_IO, EX_TIMEOUT = 73, 74, 75
RECORD_FORMAT = "adk-tools-host-verify-record/v1"
FLOOR_KB = 10 * 1024 * 1024   # the host keeps at least 10 GiB available beyond this container
FREE_CORES = 4                # and at least four cores beyond --cpus
HELPER_S = 60                 # the longest any single docker or git call may take
STOP_S = 60                   # how long docker kill is retried before the client itself is killed
POLL_S = 0.2
CT_FORMAT = "{{.State.Status}}|{{.State.StartedAt}}|{{.State.ExitCode}}|{{.State.OOMKilled}}"
GIT_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")

SELF = os.path.realpath(sys.argv[0])
with open(SELF, "rb") as _f:
    SELF_BYTES = _f.read()   # startup file snapshot; assumes no concurrent replacement while loading


class Outcome(Exception):
    """The end of a run that has a record directory: an exit code and the line that explains it."""

    def __init__(self, code, message=None):
        super().__init__(code, message)
        self.code, self.message = code, message


class Mount:
    def __init__(self, src, dst):
        self.src, self.dst = src, dst
        self.abs = self.near = None
        self.missing = False


def write_all(fd, data):
    """Write everything, or stop quietly once the reader is gone (a closed pipe, a hung-up terminal)."""
    try:
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass


def say(message):
    write_all(2, (message + "\n").encode("utf-8", "surrogateescape"))


def usage(message):
    say(f"host-verify: {message}")
    say(f"usage: see the header of {SELF}")
    sys.exit(EX_USAGE)


def refuse_record(message):
    say(f"host-verify: record refused: {message}")
    sys.exit(EX_RECORD_REFUSED)


def resolve(path):
    """Where the kernel resolves the literal path to, with every component required to exist, or None.

    A path is opened without being read, and the kernel names what it opened: so file/.. and
    missing/../x are refused exactly as the kernel would, where a string-level realpath collapses them.
    """
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except (OSError, ValueError):
        return None
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)


def resolve_near(path):
    """Where a missing path would lie, links followed as far as they exist (realpath -m)."""
    return os.path.realpath(path)


def logical_cwd():
    """The working directory as the caller's shell names it, as bash's $PWD does."""
    pwd = os.environ.get("PWD", "")
    try:
        if pwd.startswith("/") and os.path.samefile(pwd, "."):
            return pwd
    except OSError:
        pass
    return os.getcwd()


def bad_mount_chars(s):
    return any(c in s for c in ',"\n')


def exit_status(returncode):
    """A Popen return code as the shell reports it (128 + N for a process ended by signal N)."""
    return 128 - returncode if returncode < 0 else returncode


def kill_session(p):
    """SIGKILL a child started in a session of its own, and everything it started in that session."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# --- the command line: every refusal here exits 64 with nothing written ---
def parse(args):
    opts = {"--image": "", "--record": "", "--cpus": "2", "--memory": "6g", "--timeout": "900"}
    needs = {"--image": "--image needs a value", "--record": "--record needs a value",
             "--mount": "--mount needs <host path>:<container path>", "--env": "--env needs NAME=VALUE",
             "--cpus": "--cpus needs a value", "--memory": "--memory needs a value",
             "--timeout": "--timeout needs a value"}
    mounts, envs, cmd = [], [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            cmd = args[i + 1:]
            break
        if a not in needs:
            usage(f"unknown argument {a}")
        if i + 1 >= len(args):
            usage(needs[a])
        v = args[i + 1]
        if a == "--mount":
            if ":" not in v:
                usage(f"--mount {v} is not <host path>:<container path>")
            cut = v.rindex(":")
            mounts.append(Mount(v[:cut], v[cut + 1:]))
        elif a == "--env":
            envs.append(v)
        else:
            opts[a] = v
        i += 2
    return opts, mounts, envs, cmd


def check_usage(opts, mounts, envs, cmd):
    if not cmd:
        usage("no command after --")
    rec, img = opts["--record"], opts["--image"]
    cpus, memory, timeout = opts["--cpus"], opts["--memory"], opts["--timeout"]
    if not rec:
        usage("--record is required")
    if "\n" in rec:
        usage("--record: a newline in the path is not supported")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", img):
        usage("--image must be an image ID, sha256:<64 hex>; a tag can move")
    if not re.fullmatch(r"[0-9]{1,3}([.][0-9]{1,3})?", cpus) or re.fullmatch(r"0+([.]0+)?", cpus):
        usage(f"--cpus {cpus} is not a positive number below 1000 with at most three decimals")
    if not re.fullmatch(r"[1-9][0-9]{0,8}[kmgKMG]", memory):
        usage(f"--memory {memory} is not a positive size in k, m or g")
    if not re.fullmatch(r"[1-9][0-9]{0,6}", timeout):
        usage(f"--timeout {timeout} is not a positive whole number of seconds")
    if os.geteuid() == 0:
        usage("run as the invoking user, not root: the container runs with the caller's uid")
    for e in envs:
        if not re.match(r"[A-Za-z_][A-Za-z0-9_]*=", e):
            usage(f"--env {e} is not NAME=VALUE")
    cwd = logical_cwd()
    for i, m in enumerate(mounts):
        s, d = m.src, m.dst
        if not s:
            usage(f"--mount :{d} has no host path")
        if not d.startswith("/") or d == "/":
            usage(f"--mount {s}:{d}: the container path must be absolute and not /")
        if bad_mount_chars(d):
            usage(f"--mount {s}:{d}: a comma, quote or newline cannot be passed to --mount")
        if any(part in ("", ".", "..") for part in d.split("/")[1:]):
            usage(f"--mount {s}:{d}: the container path is not normalized")
        if any(o.dst == d for o in mounts[:i]):
            usage(f"--mount: {d} is mapped twice")
        # Resolved once here and used everywhere after, so the checks, docker and the record all name
        # the same path even if a symlink on the way is repointed during the run. A source that does
        # not resolve as written is kept, and recorded, as the caller wrote it (made absolute, not
        # normalized) and refused (66) before anything runs; where it would lie, links followed,
        # only serves to refuse a record directory that writing the record would turn into that input.
        m.abs = resolve(s)
        if m.abs is None:
            m.abs = s if s.startswith("/") else f"{cwd}/{s}"
            m.missing = True
            m.near = resolve_near(s)
        if bad_mount_chars(s) or bad_mount_chars(m.abs):
            usage(f"--mount {s}:{d}: a comma, quote or newline cannot be passed to --mount")
    if shutil.which("docker") is None:
        usage("docker is not on PATH")


# --- the record directory: new, writable, outside every input; refused with nothing written ---
def check_record(rec, mounts):
    r = rec
    while len(r) > 1 and r.endswith("/"):
        r = r[:-1]
    if os.path.lexists(r):
        refuse_record(f"{r} already exists; a record is written once")
    if "/" in r:
        rdir, rbase = r[:r.rindex("/")] or "/", r[r.rindex("/") + 1:]
    else:
        rdir, rbase = ".", r
    parent = resolve(rdir)
    if parent is None:
        refuse_record(f"the parent of {r} does not exist")
    if "\n" in parent:
        refuse_record(f"the parent of {r} resolves to a path with a newline")
    if not os.path.isdir(parent):
        refuse_record(f"the parent {parent} is not a directory")
    if not os.access(parent, os.W_OK):
        refuse_record(f"the parent {parent} is not writable")
    rec_abs = parent.rstrip("/") + "/" + rbase
    for m in mounts:
        if not m.missing:
            if (rec_abs + "/").startswith(m.abs.rstrip("/") + "/"):
                refuse_record(f"{rec_abs} lies inside the input {m.abs}")
            sa = m.abs
        else:
            sa = m.near   # the reverse: the launcher would create that input itself by writing the record
        if (sa.rstrip("/") + "/").startswith(rec_abs + "/"):
            refuse_record(f"the input {sa} lies inside the record directory {rec_abs}")
    return parent, rec_abs


def create_record_dir(rec_abs, mounts):
    if STOP is not None:
        say(f"host-verify: interrupted by SIG{STOP} before the record directory existed; nothing written")
        sys.exit(EX_INTERRUPTED)
    try:
        os.mkdir(rec_abs)
    except OSError:
        refuse_record(f"cannot create {rec_abs}")
    # A missing input can also pass through the record directory (out/r1/.., or a link through it); only
    # creating it shows that, so an input that resolves now is refused while the directory is still empty.
    for m in mounts:
        if m.missing and resolve(m.src) is not None:
            os.rmdir(rec_abs)
            refuse_record(f"the input {m.src} only resolves once the record directory {rec_abs} exists")


# --- from here every outcome is recorded ---
class Run:
    def __init__(self, opts, mounts, envs, cmd, parent, rec_abs):
        self.img, self.cpus, self.memory = opts["--image"], opts["--cpus"], opts["--memory"]
        self.timeout = int(opts["--timeout"])
        self.mounts, self.envs, self.cmd = mounts, envs, cmd
        self.parent, self.rec = parent, rec_abs
        self.started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.name = "adk-host-verify-%s-%d-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                                                  os.getpid(), os.urandom(3).hex())
        self.inspected = self.reason = self.failure = self.inspect_err = self.interrupted = None
        self.client_returncode = self.cli_rc = None
        self.state_status = self.state_rc = self.oom = self.removed = None
        self.created = False       # True, False, or None when the daemon could not say
        self.started_ct = self.executed = self.timed_out = self.passed = False
        self.client = None
        self.client_killed = self.settled = False
        self.docker_argv = []
        self.tmpd = self.final = None

    # -- helpers --
    def helper(self, argv, timeout=HELPER_S, env=None):
        """One docker or git call: its own session, no stdin, output through scratch files, bounded."""
        out_path, err_path = os.path.join(self.tmpd, "out"), os.path.join(self.tmpd, "err")
        try:
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                                     start_new_session=True)
        except OSError as exc:
            return None, "", str(exc)
        try:
            rc = p.wait(max(timeout, 0.1))
        except subprocess.TimeoutExpired:
            kill_session(p)
            p.wait()
            return None, self.read(out_path), f"no answer within {max(timeout, 0.1):.0f} s"
        return rc, self.read(out_path), self.read(err_path)

    @staticmethod
    def read(path, limit=None):
        try:
            with open(path, "rb") as f:
                return f.read(limit if limit is not None else -1).decode("utf-8", "replace")
        except OSError:
            return ""

    def early_stop(self):
        if STOP is not None:
            self.interrupted = STOP
            raise Outcome(EX_INTERRUPTED, f"interrupted by SIG{STOP} before any container was started")

    def refuse(self, reason):
        self.reason = reason
        raise Outcome(EX_NOT_EXECUTED, f"not executed: {reason}")

    def fail(self, failure):
        self.failure = failure
        raise Outcome(EX_SOFTWARE, f"launcher failure: {failure}")

    def fate(self):
        if self.created is False:
            return f"docker had not created container {self.name}"
        if self.created is None:
            return f"creation of container {self.name} is unknown; check and remove it by name if needed"
        if self.removed:
            return f"container {self.name} stopped and removed"
        return f"container {self.name} could not be confirmed removed; remove it by hand"

    def leftover(self):
        return f"; {self.fate()}" if self.removed is False else ""

    # -- the run --
    def main(self):
        try:
            for name in ("stdout.txt", "stderr.txt", "argv.txt"):
                open(os.path.join(self.rec, name), "wb").close()
        except OSError as exc:
            say(f"host-verify: cannot write in {self.rec} ({exc}); nothing ran (the record is incomplete)")
            return EX_RECORD_IO
        # Scratch for captured helper output, kept out of the record directory. It sits beside it, in the
        # parent already checked to be writable and outside every input ($TMPDIR might not be).
        try:
            self.tmpd = tempfile.mkdtemp(prefix=".host-verify-scratch.", dir=self.parent)
        except OSError:
            say(f"host-verify: cannot create a scratch directory in {self.parent}; nothing ran "
                f"(record in {self.rec} is incomplete)")
            return EX_RECORD_IO
        try:
            self.execute()
            raise Outcome(EX_SOFTWARE, "the launcher reached its end without an outcome")
        except Outcome as o:
            return self.finish(o.code, o.message)
        except Exception as exc:   # a defect here still ends with the container gone and a record
            self.failure = f"internal error: {type(exc).__name__}: {exc}"
            try:
                if self.client is not None and not self.settled:
                    if self.client.poll() is None:
                        self.stop(self.client)
                    self.settle()
            except Exception:
                pass
            return self.finish(EX_SOFTWARE, f"launcher failure: {self.failure}{self.leftover()}")

    def execute(self):
        self.early_stop()
        # --- the context has to match before anything starts ---
        rc, out, err = self.helper(["docker", "image", "inspect", "--format", "{{.Id}}", self.img])
        err = err[:400]
        self.early_stop()
        if rc != 0:
            if "No such image" in err:
                self.refuse(f"the image {self.img} is not present on this daemon")
            self.fail(f"docker image inspect failed: {err}")
        self.inspected = out.rstrip("\n")
        if self.inspected != self.img:
            self.refuse(f"the daemon resolves {self.img} to {self.inspected}, not to that exact ID")

        for m in self.mounts:
            if m.missing:
                self.refuse(f"the input {m.src} (for {m.dst}) does not exist or does not resolve")
        for m in self.mounts:
            a, d = m.abs, m.dst
            if not os.path.exists(a):
                self.refuse(f"the input {a} (for {d}) does not exist")
            if not os.access(a, os.R_OK):
                self.refuse(f"the input {a} (for {d}) is not readable")
            # A target nested under another read-only mount needs its mountpoint to exist, with the same
            # kind (directory or not), in the innermost enclosing mount's source already: docker would
            # otherwise have to create it inside a read-only bind, or fail at start on a type mismatch.
            outer = [o for o in self.mounts if o is not m and d.startswith(o.dst + "/")]
            if outer:
                o = max(outer, key=lambda o: len(o.dst))
                mp = o.abs + "/" + d[len(o.dst) + 1:]
                if not os.path.exists(mp):
                    self.refuse(f"the mountpoint {mp} for {d} does not exist inside the read-only input {o.abs}")
                if os.path.isdir(a) and not os.path.isdir(mp):
                    self.refuse(f"the mountpoint {mp} for the directory {a} is not a directory")
                if not os.path.isdir(a) and os.path.isdir(mp):
                    self.refuse(f"the mountpoint {mp} for the file {a} is a directory")

        # The resource floor.
        unit = self.memory[-1].lower()
        mem_kb = int(self.memory[:-1]) * {"k": 1, "m": 1024, "g": 1024 * 1024}[unit]
        avail_kb = None
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            pass
        if avail_kb is None:
            self.fail("could not read MemAvailable from /proc/meminfo")
        cores = len(os.sched_getaffinity(0))
        whole, _, frac = self.cpus.partition(".")
        cpus_ceil = int(whole) + (1 if frac.strip("0") else 0)
        self.early_stop()
        if avail_kb - mem_kb < FLOOR_KB:
            self.refuse(f"MemAvailable {avail_kb} kB leaves less than 10 GiB once {self.memory} is reserved")
        if cores - cpus_ceil < FREE_CORES:
            self.refuse(f"{cores} cores leave fewer than {FREE_CORES} free once {self.cpus} are used")

        argv = ["docker", "run", "--pull=never", "--network=none", "--name", self.name,
                "--user", f"{os.geteuid()}:{os.getegid()}", f"--cpus={self.cpus}",
                f"--memory={self.memory}", f"--memory-swap={self.memory}", "-e", "HOME=/tmp"]
        for e in self.envs:
            argv += ["-e", e]
        for m in self.mounts:
            # Docker requires explicit private propagation for forced recursive read-only binds.
            argv += ["--mount", f"type=bind,src={m.abs},dst={m.dst},readonly,"
                     "bind-propagation=rprivate,bind-recursive=readonly"]
        self.docker_argv = argv + [self.img] + self.cmd
        with open(os.path.join(self.rec, "argv.txt"), "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(" ".join(shlex.quote(a) for a in self.docker_argv) + "\n")

        # --- the run ---
        self.early_stop()
        with open(os.path.join(self.rec, "stdout.txt"), "wb") as out, \
                open(os.path.join(self.rec, "stderr.txt"), "wb") as err:
            try:
                client = self.client = subprocess.Popen(self.docker_argv, stdin=subprocess.DEVNULL, stdout=out,
                                                        stderr=err, start_new_session=True)
            except OSError as exc:
                self.fail(f"the docker client did not start: {exc}")
        deadline = time.monotonic() + self.timeout
        done = False
        stopped_by = None
        while True:
            # A normal client completion wins over a signal. Shared signal death is an interruption
            # only if already noted here: a later signal during settlement cannot change the outcome.
            try:
                returncode = client.wait(POLL_S)
                signal_at_exit = STOP
                if returncode < 0 and SIGNAMES.get(-returncode) == signal_at_exit:
                    stopped_by = signal_at_exit
                done = True
                break
            except subprocess.TimeoutExpired:
                pass
            if STOP is not None:
                break
            if time.monotonic() >= deadline:
                self.timed_out = True
                break
        if not done:
            stopped_by = STOP
            self.stop(client)
        self.settle()

        if stopped_by is not None and not self.timed_out:
            self.interrupted = stopped_by
            raise Outcome(EX_INTERRUPTED, f"interrupted by SIG{stopped_by}; {self.fate()}")
        if self.timed_out:
            raise Outcome(EX_TIMEOUT, f"timed out after {self.timeout}s; {self.fate()}")
        if self.inspect_err:
            self.fail(f"docker container inspect failed: {self.inspect_err}{self.leftover()}")
        if self.client_returncode is None or self.client_killed:
            self.fail(f"docker client ended without confirming the command "
                      f"(client exit {self.cli_rc}); {self.fate()}")
        if self.created is not True:
            self.fail(f"docker did not create the container (client exit {self.cli_rc}; see stderr.txt)")
        if not self.started_ct:
            self.fail(f"docker created the container but did not start it (client exit {self.cli_rc}; "
                      f"see stderr.txt){self.leftover()}")
        if self.state_status != "exited":
            self.fail(f"the container ended in state {self.state_status}{self.leftover()}")
        if self.state_rc != str(self.cli_rc):
            self.fail(f"the docker client exited {self.cli_rc} but the container exited {self.state_rc}"
                      f"{self.leftover()}")
        self.passed = True
        code = int(self.state_rc)
        raise Outcome(code, None if self.removed else f"the command exited {code}, but {self.fate()}")

    def stop(self, client):
        """Stop the container through the daemon: the command runs as PID 1, which ignores SIGTERM.

        docker kill fails before the container exists or while it is created but not started, and the
        client then starts it anyway: keep killing until the client ends. After STOP_S the client itself
        is killed, and a container the daemon has not reported by then is recorded as unknown.
        """
        end = time.monotonic() + STOP_S
        while client.poll() is None:
            if time.monotonic() >= end:
                kill_session(client)
                self.client_killed = True
                break
            self.helper(["docker", "kill", self.name], timeout=end - time.monotonic())
            try:
                client.wait(0.5)
            except subprocess.TimeoutExpired:
                pass
        client.wait()

    def settle(self):
        """Observe the ended client, inspect daemon state, then attempt removal by name.

        All completion paths meet here, including exception recovery after poll() discovers a dead
        child. Keep the raw code: a signal death and a normal exit 128+N are different evidence.
        """
        if self.client is not None:
            self.client_returncode = self.client.poll()
            if self.client_returncode is not None:
                self.cli_rc = exit_status(self.client_returncode)
                self.client_killed |= self.client_returncode < 0
        rc, out, err = self.helper(["docker", "container", "inspect", "--format", CT_FORMAT, self.name])
        err = err[:400]
        if rc == 0:
            self.created = True
            status, started, state_rc, oom = (out.rstrip("\n").split("|", 3) + ["", "", ""])[:4]
            self.state_status, self.state_rc, self.oom = status, state_rc, oom
            self.started_ct = not (started == "" or started.startswith("0001-01-01T"))
        elif ("No such container" in err and self.client_returncode is not None
              and not self.client_killed):
            self.created = False
        elif "No such container" in err:
            self.created = None   # the daemon may still create it for the request the killed client sent
        else:
            self.created = None
            self.inspect_err = err or f"exit {rc}"
        if self.created is not False:
            rc, _, _ = self.helper(["docker", "rm", "-f", self.name])
            self.removed = rc == 0
        self.executed = self.started_ct
        self.settled = True

    # -- the record --
    def provenance(self):
        """The commit that holds this script, named only when the script is tracked, and whether the
        startup file snapshot differs from that commit's. Loading assumes no concurrent replacement.
        The caller's GIT_* cannot redirect it, and nothing refreshes the index: the checkout may itself
        be one of the read-only inputs."""
        d, base = os.path.split(SELF)
        env = {k: v for k, v in os.environ.items() if k not in GIT_REDIRECTS}

        def git(*args):
            rc, out, _ = self.helper(["git", "--no-optional-locks", "--literal-pathspecs", "-C", d, *args],
                                     env=env)
            return out.rstrip("\n") if rc == 0 else None

        blob = hashlib.sha1(b"blob %d\0" % len(SELF_BYTES) + SELF_BYTES).hexdigest()
        commit = modified = None
        if git("ls-files", "--error-unmatch", "--", base) is not None:
            commit = git("rev-parse", "HEAD")
            if commit:
                modified = git("rev-parse", "--verify", "-q", f"HEAD:./{base}") != blob
        return {"script": SELF, "sha256": hashlib.sha256(SELF_BYTES).hexdigest(),
                "adk_tools_commit": commit or None, "script_modified_since_commit": modified}

    def write_record(self):
        unknown = self.created is None
        record = {
            "format": RECORD_FORMAT,
            "launcher_exit": self.final,
            "executed": None if unknown else self.executed,
            "not_executed_reason": self.reason,
            "launcher_failure": self.failure,
            "timed_out": self.timed_out,
            "interrupted": self.interrupted,
            "command_exit_code": int(self.state_rc) if self.passed else None,
            "image": self.img,
            "image_id_inspected": self.inspected or None,
            "command": self.cmd,
            "mounts": [{"source": m.abs, "target": m.dst, "readonly": True} for m in self.mounts],
            "env": self.envs,
            "limits": {"cpus": self.cpus, "memory": self.memory, "timeout_s": self.timeout},
            "container": {
                "name": self.name,
                "created": self.created,
                "started": None if unknown else self.started_ct,
                "status": self.state_status or None,
                "exit_code": int(self.state_rc) if re.fullmatch(r"-?[0-9]+", self.state_rc or "") else None,
                "oom_killed": {"true": True, "false": False}.get(self.oom),
                "removed": self.removed,
                "inspect_error": self.inspect_err,
            },
            "docker_client_exit": self.cli_rc,
            "docker_argv": self.docker_argv,
            "stdout": "stdout.txt",
            "stderr": "stderr.txt",
            "started_utc": self.started_utc,
            "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "launcher": self.provenance(),
        }
        tmp = os.path.join(self.rec, ".record.json.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(record, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, os.path.join(self.rec, "record.json"))
            return True
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def finish(self, code, message):
        """Record, replay the streams, and return the exit code."""
        self.final = code
        written = self.write_record()
        shutil.rmtree(self.tmpd, ignore_errors=True)
        for name, fd in (("stdout.txt", 1), ("stderr.txt", 2)):
            try:
                with open(os.path.join(self.rec, name), "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        write_all(fd, chunk)
            except OSError:
                pass
        if not written:
            say(f"host-verify: record.json could not be written in {self.rec}; the outcome was exit {code}"
                + (f": {message}" if message else ""))
            return EX_RECORD_IO
        if message:
            say(f"host-verify: {message} (recorded in {self.rec})")
        return code


def main():
    opts, mounts, envs, cmd = parse(sys.argv[1:])
    check_usage(opts, mounts, envs, cmd)
    parent, rec_abs = check_record(opts["--record"], mounts)
    create_record_dir(rec_abs, mounts)
    return Run(opts, mounts, envs, cmd, parent, rec_abs).main()


if __name__ == "__main__":
    try:
        status = main()
    except SystemExit as decided:
        status = decided.code
    # The code is decided. Python's own shutdown puts back the default action of every signal it had
    # a handler for, so a signal from here on would still end the launcher by that signal and hide a
    # code the record already holds; ignoring them keeps the two equal.
    for _sig in SIGNAMES:
        signal.signal(_sig, signal.SIG_IGN)
    sys.exit(status)
