//! Authoritative in-memory DAG scheduler.
//!
//! The Rust core owns the graph, the hierarchical write-set lock indexes and
//! the READY priority queue. Python persists each claim in PostgreSQL inside a
//! per-workspace advisory lock and re-validates the selected task with a
//! single-row GIN predicate; Postgres is the durability/ACID fence, while the
//! scheduling decision itself is made here.
//!
//! Key invariant: a READY task is in the claim queue only when `block_counts`
//! is zero. `block_counts` counts every active (LEASED/VERIFYING) lock hit that
//! would violate the three-way write-set disjointness rule. Claiming or
//! releasing an active task updates only the candidate sets indexed by its own
//! lock paths, so a 1M-node graph does not require an O(N) scan per claim.

use std::cmp::Reverse;
use std::collections::{BTreeSet, HashMap, HashSet};

#[derive(Clone, Debug)]
pub struct TaskRecord {
    pub task_id: String,
    pub workspace_id: String,
    pub task_type: String,
    pub status: String,
    pub priority: i64,
    pub seq: i64,
    /// DB `updated_at` in microseconds since epoch, used as a monotonic version
    /// so a stale sync cannot clobber a newer in-memory claim.
    pub version: i64,
    pub write_locks: Vec<String>,
    pub ancestor_locks: Vec<String>,
    pub lease_token_hash: Option<String>,
    pub leased_by: Option<String>,
}

impl TaskRecord {
    fn active(&self) -> bool {
        self.status == "LEASED" || self.status == "VERIFYING"
    }

    fn done(&self) -> bool {
        matches!(
            self.status.as_str(),
            "COMMITTED" | "FAILED" | "SPLIT_ARCHIVED"
        )
    }

    fn ready(&self) -> bool {
        self.status == "READY"
    }
}

type ReadyKey = (Reverse<i64>, i64, String);

fn ready_key(task: &TaskRecord) -> ReadyKey {
    (Reverse(task.priority), task.seq, task.task_id.clone())
}

#[derive(Default)]
pub struct WorkspaceState {
    tasks: HashMap<String, TaskRecord>,
    ready: BTreeSet<ReadyKey>,
    write_index: HashMap<String, HashSet<String>>,
    ancestor_index: HashMap<String, HashSet<String>>,
    active_write: HashMap<String, HashSet<String>>,
    active_ancestor: HashMap<String, HashSet<String>>,
    block_counts: HashMap<String, usize>,
}

impl WorkspaceState {
    fn active_hits(&self, task: &TaskRecord) -> usize {
        let mut count = 0usize;
        for lock in &task.write_locks {
            count += self.active_write.get(lock).map_or(0, HashSet::len);
            count += self.active_ancestor.get(lock).map_or(0, HashSet::len);
        }
        for lock in &task.ancestor_locks {
            count += self.active_write.get(lock).map_or(0, HashSet::len);
        }
        count
    }

    fn insert_non_active(&mut self, task: &TaskRecord) {
        let task_id = task.task_id.clone();
        for lock in &task.write_locks {
            self.write_index
                .entry(lock.clone())
                .or_default()
                .insert(task_id.clone());
        }
        for lock in &task.ancestor_locks {
            self.ancestor_index
                .entry(lock.clone())
                .or_default()
                .insert(task_id.clone());
        }
        let count = self.active_hits(task);
        self.block_counts.insert(task_id.clone(), count);
        if task.ready() && count == 0 {
            self.ready.insert(ready_key(task));
        }
    }

    fn remove_non_active(&mut self, task: &TaskRecord) {
        let task_id = &task.task_id;
        for lock in &task.write_locks {
            if let Some(set) = self.write_index.get_mut(lock) {
                set.remove(task_id);
                if set.is_empty() {
                    self.write_index.remove(lock);
                }
            }
        }
        for lock in &task.ancestor_locks {
            if let Some(set) = self.ancestor_index.get_mut(lock) {
                set.remove(task_id);
                if set.is_empty() {
                    self.ancestor_index.remove(lock);
                }
            }
        }
        self.block_counts.remove(task_id);
        self.ready.remove(&ready_key(task));
    }

    fn increment_block(&mut self, candidate_id: &str) {
        if let Some(count) = self.block_counts.get_mut(candidate_id) {
            let was_zero = *count == 0;
            *count += 1;
            if was_zero {
                if let Some(candidate) = self.tasks.get(candidate_id) {
                    if candidate.ready() {
                        self.ready.remove(&ready_key(candidate));
                    }
                }
            }
        }
    }

