#!/usr/bin/env python3
"""Build, push and announce one release (deploy gate v2).

    python scripts/release.py --commit "$GITHUB_SHA" --branch main [--dry-run]

For every service in .veynod/deploy.yml:

* its input digest is the content hash of the tracked files it is built from
  (``contracts.deploy_plan.input_digest``);
* when the registry already has ``<image>:inputs-<digest>``, that image is
  retagged ``<image>:<commit>`` (no build: nothing it is built from changed);
* otherwise it is built once with the BuildKit cache and pushed with both tags.

Then the manifest {service: {image, digest, compose}} is posted to Veynod with
the target's release token (VEYNOD_HOST, VEYNOD_RELEASE_TOKEN), which queues
the deploy when the target deploys on every release.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contracts.deploy_plan import input_digest, parse_config


def tracked_files() -> dict[str, str]:
    """Every tracked path and its blob id at HEAD."""
    out = subprocess.run(["git", "ls-files", "-s"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    files = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        files[path] = meta.split()[1]
    return files


def docker(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    print("+ docker " + " ".join(args), flush=True)
    return subprocess.run(["docker", *args], cwd=ROOT, text=True,
                          capture_output=capture, check=False)


def remote_digest(ref: str) -> str | None:
    """The manifest digest the registry holds for ``ref``, or None."""
    found = docker("buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}",
                   capture=True)
    if found.returncode != 0:
        return None
    try:
        return json.loads(found.stdout).get("digest")
    except ValueError:
        return None


def release_service(service, inputs: str, commit: str, dry_run: bool) -> tuple[dict, bool]:
    image = service.image
    cached = f"{image}:inputs-{inputs[:32]}"
    tagged = f"{image}:{commit}"
    built = False
    if dry_run:
        return {"image": image, "digest": "sha256:" + "0" * 64, "compose": list(service.compose),
                "inputs": inputs}, False
    digest = remote_digest(cached)
    if digest:
        if docker("buildx", "imagetools", "create", "--tag", tagged, cached).returncode != 0:
            raise SystemExit(f"could not retag {cached}")
    else:
        scope = f"veynod-{service.name}"
        result = docker("buildx", "build", "--file", service.dockerfile, "--push",
                        "--tag", tagged, "--tag", cached,
                        "--cache-from", f"type=gha,scope={scope}",
                        "--cache-to", f"type=gha,scope={scope},mode=max",
                        "--label", f"org.opencontainers.image.revision={commit}", ".")
        if result.returncode != 0:
            raise SystemExit(f"building {service.name} failed")
        digest = remote_digest(tagged)
        built = True
    if not digest:
        raise SystemExit(f"the registry has no digest for {tagged}")
    return {"image": image, "digest": digest, "compose": list(service.compose)}, built


def post(host: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"{host.rstrip('/')}/v1/deploys/releases", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Veynod-Release-Token": token},
        method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=str(ROOT / ".veynod" / "deploy.yml"))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the input digests; build, push and post nothing")
    args = parser.parse_args(argv)
    config = parse_config(Path(args.config).read_text())
    files = tracked_files()
    manifest, built = {}, []
    for service in config.services:
        if not service.image or not service.dockerfile:
            raise SystemExit(f"services.{service.name} needs image and dockerfile to release")
        entry, was_built = release_service(service, input_digest(config, service.name, files),
                                           args.commit, args.dry_run)
        manifest[service.name] = entry
        if was_built:
            built.append(service.name)
    body = {"commit": args.commit, "branch": args.branch, "manifest": manifest, "built": built,
            "source": os.environ.get("GITHUB_RUN_ID") and
            f"github:{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"}
    print(json.dumps(body, indent=2))
    if args.dry_run:
        return 0
    host, token = os.environ.get("VEYNOD_HOST"), os.environ.get("VEYNOD_RELEASE_TOKEN")
    if not host or not token:
        print("VEYNOD_HOST or VEYNOD_RELEASE_TOKEN is not set: images pushed, no release posted")
        return 0
    print(json.dumps(post(host, token, body), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
