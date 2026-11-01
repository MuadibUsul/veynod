//! Tree-sitter Python AST slicing.
//!
//! Mirrors `app/services/ast_slicer.py` byte-for-byte in observable behaviour:
//! decorators/signatures/docstrings survive, implementation bodies become `...`,
//! and the whitespace-normalised SHA-256 is stable across body edits.

use std::cell::RefCell;
use std::collections::hash_map::DefaultHasher;
use std::collections::{HashSet, VecDeque};
use std::hash::{Hash, Hasher};
use std::rc::Rc;

use sha2::{Digest, Sha256};
use tree_sitter::{Node, Parser};

#[derive(Clone, Debug)]
pub struct SignatureInfo {
    pub skeleton: String,
    pub signature_hash: String,
    pub docstring: Option<String>,
    pub found: bool,
}

#[derive(Clone, Debug)]
pub struct StubInfo {
    pub symbol_path: String,
    pub signature: SignatureInfo,
}

pub fn new_parser() -> Parser {
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .expect("tree-sitter-python grammar");
    parser
}

thread_local! {
    static PARSER: RefCell<Parser> = RefCell::new(new_parser());
    // The gate/bundle queries several symbols from the same file. Caching the
    // parsed tree turns N parses into one per file (bounded to 8 files/thread).
    static TREE_CACHE: RefCell<VecDeque<(u64, String, Rc<tree_sitter::Tree>)>> =
        RefCell::new(VecDeque::new());
}

fn source_key(source: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    source.hash(&mut hasher);
    hasher.finish()
}

fn parse_owned(source: &str) -> Option<Rc<tree_sitter::Tree>> {
    let key = source_key(source);
    if let Some(tree) = TREE_CACHE.with(|cache| {
        cache
            .borrow()
            .iter()
            .find(|(cached_key, cached_source, _)| *cached_key == key && cached_source == source)
            .map(|(_, _, tree)| Rc::clone(tree))
    }) {
        return Some(tree);
    }

    let tree = PARSER.with(|parser| parser.borrow_mut().parse(source, None))?;
    let tree = Rc::new(tree);
    TREE_CACHE.with(|cache| {
        let mut cache = cache.borrow_mut();
        if cache.len() >= 8 {
            cache.pop_front();
        }
        cache.push_back((key, source.to_string(), Rc::clone(&tree)));
    });
    Some(tree)
}

fn definition_name(definition: Node, source: &[u8]) -> Option<String> {
    let name_node = definition.child_by_field_name("name")?;
    name_node
        .utf8_text(source)
        .ok()
        .map(|value| value.to_string())
}

fn unwrap_definition(child: Node) -> Option<(Node, Node)> {
    if child.kind() == "decorated_definition" {
        let definition = child.child_by_field_name("definition")?;
        if matches!(
            definition.kind(),
            "function_definition" | "class_definition"
        ) {
            Some((child, definition))
        } else {
            None
        }
    } else if matches!(child.kind(), "function_definition" | "class_definition") {
        Some((child, child))
    } else {
        None
    }
}

fn collect_definitions<'a>(
    container: Node<'a>,
    source: &[u8],
    prefix: &[String],
    out: &mut Vec<(Vec<String>, Node<'a>, Node<'a>)>,
) {
    for index in 0..container.child_count() {
        let Some(child) = container.child(index) else {
            continue;
        };
        let Some((wrapper, definition)) = unwrap_definition(child) else {
            continue;
        };
        let Some(name) = definition_name(definition, source) else {
            continue;
        };
        let mut chain = prefix.to_vec();
        chain.push(name);
        out.push((chain.clone(), wrapper, definition));

        if definition.kind() == "class_definition" {
            if let Some(body) = definition.child_by_field_name("body") {
                collect_definitions(body, source, &chain, out);
            }
        }
    }
}

