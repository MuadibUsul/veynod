//! PyO3 surface for the Veynod Kernel Rust core.

use std::collections::{HashMap, HashSet};
use std::sync::{Mutex, MutexGuard};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

mod ast;
mod locks;
mod sandbox;
mod scheduler_core;

use scheduler_core::{CoreState, TaskRecord};

type TaskTuple = (
    String,
    String,
    String,
    String,
    i64,
    i64,
    i64,
    Vec<String>,
    Vec<String>,
    Option<String>,
    Option<String>,
);

#[pyclass]
struct SchedulerCore {
    inner: Mutex<CoreState>,
}

impl SchedulerCore {
    fn state(&self) -> PyResult<MutexGuard<'_, CoreState>> {
        self.inner
            .lock()
            .map_err(|_| PyRuntimeError::new_err("scheduler core lock poisoned"))
    }
}

#[pymethods]
impl SchedulerCore {
    #[new]
    fn new() -> Self {
        Self {
            inner: Mutex::new(CoreState::default()),
        }
    }

    /// Upsert task snapshots. A snapshot older than the in-memory version is
    /// ignored, so a sync racing a claim cannot clobber the lease.
    fn upsert_tasks(&self, tasks: Vec<TaskTuple>) -> PyResult<()> {
        let records: Vec<TaskRecord> = tasks
            .into_iter()
            .map(
                |(
                    task_id,
                    workspace_id,
                    task_type,
                    status,
                    priority,
                    seq,
                    version,
                    write_locks,
                    ancestor_locks,
                    lease_token_hash,
                    leased_by,
                )| TaskRecord {
                    task_id,
                    workspace_id,
                    task_type,
                    status,
                    priority,
                    seq,
                    version,
                    write_locks,
                    ancestor_locks,
                    lease_token_hash,
                    leased_by,
                },
            )
            .collect();
        self.state()?.upsert_tasks(records);
        Ok(())
    }

    fn remove_tasks(&self, task_ids: Vec<String>) -> PyResult<()> {
        self.state()?.remove_tasks(task_ids);
        Ok(())
    }

    fn claim_next(
        &self,
        workspace_id: &str,
        supported_types: Vec<String>,
        runner_id: &str,
        lease_token_hash: &str,
        claim_version: i64,
    ) -> PyResult<Option<String>> {
        let supported: HashSet<String> = supported_types.into_iter().collect();
        Ok(self
            .state()?
            .claim_next(
                workspace_id,
                &supported,
                runner_id,
                lease_token_hash,
                claim_version,
            )
            .map(|task| task.task_id))
    }

    fn revert_claim(
        &self,
        workspace_id: &str,
        task_id: &str,
        lease_token_hash: &str,
    ) -> PyResult<bool> {
        Ok(self
            .state()?
            .revert_claim(workspace_id, task_id, lease_token_hash))
    }

    fn reset(&self) -> PyResult<()> {
        self.state()?.reset();
        Ok(())
    }

    fn reset_workspace(&self, workspace_id: &str) -> PyResult<()> {
        self.state()?.reset_workspace(workspace_id);
        Ok(())
    }

    fn stats(&self, py: Python<'_>, workspace_id: &str) -> PyResult<Py<PyDict>> {
        let (tasks, ready, active, blocked) = self.state()?.stats(workspace_id);
        let dict = PyDict::new(py);
        dict.set_item("tasks", tasks)?;
        dict.set_item("ready", ready)?;
        dict.set_item("active", active)?;
        dict.set_item("blocked", blocked)?;
        Ok(dict.unbind())
    }

    fn ready_count(&self, workspace_id: &str) -> PyResult<usize> {
        Ok(self.state()?.ready_count(workspace_id))
    }
}

#[pyfunction]
fn normalize_symbol_path(raw: &str) -> PyResult<String> {
    locks::normalize(raw).map_err(PyValueError::new_err)
}

#[pyfunction]
fn expand_write_set(paths: Vec<String>) -> PyResult<(Vec<String>, Vec<String>)> {
    locks::expand_write_set(&paths).map_err(PyValueError::new_err)
}

