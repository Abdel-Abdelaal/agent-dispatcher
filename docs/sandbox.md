# Sandbox

Models can run the project's own tests, linter and type checker, and the
controller checks every change the same way before review. Nothing a model
writes ever runs on the controller's host, because that host holds credentials.

## How it works

1. **Prepare, once per lockfile.** From the task's unedited clone, the controller
   builds an image with the repository's locked dependencies
   (`uv sync --locked --all-groups`). This is the only step with network, and it
   uses the lockfile the repository already trusts, never a model's edits. The
   image is named by a hash of `pyproject.toml` and `uv.lock`, so later tasks on
   the same lockfile reuse it. Only the newest three are kept.
2. **Baseline.** The controller runs the checks once before any edit. If they
   already fail, the worker isn't asked to fix them, and the reviewer is told.
3. **The `run` tool.** OpenCode calls get one extra tool, `sandbox_run`. The
   model sends a shell command. The worktree is copied into a fresh container,
   the command runs there, and the end of its output comes back. Files the
   command writes are thrown away; only edits made with the file tools count.
4. **Checks before review.** After implementing, the controller runs the
   repository's checks (`ruff check .`, `pyright` and `pytest`, whichever the
   environment has). If they fail, the worker gets the output and one round to
   fix the cause. The reviewer sees the final result.

## The container

Every run gets a new container that is removed afterwards, with:

- no network (`--network none`), so neither the internet nor the host's own
  services are reachable;
- no host mounts: the code arrives as a tar stream on stdin;
- all Linux capabilities dropped, `no-new-privileges`, and user 1000;
- a read-only root filesystem, with only `/work` and `/tmp` writable (tmpfs);
- 1 GB of memory, 1 CPU, 256 processes and a 300-second time limit.

The copy leaves out `.git`, virtual environments, `node_modules`, caches,
symbolic links and any `.env*` file. The command travels inside the copy as a
file, never on a command line, so no shell on the container host parses model
text.

The base image is the official `uv` image with Python 3.12, pinned by digest,
plus Debian's `nodejs` (for pyright) and `git`.

## Configure

```dotenv
DISPATCHER_SANDBOX=ssh
DISPATCHER_SANDBOX_HOST=abdel@100.82.195.92
DISPATCHER_SANDBOX_SSH_KEY=C:/Users/Abdel/.ssh/id_ed25519
```

`DISPATCHER_SANDBOX` is `off` (the default), `local` for a local `docker`, or
`ssh` for `sudo -n docker` on `DISPATCHER_SANDBOX_HOST`. Over ssh, host keys
come from the `known_hosts` file beside the key, with strict checking.

If the sandbox can't be prepared, the run carries on as before, without the tool
or the checks, and records `sandbox_unavailable`.

## Limits

- Python projects with `pyproject.toml` and `uv.lock` get dependencies and
  checks. Other repositories get the base image for the `run` tool, and no
  controller checks. Android (ello) isn't covered: its SDK doesn't fit the host.
- Only OpenCode calls get the tool. Claude Code and Codex calls don't.
- Tests that need the network, a database server or a browser fail in the
  sandbox, as they would offline.
- Container isolation is strong but not a VM. Escaping it would take a kernel
  exploit, on a host that also runs other services.
