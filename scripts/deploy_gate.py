#!/usr/bin/env python3
"""Veynod VPS deploy gate runner (agentgraph/spec-vps-auto-deploy-gate v1).

Runs on the production VPS from a systemd timer (deploy/deploy-gate/), never in
GitHub Actions. One cycle:

1. heartbeat to the API (the console shows the gate offline when it stops);
2. poll ``origin/<branch>`` and queue a commit the gate has not seen (a lost
   webhook still deploys);
3. claim the newest queued run and check it:
   G-01 the commit is on the branch, newer than production, and every commit
        since production came from a merged pull request;
   G-02 ruff + pytest (and the cli/protocol tests) in throwaway containers on
        a private network with their own database: never the production
        database or ports;
   G-03 migrations only add files, the schema version only grows, an upgrade
        from the production commit applies cleanly with the right fingerprint
        and no table loses row security; the tasks the pull requests name are
        approved with no failing quality gate;
   G-04 ``deploy_tlines.sh prepare``: a checked database backup and a
        rollback tag of the running image;
   G-05 ``deploy_tlines.sh deploy`` on that stamp, then a smoke test of the
        public site; a failed smoke test rolls the image back. The database
        is never restored automatically.
4. report every check, the outcome and a plain-language owner report.

Nothing deploys while the gate is paused, and an automatic trigger deploys only
while the owner has authorized automatic deploys. Secrets come from the
deployment ``.env`` and are redacted from every log line. Standard library only.

Usage: deploy_gate.py run | status
"""

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # the runner is Linux-only; tests import this module
    fcntl = None
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "2"
CHECK_NAMES = {
    "G-01": "main and merge evidence",
    "G-02": "tests and lint",
    "G-03": "quality and migration safety",
    "G-04": "backup and rollback tag",
    "G-05": "deploy and smoke test",
}
# The same checks as the owner reads them (A-08: no technical words).
CHECK_PLAIN = {
    "G-01": "合并记录检查",
    "G-02": "自动测试",
    "G-03": "质量与数据安全检查",
    "G-04": "上线前备份",
    "G-05": "上线后自检",
}
TASK_KEY = re.compile(
    r"(?:Veynod-Task|Board task|板子任务|看板任务)\s*[:：]?\s*"
    r"`?([A-Za-z0-9][A-Za-z0-9._:-]{1,127})`?", re.IGNORECASE)
HOW_TO_TRY = re.compile(r"^#+\s*(怎么试|如何验证|How to try|How to test|Try it)\b.*$",
                        re.IGNORECASE | re.MULTILINE)
WHAT_CHANGED = re.compile(r"^#+\s*(哪里变了|What changed)\b.*$", re.IGNORECASE | re.MULTILINE)
SECRET_KEYS = re.compile(r"(TOKEN|SECRET|PASSWORD|KEY|DATABASE_URL|DSN)", re.IGNORECASE)
MAX_RANGE = 50
LOG_ROTATE_BYTES = 5_000_000


