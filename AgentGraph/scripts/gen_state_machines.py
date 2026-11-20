"""Generate docs/STATE_MACHINES.md from app/state_machines.py.

Usage: python scripts/gen_state_machines.py [--check]
With --check the script exits 1 when the committed file is out of date.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.state_machines import MACHINES  # noqa: E402

TARGET = ROOT / "docs" / "STATE_MACHINES.md"


def _id(state: str) -> str:
    return "s_" + "".join(ch if ch.isalnum() else "_" for ch in state)


def render() -> str:
    lines = [
        "# Veynod state machines",
        "",
        "Generated from `app/state_machines.py` by `scripts/gen_state_machines.py`;",
        "do not edit by hand. The database holds every writer to these columns: a",
        "move not listed here is refused with 409 `invalid_state_transition`, whose",
        "`details.allowed` lists the moves that are (`STATE_GUARD_MODE=record` lets it",
        "through and counts it instead). The same data: `GET /v1/state-machines`.",
        "",
        "| machine | column | states | moves |",
        "|---|---|---|---|",
    ]
    for m in MACHINES:
        moves = "any" if m.free else str(len(m.moves))
        lines.append(f"| [{m.name}](#{m.name.replace('.', '').replace('_', '-')}) "
                     f"| `{m.table}.{m.column}` | {len(m.states)} | {moves} |")
    for m in MACHINES:
        lines += ["", f"## {m.name}", "", f"`{m.table}.{m.column}`: {m.summary}", ""]
        lines += ["| state | meaning | next |", "|---|---|---|"]
        for state, meaning in m.states.items():
            mark = " (start)" if state in m.initial else (" (end)" if state in m.terminal else "")
            nxt = ", ".join(f"`{s}`" for s in m.allowed_from(state)) or "—"
            lines.append(f"| `{state}`{mark} | {meaning} | {nxt} |")
        if m.free:
            lines += ["", "Any value may replace any other: this is a setting."]
            continue
        lines += ["", "```mermaid", "stateDiagram-v2"]
        for state in m.initial:
            lines.append(f"    [*] --> {_id(state)}")
        for state in m.states:
            lines.append(f"    {_id(state)} : {state}")
        for (a, b) in sorted(m.moves):
            lines.append(f"    {_id(a)} --> {_id(b)}")
        for state in m.terminal:
            lines.append(f"    {_id(state)} --> [*]")
        lines += ["```", "", "| from | to | why |", "|---|---|---|"]
        for (a, b), why in sorted(m.moves.items()):
            lines.append(f"| `{a}` | `{b}` | {why} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != content:
            print("docs/STATE_MACHINES.md is out of date; run scripts/gen_state_machines.py")
            return 1
        return 0
    TARGET.write_text(content, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
