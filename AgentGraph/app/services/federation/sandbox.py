"""Skill sandbox check (P3-3, ICD_P3_REGISTRY.md §10).

Before a version is published and again before every install, the platform
inspects it without executing it:

* it can run: an executable skill's entry is a code file in a known language
  and (for Python) parses; a declarative skill carries no code files;
* it stays inside what it declares: every network, filesystem, exec or
  secrets use found in the code must be among the declared permissions;
* it carries none of the patterns that are refused whatever is declared
  (wiping a disk, a fork bomb, reading SSH keys or browser cookies, running
  decoded or downloaded code, mining pools).

The check is static: it reads the code, it never runs it on the server. A
pass means "nothing undeclared was found by checker N", not "safe". The
checker version is stored with each report so that a newer checker re-judges
old versions at their next install.
"""

from __future__ import annotations

import ast
import re
from typing import Any

CHECKER_VERSION = "1"

LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "shell",
    ".rb": "other",
    ".pl": "other",
    ".php": "other",
    ".go": "other",
    ".rs": "other",
    ".java": "other",
    ".bat": "shell",
    ".cmd": "shell",
}

# Refused whatever the skill declares.
MALICIOUS = [
    (re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME)(\s|$|\*)"), "wipes a disk or home"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"(\.ssh/id_[a-z0-9]+|\.ssh/authorized_keys)"), "reads or writes SSH keys"),
    (re.compile(r"(/Cookies\b|Login Data|cookies\.sqlite|key4\.db)"), "reads browser cookies"),
    (re.compile(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "runs a downloaded script"),
    (
        re.compile(r"(exec|eval)\s*\(\s*(base64\.b64decode|atob|bytes\.fromhex|codecs\.decode)"),
        "runs decoded code",
    ),
    (re.compile(r"stratum\+tcp://|xmrig|coinhive", re.I), "crypto mining"),
]

_PY_MODULES = {
    "network": {
        "socket",
        "ssl",
        "urllib",
        "urllib3",
        "http",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "telnetlib",
        "websocket",
        "websockets",
        "paramiko",
    },
    "exec": {"subprocess", "ctypes", "multiprocessing", "pty", "importlib", "runpy", "cffi"},
    "filesystem": {"shutil", "tempfile", "glob"},
    "secrets": {"keyring", "getpass"},
}
_PY_CALLS = {
    "exec": {
        "eval",
        "exec",
        "compile",
        "__import__",
        "os.system",
        "os.popen",
        "os.execv",
        "os.execve",
        "os.spawnv",
        "os.fork",
        "os.startfile",
    },
    "filesystem": {
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.removedirs",
        "os.rename",
        "os.replace",
        "os.mkdir",
        "os.makedirs",
        "os.chmod",
        "os.chown",
        "os.symlink",
        "os.truncate",
    },
    "secrets": {"os.getenv", "os.environ.get", "os.getenvb"},
}
# pathlib-style writes: Path(...).write_text() and friends.
_PY_FS_METHODS = {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch", "rename"}
_PY_SECRET_WORDS = re.compile(r"(\.aws/credentials|\.netrc|\.npmrc|\.pypirc|\.docker/config)")

_TEXT_RULES = {
    "javascript": [
        (
            "network",
            re.compile(
                r"require\(\s*['\"](http|https|net|tls|dgram|node-fetch|axios|ws)['\"]\s*\)"
                r"|from\s+['\"](http|https|net|tls|node-fetch|axios|ws)['\"]|\bfetch\s*\(|XMLHttpRequest"
                r"|WebSocket\s*\("
            ),
        ),
        (
            "exec",
            re.compile(
                r"child_process|\beval\s*\(|new\s+Function\s*\(|\bspawn\s*\(|\bexecSync\s*\("
            ),
        ),
        (
            "filesystem",
            re.compile(
                r"\bfs\.(write|append|unlink|rm|rmdir|mkdir|rename|copy|chmod|symlink)\w*\s*\("
            ),
        ),
        ("secrets", re.compile(r"process\.env|\.aws/credentials|\.netrc")),
    ],
    "shell": [
        ("network", re.compile(r"\b(curl|wget|nc|ncat|ssh|scp|rsync|ftp|telnet)\b")),
        ("filesystem", re.compile(r"\b(rm|mv|cp|mkdir|rmdir|chmod|chown|ln|dd|tee)\b|>>?\s*\S")),
        ("secrets", re.compile(r"\$\{?[A-Z_]*(TOKEN|SECRET|KEY|PASSWORD)\b|\.aws/|\.netrc")),
    ],
}


def _line(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _python_uses(path: str, content: str) -> tuple[bool, list[dict[str, Any]], str | None]:
    """(parses, uses, error) for one Python file."""
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError as exc:
        return False, [], f"line {exc.lineno}: {exc.msg}"
    uses: list[dict[str, Any]] = []

    def add(permission: str, node: ast.AST, detail: str) -> None:
        uses.append(
            {
                "permission": permission,
                "path": path,
                "line": getattr(node, "lineno", None),
                "detail": detail,
            }
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                root = name.split(".")[0]
                for permission, modules in _PY_MODULES.items():
                    if root in modules:
                        add(permission, node, f"imports {name}")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func) or ""
            for permission, calls in _PY_CALLS.items():
                if name in calls:
                    add(permission, node, f"calls {name}")
            if name == "open" or name.endswith(".open"):
                mode = None
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if isinstance(mode, str) and any(c in mode for c in "wax+"):
                    add("filesystem", node, f"opens a file for writing ({mode})")
                elif mode is not None and not isinstance(mode, str):
                    add("filesystem", node, "opens a file with a computed mode")
            if isinstance(node.func, ast.Attribute) and node.func.attr in _PY_FS_METHODS:
                add("filesystem", node, f"calls .{node.func.attr}()")
        elif isinstance(node, ast.Attribute):
            if _dotted(node) in {"os.environ", "os.environb"}:
                add("secrets", node, "reads the environment")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _PY_SECRET_WORDS.search(node.value):
                add("secrets", node, "names a credentials file")
    return True, uses, None


def _text_uses(language: str, path: str, content: str) -> list[dict[str, Any]]:
    uses = []
    for permission, pattern in _TEXT_RULES.get(language, []):
        for match in pattern.finditer(content):
            uses.append(
                {
                    "permission": permission,
                    "path": path,
                    "line": _line(content, match.start()),
                    "detail": f"uses {match.group(0).strip()[:40]}",
                }
            )
    if language == "shell":
        # A shell script runs commands: that is exec by definition.
        uses.append({"permission": "exec", "path": path, "line": 1, "detail": "is a shell script"})
    return uses


def language_of(path: str) -> str | None:
    lower = path.lower()
    for ext, language in LANGUAGES.items():
        if lower.endswith(ext):
            return language
    return None


def inspect(manifest: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    """Judge one version. Returns a report with a verdict of passed or failed."""
    kind = manifest.get("kind", "declarative")
    declared = sorted(set(manifest.get("permissions") or []))
    findings: list[dict[str, Any]] = []
    uses: list[dict[str, Any]] = []
    code_files = []
    runs = True

    for item in files:
        path, content = item["path"], item["content"]
        for pattern, why in MALICIOUS:
            match = pattern.search(content)
            if match:
                findings.append(
                    {
                        "rule": "refused_pattern",
                        "severity": "block",
                        "path": path,
                        "line": _line(content, match.start()),
                        "detail": why,
                    }
                )
        language = language_of(path)
        if language is None:
            continue
        code_files.append(path)
        if kind == "declarative":
            continue
        if language == "python":
            parses, found, error = _python_uses(path, content)
            uses.extend(found)
            if not parses:
                findings.append(
                    {
                        "rule": "does_not_parse",
                        "severity": "block",
                        "path": path,
                        "line": None,
                        "detail": error,
                    }
                )
                if path == manifest.get("entry"):
                    runs = False
        elif language == "other":
            findings.append(
                {
                    "rule": "language_not_checked",
                    "severity": "block",
                    "path": path,
                    "line": None,
                    "detail": "only Python, JavaScript and shell can be checked",
                }
            )
        else:
            uses.extend(_text_uses(language, path, content))

    if kind == "declarative" and code_files:
        findings.append(
            {
                "rule": "code_in_declarative",
                "severity": "block",
                "path": code_files[0],
                "line": None,
                "detail": "a declarative skill is text for agents; put examples in Markdown",
            }
        )
    if kind == "executable":
        entry = manifest.get("entry")
        if not entry or language_of(entry) is None:
            runs = False
            findings.append(
                {
                    "rule": "no_runnable_entry",
                    "severity": "block",
                    "path": entry,
                    "line": None,
                    "detail": "an executable skill's entry is a code file",
                }
            )

    detected = sorted({u["permission"] for u in uses})
    undeclared = sorted(set(detected) - set(declared))
    for permission in undeclared:
        first = next(u for u in uses if u["permission"] == permission)
        findings.append(
            {
                "rule": "undeclared_permission",
                "severity": "block",
                "path": first["path"],
                "line": first["line"],
                "detail": f"uses {permission} without declaring it ({first['detail']})",
            }
        )
    unused = sorted(set(declared) - set(detected))
    for permission in unused:
        findings.append(
            {
                "rule": "declared_but_unused",
                "severity": "note",
                "path": None,
                "line": None,
                "detail": f"declares {permission} but no use was found",
            }
        )
    verdict = "failed" if any(f["severity"] == "block" for f in findings) else "passed"
    return {
        "checker": CHECKER_VERSION,
        "method": "static",
        "verdict": verdict,
        "runs": runs,
        "declared": declared,
        "detected": detected,
        "undeclared": undeclared,
        "findings": findings[:50],
        "uses": uses[:50],
    }
