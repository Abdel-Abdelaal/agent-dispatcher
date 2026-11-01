# Sandbox

Models can run the project's own tests, linter and type checker, and the
controller checks every change the same way before review. Nothing a model
writes ever runs on the controller's host, because that host holds credentials.

## How it works

1. **Prepare, once per lockfile.** From the task's unedited clone, the controller
    builds an image with the repository's locked dependencies
    (`uv sync --locked --all-groups --no-install-project`, with `UV_NO_BUILD=1`).
    This is the only step with network. It installs locked wheels only, as an
    unprivileged user: the project isn't installed and nothing is built from
    source, so no repository code runs with network. A path file lets Python find
    the project from `/work` and `/work/src` instead. The image is named by a hash
    of `pyproject.toml` and `uv.lock`, so later tasks on the same lockfile reuse
    it. Nothing is built when the host has less than 3 GB free.
2. **Baseline.** The controller runs the checks once before any edit. If they
    already fail, the worker isn't asked to fix them, and the reviewer is told.
3. **The `run` tool.** Every OpenCode call (planner, worker and reviewer) gets
    one extra tool, `sandbox_run`. The model sends a shell command; the worktree
    is copied into a fresh container, the command runs there, and the end of its
    output comes back. Files the command writes are thrown away; only edits made
    with the file tools count.
4. **Checks before review.** After implementing, the controller runs the
    repository's checks (`ruff check .`, `pyright` and `pytest`, whichever the
    environment has). If they fail, the worker gets the output and one round to
    fix the cause. The reviewer sees the final result, and is told to reject
    changes that weaken tests or tool settings.

If the sandbox can't be prepared or reached at any point, the run carries on
without the tool or the checks and records `sandbox_unavailable`. A sandbox
fault is told apart from a failing command by a marker the container prints
before the command starts, so it never counts as a failed check.

## The container

Every run gets a new container that is removed afterwards, with:

- no network (`--network none`), so neither the internet nor the host's own
  services are reachable;
- no host mounts: the code arrives as a tar stream on stdin;
- all Linux capabilities dropped, `no-new-privileges`, and uid 65534, which has
  no account on the host;
- a read-only root filesystem, with only `/work` and `/tmp` writable (512 MB
  tmpfs each);
- 1 GB of memory, 1 CPU, 256 processes and a 300-second time limit;
- no container log (`--log-driver none`) and output kept in the tmpfs, with
  only its last 12,000 characters returned, so no command can fill a disk;
- `--pull=never`, so a missing image is an error, never a download.

The copy leaves out `.git`, virtual environments, `node_modules`, caches,
symbolic links and any `.env*` file. Each run starts with a fresh one-commit
git repository of the copied files, for tests that call git. The command travels
inside the copy as a file, never on a command line, so no shell on the container
host parses model text.

On the controller, OpenCode starts the tool's server from the worktree, so it
runs with `python -P` and `PYTHONSAFEPATH=1`: a `json.py` planted in the
repository can't replace a real module there. Sandbox errors reach the model
only as "unavailable", never with host names or key paths.

The base image is the official `uv` image with Python 3.12, pinned by digest,
plus Debian's `nodejs` (for pyright) and `git`. Old dependency images are pruned
beyond the newest three, except any built in the last three hours, which a
concurrent run may still use.

## Configure

```dotenv
DISPATCHER_SANDBOX=ssh
DISPATCHER_SANDBOX_HOST=user@container-host
DISPATCHER_SANDBOX_SSH_KEY=C:/path/to/ssh/key
```

`DISPATCHER_SANDBOX` is `off` (the default), `local` for a local `docker`, or
`ssh` for `sudo -n docker` on `DISPATCHER_SANDBOX_HOST`. Over ssh, host keys
come from the `known_hosts` file beside the key, with strict checking. A key
used only for this, with a forced command in `authorized_keys`, limits the
damage if the controller's host is ever compromised.

## Limits

- Python projects with `pyproject.toml` and `uv.lock` get dependencies and
  checks. Other repositories get the base image for the `run` tool, and no
  controller checks. Android (ello) isn't covered: its SDK doesn't fit the host.
- Only OpenCode calls get the tool. Claude Code and Codex calls don't.
- Tests that need the network, a database server or a browser fail in the
  sandbox, as they would offline. Locked packages that only ship as source fail
  to install, and the run goes on without the sandbox.
- A model can make checks pass by weakening tests or tool settings in its diff.
  The gate and the reviewer catch that, not the sandbox.
- Container isolation is strong but not a VM: escaping it needs a kernel or
  container runtime bug. The host also runs other services, so it isn't a
  dedicated machine.
