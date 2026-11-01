from __future__ import annotations

from uuid import uuid4

from app.engine.kernel_service_impl import _estimate_tokens, _fit_bundle
from app.mcp_server.syscalls import (
    ArtifactKind,
    ArtifactStatus,
    SlicedArtifactView,
    TaskBundleResponse,
)


def _view(content: str) -> SlicedArtifactView:
    return SlicedArtifactView(
        artifact_id=uuid4(),
        symbol_path="src/x.py::f",
        kind=ArtifactKind.STUB,
        status=ArtifactStatus.DRAFT,
        content_hash="",
        sliced_content=content,
    )


def test_bundle_fitter_respects_hard_ceiling() -> None:
    bundle = TaskBundleResponse(
        leased=True,
        specification="spec " * 5000,
        read_only_signatures=[_view("read " * 8000)],
        mutable_stubs=[_view("stub " * 8000)],
        frozen_tests=[_view("test " * 8000)],
    )
    _fit_bundle(bundle, 2000)
    total = _estimate_tokens(bundle.specification) + sum(
        _estimate_tokens(view.sliced_content)
        for view in [
            *bundle.read_only_signatures,
            *bundle.mutable_stubs,
            *bundle.frozen_tests,
        ]
    )
    assert total <= 2000
    assert bundle.estimated_tokens == total
    assert any(
        "# ...[context truncated]" in view.sliced_content
        for view in [*bundle.read_only_signatures, *bundle.mutable_stubs]
    )


def test_bundle_fitter_leaves_small_bundles_alone() -> None:
    bundle = TaskBundleResponse(
        leased=True,
        specification="short spec",
        mutable_stubs=[_view("def f():\n    ...\n")],
    )
    _fit_bundle(bundle, 6000)
    assert bundle.estimated_tokens < 6000
    assert bundle.mutable_stubs[0].sliced_content == "def f():\n    ...\n"
