"""Request bodies for the VPS deploy gate (v3.50)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DeployGateReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=300)


class DeployGateTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit: str | None = Field(default=None, min_length=40, max_length=40,
                               description="A full SHA on the branch; omit for its newest commit")
    reason: str | None = Field(default=None, max_length=300)


class DeployGateHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=32)
    deployed_commit: str | None = Field(default=None, max_length=40)


class DeployGateRunnerEnqueue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=100)
    commit: str = Field(min_length=40, max_length=40)


class DeployGateClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=100)


class DeployGateRunUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=100)
    status: Literal["running", "skipped", "blocked", "deployed", "rolled_back", "failed"]
    status_reason: str | None = Field(default=None, max_length=300)
    commit_sha: str | None = Field(default=None, max_length=40)
    checks: list[dict[str, Any]] = Field(default_factory=list, max_length=10)
    deploy: dict[str, Any] = Field(default_factory=dict)
    rolled_back: bool = False
    report: dict[str, Any] = Field(default_factory=dict)
    log_ref: str | None = Field(default=None, max_length=200)


class DeployGateTaskEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_keys: list[str] = Field(default_factory=list, max_length=20)