fn find_child<'a>(container: Node<'a>, name: &str, source: &[u8]) -> Option<(Node<'a>, Node<'a>)> {
    for index in 0..container.child_count() {
        let Some(child) = container.child(index) else {
            continue;
        };
        let Some((wrapper, definition)) = unwrap_definition(child) else {
            continue;
        };
        if definition_name(definition, source).as_deref() == Some(name) {
            return Some((wrapper, definition));
        }
    }
    None
}

fn find_symbol<'a>(
    root: Node<'a>,
    chain: &[String],
    source: &[u8],
) -> Option<(Node<'a>, Node<'a>)> {
    let mut container = root;
    let mut found: Option<(Node<'a>, Node<'a>)> = None;
    for name in chain {
        let pair = find_child(container, name, source)?;
        found = Some(pair);
        container = pair.1.child_by_field_name("body").unwrap_or(pair.1);
    }
    found
}

fn is_docstring_statement(node: Node) -> bool {
    node.kind() == "expression_statement"
        && node.child_count() > 0
        && node.child(0).map(|child| child.kind()) == Some("string")
}

fn is_ellipsis_statement(node: Node) -> bool {
    node.kind() == "expression_statement"
        && node.child_count() == 1
        && node.child(0).map(|child| child.kind()) == Some("ellipsis")
}

fn statement_children(body: Node) -> Vec<Node> {
    let mut statements = Vec::new();
    for index in 0..body.child_count() {
        if let Some(child) = body.child(index) {
            if child.kind() != "comment" {
                statements.push(child);
            }
        }
    }
    statements
}

fn collect_body_replacements(node: Node, out: &mut Vec<(usize, usize)>) {
    if node.kind() == "function_definition" {
        if let Some(body) = node.child_by_field_name("body") {
            let statements = statement_children(body);
            let mut start_index = 0;
            if !statements.is_empty() && is_docstring_statement(statements[0]) {
                start_index = 1;
            }
            let remaining = &statements[start_index..];
            if !remaining.is_empty() {
                out.push((remaining[0].start_byte(), body.end_byte()));
            } else if start_index == 0 {
                out.push((body.start_byte(), body.end_byte()));
            }
        }
        return;
    }

    for index in 0..node.child_count() {
        if let Some(child) = node.child(index) {
            collect_body_replacements(child, out);
        }
    }
}

fn render(wrapper: Node, source: &[u8], replacements: &mut Vec<(usize, usize)>) -> String {
    replacements.sort_by(|left, right| right.0.cmp(&left.0));
    replacements.dedup();

    let wrapper_start = wrapper.start_byte();
    let wrapper_end = wrapper.end_byte();
    let mut buffer = source[wrapper_start..wrapper_end].to_vec();
    for (start, end) in replacements.iter() {
        if *start < wrapper_start || *end > wrapper_end || start >= end {
            continue;
        }
        let relative_start = start - wrapper_start;
        let relative_end = end - wrapper_start;
        buffer.splice(relative_start..relative_end, b"...".iter().copied());
    }
    String::from_utf8_lossy(&buffer).to_string()
}