    fn decrement_block(&mut self, candidate_id: &str) {
        if let Some(count) = self.block_counts.get_mut(candidate_id) {
            if *count > 0 {
                *count -= 1;
                if *count == 0 {
                    if let Some(candidate) = self.tasks.get(candidate_id) {
                        if candidate.ready() {
                            self.ready.insert(ready_key(candidate));
                        }
                    }
                }
            }
        }
    }

    fn add_active(&mut self, task: &TaskRecord) {
        let task_id = task.task_id.clone();
        for lock in &task.write_locks {
            self.active_write
                .entry(lock.clone())
                .or_default()
                .insert(task_id.clone());
        }
        for lock in &task.ancestor_locks {
            self.active_ancestor
                .entry(lock.clone())
                .or_default()
                .insert(task_id.clone());
        }

        let mut affected: HashSet<String> = HashSet::new();
        for lock in &task.write_locks {
            if let Some(set) = self.write_index.get(lock) {
                affected.extend(set.iter().cloned());
            }
            if let Some(set) = self.ancestor_index.get(lock) {
                affected.extend(set.iter().cloned());
            }
        }
        for lock in &task.ancestor_locks {
            if let Some(set) = self.write_index.get(lock) {
                affected.extend(set.iter().cloned());
            }
        }
        for candidate in affected {
            self.increment_block(&candidate);
        }
    }

    fn remove_active(&mut self, task: &TaskRecord) {
        let task_id = &task.task_id;

        // Collect the same candidate sets before removing the active maps.
        let mut affected: HashSet<String> = HashSet::new();
        for lock in &task.write_locks {
            if let Some(set) = self.write_index.get(lock) {
                affected.extend(set.iter().cloned());
            }
            if let Some(set) = self.ancestor_index.get(lock) {
                affected.extend(set.iter().cloned());
            }
        }
        for lock in &task.ancestor_locks {
            if let Some(set) = self.write_index.get(lock) {
                affected.extend(set.iter().cloned());
            }
        }

        for lock in &task.write_locks {
            if let Some(set) = self.active_write.get_mut(lock) {
                set.remove(task_id);
                if set.is_empty() {
                    self.active_write.remove(lock);
                }
            }
        }
        for lock in &task.ancestor_locks {
            if let Some(set) = self.active_ancestor.get_mut(lock) {
                set.remove(task_id);
                if set.is_empty() {
                    self.active_ancestor.remove(lock);
                }
            }
        }

        for candidate in affected {
            self.decrement_block(&candidate);
        }
    }

    fn remove_state(&mut self, task: &TaskRecord) {
        if task.active() {
            self.remove_active(task);
        } else if !task.done() {
            self.remove_non_active(task);
        }
    }

    fn insert_state(&mut self, task: TaskRecord) {
        let task_id = task.task_id.clone();
        self.tasks.insert(task_id.clone(), task.clone());
        if task.active() {
            self.add_active(&task);
        } else if !task.done() {
            self.insert_non_active(&task);
        }
    }

    pub fn upsert(&mut self, task: TaskRecord) {
        if let Some(existing) = self.tasks.get(&task.task_id) {
            if task.version < existing.version {
                // A stale snapshot read before a newer in-memory claim/commit.
                return;
            }
        }
        if let Some(old) = self.tasks.remove(&task.task_id) {
            self.remove_state(&old);
        }
        self.insert_state(task);
    }

    pub fn remove_task(&mut self, task_id: &str) {
        if let Some(task) = self.tasks.remove(task_id) {
            self.remove_state(&task);
        }
    }

    pub fn claim_next(
        &mut self,
        supported_types: &HashSet<String>,
        runner_id: &str,
        lease_token_hash: &str,
        claim_version: i64,
    ) -> Option<TaskRecord> {
        let chosen_key = self.ready.iter().find_map(|key| {
            let task = self.tasks.get(&key.2)?;
            if supported_types.is_empty() || supported_types.contains(&task.task_type) {
                Some(key.clone())
            } else {
                None
            }
        })?;

        let task_id = chosen_key.2.clone();
        let old = self.tasks.get(&task_id)?.clone();
        self.remove_non_active(&old);

        let mut claimed = old;
        claimed.status = "LEASED".to_string();
        claimed.leased_by = Some(runner_id.to_string());
        claimed.lease_token_hash = Some(lease_token_hash.to_string());
        claimed.version = claim_version.max(claimed.version + 1);
        self.insert_state(claimed.clone());
        Some(claimed)
    }

