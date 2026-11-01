"""Run a worktree's own commands in a throwaway, locked-down container.

Models can't run code on the controller's host: it holds credentials. Instead
the worktree is copied, as a tar stream, into a fresh container that has no
network, no host mounts, no Linux capabilities and a user with no account on the
host, with memory, CPU, process, disk and time limits and no container log. The
command travels inside the stream, never on a command line, so no shell on the
container host ever parses model text.

Dependencies are installed once per lockfile, from the task's unedited checkout,
into an image the run containers start from. That is the only step with network,
and it installs locked wheels only: the project itself isn't installed and
nothing is built from source, so no repository code runs with network. The
container host is local `docker`, or `sudo -n docker` over ssh when
`DISPATCHER_SANDBOX_HOST` names one.
"""

import hashlib
import io
import os
import re
import shlex
import subprocess
import tarfile
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

# Pinned by digest: the official uv image with Python 3.12 (Debian bookworm).
UPSTREAM_IMAGE = (
    "ghcr.io/astral-sh/uv@sha256:"
    "e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"
)
# Every container runs as this uid, which has no account on the host: if one
# were ever escaped, it lands as nobody, not as a host login with sudo.
UID = "65534"
# Pyright needs Node.js, found on PATH so it never downloads one; some test
# suites need git. The environment and work directories belong to UID.
DOCKERFILE = f"""\
FROM {UPSTREAM_IMAGE}
RUN apt-get update \\
    && apt-get install -y --no-install-recommends nodejs git \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /opt/venv /work \\
    && chown {UID}:{UID} /opt/venv /work
"""
BASE_IMAGE = (
    "agent-sandbox-base:" + hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:24]
)
COMMAND_FILE = ".agent-sandbox-command"
OUTPUT_LIMIT = 12_000
# The most the controller reads back from one container call.
CAPTURE_LIMIT = 2 * 1024**2
# The largest copy sent in; /work is a 512 MB tmpfs.
ARCHIVE_LIMIT = 256 * 1024**2
PYPI = "https://pypi.org/simple"
PYPI_FILES = "https://files.pythonhosted.org/"
_LOCAL_SOURCES = ({"editable": "."}, {"virtual": "."})
# Programs run from here, never from a worktree.
_TRUSTED_DIR = Path(__file__).resolve().parent
KEEP_IMAGES = 3
# An image this new may still be in use by a concurrent run; never prune it.
PRUNE_AFTER = timedelta(hours=3)
# Below this much free disk on the container host, nothing new is built.
MIN_FREE_BYTES = 3 * 1024**3
# Printed by the container before the command runs. Without it, the failure was
# the sandbox's (ssh, docker, a missing image), never the command's.
STARTED = "agent-sandbox-started"
SNAPSHOT = (
    "git init -q && git add -A && "
    "git -c user.name=sandbox -c user.email=sandbox@localhost "
    "commit -q --no-verify -m snapshot"
)
# The repository's own checks, whichever its environment has installed.
CHECKS = (
    "status=0; "
    "if [ -x /opt/venv/bin/ruff ]; then ruff check . || status=1; fi; "
    "if [ -x /opt/venv/bin/pyright ]; then pyright || status=1; fi; "
    "if [ -x /opt/venv/bin/pytest ]; then pytest -q -p no:cacheprovider || status=1; "
    "else python -m unittest discover -s tests 2>&1 || status=1; fi; "
    "exit $status"
)
# The project is found from /work, where each run unpacks it, not installed.
_PATH_FILE = (
    '/opt/venv/bin/python -c "import site; '
    "open(site.getsitepackages()[0] + '/agent-sandbox.pth', 'w')"
    ".write('/work/src\\n/work\\n')\""
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
    # Locked wheels only: building an sdist would run its code with network.
    "UV_NO_BUILD": "1",
}
_RUN_ENV = {
    **_INSTALL_ENV,
    "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
    "UV_NO_SYNC": "1",
    "UV_FROZEN": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
# Limits for every container, the dependency build included.
_LIMITED = (
    "--pull=never",
    "--log-driver",
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
    "--user",
    f"{UID}:{UID}",
)
_LOCKED_DOWN = (
    *_LIMITED,
    "--network",
    "none",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,exec,nosuid,size=512m",  # noqa: S108 - the container's own tmpfs
    "--tmpfs",
    f"/work:rw,exec,nosuid,size=512m,uid={UID},gid={UID}",
    "--workdir",
    "/work",
)
_FREE = re.compile(r"^\S+\s+\d+\s+\d+\s+(\d+)\s", re.MULTILINE)


class SandboxError(RuntimeError):
    """The sandbox couldn't be prepared or reached; the task goes on without it."""


@dataclass(frozen=True)
class SandboxResult:
    """What a sandboxed command did, bounded for a prompt."""

    exit_code: int
    output: str

    @property
    def timed_out(self) -> bool:
        """`timeout(1)` stopped it (a command can also exit 124 itself)."""
        return self.exit_code == 124  # noqa: PLR2004 - timeout(1)'s exit code

    def report(self) -> str:
        """Return the result as text a model can act on."""
        if self.timed_out:
            head = "The command timed out and was stopped."
        elif self.exit_code == 137:  # noqa: PLR2004 - killed by SIGKILL
            head = "The command was killed: it hit the memory or process limit."
        else:
            head = f"Exit code {self.exit_code}."
        return f"{head}\n{self.output}" if self.output else head


def program(name: str) -> str:
    """Return the full path of `name` from PATH alone.

    Windows also looks in the current directory first, and this may run from a
    worktree: an `ssh.exe` committed there must never be the one that runs.
    """
    if Path(name).is_absolute():
        return name
    extensions = os.environ.get("PATHEXT", ".EXE").split(os.pathsep)
    suffixes = [""] if os.name != "nt" else [ext.lower() for ext in extensions]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder or folder == os.curdir or not Path(folder).is_absolute():
            continue
        for suffix in suffixes:
            candidate = Path(folder) / (name + suffix)
            if candidate.is_file():
                return str(candidate)
    message = f"{name} is not on PATH"
    raise SandboxError(message)


def byte_exec(argv: list[str], stdin: bytes, timeout: float) -> tuple[int, str]:
    """Run argv with binary stdin, from a fixed directory, reading a bounded reply.

    A timeout returns 124 like `timeout(1)`. The container returns at most
    OUTPUT_LIMIT characters, but a command could write past that to the
    container's own stdout, so more than CAPTURE_LIMIT bytes stops the call.
    """
    process = subprocess.Popen(  # noqa: S603 - argv is built here, never from input
        [program(argv[0]), *argv[1:]],
        cwd=_TRUSTED_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stdin_pipe, stdout_pipe = process.stdin, process.stdout
    if stdin_pipe is None or stdout_pipe is None:  # pragma: no cover - PIPE is set
        process.kill()
        message = "could not open the pipes"
        raise SandboxError(message)
    output = stdout_pipe.fileno()

    def feed() -> None:
        try:
            stdin_pipe.write(stdin)
            stdin_pipe.close()
        except OSError:
            pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        while chunk := os.read(output, 65_536):
            size += len(chunk)
            if size > CAPTURE_LIMIT:
                process.kill()
                return 1, "the sandbox returned more output than allowed"
            chunks.append(chunk)
        process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
    finally:
        timer.cancel()
    if time.monotonic() >= deadline:
        return 124, f"timed out after {timeout:.0f}s"
    return process.returncode, b"".join(chunks).decode("utf-8", errors="replace")


def check_lock(base: Path) -> None:
    """Refuse a lockfile that would download from anywhere but PyPI.

    uv fetches exactly the URLs a lockfile names, during the one step with
    network, so a lockfile pointing elsewhere could reach the host's services.
    """
    lock = tomllib.loads((base / "uv.lock").read_text(encoding="utf-8"))
    for package in lock.get("package", []):
        source = package.get("source", {})
        if source not in _LOCAL_SOURCES and source != {"registry": PYPI}:
            message = f"uv.lock installs {package.get('name')} from outside PyPI"
            raise SandboxError(message)
        files = [package.get("sdist", {}), *package.get("wheels", [])]
        for file in files:
            url = file.get("url")
            if url is not None and not str(url).startswith(PYPI_FILES):
                message = f"uv.lock downloads {package.get('name')} from outside PyPI"
                raise SandboxError(message)


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
            if buffer.tell() > ARCHIVE_LIMIT:
                message = "the worktree is too large for the sandbox"
                raise SandboxError(message)
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


def _created(text: str) -> datetime | None:
    """Parse `docker images` CreatedAt, such as `2026-11-01 01:02:03 +0000 UTC`."""
    try:
        return datetime.strptime(" ".join(text.split()[:3]), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


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

    def _check_disk(self, image: str) -> None:
        """Refuse to build when the container host is low on disk."""
        code, output = self._docker(
            "run",
            "--rm",
            *_LIMITED,
            "--network",
            "none",
            "-v",
            "/var/lib/docker:/d:ro",
            image,
            "df",
            "-P",
            "-B1",
            "/d",
        )
        match = _FREE.search(output) if code == 0 else None
        if match is None or int(match[1]) < MIN_FREE_BYTES:
            message = "the container host is low on disk, or its space is unknown"
            raise SandboxError(message)

    def _prune(self, now: datetime | None = None) -> None:
        """Remove old dependency images beyond the newest few, so the disk can't fill.

        An image newer than PRUNE_AFTER is kept, since a concurrent run may use it.
        """
        code, output = self._docker(
            "images",
            "agent-sandbox",
            "--format",
            "{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}",
        )
        if code != 0:
            return
        cutoff = (now or datetime.now(UTC)) - PRUNE_AFTER
        # `docker images` lists newest first.
        for line in output.splitlines()[KEEP_IMAGES:]:
            image, _, created = line.partition("\t")
            made = _created(created)
            if made is not None and made < cutoff:
                self._docker("rmi", image)

    def _base(self) -> None:
        """Build the shared base image from DOCKERFILE once."""
        if self._exists(BASE_IMAGE):
            return
        code, output = self._docker(
            "build",
            "-t",
            BASE_IMAGE,
            "-",
            stdin=DOCKERFILE.encode(),
            timeout=900,
        )
        if code != 0:
            message = f"base image build failed:\n{output[-2000:]}"
            raise SandboxError(message)
        # A changed DOCKERFILE leaves the old base behind; remove it.
        code, output = self._docker(
            "images", "agent-sandbox-base", "--format", "{{.Repository}}:{{.Tag}}"
        )
        for image in output.split() if code == 0 else []:
            if image != BASE_IMAGE:
                self._docker("rmi", image)

    def prepare(self, base: Path) -> str:
        """Return an image holding this lockfile's dependencies, building it once.

        `base` is the task's unedited checkout: dependencies come from the
        lockfile the repository already trusts, never from a model's edits. Only
        locked wheels are installed, as UID, and the checkout and cache live in
        tmpfs, so the image holds the environment alone.
        """
        self._base()
        key = environment_key(base)
        if key is None:
            return BASE_IMAGE
        if self._exists(key):
            return key
        check_lock(base)
        self._check_disk(BASE_IMAGE)
        name = f"agent-sandbox-build-{uuid.uuid4().hex}"
        install = (
            "tar -x -C /work && cd /work && "
            "uv sync --locked --all-groups --no-install-project && "
            f"{_PATH_FILE}"
        )
        build = [
            "run",
            "-i",
            "--name",
            name,
            *_LIMITED,
            "--tmpfs",
            "/tmp:rw,exec,nosuid,size=768m",  # noqa: S108 - the container's own tmpfs
            "--tmpfs",
            f"/work:rw,nosuid,size=256m,uid={UID},gid={UID}",
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
            committed = self._docker(
                "commit", "--change", "LABEL agent-sandbox=env", name, key
            )
            if committed[0] != 0:
                message = "could not save the dependency image"
                raise SandboxError(message)
        finally:
            self._docker("rm", "-f", name)
        self._prune()
        return key

    def run(self, image: str, worktree: Path, command: str) -> SandboxResult:
        """Run `command` in a fresh locked-down container over a copy of `worktree`.

        Raises SandboxError when the sandbox itself failed, so that is never
        mistaken for the command failing.
        """
        stream = archive(worktree, {COMMAND_FILE: command + "\n"})
        # The copy has no .git, so some tests' git calls would fail: give them a
        # one-commit repository of the copied files, with no history or remote.
        # Output goes to the tmpfs, which bounds it, and only its end comes back.
        script = (
            f"tar -x -C /work && echo {STARTED} && "
            f"{{ {SNAPSHOT}; }} >/dev/null 2>&1; "
            f"timeout -k 5 {self.timeout} sh {COMMAND_FILE} >/tmp/out 2>&1; "
            f"status=$?; tail -c {OUTPUT_LIMIT} /tmp/out; exit $status"
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
        before, started, after = output.partition(STARTED + "\n")
        if not started:
            message = "the sandbox could not run the command"
            raise SandboxError(message)
        del before
        if len(after) >= OUTPUT_LIMIT:
            after = "[earlier output cut]\n" + after
        return SandboxResult(code, after.strip())
