"""Veynod product API.

The shared ``contracts`` package lives at the repository root. In the image it
sits next to ``app/``; when a developer starts the API or the tests from
``AgentGraph/`` it does not, so the root is made importable here, once, instead
of in every module that imports a contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if (_REPO_ROOT / "contracts" / "__init__.py").is_file() and str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))