fn unescape_python(raw: &str) -> String {
    let mut output = String::with_capacity(raw.len());
    let mut chars = raw.chars().peekable();
    while let Some(character) = chars.next() {
        if character != '\\' {
            output.push(character);
            continue;
        }
        let Some(escaped) = chars.next() else {
            output.push('\\');
            break;
        };
        match escaped {
            'n' => output.push('\n'),
            't' => output.push('\t'),
            'r' => output.push('\r'),
            'a' => output.push('\u{7}'),
            'b' => output.push('\u{8}'),
            'f' => output.push('\u{c}'),
            'v' => output.push('\u{b}'),
            '0' => output.push('\0'),
            '\\' => output.push('\\'),
            '\'' => output.push('\''),
            '"' => output.push('"'),
            'x' => {
                let mut hex = String::new();
                for _ in 0..2 {
                    if let Some(next) = chars.next() {
                        hex.push(next);
                    }
                }
                if let Ok(value) = u8::from_str_radix(&hex, 16) {
                    output.push(value as char);
                } else {
                    output.push_str("\\x");
                    output.push_str(&hex);
                }
            }
            'u' | 'U' => {
                let digits = if escaped == 'u' { 4 } else { 8 };
                let mut hex = String::new();
                for _ in 0..digits {
                    if let Some(next) = chars.next() {
                        hex.push(next);
                    }
                }
                if let Ok(value) = u32::from_str_radix(&hex, 16) {
                    if let Some(decoded) = char::from_u32(value) {
                        output.push(decoded);
                    }
                } else {
                    output.push('\\');
                    output.push(escaped);
                    output.push_str(&hex);
                }
            }
            '1'..='7' => {
                let mut octal = String::new();
                octal.push(escaped);
                for _ in 0..2 {
                    if let Some(next) = chars.next() {
                        if ('0'..='7').contains(&next) {
                            octal.push(next);
                        } else {
                            // Put it back by reconstructing; peekable has no
                            // push-back, so append it directly (rare).
                            output.push_str(&octal);
                            output.push(next);
                            break;
                        }
                    }
                }
                if !octal.is_empty() {
                    if let Ok(value) = u32::from_str_radix(&octal, 8) {
                        if let Some(decoded) = char::from_u32(value) {
                            output.push(decoded);
                        }
                    }
                }
            }
            other => {
                output.push('\\');
                output.push(other);
            }
        }
    }
    output
}

fn parse_docstring(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    let bytes = trimmed.as_bytes();
    let mut index = 0;
    while index < bytes.len() && bytes[index].is_ascii_alphabetic() {
        index += 1;
    }
    let prefix = trimmed[..index].to_ascii_lowercase();
    if prefix.contains('f') || prefix.contains('b') {
        return None;
    }
    let rest = &trimmed[index..];
    let (quote, triple) = if rest.starts_with("\"\"\"") {
        ("\"\"\"", true)
    } else if rest.starts_with("'''") {
        ("'''", true)
    } else if rest.starts_with('"') {
        ("\"", false)
    } else if rest.starts_with('\'') {
        ("'", false)
    } else {
        return None;
    };
    let _ = triple;
    let start = quote.len();
    if rest.len() < start + quote.len() {
        return None;
    }
    let end = rest.len() - quote.len();
    let inner = &rest[start..end];
    if prefix.contains('r') {
        Some(inner.to_string())
    } else {
        Some(unescape_python(inner))
    }
}

fn docstring_for(definition: Node, source: &[u8]) -> Option<String> {
    let body = definition.child_by_field_name("body")?;
    let first = body.child(0)?;
    if !is_docstring_statement(first) {
        return None;
    }
    let string_node = first.child(0)?;
    let raw = string_node.utf8_text(source).ok()?;
    parse_docstring(raw)
}

fn hash_skeleton(skeleton: &str) -> String {
    let normalized = skeleton.split_whitespace().collect::<Vec<_>>().join(" ");
    let digest = Sha256::digest(normalized.as_bytes());
    format!("{digest:x}")
}

fn hash_bytes(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    format!("{digest:x}")
}

#[derive(Clone, Debug)]
pub struct SymbolSnapshot {
    pub symbol_path: String,
    pub source_hash: String,
    pub signature_hash: String,
    pub skeleton: String,
    pub docstring: Option<String>,
    pub referenced_names: Vec<String>,
}

/// Immutable per-file view used by the merge critical section: one parse
/// yields the whole-file skeleton, every symbol's raw-source hash, signature,
/// and the reference set used by the symbol graph.
#[derive(Clone, Debug)]
pub struct FileSnapshot {
    pub skeleton: String,
    pub signature_hash: String,
    pub symbols: Vec<SymbolSnapshot>,
    pub referenced_names: Vec<String>,
}

