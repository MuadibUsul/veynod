"""Unit tests for the bench_swarm acceptance evaluator and legacy replay.

These tests are deliberately independent of PostgreSQL/Git: they pin the
exit-code contract that the old benchmark lacked.  The end-to-end harness test
lives in tests/test_bench_swarm_e2e.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bench_swarm.py"


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench_swarm_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_bench()


def _input(**overrides):
    base = dict(
        expected_tasks=1000,
        db_task_count=1000,
        missing_tasks=0,
        extra_tasks=0,
        final_statuses={"COMMITTED": 1000},
        client_statuses={"COMMITTED": 1000},
        accepted_tasks=1000,
        accepted_non_terminal=0,
        client_committed_without_db=0,
        ops={
            "claim": {
                "attempts": 1000,
                "success": 1000,
                "http_4xx": 0,
                "http_5xx": 0,
                "timeouts": 0,
                "transport_errors": 0,
                "retries": 0,
                "other_errors": 0,
            },
            "submit": {
                "attempts": 1000,
                "success": 1000,
                "http_4xx": 0,
                "http_5xx": 0,
                "timeouts": 0,
                "transport_errors": 0,
                "retries": 0,
                "other_errors": 0,
            },
        },
        sse={
            "streams": 0,
            "attempts": 0,
            "opens": 0,
            "streams_opened_once": 0,
            "frames": 0,
            "http_errors": 0,
            "timeouts": 0,
            "transport_errors": 0,
        },
        pool_timeouts=0,
        production_grade=False,
        profile="dev",
        fatal_errors=[],
        metrics_available=True,
        detail_complete=True,
        max_failed=0,
        require_all_committed=True,
        max_http_error_rate=0.01,
        max_transport_error_rate=0.005,
        max_submit_error_rate=0.002,
        max_sse_error_rate=0.01,
        max_retry_rate=0.05,
        max_error_events=None,
        max_pool_timeouts=0,
        min_sse_open_ratio=0.99,
    )
    base.update(overrides)
    return bench.AcceptanceInput(**base)


def test_incomplete_run_exits_nonzero_and_names_the_gap():
    verdict = bench.evaluate_acceptance(
        _input(
            final_statuses={"COMMITTED": 280, "VERIFYING": 719, "LEASED": 1},
            client_statuses={"COMMITTED": 280, "VERIFYING": 719},
            accepted_tasks=999,
            accepted_non_terminal=719,
            ops={
                "submit": {
                    "attempts": 1000,
                    "success": 280,
                    "http_4xx": 0,
                    "http_5xx": 450,
                    "timeouts": 300,
                    "transport_errors": 151,
                    "retries": 0,
                    "other_errors": 0,
                }
            },
        )
    )
    assert verdict["passed"] is False
    assert verdict["exit_code"] == 1
    codes = {reason["code"] for reason in verdict["reasons"]}
    assert "all_tasks_terminal" in codes
    assert "committed_tasks" in codes
    assert "accepted_submissions_terminal" in codes
    assert "submit_error_budget" in codes


def test_complete_run_passes():
    verdict = bench.evaluate_acceptance(_input())
    assert verdict["passed"] is True
    assert verdict["exit_code"] == 0
    assert verdict["reasons"] == []


def test_wrong_task_count_fails_even_if_all_present_tasks_commit():
    verdict = bench.evaluate_acceptance(
        _input(
            db_task_count=999,
            expected_tasks=1000,
            missing_tasks=1,
            final_statuses={"COMMITTED": 999},
            client_statuses={"COMMITTED": 999},
            accepted_tasks=999,
        )
    )
    assert verdict["exit_code"] == 1
    assert "task_count_matches" in {reason["code"] for reason in verdict["reasons"]}


def test_extra_tasks_fail():
    verdict = bench.evaluate_acceptance(
        _input(
            db_task_count=1001,
            extra_tasks=1,
            final_statuses={"COMMITTED": 1001},
            client_statuses={"COMMITTED": 1001},
            accepted_tasks=1001,
        )
    )
    assert verdict["exit_code"] == 1
    assert "task_count_matches" in {reason["code"] for reason in verdict["reasons"]}


def test_error_rate_budget_fails_a_run_with_all_tasks_committed():
    verdict = bench.evaluate_acceptance(
        _input(
            ops={
                "submit": {
                    "attempts": 1000,
                    "success": 900,
                    "http_4xx": 10,
                    "http_5xx": 40,
                    "timeouts": 30,
                    "transport_errors": 20,
                    "retries": 100,
                    "other_errors": 0,
                }
            }
        )
    )
    codes = {reason["code"] for reason in verdict["reasons"]}
    assert verdict["exit_code"] == 1
    assert "submit_http_error_budget" in codes
    assert "submit_transport_error_budget" in codes
    assert "submit_retry_budget" in codes


def test_client_commit_without_database_commit_fails():
    verdict = bench.evaluate_acceptance(
        _input(
            final_statuses={"COMMITTED": 999, "VERIFYING": 1},
            accepted_non_terminal=1,
            client_committed_without_db=1,
        )
    )
    codes = {reason["code"] for reason in verdict["reasons"]}
    assert verdict["exit_code"] == 1
    assert "client_database_commit_agreement" in codes


def test_pool_timeouts_fail():
    verdict = bench.evaluate_acceptance(_input(pool_timeouts=104, max_pool_timeouts=0))
    assert verdict["exit_code"] == 1
    assert "db_pool_timeout_budget" in {
        reason["code"] for reason in verdict["reasons"]
    }


def test_sse_zero_streams_is_a_valid_control():
    verdict = bench.evaluate_acceptance(_input(sse={"streams": 0}))
    assert verdict["passed"] is True


def test_sse_thousand_streams_requires_open_ratio_and_frames():
    verdict = bench.evaluate_acceptance(
        _input(
            sse={
                "streams": 1000,
                "attempts": 1000,
                "opens": 980,
                "streams_opened_once": 980,
                "frames": 0,
                "http_errors": 20,
                "timeouts": 0,
                "transport_errors": 0,
            }
        )
    )
    codes = {reason["code"] for reason in verdict["reasons"]}
    assert verdict["exit_code"] == 1
    assert "sse_open_ratio" in codes
    assert "sse_frames_received" in codes
    assert "sse_error_budget" in codes


def test_production_profile_requires_gates():
    verdict = bench.evaluate_acceptance(
        _input(profile="production", production_grade=False)
    )
    assert verdict["exit_code"] == 1
    assert "production_gate_profile" in {
        reason["code"] for reason in verdict["reasons"]
    }


def test_server_submit_incomplete_fails_even_if_db_terminal():
    # A submit entered the gate but never finished is a separate server-side
    # signal from the final dag_tasks status.
    verdict = bench.evaluate_acceptance(_input(server_submit_incomplete=5))
    assert verdict["exit_code"] == 1
    assert "server_submit_incomplete" in {
        reason["code"] for reason in verdict["reasons"]
    }


def test_legacy_json_replay_exits_nonzero(tmp_path, capsys):
    legacy = {
        "label": "legacy-1000",
        "tasks": 1000,
        "duration_s": 358.0,
        "sse_streams": 1000,
        "http_errors": 901,
        "final_statuses": {"COMMITTED": 280, "VERIFYING": 719, "LEASED": 1},
    }
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps(legacy), encoding="utf-8")
    code = bench.main(
        [
            "--evaluate-json",
            str(source),
            "--profile",
            "dev",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["acceptance"]["passed"] is False
    assert printed["acceptance"]["exit_code"] == 1
    assert "all_tasks_terminal" in {
        reason["code"] for reason in printed["acceptance"]["reasons"]
    }


def test_legacy_json_replay_can_pass_for_a_complete_run(tmp_path, capsys):
    legacy = {
        "tasks": 1000,
        "final_statuses": {"COMMITTED": 1000},
        "http_errors": 0,
    }
    source = tmp_path / "legacy-complete.json"
    source.write_text(json.dumps(legacy), encoding="utf-8")
    code = bench.main(
        [
            "--evaluate-json",
            str(source),
            "--profile",
            "dev",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["acceptance"]["passed"] is True


def test_database_name_fits_postgres_limit_and_is_unique():
    first = bench._safe_db_name("hot-file", "a very long label " * 10)
    second = bench._safe_db_name("hot-file", "a very long label " * 10)
    assert len(first) <= 63
    assert first != second


def test_legacy_replay_accepts_concatenated_stdout(tmp_path, capsys):
    first = {
        "tasks": 1000,
        "final_statuses": {"COMMITTED": 316, "VERIFYING": 654, "LEASED": 30},
        "http_errors": 1360,
    }
    second = {"server_pool_errors": 10, "server_timeouts": 20}
    source = tmp_path / "legacy-concatenated.json"
    source.write_text(
        json.dumps(first) + "\n" + json.dumps(second), encoding="utf-8"
    )
    code = bench.main(
        [
            "--evaluate-json",
            str(source),
            "--profile",
            "dev",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["source"]["legacy_fields"]["final_statuses"]["VERIFYING"] == 654
    assert printed["acceptance"]["exit_code"] == 1


def test_sse_counters_expose_independent_fields():
    counters = bench.SseCounters()
    counters.attempts = 3
    counters.opens = 2
    counters.streams_opened_once = 2
    counters.frames = 17
    counters.timeouts = 1
    payload = counters.as_dict()
    assert payload["opens"] == 2
    assert payload["streams_opened_once"] == 2
    assert payload["frames"] == 17
    assert payload["timeouts"] == 1


def test_percentile_is_linear():
    assert bench.percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)
    assert bench.percentile([], 0.95) == 0.0


def test_admission_retry_after_is_parsed_and_not_a_claim_error():
    from_header = httpx.Response(429, headers={"Retry-After": "7"})
    assert bench._retry_after_seconds(from_header) == pytest.approx(7.0)

    from_body = httpx.Response(
        429, json={"detail": {"error": "admission_control", "retry_after_seconds": 3}}
    )
    assert bench._retry_after_seconds(from_body) == pytest.approx(3.0)
    assert bench._retry_after_seconds(httpx.Response(429)) == pytest.approx(5.0)

    counters = bench.OpCounters(
        attempts=10, success=6, admission_denied=4
    )
    assert counters.http_errors == 0
    assert counters.as_dict()["admission_denied"] == 4
