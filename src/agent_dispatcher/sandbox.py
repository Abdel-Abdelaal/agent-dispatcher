"""Run a worktree's own commands in a throwaway, locked-down container.

Models can't run code on the controller's host: it holds credentials. Instead
the worktree is copied, as a tar stream, into a fresh container that has no
network, no host mounts, no Linux capabilities and a non-root user, with memory,
CPU, process and time limits. The command travels inside the stream, never on a
command line, so no shell on the container host ever parses model text.

Dependencies are installed once per lockfile, from the task's unedited checkout,
into an image the run containers start from; that is the only step with network.
The container host is local `docker`, or `sudo -n docker` over ssh when
`DISPATCHER_SANDBOX_HOST` names one.
"""

import hashlib
import io
import shlex
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Pinned by digest: the official uv image with Python 3.12 (Debian bookworm).
UPSTREAM_IMAGE = (
    "ghcr.io/astral-sh/uv@sha256:"
    "e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"
)
# Pyright needs Node.js, found on PATH so it never downloads one; some test
# suites need git.
DOCKERFILE = f"""\
FROM {UPSTREAM_IMAGE}
RUN apt-get update \\
 && apt-get install -y --no-install-recommends nodejs git \\
 && rm -rf /var/lib/apt/lists/*
"""
BASE_IMAGE = (
    "agent-sandbox-base:" + hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:24]
)
COMMAND_FILE = ".agent-sandbox-command"
OUTPUT_LIMIT = 12_000
KEEP_IMAGES = 3
# The repository's own checks, whichever its environment has installed.
CHECKS = (
    "status=0; "
    "if [ -x /opt/venv/bin/ruff ]; then ruff check . || status=1; fi; "
    "if [ -x /opt/venv/bin/pyright ]; then pyright || status=1; fi; "
    "if [ -x /opt/venv/bin/pytest ]; then pytest -q -p no:cacheprovider || status=1; "
    "else python -m unittest discover -s tests 2>&1 || status=1; fi; "
    "exit $status"
)
# argv, stdin bytes and a timeout in, exit code and combined output out.
Exec = Callable[[list[str], bytes, float], tuple[int, str]]
# Never copied in: version control, local environments, caches and secrets.
_SKIPPED = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
)
_LOCKS = ("pyproject.toml", "uv.lock")
# Paths inside the container, whose /tmp is its own private tmpfs.
_INSTALL_ENV = {
    "HOME": "/tmp",  # noqa: S108
    "UV_PROJECT_ENVIRONMENT": "/opt/venv",
    "UV_CACHE_DIR": "/tmp/uv-cache",  # noqa: S108
    "UV_LINK_MODE": "copy",
}
_RUN_ENV = {
    **_INSTALL_ENV,
    "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
    "UV_NO_SYNC": "1",
    "UV_FROZEN": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
_LOCKED_DOWN = (
    "--network",
    "none",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    "256",
    "--memory",
    "1g",
    "--memory-swap",
    "1g",
    "--cpus",
    "1",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,exec,nosuid,size=512m",  # noqa: S108 - the container's own tmpfs
    "--tmpfs",
    "/work:rw,exec,nosuid,size=512m,uid=1000,gid=1000",
    "--user",
    "1000:1000",
    "--workdir",
    "/work",
)


class SandboxError(RuntimeError):
    """The sandbox couldn't be prepared; the task continues without it."""


@dataclass(frozen=True)
class SandboxResult:
    """What a sandboxed command did, bounded for a prompt."""

    exit_code: int
    output: str
    timed_out: bool = False

    def report(self) -> str:
        """Return the result as text a model can act on."""
        head = (
            "The command timed out and was stopped."
            if self.timed_out
            else f"Exit code {self.exit_code}."
        )
        return f"{head}\n{self.output}" if self.output else head


def byte_exec(argv: list[str], stdin: bytes, timeout: float) -> tuple[int, str]:
    """Run argv with binary stdin; a timeout returns 124 like `timeout(1)`."""
    try:
        done = subprocess.run(  # noqa: S603 - argv is built here, never from input
            argv, input=stdin, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    output = done.stdout + done.stderr
    return done.returncode, output.decode("utf-8", errors="replace")


def _skipped(path: PurePosixPath) -> bool:
    return any(
        part in _SKIPPED or part.lower().startswith(".env") for part in path.parts
    )


def archive(worktree: Path, extra: dict[str, str] | None = None) -> bytes:
    """Return the worktree as a tar stream, without links, secrets or caches.

    Symbolic links are left out, so nothing outside the worktree is ever read.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        files = {
            PurePosixPath(path.relative_to(worktree).as_posix()): path.read_bytes()
            for path in sorted(worktree.rglob("*"))
            if not path.is_symlink() and path.is_file()
        }
        files.update({PurePosixPath(k): v.encode() for k, v in (extra or {}).items()})
        for relative, data in files.items():
            if _skipped(relative):
                continue
            info = tarfile.TarInfo(str(relative))
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def environment_key(worktree: Path) -> str | None:
    """Name the dependency image for this lockfile, or None without one."""
    if not all((worktree / name).is_file() for name in _LOCKS):
        return None
    digest = hashlib.sha256(BASE_IMAGE.encode())
    for name in _LOCKS:
        digest.update((worktree / name).read_bytes())
    return "agent-sandbox:" + digest.hexdigest()[:24]


def _env_args(env: dict[str, str]) -> list[str]:
    return [item for key, value in env.items() for item in ("-e", f"{key}={value}")]


@dataclass(frozen=True)
class Sandbox:
    """A container host and how to reach it."""

    host: str | None = None
    ssh_key: Path | None = None
    timeout: int = 300
    execute: Exec = byte_exec

    def _docker(
        self, *args: str, stdin: bytes = b"", timeout: float = 120
    ) -> tuple[int, str]:
        """Run docker locally, or over ssh with every argument quoted for its shell."""
        if self.host is None:
            return self.execute(["docker", *args], stdin, timeout)
        login = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
        if self.ssh_key is not None:
            # Known hosts beside the key: the calling environment may be scrubbed.
            login += [
                "-i",
                str(self.ssh_key),
                "-o",
                f"UserKnownHostsFile={self.ssh_key.parent / 'known_hosts'}",
                "-o",
                "StrictHostKeyChecking=yes",
            ]
        remote = " ".join(shlex.quote(arg) for arg in ("sudo", "-n", "docker", *args))
        return self.execute([*login, self.host, remote], stdin, timeout)

    def _exists(self, image: str) -> bool:
        return self._docker("image", "inspect", "--format", "ok", image)[0] == 0

    def _prune(self, keep: int = KEEP_IMAGES) -> None:
        """Remove all but the newest dependency images, so the host disk can't fill."""
        code, output = self._docker(
            "images", "agent-sandbox", "--format", "{{.Repository}}:{{.Tag}}"
        )
        if code != 0:
            return
        # `docker images` lists newest first.
        for image in output.split()[keep:]:
            self._docker("rmi", image)

    def _base(self) -> None:
        """Build the shared base image from DOCKERFILE once."""
        if self._exists(BASE_IMAGE):
            return
        code, output = self._docker(
            "build",
            "--pull=false",
            "-t",
            BASE_IMAGE,
            "-",
            stdin=DOCKERFILE.encode(),
            timeout=900,
        )
        if code != 0:
            message = f"base image build failed:\n{output[-2000:]}"
            raise SandboxError(message)

    def prepare(self, base: Path) -> str:
        """Return an image holding this lockfile's dependencies, building it once.

        `base` is the task's unedited checkout: dependencies come from the
        lockfile the repository already trusts, never from a model's edits.
        """
        self._base()
        key = environment_key(base)
        if key is None:
            return BASE_IMAGE
        if self._exists(key):
            return key
        name = "agent-sandbox-build-" + key.rsplit(":", 1)[1]
        self._docker("rm", "-f", name)
        install = (
            "mkdir -p /work && tar -x -C /work && cd /work && "
            "uv sync --locked --all-groups"
        )
        build = [
            "run",
            "-i",
            "--name",
            name,
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "FOWNER",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "1g",
            *_env_args(_INSTALL_ENV),
            BASE_IMAGE,
            "sh",
            "-c",
            install,
        ]
        try:
            code, output = self._docker(*build, stdin=archive(base), timeout=900)
            if code != 0:
                message = f"dependency install failed:\n{output[-2000:]}"
                raise SandboxError(message)
            if self._docker("commit", name, key)[0] != 0:
                message = "could not save the dependency image"
                raise SandboxError(message)
        finally:
            self._docker("rm", "-f", name)
        self._prune()
        return key

    def run(self, image: str, worktree: Path, command: str) -> SandboxResult:
        """Run `command` in a fresh locked-down container over a copy of `worktree`."""
        stream = archive(worktree, {COMMAND_FILE: command + "\n"})
        script = (
            f"tar -x -C /work && exec timeout -k 5 {self.timeout} sh {COMMAND_FILE}"
        )
        code, output = self._docker(
            "run",
            "--rm",
            "-i",
            *_LOCKED_DOWN,
            *_env_args(_RUN_ENV),
            image,
            "sh",
            "-c",
            script,
            stdin=stream,
            timeout=self.timeout + 60,
        )
        if len(output) > OUTPUT_LIMIT:
            output = "[earlier output cut]\n" + output[-OUTPUT_LIMIT:]
        return SandboxResult(code, output.strip(), timed_out=code in {124, 137})