pub fn file_snapshot(source: &str, file_path: &str) -> FileSnapshot {
    let Some(tree) = parse_owned(source) else {
        return FileSnapshot {
            skeleton: String::new(),
            signature_hash: hash_skeleton(""),
            symbols: Vec::new(),
            referenced_names: Vec::new(),
        };
    };
    let root = tree.root_node();
    let source_bytes = source.as_bytes();

    let mut replacements = Vec::new();
    collect_body_replacements(root, &mut replacements);
    let skeleton = render(root, source_bytes, &mut replacements);

    let mut file_names = HashSet::new();
    collect_names(root, source_bytes, &mut file_names);
    let mut referenced_names: Vec<String> = file_names.into_iter().collect();
    referenced_names.sort();

    let mut definitions = Vec::new();
    collect_definitions(root, source_bytes, &[], &mut definitions);

    let mut symbols = Vec::with_capacity(definitions.len());
    for (chain, wrapper, definition) in definitions {
        let symbol_path = format!("{}::{}", file_path, chain.join("::"));
        let raw = &source_bytes[wrapper.start_byte()..wrapper.end_byte()];
        let signature = signature_from_definition(wrapper, definition, source_bytes);
        let mut names = HashSet::new();
        collect_names(wrapper, source_bytes, &mut names);
        let mut names: Vec<String> = names.into_iter().collect();
        names.sort();
        symbols.push(SymbolSnapshot {
            symbol_path,
            source_hash: hash_bytes(raw),
            signature_hash: signature.signature_hash,
            skeleton: signature.skeleton,
            docstring: signature.docstring,
            referenced_names: names,
        });
    }

    FileSnapshot {
        signature_hash: hash_skeleton(&skeleton),
        skeleton,
        symbols,
        referenced_names,
    }
}

fn not_found() -> SignatureInfo {
    SignatureInfo {
        skeleton: String::new(),
        signature_hash: hash_skeleton(""),
        docstring: None,
        found: false,
    }
}

fn signature_from_definition(wrapper: Node, definition: Node, source: &[u8]) -> SignatureInfo {
    let mut replacements = Vec::new();
    collect_body_replacements(definition, &mut replacements);
    let skeleton = render(wrapper, source, &mut replacements);
    SignatureInfo {
        signature_hash: hash_skeleton(&skeleton),
        skeleton,
        docstring: docstring_for(definition, source),
        found: true,
    }
}

pub fn signature(source: &str, symbol_path: &str) -> SignatureInfo {
    let Some(tree) = parse_owned(source) else {
        return not_found();
    };
    let root = tree.root_node();
    let source_bytes = source.as_bytes();
    let parts: Vec<String> = symbol_path
        .split("::")
        .map(|part| part.to_string())
        .collect();

    if parts.len() == 1 {
        let mut replacements = Vec::new();
        collect_body_replacements(root, &mut replacements);
        let skeleton = render(root, source_bytes, &mut replacements);
        return SignatureInfo {
            signature_hash: hash_skeleton(&skeleton),
            skeleton,
            docstring: None,
            found: true,
        };
    }

    let Some((wrapper, definition)) = find_symbol(root, &parts[1..], source_bytes) else {
        return not_found();
    };
    signature_from_definition(wrapper, definition, source_bytes)
}

pub fn signature_hash(source: &str, symbol_path: &str) -> Option<String> {
    let info = signature(source, symbol_path);
    if info.found {
        Some(info.signature_hash)
    } else {
        None
    }
}

pub fn node_source(source: &str, symbol_path: &str) -> Option<String> {
    let tree = parse_owned(source)?;
    let root = tree.root_node();
    let parts: Vec<String> = symbol_path
        .split("::")
        .map(|part| part.to_string())
        .collect();
    if parts.len() == 1 {
        return Some(source.to_string());
    }
    let (wrapper, _) = find_symbol(root, &parts[1..], source.as_bytes())?;
    Some(
        String::from_utf8_lossy(&source.as_bytes()[wrapper.start_byte()..wrapper.end_byte()])
            .to_string(),
    )
}

fn collect_names(node: Node, source: &[u8], names: &mut HashSet<String>) {
    if node.kind() == "identifier" {
        if let Ok(text) = node.utf8_text(source) {
            names.insert(text.to_string());
        }
    } else if node.kind() == "attribute" {
        if let Some(attribute) = node.child_by_field_name("attribute") {
            if let Ok(text) = attribute.utf8_text(source) {
                names.insert(text.to_string());
            }
        }
    }
    for index in 0..node.child_count() {
        if let Some(child) = node.child(index) {
            collect_names(child, source, names);
        }
    }
}