# ------------------------------------------------------------------ config


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a dotenv file; values are never printed."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    deploy_dir: Path
    api: str
    runner_token: str
    repo: str = "MuadibUsul/veynod"
    branch: str = "main"
    workdir: Path = Path("/var/lib/veynod-deploy-gate")
    log_dir: Path = Path("/var/log/veynod-deploy-gate")
    public_url: str = "https://veynod.com"
    allow_direct_push: bool = False
    require_task: bool = True
    run_node_checks: bool = True
    github_token: str = ""
    test_timeout: int = 3600
    deploy_timeout: int = 3600
    pg_image: str = "pgvector/pgvector:pg16"
    python_image: str = "python:3.12-slim"
    node_image: str = "node:22-slim"
    host: str = field(default_factory=socket.gethostname)
    secrets: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, deploy_dir: str | None = None) -> Config:
        base = Path(deploy_dir or os.environ.get("DEPLOY_DIR") or "/home/deploy/agentgraph")
        values = {**read_env_file(base / ".env"),
                  **{k: v for k, v in os.environ.items() if k.startswith(("DEPLOY_", "PUBLIC_"))}}
        public = values.get("PUBLIC_URL") or "https://veynod.com"
        token = values.get("DEPLOY_GATE_RUNNER_TOKEN", "")
        if not token:
            raise SystemExit("DEPLOY_GATE_RUNNER_TOKEN is not set in the deployment .env")
        secrets = [value for key, value in values.items()
                   if SECRET_KEYS.search(key) and len(value) >= 8]
        return cls(
            deploy_dir=base,
            api=(values.get("DEPLOY_GATE_API") or public).rstrip("/"),
            runner_token=token,
            repo=values.get("DEPLOY_GATE_REPO") or "MuadibUsul/veynod",
            branch=values.get("DEPLOY_GATE_BRANCH") or "main",
            workdir=Path(values.get("DEPLOY_GATE_WORKDIR") or "/var/lib/veynod-deploy-gate"),
            log_dir=Path(values.get("DEPLOY_GATE_LOG_DIR") or "/var/log/veynod-deploy-gate"),
            public_url=public.rstrip("/"),
            allow_direct_push=_flag(values.get("DEPLOY_GATE_ALLOW_DIRECT_PUSH"), False),
            require_task=_flag(values.get("DEPLOY_GATE_REQUIRE_TASK"), True),
            run_node_checks=_flag(values.get("DEPLOY_GATE_NODE_CHECKS"), True),
            github_token=values.get("DEPLOY_GATE_GITHUB_TOKEN", ""),
            test_timeout=int(values.get("DEPLOY_GATE_TEST_TIMEOUT") or 3600),
            deploy_timeout=int(values.get("DEPLOY_GATE_DEPLOY_TIMEOUT") or 3600),
            pg_image=values.get("DEPLOY_GATE_PG_IMAGE") or "pgvector/pgvector:pg16",
            python_image=values.get("DEPLOY_GATE_PYTHON_IMAGE") or "python:3.12-slim",
            node_image=values.get("DEPLOY_GATE_NODE_IMAGE") or "node:22-slim",
            secrets=secrets,
        )


# ------------------------------------------------------------- side effects


@dataclass
class Result:
    code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Log:
    """The technical log of one cycle; secrets are redacted before writing."""

    def __init__(self, path: Path | None, secrets: list[str]):
        self.path = path
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self.lines: list[str] = []

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def write(self, text: str) -> None:
        text = self.redact(text)
        self.lines.append(text)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > LOG_ROTATE_BYTES:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + text + "\n")


class Shell:
    def __init__(self, log: Log):
        self.log = log

    def run(self, cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
            timeout: int = 600) -> Result:
        self.log.write("$ " + " ".join(shlex.quote(part) for part in cmd))
        try:
            done = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})},
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=timeout, check=False)
            result = Result(done.returncode, done.stdout or "")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            result = Result(124, out + f"\n(timed out after {timeout}s)")
        except OSError as exc:
            result = Result(127, str(exc))
        tail = result.output[-20000:]
        if tail:
            self.log.write(tail.rstrip())
        self.log.write(f"(exit {result.code})")
        return result


class Api:
    def __init__(self, config: Config):
        self.base = config.api
        self.token = config.runner_token

    def call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base}/v1/deploy-gate/runner{path}", method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json", "User-Agent": "veynod-deploy-gate"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "{}")


class GitHub:
    def __init__(self, config: Config):
        self.repo = config.repo
        self.token = config.github_token

    def pulls_for_commit(self, sha: str) -> list[dict[str, Any]]:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "veynod-deploy-gate"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/commits/{sha}/pulls", headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8") or "[]")
        return data if isinstance(data, list) else []