#[pyfunction]
fn is_sub_write_set(parent: Vec<String>, child: Vec<String>) -> PyResult<bool> {
    locks::is_sub_write_set(&parent, &child).map_err(PyValueError::new_err)
}

#[pyfunction]
fn is_covered_by_write_set(target: &str, allowed: Vec<String>) -> PyResult<bool> {
    locks::is_covered(target, &allowed).map_err(PyValueError::new_err)
}

#[pyfunction]
fn conflicts(left: &str, right: &str) -> PyResult<bool> {
    locks::conflicts(left, right).map_err(PyValueError::new_err)
}

#[pyfunction]
fn promote_new_files(paths: Vec<String>, existing: HashSet<String>) -> PyResult<Vec<String>> {
    locks::promote_new_files(&paths, &existing).map_err(PyValueError::new_err)
}

#[pyfunction]
fn file_paths_for_locks(lock_paths: Vec<String>) -> Vec<String> {
    locks::file_paths_for_locks(&lock_paths)
        .into_iter()
        .collect()
}

/// tasks: [(task_id, write_locks, ancestor_locks, status, priority, seq)]
#[pyfunction]
fn plan_claimable(
    tasks: Vec<(String, Vec<String>, Vec<String>, String, i64, i64)>,
) -> PyResult<(Vec<String>, Vec<(String, Vec<String>)>)> {
    let snapshots: Vec<locks::TaskSnapshot> = tasks
        .into_iter()
        .map(
            |(task_id, write_locks, ancestor_locks, status, priority, seq)| locks::TaskSnapshot {
                task_id,
                write_locks,
                ancestor_locks,
                status,
                priority,
                seq,
            },
        )
        .collect();
    let plan = locks::plan_claimable(snapshots);
    Ok((plan.claimable, plan.conflicts))
}

#[pyfunction]
fn signature(source: &str, symbol_path: &str) -> PyResult<(String, String, Option<String>, bool)> {
    let info = ast::signature(source, symbol_path);
    Ok((
        info.skeleton,
        info.signature_hash,
        info.docstring,
        info.found,
    ))
}

#[pyfunction]
fn signature_hash(source: &str, symbol_path: &str) -> Option<String> {
    ast::signature_hash(source, symbol_path)
}

#[pyfunction]
fn node_source(source: &str, symbol_path: &str) -> Option<String> {
    ast::node_source(source, symbol_path)
}

#[pyfunction]
fn referenced_names(source: &str, symbol_path: &str) -> Vec<String> {
    ast::referenced_names(source, symbol_path)
}

/// Returns [(symbol_path, skeleton, hash, docstring, found)].
#[pyfunction]
fn discover_stubs(
    source: &str,
    file_path: &str,
) -> Vec<(String, String, String, Option<String>, bool)> {
    ast::discover_stubs(source, file_path)
        .into_iter()
        .map(|stub| {
            (
                stub.symbol_path,
                stub.signature.skeleton,
                stub.signature.signature_hash,
                stub.signature.docstring,
                stub.signature.found,
            )
        })
        .collect()
}

#[pyfunction]
fn file_symbols(source: &str, file_path: &str) -> Vec<String> {
    ast::file_symbols(source, file_path)
}

#[pyfunction]
fn file_functions(source: &str, file_path: &str) -> Vec<String> {
    ast::file_functions(source, file_path)
}

type SymbolSnapshotTuple = (
    String,
    String,
    String,
    String,
    Option<String>,
    Vec<String>,
);

/// One parse per file: whole-file skeleton plus every symbol's raw-source hash,
/// signature and reference names. Used by the merge snapshot cache.
#[pyfunction]
fn file_snapshot(
    source: &str,
    file_path: &str,
) -> (String, String, Vec<SymbolSnapshotTuple>, Vec<String>) {
    let snapshot = ast::file_snapshot(source, file_path);
    (
        snapshot.skeleton,
        snapshot.signature_hash,
        snapshot
            .symbols
            .into_iter()
            .map(|symbol| {
                (
                    symbol.symbol_path,
                    symbol.source_hash,
                    symbol.signature_hash,
                    symbol.skeleton,
                    symbol.docstring,
                    symbol.referenced_names,
                )
            })
            .collect(),
        snapshot.referenced_names,
    )
}

