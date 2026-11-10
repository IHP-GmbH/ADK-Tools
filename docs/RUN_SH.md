# run.sh: what a launcher can rely on

`run.sh` starts one container of the adk-tools image per call. This page covers what a program
that calls it, such as an editor task or a script, can rely on:
- what goes into the container;
- what `run.sh` prints and where;
- its exit codes;
- what happens when the image is missing.

Anything not listed here, such as the wording of the informational notes, can change without notice.

## Input

`ADK_TOOLS_IMAGE` is required. It names the image by its ID, `sha256:` followed by 64 lowercase hex
digits, which is what `docker image inspect --format '{{.Id}}'` prints. A tag or a short ID is
refused.

The other variables (`ADK_WORK`, `ADK_DISPLAY`, `ADK_NETWORK`, `ADK_MEM_LIMIT`, `ADK_CPUS`,
`ADK_OPENROAD`) are described in the header of `run.sh`. Each call reads them afresh, so a launcher
can set any of them for a single call. For example, a call that needs a GUI sets `ADK_DISPLAY=:N`
and other calls set `ADK_DISPLAY=none`. `CHIPLET_FLOW_HOST` and `CHIPLET_FLOW_DIE_PROJECT` are not
read by `run.sh`: each is passed into the container as it is when set, and no other caller variable
is.

The arguments are the command to run in the container. Options of `run.sh`'s own may lead them,
and the first word that is not one starts the command:
- `--examples DIR` mounts the examples checkout `DIR` read-only at `/examples`, and holds the image
  to its `DIR/release-lock.json`;
- `--lock FILE` holds the image to the release lock `FILE` instead: with `--examples`, in place
  of the checkout's own lock; alone, the image is checked and nothing is mounted;
- `--batch` makes the call non-interactive: the container gets no stdin, no TTY and no display,
  whatever the caller has (unless `--tty` or `--display` below), and `ADK_DISPLAY` is not read;
- `--display`, with `--batch` only, gives that call the display after all, by the rules for
  `ADK_DISPLAY` and `DISPLAY` below, still with no stdin and no TTY unless `--tty` gives them;
- `--tty`, with `--batch` only, gives that call the caller's stdin and a TTY after all when
  `run.sh`'s stdin and stdout are both terminals, so a program can ask the person a question; with
  either one not a terminal (a pipe, a file, CI) the call stays non-interactive. A run with a TTY,
  this one or an interactive one, ends its container when the terminal hangs up (see Exit codes);
- `--dry-run` makes every check, prints the `docker run` command on stdout as one shell-quoted
  line, and runs nothing. It creates no `ADK_WORK` directory;
- `--cwd REL` starts the command in `/work/REL` instead of `/work`. `REL` is relative, has no
  `..`, and names a directory of `ADK_WORK` with no symlink on the way; anything else is refused.