def http_get(url: str, *, method: str = "GET", body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, method=method, data=body, headers={
        "User-Agent": "veynod-deploy-gate", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        return 0, str(error)


# -------------------------------------------------------------------- gate


@dataclass
class Check:
    id: str
    ok: bool
    summary: str

    def out(self) -> dict[str, Any]:
        return {"id": self.id, "ok": self.ok, "summary": self.summary[:300]}


@dataclass
class Outcome:
    status: str
    reason: str
    commit: str | None
    checks: list[Check]
    deploy: dict[str, str] = field(default_factory=dict)
    rolled_back: bool = False
    report: dict[str, Any] = field(default_factory=dict)
    task_keys: list[str] = field(default_factory=list)


class Gate:
    def __init__(self, config: Config, *, shell: Shell, api: Api, github: GitHub,
                 log: Log, http: Callable[..., tuple[int, str]] = http_get):
        self.config = config
        self.shell = shell
        self.api = api
        self.github = github
        self.log = log
        self.http = http
        self.repo_dir = config.workdir / "repo"
        self.quarantined = 0

    # ------------------------------------------------------------ helpers

    def git(self, *args: str, cwd: Path | None = None, timeout: int = 300) -> Result:
        return self.shell.run(["git", *args], cwd=cwd or self.repo_dir, timeout=timeout)

    def production_commit(self) -> str | None:
        result = self.git("rev-parse", "HEAD", cwd=self.config.deploy_dir)
        sha = result.output.strip().splitlines()[-1] if result.ok and result.output.strip() else ""
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None

    def sync_repo(self) -> str | None:
        remote = f"https://github.com/{self.config.repo}.git"
        if not (self.repo_dir / ".git").exists():
            self.config.workdir.mkdir(parents=True, exist_ok=True)
            if not self.shell.run(["git", "clone", "--no-checkout", remote, str(self.repo_dir)],
                                  timeout=1800).ok:
                return None
        if not self.git("fetch", "--prune", "origin",
                        f"+refs/heads/{self.config.branch}:refs/remotes/origin/{self.config.branch}",
                        timeout=900).ok:
            return None
        head = self.git("rev-parse", f"origin/{self.config.branch}")
        sha = head.output.strip().splitlines()[-1] if head.ok and head.output.strip() else ""
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.git("merge-base", "--is-ancestor", older, newer).ok

    def first_parent_range(self, deployed: str | None, commit: str) -> list[str] | None:
        spec = f"{deployed}..{commit}" if deployed else f"{commit}~1..{commit}"
        result = self.git("rev-list", "--first-parent", spec)
        if not result.ok:
            return None
        return [line for line in result.output.split() if re.fullmatch(r"[0-9a-f]{40}", line)]

    def file_at(self, rev: str, path: str) -> str | None:
        result = self.git("show", f"{rev}:{path}")
        return result.output if result.ok else None

    @staticmethod
    def schema_version(database_py: str | None) -> tuple[int, ...] | None:
        if not database_py:
            return None
        found = re.findall(r'\(\s*"(\d+\.\d+\.\d+)"\s*,\s*_MIGRATIONS_DIR', database_py)
        return tuple(int(part) for part in found[-1].split(".")) if found else None

    @staticmethod
    def app_version(config_py: str | None) -> str | None:
        match = re.search(r'app_version:\s*str\s*=\s*"([^"]+)"', config_py or "")
        return match.group(1) if match else None

    # ------------------------------------------------------------- cycle

    def cycle(self) -> dict[str, Any]:
        deployed = self.production_commit()
        state = self.api.call("/heartbeat", {"host": self.config.host, "version": VERSION,
                                             "deployed_commit": deployed})
        head = self.sync_repo()
        if head and head != (state.get("deployed_commit") or deployed):
            self.api.call("/runs", {"host": self.config.host, "commit": head})
        claim = self.api.call("/claim", {"host": self.config.host})
        run = claim.get("run")
        if not run:
            return {"run": None}
        # Idle cycles share gate.log; each run gets its own technical log.
        if self.log.path is not None:
            self.log.path = self.config.log_dir / f"{run['id']}.log"
        try:
            outcome = self.process(run, claim, head=head,
                                   deployed=claim.get("deployed_commit") or deployed)
        except Exception as exc:  # noqa: BLE001 - report it rather than leave the run hanging
            self.log.write(f"run failed unexpectedly: {exc!r}")
            outcome = self.finish("failed", "the gate stopped unexpectedly",
                                  run.get("commit_sha"), [], [], unfinished=[
                                      "部署门这次中途出错，没有上线；下一次合并会重新检查。"])
        finally:
            for suffix in ("src", "old"):
                path = self.config.workdir / "runs" / f"{run['id']}-{suffix}"
                if path.exists():
                    self.git("worktree", "remove", "--force", str(path))
        report = {
            "host": self.config.host, "status": outcome.status,
            "status_reason": outcome.reason, "commit_sha": outcome.commit,
            "checks": [check.out() for check in outcome.checks], "deploy": outcome.deploy,
            "rolled_back": outcome.rolled_back, "report": outcome.report,
            "log_ref": str(self.log.path) if self.log.path else None}
        # An older server rejects fields it does not know; send only what it accepts.
        if "task_keys" in (state.get("runner_fields") or []):
            report["task_keys"] = outcome.task_keys
        self.api.call(f"/runs/{run['id']}", report)
        return {"run": run["id"], "status": outcome.status}

    def process(self, run: dict[str, Any], claim: dict[str, Any], *, head: str | None,
                deployed: str | None) -> Outcome:
        commit = run.get("commit_sha") or head
        manual = run.get("trigger_kind") == "manual"
        if not commit:
            return self.finish("failed", "the branch could not be read", None, [], [],
                               unfinished=["门没能读取代码仓库，这次没有部署。"])
        if commit == deployed and not manual:
            return self.finish("skipped", "already deployed", commit, [], [])
        checks: list[Check] = []
        pulls: list[dict[str, Any]] = []
        g01 = self.check_merge(commit, deployed, pulls)
        checks.append(g01)
        if g01.ok:
            checks.append(self.check_tests(commit, run["id"]))
            checks.append(self.check_quality(commit, deployed, pulls, run["id"]))
        failed = [check for check in checks if not check.ok]
        if failed:
            return self.finish("blocked", f"{failed[0].id} failed", commit, checks, pulls,
                               unfinished=[f"{CHECK_PLAIN[c.id]}没有通过，所以没有上线。"
                                           for c in failed])
        # Re-read the switches: a pause or revoke during the checks still wins.
        state = self.api.call("/heartbeat", {"host": self.config.host, "version": VERSION,
                                             "deployed_commit": deployed})
        if state.get("paused"):
            return self.finish("skipped", "the gate was paused during the checks", commit,
                               checks, pulls, unfinished=["部署门已暂停，这次没有上线。"])
        # The server combines the operator's authorization with the board owner's
        # switch (3.17); an older server only reports the operator's.
        effective = state.get("auto_deploy_effective", state.get("auto_deploy_authorized"))
        if not (manual or (claim.get("deploy_allowed") and effective)):
            if state.get("auto_deploy_blocked_by") == "owner":
                return self.finish("blocked", "the owner does not allow automatic deploys",
                                   commit, checks, pulls, unfinished=[
                                       "检查都通过了，但 owner 还没有允许自动上线，所以没有上线。"
                                       "在控制台“治理”页打开“允许自动上线”（且不是“每一步都问我”），"
                                       "或在部署门页面点“立即部署”。"])
            return self.finish("blocked", "automatic deploys are not authorized", commit,
                               checks, pulls, unfinished=[
                                   "检查都通过了，但自动部署还没有授权，所以没有上线。"
                                   "在部署门页面点“立即部署”或“授权自动部署”即可。"])
        g04, stamp = self.prepare()
        checks.append(g04)
        if not g04.ok:
            return self.finish("blocked", "G-04 failed", commit, checks, pulls,
                               unfinished=["上线前的数据备份没有做成，为了安全没有上线。"])
        g05, rolled_back = self.deploy(commit, deployed, stamp)
        checks.append(g05)
        deploy = {"stamp": stamp or "", "result": "ok" if g05.ok else "failed",
                  "app_version": self.app_version(self.file_at(commit, "app/config.py")) or ""}
        if not g05.ok:
            return self.finish("rolled_back" if rolled_back else "failed", "G-05 failed",
                               commit, checks, pulls, deploy=deploy, rolled_back=rolled_back,
                               unfinished=["新版本上线后自检没通过，已经自动退回上一个版本，"
                                           "数据没有动。" if rolled_back else
                                           "新版本上线后自检没通过，自动退回也没有成功，需要人看一下。"])
        return self.finish("deployed", "deployed", commit, checks, pulls, deploy=deploy)

    def finish(self, status: str, reason: str, commit: str | None, checks: list[Check],
               pulls: list[dict[str, Any]], *, deploy: dict[str, str] | None = None,
               rolled_back: bool = False, unfinished: list[str] | None = None) -> Outcome:
        self.log.write(f"outcome: {status} ({reason})")
        unfinished = list(unfinished or [])
        if self.quarantined:
            unfinished.append(f"有 {self.quarantined} 项已知问题这次没有纳入自动测试，"
                              "还需要修好。")
        return Outcome(status, reason, commit, checks, deploy or {}, rolled_back,
                       owner_report(pulls, deployed=status == "deployed",
                                    rolled_back=rolled_back, unfinished=unfinished),
                       pull_task_keys(pulls))

    # -------------------------------------------------------------- G-01

    def check_merge(self, commit: str, deployed: str | None,
                    pulls: list[dict[str, Any]]) -> Check:
        branch = f"origin/{self.config.branch}"
        if not self.is_ancestor(commit, branch):
            return Check("G-01", False, f"the commit is not on {self.config.branch}")
        if deployed and not self.is_ancestor(deployed, commit):
            return Check("G-01", False, "the commit is not newer than production")
        commits = self.first_parent_range(deployed, commit)
        if commits is None:
            return Check("G-01", False, "the commit range could not be read")
        if len(commits) > MAX_RANGE:
            return Check("G-01", False, f"{len(commits)} commits since production; "
                                        f"the gate checks at most {MAX_RANGE}")
        unmerged = []
        for sha in commits:
            try:
                found = [pull for pull in self.github.pulls_for_commit(sha)
                         if pull.get("merged_at")
                         and (pull.get("base") or {}).get("ref") == self.config.branch]
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.log.write(f"pull request lookup failed for {sha}: {exc}")
                return Check("G-01", False, "merge evidence could not be read from GitHub")
            if found:
                for pull in found:
                    if all(p.get("number") != pull.get("number") for p in pulls):
                        pulls.append(pull)
            else:
                unmerged.append(sha[:12])
        if unmerged and not self.config.allow_direct_push:
            return Check("G-01", False, "commits without a merged pull request: "
                                        + ", ".join(unmerged[:5]))
        return Check("G-01", True, f"{len(commits)} commit(s), {len(pulls)} merged pull "
                                   f"request(s)" + (", direct pushes allowed" if unmerged else ""))

    # -------------------------------------------------------------- G-02

    def worktree(self, rev: str, name: str) -> Path | None:
        path = self.config.workdir / "runs" / name
        if path.exists():
            self.git("worktree", "remove", "--force", str(path))
        self.git("worktree", "prune")
        return path if self.git("worktree", "add", "--detach", str(path), rev).ok else None

    def check_tests(self, commit: str, run_id: str) -> Check:
        src = self.worktree(commit, f"{run_id}-src")
        if src is None:
            return Check("G-02", False, "the commit could not be checked out")
        quarantine = read_quarantine(src)
        self.quarantined = len(quarantine)
        deselect = " ".join(f"--deselect {shlex.quote(item)}" for item in quarantine)
        result = self.in_isolation(src, None, run_id, (
            "pip install -q -r requirements-dev.txt && ruff check app tests && "
            f"pytest -q -p no:cacheprovider --junitxml=/src/.gate-junit.xml {deselect}"))
        summary = junit_summary(src / ".gate-junit.xml")
        if not result.ok:
            return Check("G-02", False, "tests or lint failed: " + (summary or "see the log"))
        if self.config.run_node_checks:
            node = self.shell.run([
                "docker", "run", "--rm", "-v", f"{src}:/src", "-w", "/src",
                self.config.node_image, "sh", "-c", hand_back(
                    "npm test --prefix cli && npm ci --prefix protocol "
                    "&& npm test --prefix protocol"),
            ], timeout=self.config.test_timeout)
            if not node.ok:
                return Check("G-02", False, "cli or protocol tests failed")
        extra = f"; {len(quarantine)} quarantined" if quarantine else ""
        return Check("G-02", True, (summary or "tests passed") + extra)

    def in_isolation(self, src: Path, old: Path | None, run_id: str, script: str) -> Result:
        """Run ``script`` in a python container next to a throwaway pgvector database.

        Both containers sit on their own docker network and publish no ports,
        so nothing here can reach the production database or listen on the
        production ports (R-04).
        """
        name = f"veynod-gate-{run_id[-10:].lower()}-{uuid.uuid4().hex[:6]}"
        db = f"{name}-db"
        dsn = f"postgresql+asyncpg://gate:gate@{db}:5432/gate"
        try:
            if not self.shell.run(["docker", "network", "create", name]).ok:
                return Result(1, "network create failed")
            if not self.shell.run(["docker", "run", "-d", "--name", db, "--network", name,
                                   "-e", "POSTGRES_USER=gate", "-e", "POSTGRES_PASSWORD=gate",
                                   "-e", "POSTGRES_DB=gate", self.config.pg_image]).ok:
                return Result(1, "database container failed")
            for _ in range(60):
                if self.shell.run(["docker", "exec", db, "pg_isready", "-U", "gate",
                                   "-d", "gate"], timeout=30).ok:
                    break
                time.sleep(2)
            else:
                return Result(1, "database did not become ready")
            runner = Path(__file__).resolve()
            mounts = ["-v", f"{src}:/src", "-v", f"{runner}:/gate/deploy_gate.py:ro"]
            if old:
                mounts += ["-v", f"{old}:/old:ro"]
            return self.shell.run([
                "docker", "run", "--rm", "--network", name, *mounts, "-w", "/src",
                "-v", "veynod-gate-pip:/root/.cache/pip",
                "-e", f"TEST_DATABASE_URL={dsn}", "-e", f"GATE_DSN={dsn}",
                self.config.python_image, "sh", "-c", hand_back(script),
            ], timeout=self.config.test_timeout)
        finally:
            self.shell.run(["docker", "rm", "-f", db])
            self.shell.run(["docker", "network", "rm", name])

    # -------------------------------------------------------------- G-03

    def check_quality(self, commit: str, deployed: str | None,
                      pulls: list[dict[str, Any]], run_id: str) -> Check:
        problems = []
        if deployed:
            changed = self.git("diff", "--name-status", deployed, commit, "--", "sql/migrations")
            if not changed.ok:
                problems.append("migration changes could not be read")
            else:
                edited = [line.split()[-1] for line in changed.output.splitlines()
                          if line.strip() and not line.startswith("A")]
                if edited:
                    problems.append("shipped migrations were edited: " + ", ".join(edited[:3]))
            old = self.schema_version(self.file_at(deployed, "app/database.py"))
            new = self.schema_version(self.file_at(commit, "app/database.py"))
            if old and new and new < old:
                problems.append("the schema version went backwards")
            if not problems:
                upgrade = self.check_upgrade(commit, deployed, run_id)
                if upgrade:
                    problems.append(upgrade)
        task_problem = self.check_tasks(pulls)
        if task_problem:
            problems.append(task_problem)
        if problems:
            return Check("G-03", False, "; ".join(problems))
        return Check("G-03", True, "migrations upgrade cleanly, row security kept, "
                                   "tasks approved")

    def check_upgrade(self, commit: str, deployed: str, run_id: str) -> str | None:
        src = self.config.workdir / "runs" / f"{run_id}-src"
        old = self.worktree(deployed, f"{run_id}-old")
        if old is None or not src.exists():
            return "the upgrade rehearsal could not be prepared"
        result = self.in_isolation(src, old, run_id, (
            "pip install -q -r requirements.txt >/dev/null && "
            "cd /old && python -c \"import asyncio,os; from app.database import apply_schema; "
            "asyncio.run(apply_schema(os.environ['GATE_DSN']))\" && "
            "python /gate/deploy_gate.py rls-snapshot > /src/.gate-rls-before.json && "
            "cd /src && python -c \"import asyncio,os; from app.database import apply_schema; "
            "asyncio.run(apply_schema(os.environ['GATE_DSN']))\" && "
            "python /gate/deploy_gate.py rls-snapshot --expect-current "
            "> /src/.gate-rls-after.json"))
        if not result.ok:
            return "the upgrade from production did not apply cleanly"
        try:
            before = json.loads((src / ".gate-rls-before.json").read_text(encoding="utf-8"))
            after = json.loads((src / ".gate-rls-after.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "the upgrade rehearsal left no result"
        lost = sorted(set(before["secured"]) - set(after["secured"]))
        if lost:
            return "row security was lost on: " + ", ".join(lost[:5])
        # Some operator tables carry tenant_id without RLS by design (the app role
        # has no grant); only a table that newly lacks row security is a regression.
        exposed = sorted(set(after["unsecured_tenant_tables"])
                         - set(before["unsecured_tenant_tables"]))
        if exposed:
            return "new tenant tables without row security: " + ", ".join(exposed[:5])
        if not after["version_applied"] or not after["fingerprint_applied"]:
            return "the schema version or fingerprint was not recorded"
        return None

    def check_tasks(self, pulls: list[dict[str, Any]]) -> str | None:
        keys = pull_task_keys(pulls)
        if not keys:
            return "no merged pull request names a board task" if self.config.require_task \
                else None
        evidence = self.api.call("/task-evidence", {"task_keys": keys})
        if not evidence.get("configured"):
            return "the board workspace is not configured on the server"
        missing = []
        for task in evidence.get("tasks", []):
            approved = task.get("verdict") == "APPROVE" or task.get("status") == "DONE"
            if not task.get("found") or not approved or task.get("failing_gates"):
                missing.append(task.get("task_key"))
        if missing:
            return "tasks not approved or with failing gates: " + ", ".join(missing[:5])
        return None

    # -------------------------------------------------------- G-04, G-05

    def deploy_script(self, *args: str, env: dict[str, str] | None = None,
                      timeout: int = 1800) -> Result:
        return self.shell.run(["scripts/deploy_tlines.sh", *args], cwd=self.config.deploy_dir,
                              env={"PUBLIC_URL": self.config.public_url, **(env or {})},
                              timeout=timeout)

    def prepare(self) -> tuple[Check, str | None]:
        result = self.deploy_script("prepare")
        match = re.findall(r"stamp: (\d{8}T\d{6}Z)", result.output)
        if not result.ok or not match:
            return Check("G-04", False, "backup or rollback tag failed"), None
        return Check("G-04", True, "backup checked and running image tagged"), match[-1]

    def deploy(self, commit: str, deployed: str | None, stamp: str | None) -> tuple[Check, bool]:
        status = self.git("status", "--porcelain", "--untracked-files=no",
                          cwd=self.config.deploy_dir)
        if not status.ok or status.output.strip():
            return Check("G-05", False, "the production checkout has local changes"), False
        if not self.git("fetch", "origin", self.config.branch, cwd=self.config.deploy_dir,
                        timeout=900).ok or \
                not self.git("checkout", "--detach", commit, cwd=self.config.deploy_dir).ok:
            return Check("G-05", False, "the production checkout could not move"), False
        schema = self.schema_version(self.file_at(commit, "app/database.py"))
        env = {"DEPLOY_STAMP": stamp or ""}
        if schema:
            env["EXPECTED_SCHEMA"] = ".".join(str(part) for part in schema)
        result = self.deploy_script("deploy", env=env, timeout=self.config.deploy_timeout)
        if not result.ok:
            # deploy_tlines.sh already rolled the image back; match the checkout to it.
            self.restore_checkout(deployed)
            rolled = ("rolling back the image only" in result.output
                      and "image rollback did not become ready" not in result.output)
            return Check("G-05", False, "deploy script failed its own verification"), rolled
        failures = self.smoke(self.app_version(self.file_at(commit, "app/config.py")))
        if failures:
            rolled = self.deploy_script("rollback-image", stamp or "").ok
            self.restore_checkout(deployed)
            return Check("G-05", False, "smoke test failed: " + ", ".join(failures)), rolled
        return Check("G-05", True, "deployed and smoke test passed"), False

    def restore_checkout(self, deployed: str | None) -> None:
        if deployed:
            self.git("checkout", "--detach", deployed, cwd=self.config.deploy_dir)

    def smoke(self, version: str | None) -> list[str]:
        base = self.config.public_url
        failures = []
        code, _ = self.http(f"{base}/readyz")
        if code != 200:
            failures.append(f"readyz {code}")
        code, body = self.http(f"{base}/healthz")
        try:
            live = json.loads(body).get("version") if code == 200 else None
        except ValueError:
            live = None
        if version and live != version:
            failures.append(f"version {live} != {version}")
        code, _ = self.http(f"{base}/docs/")
        if code != 200:
            failures.append(f"docs {code}")
        code, _ = self.http(f"{base}/mcp/", method="POST", body=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "deploy-gate", "version": VERSION}}}).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        if code not in (200, 401):
            failures.append(f"mcp {code}")
        return failures


def hand_back(script: str) -> str:
    """Containers run as root; give the files they wrote back to the runner's user."""
    return f"{script}; status=$?; chown -R {os.getuid()}:{os.getgid()} /src; exit $status"


# ------------------------------------------------------------- owner report


def _section(body: str, heading: re.Pattern[str]) -> list[str]:
    match = heading.search(body or "")
    if not match:
        return []
    rest = body[match.end():]
    end = re.search(r"^#+\s", rest, re.MULTILINE)
    lines = []
    for line in (rest[:end.start()] if end else rest).splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
        if line:
            lines.append(line)
    return lines


def pull_task_keys(pulls: list[dict[str, Any]]) -> list[str]:
    """The board tasks the merged pull requests name, in order, once each."""
    keys: list[str] = []
    for pull in pulls:
        text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
        keys += TASK_KEY.findall(text)
    return list(dict.fromkeys(keys))[:20]


def owner_report(pulls: list[dict[str, Any]], *, deployed: bool, rolled_back: bool,
                 unfinished: list[str]) -> dict[str, Any]:
    """Three plain answers for the owner: what changed, how to try it, what is
    not done. Commit ids, schema numbers and test counts stay in the log (A-08)."""
    changed: list[str] = []
    how: list[str] = []
    for pull in pulls[:10]:
        body = pull.get("body") or ""
        changed += _section(body, WHAT_CHANGED)[:3] or [str(pull.get("title") or "").strip()]
        how += _section(body, HOW_TO_TRY)[:3]
    changed = [line for line in dict.fromkeys(changed) if line][:10]
    if deployed and not how:
        how = ["打开 veynod.com 照常使用，确认页面能打开、刚才的改动已经出现。"]
    if not deployed:
        how = []
    return {"changed": changed, "how_to_try": list(dict.fromkeys(how))[:10],
            "rolled_back": rolled_back, "unfinished": unfinished[:10]}


def read_quarantine(src: Path) -> list[str]:
    """Tests the repository itself marks as known-broken (deploy/gate-quarantine.txt).

    The list is versioned and reviewed like code, and every entry is named in
    the owner report as not done, so a skipped test is never silent.
    """
    path = src / "deploy" / "gate-quarantine.txt"
    if not path.is_file():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("tests/"):
            items.append(line)
    return items


def junit_summary(path: Path) -> str:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return ""
    suites = [root] if root.tag == "testsuite" else list(root)
    tests = sum(int(suite.get("tests", 0)) for suite in suites)
    failed = sum(int(suite.get("failures", 0)) + int(suite.get("errors", 0)) for suite in suites)
    names = [case.get("name", "") for case in root.iter("testcase")
             if any(child.tag in ("failure", "error") for child in case)]
    text = f"{tests - failed}/{tests} tests passed"
    return text + (": " + ", ".join(names[:3]) if names else "")


# ------------------------------------------------------- inside the container


def rls_snapshot(expect_current: bool) -> int:
    """Printed as JSON inside the rehearsal container (needs the app's deps)."""
    import asyncio

    import asyncpg

    async def snapshot() -> dict[str, Any]:
        dsn = os.environ["GATE_DSN"].replace("postgresql+asyncpg://", "postgresql://", 1)
        conn = await asyncpg.connect(dsn)
        try:
            secured = [row["relname"] for row in await conn.fetch(
                "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relrowsecurity")]
            unsecured = [row["table_name"] for row in await conn.fetch(
                "SELECT DISTINCT c.table_name FROM information_schema.columns c "
                "JOIN pg_class p ON p.relname = c.table_name "
                "JOIN pg_namespace n ON n.oid = p.relnamespace AND n.nspname = 'public' "
                "WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id' "
                "AND p.relkind IN ('r', 'p') AND p.relispartition = false "
                "AND NOT (p.relrowsecurity AND p.relforcerowsecurity)")]
            result: dict[str, Any] = {"secured": sorted(secured),
                                      "unsecured_tenant_tables": sorted(unsecured)}
            if expect_current:
                sys.path.insert(0, os.getcwd())
                from app.database import CURRENT_SCHEMA_VERSION, _schema_fingerprint

                result["version_applied"] = bool(await conn.fetchval(
                    "SELECT 1 FROM schema_migrations WHERE version = $1",
                    CURRENT_SCHEMA_VERSION))
                result["fingerprint_applied"] = bool(await conn.fetchval(
                    "SELECT 1 FROM schema_migrations WHERE version = $1",
                    _schema_fingerprint(Path(os.getcwd()))))
            return result
        finally:
            await conn.close()

    print(json.dumps(asyncio.run(snapshot())))
    return 0


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="one gate cycle (the systemd timer runs this)")
    run.add_argument("--deploy-dir", default=None)
    sub.add_parser("status", help="print the gate state from the API").add_argument(
        "--deploy-dir", default=None)
    snap = sub.add_parser("rls-snapshot", help=argparse.SUPPRESS)
    snap.add_argument("--expect-current", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "rls-snapshot":
        return rls_snapshot(args.expect_current)

    config = Config.load(args.deploy_dir)
    if args.command == "status":
        log = Log(None, config.secrets)
        api = Api(config)
        print(json.dumps(api.call("/heartbeat", {"host": config.host, "version": VERSION}),
                         indent=2, ensure_ascii=False))
        return 0
    config.workdir.mkdir(parents=True, exist_ok=True)
    with (config.workdir / "gate.lock").open("w") as lock:
        try:
            if fcntl is None:
                print("the deploy gate runner needs fcntl (Linux)")
                return 1
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another gate cycle is running")
            return 0
        log = Log(config.log_dir / "gate.log", config.secrets)
        gate = Gate(config, shell=Shell(log), api=Api(config), github=GitHub(config), log=log)
        try:
            result = gate.cycle()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.write(f"cycle failed: {exc}")
            print(log.redact(f"gate cycle failed: {exc}"), file=sys.stderr)
            return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