pub fn referenced_names(source: &str, symbol_path: &str) -> Vec<String> {
    let Some(tree) = parse_owned(source) else {
        return Vec::new();
    };
    let root = tree.root_node();
    let source_bytes = source.as_bytes();
    let parts: Vec<String> = symbol_path
        .split("::")
        .map(|part| part.to_string())
        .collect();
    let node = if parts.len() == 1 {
        root
    } else {
        match find_symbol(root, &parts[1..], source_bytes) {
            Some((wrapper, _)) => wrapper,
            None => return Vec::new(),
        }
    };
    let mut names = HashSet::new();
    collect_names(node, source_bytes, &mut names);
    let mut output: Vec<String> = names.into_iter().collect();
    output.sort();
    output
}

fn is_stub_definition(definition: Node) -> bool {
    let Some(body) = definition.child_by_field_name("body") else {
        return false;
    };
    let statements = statement_children(body);
    if statements.is_empty() {
        return false;
    }
    let index = if is_docstring_statement(statements[0]) {
        1
    } else {
        0
    };
    let remaining = &statements[index..];
    if remaining.is_empty() {
        return index == 1;
    }
    if remaining.len() != 1 {
        return false;
    }
    let statement = remaining[0];
    statement.kind() == "pass_statement" || is_ellipsis_statement(statement)
}

pub fn discover_stubs(source: &str, file_path: &str) -> Vec<StubInfo> {
    let Some(tree) = parse_owned(source) else {
        return Vec::new();
    };
    let root = tree.root_node();
    let source_bytes = source.as_bytes();
    let mut definitions = Vec::new();
    collect_definitions(root, source_bytes, &[], &mut definitions);

    let mut stubs = Vec::new();
    for (chain, wrapper, definition) in definitions {
        if definition.kind() != "function_definition" || !is_stub_definition(definition) {
            continue;
        }
        let symbol_path = format!("{}::{}", file_path, chain.join("::"));
        stubs.push(StubInfo {
            symbol_path,
            signature: signature_from_definition(wrapper, definition, source_bytes),
        });
    }
    stubs
}

pub fn file_symbols(source: &str, file_path: &str) -> Vec<String> {
    let Some(tree) = parse_owned(source) else {
        return Vec::new();
    };
    let root = tree.root_node();
    let mut definitions = Vec::new();
    collect_definitions(root, source.as_bytes(), &[], &mut definitions);
    definitions
        .into_iter()
        .map(|(chain, _, _)| format!("{}::{}", file_path, chain.join("::")))
        .collect()
}

pub fn file_functions(source: &str, file_path: &str) -> Vec<String> {
    let Some(tree) = parse_owned(source) else {
        return Vec::new();
    };
    let root = tree.root_node();
    let mut definitions = Vec::new();
    collect_definitions(root, source.as_bytes(), &[], &mut definitions);
    definitions
        .into_iter()
        .filter(|(_, _, definition)| definition.kind() == "function_definition")
        .map(|(chain, _, _)| format!("{}::{}", file_path, chain.join("::")))
        .collect()
}

/// One file's full scan result (used by the whole-repo Rust scanner).
#[derive(Clone, Debug)]
pub struct FileScan {
    pub file_path: String,
    pub stubs: Vec<StubInfo>,
    pub symbols: Vec<String>,
    pub references: Vec<(String, Vec<String>)>,
}