`bin/flow` is the launcher for one-shot calls, such as an editor task. It runs
`bin/flow [--dry-run] [--lock FILE] [--image ID] -- <program> <args...>`, where `<program>` is `packaging-flow` or
`chiplet-system-description` and nothing else. The call goes through `run.sh --batch` (`packaging-flow
verify-open`, which opens a window, through `run.sh --batch --display`; `packaging-flow
verify-sign` and `verify-accept`, which ask the person at a terminal, through
`run.sh --batch --tty`), with `ADK_WORK` at
`/work` exactly as `run.sh` mounts it (default: `~/adk-work`), never the current directory, so a
path under `/work` names the same file whichever launcher wrote it. The current directory, symlinks
resolved, has to be inside `ADK_WORK`, and the command starts at the same place below `/work`
(`run.sh --cwd`): from `ADK_WORK/examples/a` it runs in `/work/examples/a`. The release lock is
`--lock FILE`, else `ADK_FLOW_LOCK`, else `release-lock.json` in the current directory (an
example's folder, where the examples repository puts it). The image is `--image ID`, else
`ADK_TOOLS_IMAGE`, else the lock's reference image; `bin/adk-vscode` takes `--image ID` too and
writes the image the tasks run into `adkTools.image`. The caps are 2 CPUs and 5g unless `ADK_CPUS` or `ADK_MEM_LIMIT` is set and not empty,
lower than `run.sh`'s 4 and 8g, so a VS Code task builds at the 2 threads of an example's shell
run; to raise them from VS Code, export them in the shell that starts it. The output, the exit
codes and the refusals below are `run.sh`'s. `bin/flow`
adds one-line refusals of its own, also exit 1 and starting with `error: `, for a missing program
or lock and for a current directory outside `ADK_WORK`, which names both, and for `ADK_WORK`,
`ADK_TOOLS_IMAGE` or `ADK_FLOW_LOCK` set but empty, which is what a VS Code task passes for a
setting nobody wrote: that line names the setting (`adkTools.work`, `adkTools.image`,
`adkTools.lock`) and `bin/adk-vscode <folder>`, which writes all of them, with `adkTools.root`,
into the folder's `.vscode/settings.json` and opens it. A folder with no
`.vscode/tasks.json` gets the flow's tasks first: `bin/adk-vscode` runs `packaging-flow tasks
--vscode` through `bin/flow`, with the values it writes, and puts the file in place only once
whole. A tasks file already there is left as it is, and no container starts for it; a
link by that name to nothing is refused.

A release lock is canonical JSON (sorted keys, two-space indent, a final newline) of one of two
schemas. `adk-examples.release-lock/1` has exactly the keys `adk_tools`, `image` (the config
digest of the reference image the examples were checked on), `pin_set` and `schema`.
`adk-examples.release-lock/2` adds `image_manifest`: the ID Docker's containerd image store gives
that image's archive (the manifest's digest), since that store cannot find an image by the config
digest `image` names. `run.sh` refuses a lock of another form or schema. The image has to carry
the lock's `pin_set` in its `org.ihp.adk-tools.pin-set` label, which `build.sh` writes; an image
without it cannot be held to a lock. When `ADK_TOOLS_IMAGE` is neither the lock's `image` nor its
`image_manifest` (an image you built, say), it runs after one note naming both IDs. The lock's
`adk_tools`, the adk-tools commit the examples go with, is not read, since a build of the same pin
set may come from another adk-tools commit.

`ADK_STAGING_OVERLAY="<name>=<path>[ <name>=<path>...]"` runs a host checkout's code in place of the
image's staged tree `<name>` (one of `assembly-lvs`, `chiplet-cosim`, `chiplet-flow`,
`chiplet-spec`, `interposer-pnr`, `packaging-flow`), mounted read-only at
`/opt/adk-tools/staging/<name>`, with the image's tools. It is for trying a staged tree's change
without an image build. `run.sh` refuses it before any container starts when it names no overlay
(whitespace only), when a name is not a staged tree or comes twice, a path has whitespace, is not the top of a git checkout, has a `.git`
that is not a directory (a worktree; use a clone) or borrows objects through alternates, or has no
commit, and when the checkout's entry points or top-level packages differ from the image's
metadata for that tree (such a change needs a reinstall, which is an image build).
`ci/check_overlay.py` makes that last check in a throwaway container of the image, with no
network, and needs `python3` on the host. Under a release lock an overlay is refused unless
`ADK_CI=1` is set.

## In the container

Every container `run.sh` starts has `ADK_TOOLS_IMAGE` set to the ID it was started from, in the
same form as the input. A command that records which image it ran in reads it there. Nothing in the
image changes behaviour because of it.

The value is the daemon's own ID for the image. Before it does anything else, `run.sh` asks the
daemon for the ID of `ADK_TOOLS_IMAGE` and refuses unless it gets back the same string.

## Output

`run.sh` writes nothing to stdout except the command under `--dry-run`. Everything else on stdout
comes from the command in the container.

`run.sh` writes three kinds of line to stderr.

**A refusal** is one line starting with `error: `. It may be followed by lines indented with seven
spaces that say how to fix it. After a refusal `run.sh` exits 1 and starts no container. The only
thing it may leave behind is the `ADK_WORK` directory: it creates that directory before it reads
the X cookie, and a cookie it cannot write is the one refusal that comes after that point.

**The notes** are lines starting with `run.sh: `. They are printed together, once every check has
passed, right before the container starts. The first three are fixed:

```
run.sh: /work is <absolute path of ADK_WORK>
run.sh: image <ID>, as ADK_TOOLS_IMAGE in the container
run.sh: display :<N> ...        or        run.sh: no display ...
```

The third line always appears, and it starts with exactly one of two prefixes:
- `run.sh: display :<N>`, where `<N>` is the X display number passed into the container;
- `run.sh: no display`, when the container gets no display. This covers `ADK_DISPLAY=none`, no
  display found, a display socket that is not an X socket, and another user's socket.

The words after either prefix say where the display came from or why there is none. They are for
the person reading them, not part of this interface. A container with no display still starts;
only GUI tools inside it fail.

The notes that follow describe the memory cap, OpenROAD, the Nix store paths, the network and the
release lock. Their number, order and wording are not part of this interface.

With `ADK_STAGING_OVERLAY` set, the notes end with one line per overlay, in a fixed form:

```
run.sh: OVERLAY staging/<name> <- <path> (tree <sha40>); this run is NOT the image's alone
```

`<sha40>` is the git tree of everything git would commit in the checkout, tracked or not, written
with a throwaway index, so a dirty checkout is named exactly and its own index is not touched. The
container gets the same trees as `ADK_STAGING_OVERLAY_TREES="<name>=<sha40>[ ...]"`, sorted by
name. Without the variable there is no such line, no mount and no variable.

**After the container**, one more note appears when its exit code is 137:
`run.sh: exit 137: the container was killed (SIGKILL); ...`.

The container's own stderr follows the notes, interleaved as the command writes it; with a TTY
(an interactive run, or `--batch --tty` at a terminal) it reaches the terminal with stdout.

## Exit codes

How to read the exit code depends on whether the `run.sh: /work is` line was printed.

- **Not printed:** `run.sh` refused, and the code is 1.
- **Printed:** the code is `docker run`'s, which means:

  | Code | Meaning |
  | --- | --- |
  | any | the command's own exit code |
  | 125 | docker could not create or start the container |
  | 126 | the command was found but could not be run |
  | 127 | the command was not found |
  | 137 | the container was killed; the note after it says so |
  | 129, 130, 143 | a run with a TTY got a hangup, an interrupt or a TERM; `run.sh` killed the docker client and the container |

A launcher that has to tell a refusal from a command that exited 1 checks for that line.

## When the image is not there

Each of these is a refusal: exit 1, no notes, no container, nothing created.

| Case | First line |
| --- | --- |
| `ADK_TOOLS_IMAGE` unset or empty | `error: ADK_TOOLS_IMAGE is unset or empty; run.sh has no default image` |
| not an ID (a tag, a short ID, upper case) | `error: ADK_TOOLS_IMAGE must be an image ID, sha256:<64 hex> (got '<value>')` |
| an ID the daemon does not hold | `error: no image <ID> on this docker daemon` |
| the daemon cannot be reached or refuses the query | `error: docker image inspect <ID> failed: <docker's first error line>` |

The first two cases are followed by the lines on how to get an ID. The third is followed by
`       run.sh never pulls; build the image (README.md, Build) and pass its ID, or load its archive (docker load -i <file>).`

On a daemon that uses the containerd image store (its `docker info` DriverStatus names
`io.containerd.snapshotter`), four more lines follow:

```
       This daemon uses the containerd image store, which finds an image loaded from an archive by another ID.
       A release lock of schema /2 names that ID too (image_manifest): pass it instead, and no switch is needed.
       If this ID came from a /1 release lock or an image archive, turn that store off and load the archive again;
       docs/RUN_SH.md ("When the image is not there") says how, and that it hides the store's images until it is back on.
```

That store names an image loaded from an archive by its manifest's digest, and finds none by the
config digest a release lock and an archive name, so the image is there and still not found. An
image built on that store is found by its own ID, so for a mistyped or stale ID the same refusal
needs no switch. The other way round, an image built or loaded on the classic store is hidden
while the containerd store is on, and is found again once it is off.

To switch, when the ID did come from a /1 lock or an archive: `bin/adk-vscode`, which always
starts from a lock, prints these lines after its first two when the missing image is the lock's
`image` and the lock is /1 (for a /2 lock it names the lock's `image_manifest` instead), and they
hold for `run.sh` too:

```
       On Docker Desktop, turn off "Use containerd for pulling and storing images" (Settings, General).
       On Docker Engine, set "features": {"containerd-snapshotter": false} in /etc/docker/daemon.json and restart the daemon.
       A rootless daemon reads ~/.config/docker/daemon.json instead.
       Then load the image's archive again (docker load -i <file>).
       Images pulled, built or loaded while the containerd store is on are hidden while it is off, and come back when it is on again.
```

`run.sh` never pulls an image.

## When the image carries its tools

An image with the `org.ihp.adk-tools.nix-closure-inside` label holds OpenROAD, LibreLane, its
interpreter, the sg13g2 PDK and the LibreLane launcher in its own `/nix/store`, and its ENV names
them: `OPENROAD`, `ADK_NIX_LIBRELANE`, `ADK_NIX_LIBRELANE_PYTHON`, `ADK_NIX_SG13G2_PDK`,
`CHIPLET_FLOW_LIBRELANE_LAUNCHER` and `CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256`. The label's value
is the sha256 of the `nix/host-closure.lock` it was built from. For such an image `run.sh` needs
no Nix on the host: it mounts nothing under `/nix`, passes none of those six variables, and
ignores `ADK_OPENROAD` with a note. One note names the label, and one per variable gives the
image's value.

These are refusals, with exit 1 and nothing created: a label whose value is not 64 lower-case
hex digits (an empty one included), an image whose ENV lacks one of the six variables, and a
`CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256` that is not the image's
`org.ihp.adk-tools.librelane-launcher-sha256` label.

An image without the label is launched as the next section says.

## When the host lacks Nix

An image without the `org.ihp.adk-tools.nix-closure-inside` label needs Nix on the host, for
the OpenROAD the image names. A refusal for something the
host's Nix store lacks starts with `error: needs Nix on the host: `: no Nix store at all, or no
OpenROAD in it. A path of the image's closure list that the host lacks, or a LibreLane,
interpreter or PDK path, is a note with the same words after `run.sh: `, and the container
starts. Each says to run `nix build .#openroad .#librelane .#librelane-python .#sg13g2-pdk` (or
the one package it lacks) in an adk-tools checkout. A launcher that tells this case apart matches
the prefix, not the rest of the line.

With the LibreLane in, the image's LibreLane launcher goes in too, as
`CHIPLET_FLOW_LIBRELANE_LAUNCHER` with its sha256 in `CHIPLET_FLOW_LIBRELANE_LAUNCHER_SHA256`,
when the host's `bin/librelane` in that store path has the sha256 the image recorded when it was
built. When it does not, or the image has no launcher, a note says so, the container starts, and
a die build in it refuses.

These are refusals too, with exit 1 and nothing created: an image whose
`org.ihp.adk-tools.nix-host-closure` label is not the one its
`org.ihp.adk-tools.nix-host-closure-sha256` label names, an image with only one of the two, a list
that is not `<store>/<hash>-<name>` paths joined by single spaces, and a list without the OpenROAD
the image's own label names.