    pub fn revert_claim(&mut self, task_id: &str, lease_token_hash: &str) -> bool {
        let Some(task) = self.tasks.get(task_id).cloned() else {
            return false;
        };
        if task.status != "LEASED" || task.lease_token_hash.as_deref() != Some(lease_token_hash) {
            return false;
        }
        self.tasks.remove(task_id);
        self.remove_state(&task);

        let mut reverted = task;
        reverted.status = "READY".to_string();
        reverted.leased_by = None;
        reverted.lease_token_hash = None;
        reverted.version += 1;
        self.insert_state(reverted);
        true
    }

    pub fn counts(&self) -> (usize, usize, usize, usize) {
        let active = self.tasks.values().filter(|task| task.active()).count();
        let ready = self.ready.len();
        let blocked = self
            .tasks
            .values()
            .filter(|task| !task.active() && !task.done() && !task.ready())
            .count();
        (self.tasks.len(), ready, active, blocked)
    }
}

#[derive(Default)]
pub struct CoreState {
    workspaces: HashMap<String, WorkspaceState>,
    locations: HashMap<String, String>,
}

impl CoreState {
    pub fn upsert_tasks(&mut self, tasks: Vec<TaskRecord>) {
        for task in tasks {
            self.locations
                .insert(task.task_id.clone(), task.workspace_id.clone());
            self.workspaces
                .entry(task.workspace_id.clone())
                .or_default()
                .upsert(task);
        }
    }

    pub fn remove_tasks(&mut self, task_ids: Vec<String>) {
        for task_id in task_ids {
            if let Some(workspace_id) = self.locations.remove(&task_id) {
                if let Some(state) = self.workspaces.get_mut(&workspace_id) {
                    state.remove_task(&task_id);
                }
            }
        }
    }

    pub fn claim_next(
        &mut self,
        workspace_id: &str,
        supported_types: &HashSet<String>,
        runner_id: &str,
        lease_token_hash: &str,
        claim_version: i64,
    ) -> Option<TaskRecord> {
        self.workspaces.get_mut(workspace_id).and_then(|state| {
            state.claim_next(supported_types, runner_id, lease_token_hash, claim_version)
        })
    }

    pub fn revert_claim(
        &mut self,
        workspace_id: &str,
        task_id: &str,
        lease_token_hash: &str,
    ) -> bool {
        self.workspaces
            .get_mut(workspace_id)
            .map(|state| state.revert_claim(task_id, lease_token_hash))
            .unwrap_or(false)
    }

    pub fn reset(&mut self) {
        self.workspaces.clear();
        self.locations.clear();
    }

    pub fn reset_workspace(&mut self, workspace_id: &str) {
        if let Some(state) = self.workspaces.remove(workspace_id) {
            for task_id in state.tasks.keys() {
                self.locations.remove(task_id);
            }
        }
    }

    pub fn stats(&self, workspace_id: &str) -> (usize, usize, usize, usize) {
        self.workspaces
            .get(workspace_id)
            .map(|state| state.counts())
            .unwrap_or((0, 0, 0, 0))
    }

