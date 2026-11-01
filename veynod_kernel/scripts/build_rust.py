"""Build and install the optional Rust core extension.

Usage:
    python scripts/build_rust.py

Requires a Rust toolchain (rustc/cargo). Maturin is installed automatically if
missing. The wheel is installed into the current Python environment with
`--force-reinstall --no-deps` so the app imports the fresh `veynod_core`.
"""

from __future__ import annotations

import glob
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRATE = ROOT / "rust" / "veynod_core"


def run(command: list[str], cwd: Path = ROOT) -> None:
    print("$", " ".join(command))
    result = subprocess.run(command, cwd=str(cwd))
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def ensure_tooling() -> None:
    for tool in ("cargo", "rustc"):
        result = subprocess.run([tool, "--version"], capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(
                f"{tool} not found. Install the Rust toolchain from https://rustup.rs/."
            )
        print(result.stdout.strip())
    try:
        import maturin  # noqa: F401
    except ImportError:
        print("maturin not installed; installing...")
        run([sys.executable, "-m", "pip", "install", "maturin>=1.7,<2.0"])


def main() -> None:
    if not (CRATE / "Cargo.toml").exists():
        raise SystemExit(f"Rust crate not found at {CRATE}")
    ensure_tooling()
    run([sys.executable, "-m", "maturin", "build", "--release"], CRATE)

    wheels = sorted(glob.glob(str(CRATE / "target" / "wheels" / "veynod_core-*.whl")))
    if not wheels:
        raise SystemExit("maturin did not produce a wheel.")
    wheel = wheels[-1]
    run([sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", wheel])

    result = subprocess.run(
        [sys.executable, "-c", "import veynod_core; print('veynod_core', veynod_core.__version__)"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    print("Rust core installed. Set VEYNOD_DISABLE_RUST=1 to force the Python fallback.")


if __name__ == "__main__":
    main()
