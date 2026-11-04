"""What a deploy changes: the one place that decides it.

A project describes its services in ``.veynod/deploy.yml``::

    services:
      api:
        paths: ["AgentGraph/**", "deploy/Dockerfile.api"]
        image: ghcr.io/acme/app-api        # repository, without a tag
        dockerfile: deploy/Dockerfile.api  # built from the repository root
      worker:
        paths: ["worker/**"]
        image: ghcr.io/acme/app-worker
    shared: ["contracts/**", "requirements/**"]   # a change here touches every service
    health: {url: https://example.com/healthz, timeout_s: 120}

Two decisions follow from it, and both live here so CI, the server and the
tests agree:

* which services a commit has to rebuild (``services_for_paths``): CI builds
  only those and retags the rest, so an unchanged service costs no build;
* which services a deploy has to restart (``plan``): the deploy computer pulls
  and restarts only the services whose image digest changed, so an unchanged
  service costs no pull and no restart.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Any

#: A release names, per service, the image and the digest CI pushed for it.
Manifest = dict[str, dict[str, str]]


@dataclass(frozen=True)
class Service:
    name: str
    paths: tuple[str, ...]
    image: str = ""
    dockerfile: str = ""


@dataclass(frozen=True)
class DeployConfig:
    services: tuple[Service, ...]
    shared: tuple[str, ...] = ()
    health: dict[str, Any] = field(default_factory=dict)

    def names(self) -> list[str]:
        return [service.name for service in self.services]


class DeployConfigError(ValueError):
    """.veynod/deploy.yml says something this module cannot act on."""


def _clean(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path


def _patterns(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise DeployConfigError(f"{where} must be a list of path patterns")
    return tuple(_clean(item) for item in value)


def parse_config(raw: str | dict[str, Any]) -> DeployConfig:
    """Read .veynod/deploy.yml (YAML or JSON text, or the parsed mapping)."""
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                import yaml  # only needed where the YAML file itself is read (CI)
            except ImportError as exc:  # pragma: no cover - depends on the host
                raise DeployConfigError("PyYAML is needed to read deploy.yml") from exc
            data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise DeployConfigError("deploy config must be a mapping")
    services_raw = data.get("services")
    if not isinstance(services_raw, dict) or not services_raw:
        raise DeployConfigError("services must name at least one service")
    services = []
    for name, spec in services_raw.items():
        if not isinstance(name, str) or not name.replace("-", "").replace("_", "").isalnum():
            raise DeployConfigError(f"service name {name!r} must be letters, digits, - or _")
        spec = spec or {}
        if not isinstance(spec, dict):
            raise DeployConfigError(f"services.{name} must be a mapping")
        paths = _patterns(spec.get("paths"), f"services.{name}.paths")
        if not paths:
            raise DeployConfigError(f"services.{name}.paths must list what the service is built from")
        services.append(Service(name=name, paths=paths, image=str(spec.get("image") or ""),
                                dockerfile=str(spec.get("dockerfile") or "")))
    health = data.get("health") or {}
    if not isinstance(health, dict):
        raise DeployConfigError("health must be a mapping")
    return DeployConfig(services=tuple(services), shared=_patterns(data.get("shared"), "shared"),
                        health=dict(health))


def _matches(path: str, pattern: str) -> bool:
    path = _clean(path)
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatchcase(path, pattern)


def services_for_paths(config: DeployConfig, changed_paths: list[str]) -> list[str]:
    """The services a set of changed files makes CI rebuild, in config order.

    A change under ``shared`` rebuilds every service. A path no service claims
    rebuilds nothing (docs, tests): that is the point of being incremental.
    """
    if any(_matches(path, pattern) for path in changed_paths for pattern in config.shared):
        return config.names()
    return [service.name for service in config.services
            if any(_matches(path, pattern) for path in changed_paths for pattern in service.paths)]


@dataclass(frozen=True)
class Plan:
    """What one deploy does on the server."""

    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.changed and not self.removed

    def as_dict(self) -> dict[str, list[str]]:
        return {"changed": list(self.changed), "unchanged": list(self.unchanged),
                "removed": list(self.removed)}


def _digest(entry: Any) -> str:
    return str(entry.get("digest") or "") if isinstance(entry, dict) else ""


def plan(previous: Manifest | None, release: Manifest) -> Plan:
    """Services whose digest differs between what runs and the release.

    A service the release lists with a digest equal to the running one is not
    pulled and not restarted. A first deploy (nothing running) changes every
    service. A digest missing from the release is treated as changed, never as
    "same": restarting once is cheaper than keeping a service on a stale image.
    """
    previous = previous or {}
    changed, unchanged = [], []
    for name in sorted(release):
        new = _digest(release[name])
        if new and new == _digest(previous.get(name)):
            unchanged.append(name)
        else:
            changed.append(name)
    removed = sorted(name for name in previous if name not in release)
    return Plan(changed=tuple(changed), unchanged=tuple(unchanged), removed=tuple(removed))


def validate_manifest(value: Any) -> Manifest:
    """A release manifest as CI posts it: {service: {image, digest}}."""
    if not isinstance(value, dict) or not value:
        raise DeployConfigError("manifest must map at least one service to its image")
    out: Manifest = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise DeployConfigError("manifest entries are {service: {image, digest}}")
        image, digest = str(entry.get("image") or ""), str(entry.get("digest") or "")
        if not image or not digest.startswith("sha256:") or len(digest) != 71:
            raise DeployConfigError(f"manifest.{name} needs an image and a sha256 digest")
        out[name] = {"image": image[:300], "digest": digest}
    return out


__all__ = [
    "DeployConfig",
    "DeployConfigError",
    "Manifest",
    "Plan",
    "Service",
    "parse_config",
    "plan",
    "services_for_paths",
    "validate_manifest",
]