    pub fn ready_count(&self, workspace_id: &str) -> usize {
        self.workspaces
            .get(workspace_id)
            .map(|state| state.ready.len())
            .unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn task(
        id: &str,
        status: &str,
        write: &[&str],
        ancestor: &[&str],
        priority: i64,
        version: i64,
    ) -> TaskRecord {
        TaskRecord {
            task_id: id.to_string(),
            workspace_id: "ws".to_string(),
            task_type: "IMPLEMENT_BODY".to_string(),
            status: status.to_string(),
            priority,
            seq: version,
            version,
            write_locks: write.iter().map(|value| value.to_string()).collect(),
            ancestor_locks: ancestor.iter().map(|value| value.to_string()).collect(),
            lease_token_hash: None,
            leased_by: None,
        }
    }

    #[test]
    fn claims_only_disjoint_ready_tasks() {
        let mut core = CoreState::default();
        core.upsert_tasks(vec![
            task("active", "LEASED", &["src/a.py"], &["src"], 1, 1),
            task(
                "blocked",
                "READY",
                &["src/a.py::f"],
                &["src", "src/a.py"],
                100,
                2,
            ),
            task("ok", "READY", &["src/b.py::h"], &["src", "src/b.py"], 50, 3),
        ]);
        let claimed = core
            .claim_next("ws", &HashSet::new(), "runner", "token", 100)
            .unwrap();
        assert_eq!(claimed.task_id, "ok");
        assert!(core
            .claim_next("ws", &HashSet::new(), "runner", "token2", 200)
            .is_none());
    }

    #[test]
    fn release_unblocks_contenders() {
        let mut core = CoreState::default();
        core.upsert_tasks(vec![
            task("active", "LEASED", &["src/a.py"], &["src"], 1, 1),
            task(
                "waiting",
                "READY",
                &["src/a.py::f"],
                &["src", "src/a.py"],
                100,
                2,
            ),
        ]);
        assert!(core
            .claim_next("ws", &HashSet::new(), "runner", "token", 100)
            .is_none());
        core.upsert_tasks(vec![task(
            "active",
            "COMMITTED",
            &["src/a.py"],
            &["src"],
            1,
            3,
        )]);
        let claimed = core
            .claim_next("ws", &HashSet::new(), "runner", "token2", 200)
            .unwrap();
        assert_eq!(claimed.task_id, "waiting");
    }

    #[test]
    fn stale_snapshot_does_not_clobber_claim() {
        let mut core = CoreState::default();
        core.upsert_tasks(vec![task("a", "READY", &["src/a.py::f"], &["src"], 1, 10)]);
        let claimed = core
            .claim_next("ws", &HashSet::new(), "runner", "token", 100)
            .unwrap();
        assert_eq!(claimed.status, "LEASED");
        // A sync that read the DB before the claim carries version 10.
        core.upsert_tasks(vec![task("a", "READY", &["src/a.py::f"], &["src"], 1, 10)]);
        assert!(core
            .claim_next("ws", &HashSet::new(), "runner", "token2", 200)
            .is_none());
    }

    #[test]
    fn revert_restores_ready_task() {
        let mut core = CoreState::default();
        core.upsert_tasks(vec![task("a", "READY", &["src/a.py::f"], &["src"], 1, 1)]);
        let claimed = core
            .claim_next("ws", &HashSet::new(), "runner", "token", 100)
            .unwrap();
        assert!(core.revert_claim("ws", &claimed.task_id, "token"));
        let again = core
            .claim_next("ws", &HashSet::new(), "runner", "token2", 200)
            .unwrap();
        assert_eq!(again.task_id, "a");
    }

    #[test]
    #[ignore = "1M scale test; run with cargo test --release -- --ignored"]
    fn million_node_claim() {
        let mut core = CoreState::default();
        let mut tasks = Vec::with_capacity(1_000_000);
        for index in 0..1_000_000i64 {
            tasks.push(TaskRecord {
                task_id: format!("t{index}"),
                workspace_id: "ws".to_string(),
                task_type: "IMPLEMENT_BODY".to_string(),
                status: "READY".to_string(),
                priority: 100,
                seq: index,
                version: index + 1,
                write_locks: vec![format!("src/f{index}.py::f")],
                ancestor_locks: vec!["src".to_string()],
                lease_token_hash: None,
                leased_by: None,
            });
        }

        let load_started = std::time::Instant::now();
        core.upsert_tasks(tasks);
        let load_elapsed = load_started.elapsed();

        let claim_started = std::time::Instant::now();
        for index in 0..1000i64 {
            let claimed = core.claim_next(
                "ws",
                &HashSet::new(),
                "runner",
                &format!("token{index}"),
                10_000 + index,
            );
            assert!(claimed.is_some());
        }
        let claim_elapsed = claim_started.elapsed();
        eprintln!(
            "1M upsert: {:?}; 1000 claims: {:?}; stats: {:?}",
            load_elapsed,
            claim_elapsed,
            core.stats("ws")
        );
    }

    #[test]
    fn filters_by_supported_types() {
        let mut core = CoreState::default();
        let mut write_task = task("write", "READY", &["tests/a.py"], &["tests"], 100, 1);
        write_task.task_type = "WRITE_TEST".to_string();
        let mut impl_task = task("impl", "READY", &["src/a.py::f"], &["src"], 10, 2);
        impl_task.task_type = "IMPLEMENT_BODY".to_string();
        core.upsert_tasks(vec![write_task, impl_task]);
        let supported: HashSet<String> = ["IMPLEMENT_BODY".to_string()].into_iter().collect();
        let claimed = core
            .claim_next("ws", &supported, "runner", "token", 100)
            .unwrap();
        assert_eq!(claimed.task_id, "impl");
    }
}
