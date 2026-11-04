"""Invariants of the repository as one whole (not three repos side by side)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = [
    ROOT / "AgentGraph" / "requirements.txt",
    ROOT / "veynod_kernel" / "requirements.txt",
    ROOT / "agent_runtime" / "requirements.txt",
]


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(\[[^\]]+\])?\s*==\s*(\S+)", line)
        assert match, f"{path.relative_to(ROOT)}: '{line}' is not an exact pin"
        pins[match.group(1).lower()] = match.group(3)
    return pins


def test_every_service_pins_exact_versions_and_they_agree() -> None:
    seen: dict[str, tuple[str, str]] = {}
    for path in REQUIREMENTS:
        for name, version in _pins(path).items():
            if name in seen:
                other, where = seen[name]
                assert version == other, (
                    f"{name}: {path.relative_to(ROOT)} pins {version}, {where} pins {other}")
            else:
                seen[name] = (version, str(path.relative_to(ROOT)))


def test_every_image_builds_from_the_root_and_copies_what_exists() -> None:
    dockerfiles = sorted((ROOT / "deploy").glob("Dockerfile.*"))
    assert {path.name for path in dockerfiles} >= {
        "Dockerfile.api", "Dockerfile.kernel", "Dockerfile.runtime"}
    for path in dockerfiles:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if not parts or parts[0] != "COPY" or any(p.startswith("--from") for p in parts):
                continue
            for source in [p for p in parts[1:-1] if not p.startswith("--")]:
                assert (ROOT / source).exists(), f"{path.name}: COPY {source} is not in the repo"
    assert not list(ROOT.glob("*/Dockerfile")), "service Dockerfiles live in deploy/"


def test_one_ci_and_one_cli() -> None:
    nested = [p for p in ROOT.glob("*/.github") if p.is_dir()]
    assert not nested, f"GitHub only reads the root .github/: {nested}"
    assert (ROOT / ".github" / "workflows" / "ci.yml").is_file()
    manifests = [p for p in ROOT.glob("**/package.json")
                 if "node_modules" not in p.parts and '"name": "veynod"' in p.read_text()]
    assert [p.relative_to(ROOT).as_posix() for p in manifests] == ["AgentGraph/cli/package.json"]


def test_the_deploy_config_matches_the_dockerfiles_and_the_compose_file() -> None:
    """Every released service has its Dockerfile, and runs as named compose services."""
    yaml = pytest.importorskip("yaml")
    from contracts.deploy_plan import parse_config

    config = parse_config((ROOT / ".veynod" / "deploy.yml").read_text())
    compose = yaml.safe_load((ROOT / "deploy" / "vps" / "docker-compose.unified.yml").read_text())
    for service in config.services:
        assert (ROOT / service.dockerfile).is_file(), service
        assert service.image.startswith("ghcr.io/"), service
        for name in service.compose:
            image = compose["services"][name]["image"]
            variable = "VEYNOD_IMAGE_" + service.name.upper().replace("-", "_")
            assert image.startswith("${" + variable + ":-"), (name, image)