/// Walk a working tree with ripgrep's ignore rules and parse every Python file
/// on all CPU cores. The caller releases the GIL around this function.
pub fn scan_repository(root: &str, include_tests: bool) -> Result<Vec<FileScan>, String> {
    use ignore::WalkBuilder;
    use rayon::prelude::*;
    use std::fs;
    use std::path::{Path, PathBuf};

    let root_path = Path::new(root);
    let mut files: Vec<(String, PathBuf)> = Vec::new();
    let mut builder = WalkBuilder::new(root_path);
    builder
        .hidden(false)
        .git_ignore(true)
        .git_global(false)
        .git_exclude(true);

    for entry in builder.build() {
        let entry = entry.map_err(|error| error.to_string())?;
        let Some(file_type) = entry.file_type() else {
            continue;
        };
        if !file_type.is_file() {
            continue;
        }
        let path = entry.path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("py") {
            continue;
        }
        let relative = path
            .strip_prefix(root_path)
            .unwrap_or(path)
            .to_string_lossy()
            .replace('\\', "/");
        if relative.split('/').any(|part| {
            matches!(
                part,
                ".git"
                    | "target"
                    | "node_modules"
                    | ".venv"
                    | "venv"
                    | "__pycache__"
                    | ".pytest_cache"
                    | ".mypy_cache"
                    | ".ruff_cache"
            )
        }) {
            continue;
        }
        if !include_tests
            && (relative.starts_with("tests/")
                || relative
                    .rsplit('/')
                    .next()
                    .map(|name| name.starts_with("test_"))
                    .unwrap_or(false))
        {
            continue;
        }
        files.push((relative, path.to_path_buf()));
    }

    let scans: Result<Vec<FileScan>, String> = files
        .par_iter()
        .map(|(relative, path)| {
            let content =
                fs::read_to_string(path).map_err(|error| format!("{relative}: {error}"))?;
            let stubs = discover_stubs(&content, relative);
            let symbols = file_symbols(&content, relative);
            let references = symbols
                .iter()
                .map(|symbol| (symbol.clone(), referenced_names(&content, symbol)))
                .collect();
            Ok(FileScan {
                file_path: relative.clone(),
                stubs,
                symbols,
                references,
            })
        })
        .collect();
    scans
}

#[cfg(test)]
mod tests {
    use super::*;

    const SOURCE: &str = r#"
"""module doc"""


class A:
    """A doc."""

    x: int = 1

    def f(self, a: int) -> int:
        """f doc."""
        return a + 1


@decorator
def h(x: int) -> int:
    """h doc"""
    return x * 2


def stub(a: int) -> int:
    """stub doc"""
    ...
"#;

    #[test]
    fn signature_strips_body_and_keeps_docstring() {
        let info = signature(SOURCE, "src/a.py::h");
        assert!(info.found);
        assert_eq!(info.docstring.as_deref(), Some("h doc"));
        assert!(info.skeleton.contains("@decorator"));
        assert!(!info.skeleton.contains("x * 2"));
    }

    #[test]
    fn hash_stable_across_body_change() {
        let left = signature_hash(SOURCE, "src/a.py::h").unwrap();
        let changed = SOURCE.replace("x * 2", "x * 3");
        assert_eq!(left, signature_hash(&changed, "src/a.py::h").unwrap());
    }

    #[test]
    fn scans_a_repository_in_parallel() {
        let root = std::env::temp_dir().join(format!(
            "veynod-scan-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|duration| duration.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::write(
            root.join("src/a.py"),
            "def f(a: int) -> int:
    \"\"\"f\"\"\"
    ...
",
        )
        .unwrap();
        std::fs::write(
            root.join("src/b.py"),
            "def g() -> int:
    return 1
",
        )
        .unwrap();
        let scans = scan_repository(root.to_str().unwrap(), true).unwrap();
        assert_eq!(scans.len(), 2);
        assert!(scans.iter().any(|scan| scan.file_path == "src/a.py"
            && scan
                .stubs
                .iter()
                .any(|stub| stub.symbol_path == "src/a.py::f")));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn discovers_stubs() {
        let stubs = discover_stubs(SOURCE, "src/a.py");
        let symbols: Vec<&str> = stubs.iter().map(|stub| stub.symbol_path.as_str()).collect();
        assert!(symbols.contains(&"src/a.py::stub"));
        assert!(!symbols.contains(&"src/a.py::h"));
    }
}
