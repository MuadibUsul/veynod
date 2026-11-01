"""The VPS deploy gate runner's decisions (scripts/deploy_gate.py).

agentgraph/spec-vps-auto-deploy-gate v1: A-02 (G-01..G-04 all pass before the
deploy script runs; any failure leaves production alone and names the check),
A-03 (deploy_tlines.sh does the backup and rollback tag; a failed backup
stops), A-04 (a failed smoke test rolls the image back, never the database),
A-07 (no GitHub Actions: local evidence only) and A-08 (the owner report has
no technical detail). Every command goes through a fake shell, so nothing here
touches docker, git remotes or the network.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("deploy_gate", ROOT / "scripts" / "deploy_gate.py")
gate_module = importlib.util.module_from_spec(_spec)
sys.modules["deploy_gate"] = gate_module  # dataclasses resolve their module
_spec.loader.exec_module(gate_module)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the deploy gate runner is a Linux-only VPS program (fcntl, POSIX paths)",
)

DEPLOYED = "a" * 40
CANDIDATE = "b" * 40
MIDDLE = "c" * 40
STAMP = "20261028T101010Z"
DATABASE_OLD = '("3.49.0", _MIGRATIONS_DIR / "v3_49_blob_tenants.sql"),\n]'
DATABASE_NEW = DATABASE_OLD.replace("]", '("3.50.0", _MIGRATIONS_DIR / "v3_50_x.sql"),\n]')
PR_BODY = ("Board task `gate-task`\n\n## 哪里变了\n- 任务看板显示谁在线\n\n"
           "## 怎么试\n1. 打开控制台的任务看板\n2. 看每张卡片右上角的小圆点\n")


class FakeShell:
    def __init__(self, workdir: Path, **overrides: Any):
        self.workdir = workdir
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.ancestors = {(CANDIDATE, "origin/main"), (DEPLOYED, CANDIDATE)}
        self.first_parent = [CANDIDATE, MIDDLE]
        self.migration_diff = "A\tsql/migrations/v3_50_x.sql\n"
        self.tests_pass = True
        self.prepare_ok = True
        self.deploy_ok = True
        self.lost_rls: list[str] = []
        self.new_unsecured: list[str] = []
        self.porcelain = ""
        self.__dict__.update(overrides)

    def run(self, cmd: list[str], *, cwd: Path | None = None,
            env: dict[str, str] | None = None, timeout: int = 600):
        self.calls.append(cmd)
        self.envs.append(env or {})
        ok, out = True, ""
        joined = " ".join(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            out = DEPLOYED if cmd[2] == "HEAD" else CANDIDATE
        elif cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            ok = (cmd[3], cmd[4]) in self.ancestors
        elif cmd[:3] == ["git", "rev-list", "--first-parent"]:
            out = "\n".join(self.first_parent)
        elif cmd[:2] == ["git", "show"]:
            rev, path = cmd[2].split(":", 1)
            if path == "app/database.py":
                out = DATABASE_OLD if rev == DEPLOYED else DATABASE_NEW
            elif path == "app/config.py":
                out = 'app_version: str = "3.14.0"'
        elif cmd[:3] == ["git", "diff", "--name-status"]:
            out = self.migration_diff
        elif cmd[:3] == ["git", "worktree", "add"]:
            Path(cmd[4]).mkdir(parents=True, exist_ok=True)
        elif cmd[:2] == ["git", "status"]:
            out = self.porcelain
        elif cmd[:2] == ["docker", "run"] and "--rm" in cmd:
            script = cmd[-1]
            src = Path(next(part.split(":")[0] for part in cmd if part.endswith(":/src")))
            if "pytest" in script:
                ok = self.tests_pass
                failures = 0 if ok else 1
                (src / ".gate-junit.xml").write_text(
                    f'<testsuites><testsuite tests="418" failures="{failures}" errors="0">'
                    + ('<testcase name="test_x"><failure/></testcase>' if failures else "")
                    + "</testsuite></testsuites>", encoding="utf-8")
            elif "apply_schema" in script:
                before = ["shared_documents", "collab_tasks"]
                after = [name for name in before if name not in self.lost_rls]
                legacy = ["tenant_policies"]  # operator table: tenant_id, no RLS by design
                (src / ".gate-rls-before.json").write_text(json.dumps(
                    {"secured": before, "unsecured_tenant_tables": legacy}), encoding="utf-8")
                (src / ".gate-rls-after.json").write_text(json.dumps(
                    {"secured": after, "unsecured_tenant_tables": legacy + self.new_unsecured,
                     "version_applied": True, "fingerprint_applied": True}), encoding="utf-8")
        elif "deploy_tlines.sh" in joined:
            if cmd[1] == "prepare":
                ok = self.prepare_ok
                out = f"[deploy] prepared. stamp: {STAMP}" if ok else "[deploy] backup is empty"
            elif cmd[1] == "deploy":
                ok = self.deploy_ok
                out = "" if ok else ("[deploy] deployment failed verification\n"
                                     "[deploy] rolling back the image only; the database "
                                     "is preserved")
        return gate_module.Result(0 if ok else 1, out)

    def commands(self, needle: str) -> list[list[str]]:
        return [cmd for cmd in self.calls if needle in " ".join(cmd)]


class FakeApi:
    def __init__(self, *, paused: bool = False, authorized: bool = True,
                 tasks: list[dict[str, Any]] | None = None, run: dict[str, Any] | None = None,
                 deploy_allowed: bool = True, owner_allows: bool | None = None,
                 runner_fields: list[str] | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.paused = paused
        self.authorized = authorized
        self.owner_allows = owner_allows  # None: a server from before 3.17
        self.tasks = tasks if tasks is not None else [
            {"task_key": "gate-task", "found": True, "status": "REVIEWING",
             "verdict": "APPROVE", "failing_gates": []}]
        self.run = run
        self.deploy_allowed = deploy_allowed
        self.runner_fields = runner_fields  # None: a server from before 3.18

    def call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, body))
        if path == "/heartbeat":
            state = {"paused": self.paused, "auto_deploy_authorized": self.authorized,
                     "deployed_commit": DEPLOYED}
            if self.owner_allows is not None:
                effective = self.authorized and self.owner_allows
                state.update(auto_deploy_effective=effective, auto_deploy_blocked_by=None
                             if effective else ("operator" if not self.authorized else "owner"))
            if self.runner_fields is not None:
                state["runner_fields"] = self.runner_fields
            return state
        if path == "/claim":
            return {"run": self.run, "deploy_allowed": self.deploy_allowed,
                    "deployed_commit": DEPLOYED}
        if path == "/task-evidence":
            return {"configured": True, "tasks": self.tasks}
        return {"status": "queued"}


class FakeGitHub:
    def __init__(self, merged: dict[str, list[dict[str, Any]]] | None = None):
        default = [{"number": 41, "merged_at": "2026-10-28T00:00:00Z", "base": {"ref": "main"},
                    "title": "Show presence on the task board", "body": PR_BODY}]
        self.merged = merged if merged is not None else {CANDIDATE: default, MIDDLE: default}

    def pulls_for_commit(self, sha: str) -> list[dict[str, Any]]:
        return self.merged.get(sha, [])


def smoke_ok(url: str, **_: Any) -> tuple[int, str]:
    if url.endswith("/healthz"):
        return 200, json.dumps({"status": "ok", "version": "3.14.0"})
    if url.endswith("/mcp/"):
        return 401, ""
    return 200, "ok"


def smoke_bad(url: str, **_: Any) -> tuple[int, str]:
    if url.endswith("/healthz"):
        return 200, json.dumps({"status": "ok", "version": "3.13.2"})  # old image still live
    return smoke_ok(url)


def make(tmp_path: Path, *, shell: FakeShell | None = None, api: FakeApi | None = None,
         github: FakeGitHub | None = None, http=smoke_ok, **config: Any):
    cfg = gate_module.Config(deploy_dir=tmp_path / "deploy", api="https://example.test",
                             runner_token="runner-secret-value", workdir=tmp_path / "work",
                             log_dir=tmp_path / "logs", public_url="https://example.test",
                             host="vps-test", secrets=["runner-secret-value"], **config)
    log = gate_module.Log(tmp_path / "logs" / "cycle.log", cfg.secrets)
    shell = shell or FakeShell(tmp_path)
    api = api or FakeApi()
    gate = gate_module.Gate(cfg, shell=shell, api=api, github=github or FakeGitHub(), log=log,
                            http=http)
    return gate, shell, api


def run_once(gate, kind: str = "webhook", commit: str | None = CANDIDATE):
    run = {"id": "dgr_TEST", "trigger_kind": kind, "commit_sha": commit}
    return gate.process(run, {"deploy_allowed": True, "deployed_commit": DEPLOYED},
                        head=CANDIDATE, deployed=DEPLOYED)


def failed(outcome) -> list[str]:
    return [check.id for check in outcome.checks if not check.ok]


def test_all_checks_pass_then_the_deploy_script_runs_on_the_prepared_stamp(tmp_path):
    gate, shell, api = make(tmp_path)
    outcome = run_once(gate)
    assert outcome.status == "deployed", outcome
    assert [check.id for check in outcome.checks] == ["G-01", "G-02", "G-03", "G-04", "G-05"]
    assert all(check.ok for check in outcome.checks)
    order = [" ".join(cmd) for cmd in shell.calls]
    prepare = next(i for i, cmd in enumerate(order) if cmd.endswith("deploy_tlines.sh prepare"))
    deploy = next(i for i, cmd in enumerate(order) if cmd.endswith("deploy_tlines.sh deploy"))
    tests = next(i for i, cmd in enumerate(order) if "--junitxml" in cmd)
    assert tests < prepare < deploy  # checks first, then backup + tag, then deploy (A-02, A-03)
    env = shell.envs[deploy]
    assert env["DEPLOY_STAMP"] == STAMP and env["EXPECTED_SCHEMA"] == "3.50.0"
    assert ["git", "checkout", "--detach", CANDIDATE] in shell.calls
    assert not shell.commands("rollback")
    assert outcome.deploy["stamp"] == STAMP
    # A-07: the only GitHub traffic is the merge-evidence lookup; nothing asks for CI.
    assert not any("actions" in " ".join(cmd) for cmd in shell.calls)


def test_tests_run_isolated_from_production(tmp_path):
    gate, shell, _ = make(tmp_path)
    run_once(gate)
    for cmd in shell.commands("docker run"):
        assert "-p" not in cmd and "--publish" not in cmd  # no published ports (R-04)
        joined = " ".join(cmd)
        assert "agentgraph-api" not in joined and "@db:" not in joined
    network = shell.commands("docker network create")
    assert network and all(cmd[-1].startswith("veynod-gate-") for cmd in network)
    assert shell.commands("docker network rm") and shell.commands("docker rm -f")


@pytest.mark.parametrize("change,check,reason", [
    ({"ancestors": {(DEPLOYED, CANDIDATE)}}, "G-01", "not on main"),
    ({"ancestors": {(CANDIDATE, "origin/main")}}, "G-01", "older than production"),
    ({"tests_pass": False}, "G-02", "a failing test"),
    ({"migration_diff": "M\tsql/migrations/v3_49_blob_tenants.sql\n"}, "G-03",
     "an edited shipped migration"),
    ({"lost_rls": ["collab_tasks"]}, "G-03", "a table losing row security"),
    ({"new_unsecured": ["leaky_table"]}, "G-03", "a new tenant table without row security"),
    ({"prepare_ok": False}, "G-04", "a failed backup"),
])
def test_any_failing_check_leaves_production_alone(tmp_path, change, check, reason):
    gate, shell, _ = make(tmp_path, shell=FakeShell(tmp_path, **change))
    outcome = run_once(gate)
    assert outcome.status == "blocked", reason
    assert failed(outcome) == [check], reason
    assert outcome.reason == f"{check} failed"
    assert not shell.commands("deploy_tlines.sh deploy"), reason
    assert ["git", "checkout", "--detach", CANDIDATE] not in shell.calls
    assert outcome.report["unfinished"], reason


def test_a_commit_without_a_merged_pull_request_is_refused(tmp_path):
    github = FakeGitHub({CANDIDATE: FakeGitHub().merged[CANDIDATE]})  # MIDDLE was pushed directly
    gate, shell, _ = make(tmp_path, github=github)
    outcome = run_once(gate)
    assert failed(outcome) == ["G-01"] and "without a merged pull request" in \
        outcome.checks[0].summary
    assert not shell.commands("--junitxml")
    allowed, shell, _ = make(tmp_path / "allowed", github=github, allow_direct_push=True)
    assert run_once(allowed).status == "deployed"


def test_tasks_must_be_approved_without_failing_gates(tmp_path):
    rejected = FakeApi(tasks=[{"task_key": "gate-task", "found": True, "status": "REVIEWING",
                               "verdict": "REJECT", "failing_gates": []}])
    gate, shell, _ = make(tmp_path, api=rejected)
    assert failed(run_once(gate)) == ["G-03"]
    gates = FakeApi(tasks=[{"task_key": "gate-task", "found": True, "status": "DONE",
                            "verdict": "APPROVE", "failing_gates": ["TESTS"]}])
    gate, _, _ = make(tmp_path / "gates", api=gates)
    assert failed(run_once(gate)) == ["G-03"]
    unnamed = FakeGitHub({sha: [{**pulls[0], "body": "no task here"}]
                          for sha, pulls in FakeGitHub().merged.items()})
    gate, _, _ = make(tmp_path / "unnamed", github=unnamed)
    outcome = run_once(gate)
    assert failed(outcome) == ["G-03"] and "names a board task" in outcome.checks[-1].summary


def test_a_failed_smoke_test_rolls_back_the_image_and_never_the_database(tmp_path):
    gate, shell, _ = make(tmp_path, http=smoke_bad)
    outcome = run_once(gate)
    assert outcome.status == "rolled_back" and outcome.rolled_back is True
    assert failed(outcome) == ["G-05"]
    assert shell.commands("deploy_tlines.sh rollback-image")[0][-1] == STAMP
    assert not [cmd for cmd in shell.calls if cmd[-2:-1] == ["rollback"]
                or (len(cmd) > 1 and cmd[1] == "rollback")]  # no database restore
    assert ["git", "checkout", "--detach", DEPLOYED] in shell.calls  # tree matches the image
    assert outcome.report["rolled_back"] is True


def test_a_deploy_script_failure_is_reported_as_its_image_rollback(tmp_path):
    gate, shell, _ = make(tmp_path, shell=FakeShell(tmp_path, deploy_ok=False))
    outcome = run_once(gate)
    assert outcome.status == "rolled_back" and failed(outcome) == ["G-05"]
    assert not shell.commands("rollback-image")  # the script already did it
    assert ["git", "checkout", "--detach", DEPLOYED] in shell.calls


def test_a_failed_image_rollback_is_not_reported_as_rolled_back(tmp_path):
    shell = FakeShell(tmp_path, deploy_ok=False)
    original = shell.run

    def run(cmd, **kwargs):
        result = original(cmd, **kwargs)
        if cmd[-1:] == ["deploy"] and "deploy_tlines.sh" in cmd[0]:
            return gate_module.Result(1, result.output + "\n[deploy] image rollback did not "
                                         "become ready; inspect the log")
        return result

    shell.run = run
    gate, _, _ = make(tmp_path, shell=shell)
    outcome = run_once(gate)
    assert outcome.status == "failed" and outcome.rolled_back is False
    assert any("需要人看一下" in line for line in outcome.report["unfinished"])


def test_an_unexpected_error_reports_the_run_failed(tmp_path):
    claimed = FakeApi(run={"id": "dgr_ERR", "trigger_kind": "poll", "commit_sha": CANDIDATE})
    github = FakeGitHub()
    github.pulls_for_commit = lambda sha: (_ for _ in ()).throw(RuntimeError("boom"))
    gate, shell, _ = make(tmp_path, api=claimed, github=github)
    assert gate.cycle() == {"run": "dgr_ERR", "status": "failed"}
    path, body = claimed.calls[-1]
    assert path == "/runs/dgr_ERR" and body["status"] == "failed"
    assert body["report"]["unfinished"] and not shell.commands("deploy_tlines.sh")


def test_switches_win_over_passing_checks(tmp_path):
    gate, shell, _ = make(tmp_path, api=FakeApi(authorized=False))
    outcome = run_once(gate, kind="webhook")
    assert outcome.status == "blocked" and outcome.reason == "automatic deploys are not authorized"
    assert all(check.ok for check in outcome.checks)
    assert not shell.commands("deploy_tlines.sh")
    manual, shell, _ = make(tmp_path / "manual", api=FakeApi(authorized=False))
    assert run_once(manual, kind="manual").status == "deployed"
    paused, shell, _ = make(tmp_path / "paused", api=FakeApi(paused=True))
    outcome = run_once(paused, kind="manual")
    assert outcome.status == "skipped" and not shell.commands("deploy_tlines.sh")


def test_the_owner_switch_stops_automatic_deploys(tmp_path):
    gate, shell, _ = make(tmp_path, api=FakeApi(owner_allows=False))
    outcome = run_once(gate, kind="webhook")
    assert outcome.status == "blocked"
    assert outcome.reason == "the owner does not allow automatic deploys"
    assert "允许自动上线" in outcome.report["unfinished"][0]
    assert not shell.commands("deploy_tlines.sh")
    allowed, shell, _ = make(tmp_path / "allowed", api=FakeApi(owner_allows=True))
    assert run_once(allowed, kind="webhook").status == "deployed"
    # The operator's own "Deploy now" still goes live.
    manual, shell, _ = make(tmp_path / "manual", api=FakeApi(owner_allows=False))
    assert run_once(manual, kind="manual").status == "deployed"


def test_the_owner_report_is_plain_language(tmp_path):
    gate, _, _ = make(tmp_path)
    report = run_once(gate).report
    assert report["changed"] == ["任务看板显示谁在线"]
    assert report["how_to_try"] == ["打开控制台的任务看板", "看每张卡片右上角的小圆点"]
    assert report["rolled_back"] is False and report["unfinished"] == []
    blocked, _, _ = make(tmp_path / "b", shell=FakeShell(tmp_path / "b", tests_pass=False))
    for outcome_report in (report, run_once(blocked).report):
        text = json.dumps(outcome_report, ensure_ascii=False)
        assert not re.search(r"[0-9a-f]{12,}", text)
        for word in ("schema", "pytest", "G-0", STAMP, "ruff", "migration"):
            assert word not in text, word


def test_the_cycle_queues_a_new_branch_head_and_claims(tmp_path):
    api = FakeApi(run=None)
    gate, shell, _ = make(tmp_path, api=api)
    assert gate.cycle() == {"run": None}
    assert ("/runs", {"host": "vps-test", "commit": CANDIDATE}) in api.calls
    claimed = FakeApi(run={"id": "dgr_X", "trigger_kind": "poll", "commit_sha": CANDIDATE})
    gate, _, _ = make(tmp_path / "c", api=claimed)
    assert gate.cycle() == {"run": "dgr_X", "status": "deployed"}
    path, body = claimed.calls[-1]
    assert path == "/runs/dgr_X" and body["status"] == "deployed"
    assert set(body["report"]) == {"changed", "how_to_try", "rolled_back", "unfinished"}


def test_the_run_names_its_tasks_only_to_a_server_that_accepts_them(tmp_path):
    """Loop A-03: the server learns which tasks a deploy carried; an older
    server, which rejects unknown fields, is not sent them."""
    run = {"id": "dgr_K", "trigger_kind": "poll", "commit_sha": CANDIDATE}
    current = FakeApi(run=run, runner_fields=["task_keys"])
    gate, _, _ = make(tmp_path, api=current)
    assert gate.cycle()["status"] == "deployed"
    assert current.calls[-1][1]["task_keys"] == ["gate-task"]

    older = FakeApi(run=run)
    gate, _, _ = make(tmp_path / "old", api=older)
    assert gate.cycle()["status"] == "deployed"
    assert "task_keys" not in older.calls[-1][1]


def test_pull_task_keys_reads_each_named_task_once():
    pulls = [{"title": "Board task: alpha-1", "body": "看板任务 `beta.2`\nBoard task alpha-1"},
             {"title": "no task here", "body": ""}]
    assert gate_module.pull_task_keys(pulls) == ["alpha-1", "beta.2"]


def test_secrets_never_reach_the_log(tmp_path):
    log = gate_module.Log(tmp_path / "x.log", ["runner-secret-value", "pg-password-123"])
    log.write("connecting with runner-secret-value to postgres://u:pg-password-123@db")
    written = (tmp_path / "x.log").read_text(encoding="utf-8")
    assert "runner-secret-value" not in written and "pg-password-123" not in written


def test_quarantined_tests_are_named_in_the_report(tmp_path):
    gate, shell, _ = make(tmp_path)
    original = gate_module.read_quarantine
    gate_module.read_quarantine = lambda src: ["tests/test_events.py::test_slow"]
    try:
        outcome = run_once(gate)
    finally:
        gate_module.read_quarantine = original
    assert outcome.status == "deployed"
    assert "--deselect tests/test_events.py::test_slow" in " ".join(
        shell.commands("--junitxml")[0])
    assert any("已知问题" in line for line in outcome.report["unfinished"])


def test_each_run_gets_its_own_log_and_worktrees_are_removed(tmp_path):
    claimed = FakeApi(run={"id": "dgr_LOG", "trigger_kind": "poll", "commit_sha": CANDIDATE})
    gate, shell, _ = make(tmp_path, api=claimed)
    gate.cycle()
    assert gate.log.path == tmp_path / "logs" / "dgr_LOG.log"
    assert (tmp_path / "logs" / "dgr_LOG.log").exists()
    removed = [cmd[-1] for cmd in shell.commands("worktree remove")]
    assert str(tmp_path / "work" / "runs" / "dgr_LOG-src") in removed


def test_env_file_and_quarantine_parsing(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# comment\nDEPLOY_GATE_RUNNER_TOKEN="abc def"\nPUBLIC_URL=https://x.test\n'
                   "EMPTY=\n", encoding="utf-8")
    assert gate_module.read_env_file(env) == {"DEPLOY_GATE_RUNNER_TOKEN": "abc def",
                                              "PUBLIC_URL": "https://x.test", "EMPTY": ""}
    quarantine = tmp_path / "deploy" / "gate-quarantine.txt"
    quarantine.parent.mkdir()
    quarantine.write_text("# known slow\ntests/test_a.py::test_b  # tracked in perf task\n"
                          "not-a-test\n", encoding="utf-8")
    assert gate_module.read_quarantine(tmp_path) == ["tests/test_a.py::test_b"]
