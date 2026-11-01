from __future__ import annotations

from app.services.ast_slicer import ASTContextSlicer, hash_signature_text

SOURCE = '''"""module doc"""
import os


class A(Base):
    """A doc."""
    x: int = 1

    @staticmethod
    async def f(a: int, b: str = "x") -> int:
        """f doc."""
        return a + 1

    def g(self):
        def inner():
            return 2
        return inner()


@decorator
def h(x: int) -> int:
    """h doc"""
    y = x * 2
    return y


def stub(a: int) -> int:
    """stub doc"""
    ...
'''


def test_function_skeleton_keeps_signature_and_docstring_and_folds_body() -> None:
    slicer = ASTContextSlicer()
    info = slicer.extract_artifact_signature(SOURCE, "src/a.py::h")
    assert info.found
    assert info.docstring == "h doc"
    assert "@decorator" in info.skeleton
    assert "def h(x: int) -> int:" in info.skeleton
    assert "return y" not in info.skeleton
    assert "..." in info.skeleton


def test_signature_hash_stable_across_body_edits() -> None:
    slicer = ASTContextSlicer()
    changed = SOURCE.replace("y = x * 2", "y = x * 3")
    assert slicer.extract_signature_hash(SOURCE, "src/a.py::h") == (
        slicer.extract_signature_hash(changed, "src/a.py::h")
    )


def test_signature_hash_changes_with_signature() -> None:
    slicer = ASTContextSlicer()
    changed = SOURCE.replace(
        "def h(x: int) -> int:", "def h(x: int, z: int = 0) -> int:"
    )
    assert slicer.extract_signature_hash(SOURCE, "src/a.py::h") != (
        slicer.extract_signature_hash(changed, "src/a.py::h")
    )


def test_class_skeleton_folds_method_bodies_but_keeps_attributes() -> None:
    slicer = ASTContextSlicer()
    info = slicer.extract_artifact_signature(SOURCE, "src/a.py::A")
    assert info.found
    assert info.docstring == "A doc."
    assert "x: int = 1" in info.skeleton
    assert "async def f(a: int" in info.skeleton
    assert "return a + 1" not in info.skeleton
    assert "return inner()" not in info.skeleton


def test_whole_file_skeleton() -> None:
    slicer = ASTContextSlicer()
    info = slicer.extract_artifact_signature(SOURCE, "src/a.py")
    assert info.found
    assert "return a + 1" not in info.skeleton
    assert "class A(Base):" in info.skeleton


def test_missing_symbol_is_not_found() -> None:
    slicer = ASTContextSlicer()
    info = slicer.extract_artifact_signature(SOURCE, "src/a.py::A::nope")
    assert not info.found
    assert info.signature_hash == hash_signature_text("")


def test_hash_signature_text_matches_slicer_hash() -> None:
    slicer = ASTContextSlicer()
    info = slicer.extract_artifact_signature(SOURCE, "src/a.py::h")
    assert hash_signature_text(info.skeleton) == info.signature_hash


def test_node_source_returns_full_body() -> None:
    slicer = ASTContextSlicer()
    source = slicer.extract_node_source(SOURCE, "src/a.py::h")
    assert source is not None
    assert "return y" in source


def test_discover_stubs_only_returns_empty_bodies() -> None:
    slicer = ASTContextSlicer()
    stubs = slicer.discover_stubs(SOURCE, "src/a.py")
    symbols = [stub["symbol_path"] for stub in stubs]
    assert "src/a.py::stub" in symbols
    assert "src/a.py::h" not in symbols
    assert "src/a.py::A::f" not in symbols


def test_referenced_names_collects_identifiers_and_attributes() -> None:
    source = '''
def caller(pricing):
    result = pricing.calc_discount(10)
    return build(result)
'''
    slicer = ASTContextSlicer()
    names = slicer.extract_referenced_names(source, "src/x.py::caller")
    assert "pricing" in names
    assert "calc_discount" in names
    assert "build" in names


def test_sensitive_guard_allows_same_file_unrelated_symbol_bodies() -> None:
    from app.services.verification_gate import sensitive_file_unchanged

    slicer = ASTContextSlicer()
    base = (
        "def f1():\n    return 1\n\n\n"
        "def f2():\n    return 2\n\n\n"
        "def f3():\n    return 3\n"
    )
    head = (
        "def f1():\n    return 10\n\n\n"
        "def f2():\n    return 2\n\n\n"
        "def f3():\n    return 3\n"
    )
    assert sensitive_file_unchanged(
        slicer, "src/x.py", base, head, ["src/x.py::f2"]
    )


def test_sensitive_guard_rejects_target_body_or_signature_change() -> None:
    from app.services.verification_gate import sensitive_file_unchanged

    slicer = ASTContextSlicer()
    base = "def f2():\n    return 2\n"
    body_changed = "def f2():\n    return 20\n"
    signature_changed = "def f2(x: int = 0):\n    return 2\n"
    assert not sensitive_file_unchanged(
        slicer, "src/x.py", base, body_changed, ["src/x.py::f2"]
    )
    assert not sensitive_file_unchanged(
        slicer, "src/x.py", base, signature_changed, ["src/x.py::f2"]
    )


def test_sensitive_guard_rejects_import_or_module_level_change() -> None:
    from app.services.verification_gate import sensitive_file_unchanged

    slicer = ASTContextSlicer()
    base = "def f2():\n    return 2\n"
    imported = "import math\n\n\ndef f2():\n    return 2\n"
    global_changed = "CONST = 1\n\n\ndef f2():\n    return 2\n"
    assert not sensitive_file_unchanged(
        slicer, "src/x.py", base, imported, ["src/x.py::f2"]
    )
    assert not sensitive_file_unchanged(
        slicer, "src/x.py", base, global_changed, ["src/x.py::f2"]
    )


def test_sensitive_guard_file_level_artifact_tracks_whole_file() -> None:
    from app.services.verification_gate import sensitive_file_unchanged

    slicer = ASTContextSlicer()
    base = "def f1():\n    return 1\n"
    head = "def f1():\n    return 2\n"
    assert not sensitive_file_unchanged(
        slicer, "src/x.py", base, head, ["src/x.py"]
    )


def test_file_snapshot_matches_slicer_contract() -> None:
    import hashlib

    from app.services.artifact_snapshot import FileASTSnapshot
    from app.services.ast_slicer import get_slicer

    slicer = get_slicer()
    snapshot = FileASTSnapshot.from_slicer(slicer, SOURCE, "src/a.py")
    assert snapshot.signature_hash == slicer.extract_signature_hash(
        SOURCE, "src/a.py"
    )
    info = snapshot.artifact_info("src/a.py::h")
    assert info is not None
    source = slicer.extract_node_source(SOURCE, "src/a.py::h")
    assert info.source_hash == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert info.signature_hash == slicer.extract_signature_hash(SOURCE, "src/a.py::h")
    assert "y" in info.referenced_names
    assert snapshot.artifact_info("src/a.py::missing") is None
    assert snapshot.source_hash_for("src/a.py") == snapshot.content_hash


def test_snapshot_cache_tracks_commit_ranges() -> None:
    from app.services.artifact_snapshot import ArtifactSnapshotCache

    cache = ArtifactSnapshotCache()
    cache.register_commit("c1", "c0", ["src/a.py"])
    cache.register_commit("c2", "c1", ["src/b.py"])
    assert cache.changed_files_in_range("c0", "c2") == {
        "src/a.py",
        "src/b.py",
    }
    assert cache.changed_files_in_range("c0", "c0") == set()
    assert cache.changed_files_in_range("c0", "external") is None
