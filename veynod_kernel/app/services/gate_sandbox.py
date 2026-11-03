"""Isolated execution for agent-supplied gate commands.

Two modes:

* ``docker``: production mode.  The worktree is the only host path mounted;
  the container gets a read-only root, tmpfs /tmp, no network, CPU/memory/PID
  limits and no database/admin environment.
* ``local``: fallback for development and CI hosts without a gate image.  The
  environment is an allowlist (never DATABASE_URL or any VN_*/token/secret),
  HOME/TMP are isolated per command, and POSIX resource limits are applied.
  Local mode is not a security sandbox; production must use Docker.

Both modes enforce wall-clock timeout and process-tree termination.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.config import settings
from app.services.git_ops import CommandResult

_ALLOWED_ENV = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "LANG",
    "LC_ALL",
}
_SECRET_MARKERS = (
    "DATABASE",
    "ADMIN",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "AWS_",
    "GCP_",
    "AZURE_",
)


def sanitized_environment(cwd: Path) -> tuple[dict[str, str], Path]:
    """Return (safe env, isolated temp HOME).  No DB/admin credentials leak."""
    sandbox_tmp = Path(tempfile.mkdtemp(prefix="veynod-gate-sandbox-"))
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if key in _ALLOWED_ENV and not any(
            marker in upper for marker in _SECRET_MARKERS
        ):
            env[key] = value
    env["PYTHONPATH"] = str(cwd)
    env["PYTHONUNBUFFERED"] = "1"
    env["HOME"] = str(sandbox_tmp)
    env["USERPROFILE"] = str(sandbox_tmp)
    env["TEMP"] = str(sandbox_tmp)
    env["TMP"] = str(sandbox_tmp)
    env["VEYNOD_SANDBOX"] = "1"
    return env, sandbox_tmp


def _tail(text: str) -> str:
    limit = settings.gate_output_chars
    return text if len(text) <= limit else text[-limit:]


def _posix_limits(timeout: float) -> None:  # pragma: no cover - POSIX only
    import resource

    cpu = max(1, int(math.ceil(timeout)) + 1)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    memory = int(settings.gate_memory_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(
        resource.RLIMIT_NPROC, (settings.gate_pids_limit, settings.gate_pids_limit)
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
    else:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


async def _run_local(
    command: str, cwd: Path, timeout: float
) -> CommandResult:
    env, sandbox_tmp = sanitized_environment(cwd)
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | 0x0800_0000
        )
    else:
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = lambda: _posix_limits(timeout)
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **kwargs,
    )
    timed_out = False
    try:
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=max(0.05, timeout)
            )
        except asyncio.TimeoutError:
            timed_out = True
            _kill_tree(proc)
            stdout, _ = await proc.communicate()
        except asyncio.CancelledError:
            # Cancellation (task cancelled / worker shutdown) must not orphan
            # the agent's test process.
            _kill_tree(proc)
            with contextlib.suppress(Exception):
                await proc.communicate()
            raise
    finally:
        shutil.rmtree(sandbox_tmp, ignore_errors=True)
    output = (stdout or b"").decode("utf-8", errors="replace")
    return CommandResult(124 if timed_out else (proc.returncode or 0), _tail(output))


async def _run_docker(
    command: str, cwd: Path, timeout: float
) -> CommandResult:
    image = (settings.gate_sandbox_image or "").strip()
    if not image:
        return CommandResult(
            125,
            "[Sandbox] VN_GATE_SANDBOX=docker but VN_GATE_SANDBOX_IMAGE is empty.",
        )
    host_repos = Path(os.environ.get("VN_GATE_HOST_REPOS_DIR", ""))
    container_repos = Path(os.environ.get("VN_GATE_CONTAINER_REPOS_DIR", "/repos"))
    if not host_repos.is_absolute() or not container_repos.is_absolute():
        # The worker runs on the host itself (no container mount topology):
        # mount the worktree path directly.  Production refuses this layout at
        # startup (security.py), so only development reaches this fallback.
        host_worktree = cwd.resolve()
    else:
        try:
            relative = cwd.resolve().relative_to(container_repos.resolve())
        except ValueError:
            return CommandResult(
                125, "[Sandbox] Gate worktree is outside the repository mount.")
        host_worktree = host_repos / relative
    name = f"veynod-gate-{uuid.uuid4().hex[:12]}"
    args = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        settings.gate_docker_network,
        "--cpus",
        str(settings.gate_cpu_limit),
        "--memory",
        f"{settings.gate_memory_mb}m",
        "--pids-limit",
        str(settings.gate_pids_limit),
        "--ulimit",
        f"nofile={settings.gate_nofile_limit}:{settings.gate_nofile_limit}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m",
        *(
            ["--user", settings.gate_docker_user]
            if settings.gate_docker_user.strip()
            else []
        ),
        "-v",
        f"{host_worktree}:/workspace:rw",
        "-w",
        "/workspace",
        "-e",
        "PYTHONPATH=/workspace",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-e",
        "HOME=/tmp",
        "-e",
        "XDG_CACHE_HOME=/tmp/.cache",
        image,
        "/bin/sh",
        "-lc",
        command,
    ]
    docker_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "DOCKER_HOST", "DOCKER_CONFIG", "SYSTEMROOT", "SystemRoot", "COMSPEC"}
    }
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        env=docker_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    timed_out = False
    try:
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=max(0.05, timeout)
            )
        except asyncio.TimeoutError:
            timed_out = True
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "docker",
                    "kill",
                    name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            with contextlib.suppress(Exception):
                proc.kill()
            stdout, _ = await proc.communicate()
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "docker",
                    "kill",
                    name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.communicate()
            raise
    finally:
        with contextlib.suppress(Exception):
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()
    output = (stdout or b"").decode("utf-8", errors="replace")
    return CommandResult(124 if timed_out else (proc.returncode or 0), _tail(output))


async def run_sandboxed_command(
    command: str,
    cwd: Path,
    *,
    timeout: float | None = None,
) -> CommandResult:
    limit = float(timeout or settings.gate_timeout_seconds)
    if settings.gate_sandbox == "docker":
        return await _run_docker(command, cwd, limit)
    return await _run_local(command, cwd, limit)


__all__ = ["run_sandboxed_command", "sanitized_environment"]
