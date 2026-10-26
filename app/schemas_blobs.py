"""Pydantic contracts for blob storage and public share links (v3.40)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field, model_validator

from app.schemas import StrictModel


class BlobUploadCreateRequest(StrictModel):
    file_name: str = Field(min_length=1, max_length=512)
    media_type: str = Field(default="application/octet-stream", max_length=256)
    total_parts: int | None = Field(default=None, ge=1, le=1000)


class BlobAttachRequest(StrictModel):
    blob_id: uuid.UUID
    file_name: str | None = Field(default=None, max_length=512)


class CleanupGrantRequest(StrictModel):
    grantee: str = Field(min_length=1, max_length=160)
    namespace: str | None = Field(default=None, max_length=64)
    doc_key_prefix: str | None = Field(default=None, max_length=256)
    expires_hours: int = Field(default=24, ge=1, le=24 * 365)


class BlobMigrationRequest(StrictModel):
    file_name: str = Field(min_length=1, max_length=512)
    apply: bool = False


class ShareLinkCreateRequest(StrictModel):
    expiry: Literal["24h", "7d", "30d", "permanent", "custom"] = "24h"
    custom_hours: int | None = Field(default=None, ge=1, le=24 * 365 * 5)
    custom_days: int | None = Field(default=None, ge=1, le=365 * 5)

    @model_validator(mode="after")
    def _custom_expiry(self) -> ShareLinkCreateRequest:
        if self.expiry == "custom":
            if (self.custom_hours is None) == (self.custom_days is None):
                raise ValueError("custom expiry needs exactly one of custom_hours or custom_days")
            if (self.custom_hours is not None and self.custom_hours <= 0) or (
                self.custom_days is not None and self.custom_days <= 0
            ):
                raise ValueError("custom expiry must be positive")
        elif self.custom_hours is not None or self.custom_days is not None:
            raise ValueError("custom_hours/custom_days are only valid with expiry='custom'")
        return self
