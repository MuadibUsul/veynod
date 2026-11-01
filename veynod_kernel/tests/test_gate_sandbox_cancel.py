from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.services.gate_sandbox import run_sandboxed_command


@pytest.mark.asyncio
async def test_cancelling_gate_command_kills_the_process_tree(tmp_path: Path) -> None:
    started = tmp_path / "started"
    done = tmp_path / "done"
    script = tmp_path / "slow_gate.py"
    root = tmp_path.as_posix()
    script.write_text(
        "import pathlib, time\n"
        f"root = pathlib.Path({root!r})\n"
        "(root / 'started').write_text('1')\n"
        "time.sleep(5)\n"
        "(root / 'done').write_text('1')\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}"'
    task = asyncio.create_task(
        run_sandboxed_command(command, tmp_path, timeout=60)
    )

    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.05)
    assert started.exists(), "sandbox command did not start"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(1.0)
    assert not done.exists(), "cancelled sandbox process kept running"
