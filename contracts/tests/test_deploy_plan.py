"""Incremental deploys: what a commit rebuilds and what a release restarts."""

from __future__ import annotations

import pytest

from contracts.deploy_plan import (
    DeployConfigError,
    parse_config,
    plan,
    services_for_paths,
    validate_manifest,
)

CONFIG = {
    "services": {
        "api": {"paths": ["AgentGraph/**", "deploy/Dockerfile.api"], "image": "ghcr.io/a/api"},
        "kernel": {"paths": ["veynod_kernel/**", "deploy/Dockerfile.kernel"]},
        "runtime": {"paths": ["./agent_runtime/**"]},
    },
    "shared": ["contracts/**", "requirements/*.txt"],
    "health": {"url": "https://example.com/healthz"},
}


def _digest(char: str) -> str:
    return "sha256:" + char * 64


def test_a_commit_rebuilds_only_the_services_it_touches() -> None:
    config = parse_config(CONFIG)
    assert services_for_paths(config, ["AgentGraph/app/main.py"]) == ["api"]
    assert services_for_paths(config, ["agent_runtime/x.py", "deploy/Dockerfile.kernel"]) == [
        "kernel", "runtime"]
    # Docs and the deploy file of nobody rebuild nothing.
    assert services_for_paths(config, ["docs/README.md", "README.md"]) == []
    # A prefix is a directory, not a string prefix.
    assert services_for_paths(config, ["AgentGraphX/a.py"]) == []


def test_a_shared_change_rebuilds_every_service() -> None:
    config = parse_config(CONFIG)
    assert services_for_paths(config, ["contracts/kernel_http.py"]) == ["api", "kernel", "runtime"]
    assert services_for_paths(config, ["requirements/base.txt"]) == ["api", "kernel", "runtime"]


def test_config_reads_json_text_and_names_its_mistakes() -> None:
    import json

    config = parse_config(json.dumps(CONFIG))
    assert config.names() == ["api", "kernel", "runtime"]
    assert config.services[2].paths == ("agent_runtime/**",)
    assert config.health["url"] == "https://example.com/healthz"
    for bad in ({}, {"services": {}}, {"services": {"a b": {"paths": ["x"]}}},
                {"services": {"api": {}}}, {"services": {"api": {"paths": "x"}}},
                {"services": {"api": {"paths": ["x"]}}, "health": "up"}, []):
        with pytest.raises(DeployConfigError):
            parse_config(bad)  # type: ignore[arg-type]


def test_config_reads_yaml_when_pyyaml_is_there() -> None:
    pytest.importorskip("yaml")
    config = parse_config("services:\n  api:\n    paths: [\"AgentGraph/**\"]\n")
    assert config.names() == ["api"]


def test_a_release_restarts_only_services_whose_digest_changed() -> None:
    running = {"api": {"image": "i/api", "digest": _digest("a")},
               "kernel": {"image": "i/kernel", "digest": _digest("k")},
               "old": {"image": "i/old", "digest": _digest("o")}}
    release = {"api": {"image": "i/api", "digest": _digest("b")},
               "kernel": {"image": "i/kernel", "digest": _digest("k")},
               "runtime": {"image": "i/runtime", "digest": _digest("r")}}
    result = plan(running, release)
    assert result.changed == ("api", "runtime")
    assert result.unchanged == ("kernel",)
    assert result.removed == ("old",)
    assert not result.empty
    assert result.as_dict()["changed"] == ["api", "runtime"]


def test_first_deploy_changes_everything_and_same_release_changes_nothing() -> None:
    release = {"api": {"image": "i/api", "digest": _digest("a")}}
    assert plan(None, release).changed == ("api",)
    assert plan(release, release).empty
    # A missing digest is never "the same".
    assert plan({"api": {"image": "i/api"}}, {"api": {"image": "i/api"}}).changed == ("api",)


def test_a_manifest_needs_an_image_and_a_real_digest() -> None:
    good = {"api": {"image": "ghcr.io/a/api", "digest": _digest("a"), "extra": "dropped"}}
    assert validate_manifest(good) == {"api": {"image": "ghcr.io/a/api", "digest": _digest("a")}}
    for bad in ({}, {"api": "x"}, {"api": {"image": "", "digest": _digest("a")}},
                {"api": {"image": "i", "digest": "sha256:short"}}, None):
        with pytest.raises(DeployConfigError):
            validate_manifest(bad)