type StubTuple = (String, String, String, Option<String>, bool);
type ScanTuple = (
    String,
    Vec<StubTuple>,
    Vec<String>,
    Vec<(String, Vec<String>)>,
);

/// Whole-working-tree Rust scanner: ignore rules + rayon + GIL release.
#[pyfunction]
#[pyo3(signature = (repo_path, include_tests=false))]
fn scan_workspace_repo(
    py: Python<'_>,
    repo_path: &str,
    include_tests: bool,
) -> PyResult<Vec<ScanTuple>> {
    let root = repo_path.to_string();
    let scans = py
        .allow_threads(move || ast::scan_repository(&root, include_tests))
        .map_err(PyRuntimeError::new_err)?;
    Ok(scans
        .into_iter()
        .map(|scan| {
            (
                scan.file_path,
                scan.stubs
                    .into_iter()
                    .map(|stub| {
                        (
                            stub.symbol_path,
                            stub.signature.skeleton,
                            stub.signature.signature_hash,
                            stub.signature.docstring,
                            stub.signature.found,
                        )
                    })
                    .collect(),
                scan.symbols,
                scan.references,
            )
        })
        .collect())
}

#[pyfunction]
#[pyo3(signature = (command, cwd, timeout_seconds=600.0, extra_env=None, output_limit=4000))]
fn run_command(
    py: Python<'_>,
    command: &str,
    cwd: &str,
    timeout_seconds: f64,
    extra_env: Option<HashMap<String, String>>,
    output_limit: usize,
) -> PyResult<Py<PyDict>> {
    let command = command.to_string();
    let cwd = cwd.to_string();
    let env = extra_env.unwrap_or_default();
    let result = py
        .allow_threads(move || {
            sandbox::run_command(&command, &cwd, timeout_seconds, &env, output_limit)
        })
        .map_err(PyRuntimeError::new_err)?;

    let dict = PyDict::new(py);
    dict.set_item("returncode", result.returncode)?;
    dict.set_item("output", result.output)?;
    dict.set_item("timed_out", result.timed_out)?;
    dict.set_item("duration_ms", result.duration_ms)?;
    Ok(dict.unbind())
}

#[pymodule]
fn veynod_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add_class::<SchedulerCore>()?;
    module.add_function(wrap_pyfunction!(normalize_symbol_path, module)?)?;
    module.add_function(wrap_pyfunction!(expand_write_set, module)?)?;
    module.add_function(wrap_pyfunction!(is_sub_write_set, module)?)?;
    module.add_function(wrap_pyfunction!(is_covered_by_write_set, module)?)?;
    module.add_function(wrap_pyfunction!(conflicts, module)?)?;
    module.add_function(wrap_pyfunction!(promote_new_files, module)?)?;
    module.add_function(wrap_pyfunction!(file_paths_for_locks, module)?)?;
    module.add_function(wrap_pyfunction!(plan_claimable, module)?)?;
    module.add_function(wrap_pyfunction!(signature, module)?)?;
    module.add_function(wrap_pyfunction!(signature_hash, module)?)?;
    module.add_function(wrap_pyfunction!(node_source, module)?)?;
    module.add_function(wrap_pyfunction!(referenced_names, module)?)?;
    module.add_function(wrap_pyfunction!(discover_stubs, module)?)?;
    module.add_function(wrap_pyfunction!(file_symbols, module)?)?;
    module.add_function(wrap_pyfunction!(file_functions, module)?)?;
    module.add_function(wrap_pyfunction!(file_snapshot, module)?)?;
    module.add_function(wrap_pyfunction!(scan_workspace_repo, module)?)?;
    module.add_function(wrap_pyfunction!(run_command, module)?)?;
    Ok(())
}
