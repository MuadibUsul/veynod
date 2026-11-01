"""Tree-sitter based AST slicing.

Responsibilities:
* isolate the *signature* of a symbol (decorators + signature + docstring),
  replacing implementation bodies with ``...``;
* compute a whitespace-normalised SHA-256 that is stable across body edits and
  formatting changes, so the gate can prove a frozen contract was not mutated;
* extract the identifiers/attributes a symbol references (symbol graph);
* discover ``...``/``pass`` stubs for the cold-start scanner.

Only Python is sliced in v4.0; the canonical symbol format is language-agnostic.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from typing import Iterable, Iterator

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from app.engine import rust_bridge

PY_LANGUAGE = Language(tspython.language())

_FUNCTION_NODES = {"function_definition"}
_CLASS_NODES = {"class_definition"}


@dataclass(frozen=True)
class SignatureInfo:
    skeleton: str
    signature_hash: str
    docstring: str | None
    found: bool


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise(text: str) -> str:
    return " ".join(text.split())


def hash_signature_text(text: str) -> str:
    """Hash a hand-authored signature with the same normalisation as the slicer."""
    return _hash_text(_normalise(text))


class ASTContextSlicer:
    def __init__(self) -> None:
        self.parser = Parser(PY_LANGUAGE)

    # ------------------------------------------------------------------ parse
    def parse(self, source: str) -> tuple[Node, bytes]:
        source_bytes = source.encode("utf-8")
        return self.parser.parse(source_bytes).root_node, source_bytes

    # -------------------------------------------------------------- discovery
    def _iter_definitions(
        self, container: Node, prefix: list[str]
    ) -> Iterator[tuple[list[str], Node, Node]]:
        """Yield (symbol chain, wrapper node, definition node) below a container."""
        for child in container.children:
            wrapper = child
            if child.type == "decorated_definition":
                definition = child.child_by_field_name("definition")
            elif child.type in _FUNCTION_NODES | _CLASS_NODES:
                definition = child
            else:
                continue

            if definition is None or definition.type not in _FUNCTION_NODES | _CLASS_NODES:
                continue
            name_node = definition.child_by_field_name("name")
            if name_node is None or name_node.text is None:
                continue
            name = name_node.text.decode("utf-8", errors="replace")
            chain = [*prefix, name]
            yield chain, wrapper, definition

            if definition.type in _CLASS_NODES:
                body = definition.child_by_field_name("body")
                if body is not None:
                    yield from self._iter_definitions(body, chain)

    def _find_symbol(
        self, root: Node, symbol_chain: list[str]
    ) -> tuple[Node, Node] | None:
        container = root
        found: tuple[Node, Node] | None = None
        for index, name in enumerate(symbol_chain):
            found = None
            for chain, wrapper, definition in self._iter_definitions(
                container, symbol_chain[:index]
            ):
                if chain[-1] == name:
                    found = (wrapper, definition)
                    break
            if found is None:
                return None
            container = found[1].child_by_field_name("body") or found[1]
        return found

    # --------------------------------------------------------------- docstring
    @staticmethod
    def _is_docstring_statement(node: Node) -> bool:
        return (
            node.type == "expression_statement"
            and len(node.children) > 0
            and node.children[0].type == "string"
        )

    @staticmethod
    def _is_ellipsis_statement(node: Node) -> bool:
        return (
            node.type == "expression_statement"
            and len(node.children) == 1
            and node.children[0].type == "ellipsis"
        )

    def _docstring(self, definition: Node, source_bytes: bytes) -> str | None:
        body = definition.child_by_field_name("body")
        if body is None or not body.children:
            return None
        first = body.children[0]
        if not self._is_docstring_statement(first):
            return None
        raw = source_bytes[first.start_byte : first.end_byte].decode(
            "utf-8", errors="replace"
        )
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
        return value if isinstance(value, str) else None

    # --------------------------------------------------------------- skeleton
    def _collect_body_replacements(
        self,
        node: Node,
        source_bytes: bytes,
        out: list[tuple[int, int]],
        *,
        target_only: bool,
    ) -> None:
        if node.type in _FUNCTION_NODES:
            body = node.child_by_field_name("body")
            if body is not None and body.children:
                statements = [c for c in body.children if c.type != "comment"]
                start_index = 1 if statements and self._is_docstring_statement(statements[0]) else 0
                remaining = statements[start_index:]
                if remaining:
                    out.append((remaining[0].start_byte, body.end_byte))
                elif start_index == 0:
                    out.append((body.start_byte, body.end_byte))
            return

        for child in node.children:
            self._collect_body_replacements(
                child, source_bytes, out, target_only=target_only
            )

    def _render(self, wrapper: Node, source_bytes: bytes, replacements: list[tuple[int, int]]) -> str:
        replacements = sorted(set(replacements), reverse=True)
        buffer = bytearray(source_bytes[wrapper.start_byte : wrapper.end_byte])
        for start, end in replacements:
            if start < wrapper.start_byte or end > wrapper.end_byte:
                continue
            buffer[start - wrapper.start_byte : end - wrapper.start_byte] = b"..."
        return buffer.decode("utf-8", errors="replace")

    def _signature_from_definition(
        self, wrapper: Node, definition: Node, source_bytes: bytes
    ) -> SignatureInfo:
        replacements: list[tuple[int, int]] = []
        if definition.type in _FUNCTION_NODES:
            self._collect_body_replacements(definition, source_bytes, replacements, target_only=True)
        else:
            # Class artifact: keep class attributes, remove every method body.
            self._collect_body_replacements(definition, source_bytes, replacements, target_only=False)

        skeleton = self._render(wrapper, source_bytes, replacements)
        return SignatureInfo(
            skeleton=skeleton,
            signature_hash=_hash_text(_normalise(skeleton)),
            docstring=self._docstring(definition, source_bytes),
            found=True,
        )

    def extract_artifact_signature(self, source: str, symbol_path: str) -> SignatureInfo:
        """Return the isolated, body-free skeleton of one artifact.

        ``symbol_path`` is the canonical ``file.py::Class::method`` form. A path
        without ``::`` means the artifact is the whole file.
        """
        root, source_bytes = self.parse(source)
        parts = symbol_path.split("::")
        if len(parts) == 1:
            replacements: list[tuple[int, int]] = []
            self._collect_body_replacements(root, source_bytes, replacements, target_only=False)
            skeleton = self._render(root, source_bytes, replacements)
            return SignatureInfo(skeleton, _hash_text(_normalise(skeleton)), None, True)

        resolved = self._find_symbol(root, parts[1:])
        if resolved is None:
            return SignatureInfo("", _hash_text(""), None, False)

        wrapper, definition = resolved
        return self._signature_from_definition(wrapper, definition, source_bytes)

    def extract_signature_hash(self, source: str, symbol_path: str) -> str | None:
        info = self.extract_artifact_signature(source, symbol_path)
        return info.signature_hash if info.found else None

    def extract_node_source(self, source: str, symbol_path: str) -> str | None:
        """Full current source of a symbol (for the mutable-stub view)."""
        root, source_bytes = self.parse(source)
        parts = symbol_path.split("::")
        if len(parts) == 1:
            return source
        resolved = self._find_symbol(root, parts[1:])
        if resolved is None:
            return None
        wrapper, _ = resolved
        return source_bytes[wrapper.start_byte : wrapper.end_byte].decode(
            "utf-8", errors="replace"
        )

    # ----------------------------------------------------------- symbol graph
    @staticmethod
    def _collect_names(node: Node, source_bytes: bytes) -> set[str]:
        names: set[str] = set()
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "identifier" and current.text is not None:
                names.add(current.text.decode("utf-8", errors="replace"))
            elif current.type == "attribute":
                attr = current.child_by_field_name("attribute")
                if attr is not None and attr.text is not None:
                    names.add(attr.text.decode("utf-8", errors="replace"))
            stack.extend(current.children)
        return names

    def extract_referenced_names(self, source: str, symbol_path: str) -> set[str]:
        """Best-effort call/attribute reference set used by the symbol graph."""
        root, _ = self.parse(source)
        parts = symbol_path.split("::")
        if len(parts) == 1:
            node = root
        else:
            resolved = self._find_symbol(root, parts[1:])
            if resolved is None:
                return set()
            node = resolved[0]
        return self._collect_names(node, source.encode("utf-8"))

    def file_snapshot(self, source: str, file_path: str) -> dict:
        """One parse yields the whole-file skeleton and every symbol snapshot."""
        root, source_bytes = self.parse(source)
        replacements: list[tuple[int, int]] = []
        self._collect_body_replacements(root, source_bytes, replacements, target_only=False)
        skeleton = self._render(root, source_bytes, replacements)
        symbols: list[dict] = []
        for chain, wrapper, definition in self._iter_definitions(root, []):
            info = self._signature_from_definition(wrapper, definition, source_bytes)
            raw = source_bytes[wrapper.start_byte : wrapper.end_byte]
            symbols.append(
                {
                    "symbol_path": "::".join([file_path, *chain]),
                    "source_hash": _hash_text(raw.decode("utf-8", errors="replace")),
                    "signature_hash": info.signature_hash,
                    "skeleton": info.skeleton,
                    "docstring": info.docstring,
                    "referenced_names": sorted(
                        self._collect_names(wrapper, source_bytes)
                    ),
                }
            )
        return {
            "skeleton": skeleton,
            "signature_hash": _hash_text(_normalise(skeleton)),
            "symbols": symbols,
            "referenced_names": sorted(self._collect_names(root, source_bytes)),
        }

    # --------------------------------------------------------------- stubs
    @staticmethod
    def is_stub_definition(definition: Node) -> bool:
        body = definition.child_by_field_name("body")
        if body is None:
            return False
        statements = [c for c in body.children if c.type != "comment"]
        if not statements:
            return False
        index = 1 if ASTContextSlicer._is_docstring_statement(statements[0]) else 0
        remaining = statements[index:]
        if not remaining:
            # ``def f(): "doc"`` has no implementation either.
            return index == 1
        if len(remaining) != 1:
            return False
        statement = remaining[0]
        return statement.type == "pass_statement" or ASTContextSlicer._is_ellipsis_statement(statement)

    def discover_stubs(self, source: str, file_path: str) -> list[dict]:
        root, source_bytes = self.parse(source)
        results: list[dict] = []
        for chain, wrapper, definition in self._iter_definitions(root, []):
            if definition.type not in _FUNCTION_NODES:
                continue
            if not self.is_stub_definition(definition):
                continue
            symbol_path = "::".join([file_path, *chain])
            results.append(
                {
                    "symbol_path": symbol_path,
                    "signature": self._signature_from_definition(
                        wrapper, definition, source_bytes
                    ),
                }
            )
        return results

    # ------------------------------------------------------------ convenience
    def file_symbols(self, source: str, file_path: str) -> list[str]:
        root, _ = self.parse(source)
        return [
            "::".join([file_path, *chain])
            for chain, _, _ in self._iter_definitions(root, [])
        ]

    def file_functions(self, source: str, file_path: str) -> Iterable[str]:
        root, _ = self.parse(source)
        for chain, _, definition in self._iter_definitions(root, []):
            if definition.type in _FUNCTION_NODES:
                yield "::".join([file_path, *chain])


class RustContextSlicer:
    """Drop-in slicer backed by the `veynod_core` Rust extension."""

    backend = "rust"

    def __init__(self) -> None:
        self._core = rust_bridge.core()

    def extract_artifact_signature(self, source: str, symbol_path: str) -> SignatureInfo:
        skeleton, signature_hash, docstring, found = self._core.signature(
            source, symbol_path
        )
        return SignatureInfo(
            skeleton=skeleton,
            signature_hash=signature_hash,
            docstring=docstring,
            found=bool(found),
        )

    def extract_signature_hash(self, source: str, symbol_path: str) -> str | None:
        return self._core.signature_hash(source, symbol_path)

    def extract_node_source(self, source: str, symbol_path: str) -> str | None:
        return self._core.node_source(source, symbol_path)

    def extract_referenced_names(self, source: str, symbol_path: str) -> set[str]:
        return set(self._core.referenced_names(source, symbol_path))

    def file_snapshot(self, source: str, file_path: str) -> dict:
        skeleton, signature_hash, symbols, referenced_names = self._core.file_snapshot(
            source, file_path
        )
        return {
            "skeleton": skeleton,
            "signature_hash": signature_hash,
            "symbols": [
                {
                    "symbol_path": symbol_path,
                    "source_hash": source_hash,
                    "signature_hash": symbol_hash,
                    "skeleton": symbol_skeleton,
                    "docstring": docstring,
                    "referenced_names": list(names),
                }
                for (
                    symbol_path,
                    source_hash,
                    symbol_hash,
                    symbol_skeleton,
                    docstring,
                    names,
                ) in symbols
            ],
            "referenced_names": list(referenced_names),
        }

    def discover_stubs(self, source: str, file_path: str) -> list[dict]:
        return [
            {
                "symbol_path": symbol_path,
                "signature": SignatureInfo(
                    skeleton=skeleton,
                    signature_hash=signature_hash,
                    docstring=docstring,
                    found=bool(found),
                ),
            }
            for symbol_path, skeleton, signature_hash, docstring, found in self._core.discover_stubs(
                source, file_path
            )
        ]

    def file_symbols(self, source: str, file_path: str) -> list[str]:
        return list(self._core.file_symbols(source, file_path))

    def file_functions(self, source: str, file_path: str) -> Iterable[str]:
        return list(self._core.file_functions(source, file_path))


Slicer = ASTContextSlicer | RustContextSlicer


def get_slicer() -> Slicer:
    """Use the Rust core when installed; otherwise the pure-Python slicer."""
    if rust_bridge.available():
        return RustContextSlicer()
    return ASTContextSlicer()


__all__ = [
    "ASTContextSlicer",
    "RustContextSlicer",
    "SignatureInfo",
    "PY_LANGUAGE",
    "Slicer",
    "get_slicer",
    "hash_signature_text",
]
