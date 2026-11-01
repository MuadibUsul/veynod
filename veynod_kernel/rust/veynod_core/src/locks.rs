//! Hierarchical write-set lock algebra.
//!
//! Canonical form: `src/order/pricing.py::PricingEngine::calc_discount`.
//! The Python implementation in `app/mcp_server/syscalls.py` is kept as the
//! protocol-level validator; this module is the high-throughput engine path.

use std::collections::{BTreeSet, HashSet};

pub fn normalize(raw: &str) -> Result<String, String> {
    let mut cleaned = raw.trim().replace('\\', "/");
    cleaned = cleaned.trim_matches('/').to_string();
    while cleaned.starts_with("./") {
        cleaned = cleaned[2..].to_string();
    }
    if cleaned.is_empty() || cleaned.contains('\u{0}') {
        return Err(format!("Invalid or unsafe symbol path: {raw:?}"));
    }

    let parts: Vec<&str> = cleaned.split("::").collect();
    let file_segments: Vec<&str> = parts[0]
        .split('/')
        .filter(|segment| !segment.is_empty())
        .collect();
    if file_segments.is_empty()
        || file_segments
            .iter()
            .any(|segment| *segment == "." || *segment == "..")
    {
        return Err(format!("Invalid or unsafe symbol path: {raw:?}"));
    }

    let mut symbols: Vec<String> = Vec::new();
    for raw_symbol in parts.iter().skip(1) {
        let expanded = raw_symbol.replace('.', "::");
        for symbol in expanded.split("::") {
            let symbol = symbol.trim();
            if symbol.is_empty() {
                continue;
            }
            if symbol.contains("..")
                || symbol.contains('/')
                || symbol.contains('\\')
                || symbol.contains('\u{0}')
            {
                return Err(format!("Invalid or unsafe symbol path: {raw:?}"));
            }
            symbols.push(symbol.to_string());
        }
    }

    let mut result = file_segments.join("/");
    for symbol in symbols {
        result.push_str("::");
        result.push_str(&symbol);
    }
    Ok(result)
}

pub fn expand_write_set(paths: &[String]) -> Result<(Vec<String>, Vec<String>), String> {
    let mut write: BTreeSet<String> = BTreeSet::new();
    let mut ancestor: BTreeSet<String> = BTreeSet::new();

    for raw in paths {
        let normalized = normalize(raw)?;
        write.insert(normalized.clone());
        let parts: Vec<&str> = normalized.split("::").collect();
        let file_part = parts[0];
        let directories: Vec<&str> = file_part.split('/').collect();
        for index in 1..directories.len() {
            ancestor.insert(directories[..index].join("/"));
        }
        ancestor.insert(file_part.to_string());
        for index in 1..parts.len() {
            ancestor.insert(parts[..=index].join("::"));
        }
    }
    for lock in &write {
        ancestor.remove(lock);
    }
    Ok((write.into_iter().collect(), ancestor.into_iter().collect()))
}

pub fn is_sub_write_set(parent_locks: &[String], child_paths: &[String]) -> Result<bool, String> {
    for raw in child_paths {
        let normalized = normalize(raw)?;
        let covered = parent_locks.iter().any(|parent| {
            normalized == *parent
                || normalized.starts_with(&format!("{parent}::"))
                || normalized.starts_with(&format!("{parent}/"))
        });
        if !covered {
            return Ok(false);
        }
    }
    Ok(true)
}

pub fn is_covered(target: &str, allowed: &[String]) -> Result<bool, String> {
    let normalized = normalize(target)?;
    for raw in allowed {
        let parent = normalize(raw)?;
        if normalized == parent
            || normalized.starts_with(&format!("{parent}::"))
            || normalized.starts_with(&format!("{parent}/"))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

pub fn conflicts(left: &str, right: &str) -> Result<bool, String> {
    let a = normalize(left)?;
    let b = normalize(right)?;
    if a == b {
        return Ok(true);
    }
    Ok(a.starts_with(&format!("{b}::"))
        || b.starts_with(&format!("{a}::"))
        || a.starts_with(&format!("{b}/"))
        || b.starts_with(&format!("{a}/")))
}

pub fn promote_new_files(
    paths: &[String],
    existing: &HashSet<String>,
) -> Result<Vec<String>, String> {
    let mut promoted: BTreeSet<String> = BTreeSet::new();
    for raw in paths {
        let normalized = normalize(raw)?;
        let file_part = normalized.split("::").next().unwrap_or(normalized.as_str());
        if existing.contains(file_part) {
            promoted.insert(normalized);
        } else {
            promoted.insert(file_part.to_string());
        }
    }
    Ok(promoted.into_iter().collect())
}

pub fn file_paths_for_locks(locks: &[String]) -> BTreeSet<String> {
    locks
        .iter()
        .map(|lock| lock.split("::").next().unwrap_or(lock).to_string())
        .collect()
}

fn overlaps(left: &[String], right: &[String]) -> bool {
    left.iter()
        .any(|item| right.iter().any(|other| item == other))
}

fn conflicts_with_task(
    write: &[String],
    ancestor: &[String],
    other_write: &[String],
    other_ancestor: &[String],
) -> bool {
    overlaps(write, other_write)
        || overlaps(write, other_ancestor)
        || overlaps(ancestor, other_write)
}

pub struct TaskSnapshot {
    pub task_id: String,
    pub write_locks: Vec<String>,
    pub ancestor_locks: Vec<String>,
    pub status: String,
    pub priority: i64,
    pub seq: i64,
}

pub struct Plan {
    pub claimable: Vec<String>,
    pub conflicts: Vec<(String, Vec<String>)>,
}

/// Compute the maximal concurrently-claimable set from a snapshot.
///
/// Active = LEASED or VERIFYING. READY tasks are considered in priority/seq
/// order; a selected task joins the active lock union. This mirrors the SQL
/// claim predicate but runs entirely in memory. The authoritative claim still
/// happens transactionally in PostgreSQL.
pub fn plan_claimable(mut tasks: Vec<TaskSnapshot>) -> Plan {
    let mut active: Vec<TaskSnapshot> = tasks
        .iter()
        .filter(|task| task.status == "LEASED" || task.status == "VERIFYING")
        .map(|task| TaskSnapshot {
            task_id: task.task_id.clone(),
            write_locks: task.write_locks.clone(),
            ancestor_locks: task.ancestor_locks.clone(),
            status: task.status.clone(),
            priority: task.priority,
            seq: task.seq,
        })
        .collect();

    let mut ready: Vec<TaskSnapshot> = tasks
        .iter()
        .filter(|task| task.status == "READY")
        .map(|task| TaskSnapshot {
            task_id: task.task_id.clone(),
            write_locks: task.write_locks.clone(),
            ancestor_locks: task.ancestor_locks.clone(),
            status: task.status.clone(),
            priority: task.priority,
            seq: task.seq,
        })
        .collect();
    tasks.clear();

    ready.sort_by(|left, right| {
        right
            .priority
            .cmp(&left.priority)
            .then_with(|| left.seq.cmp(&right.seq))
    });

    let mut claimable: Vec<String> = Vec::new();
    let mut conflicts: Vec<(String, Vec<String>)> = Vec::new();

    // The three-way overlap predicate is equivalent to an intersection with
    // the union of active locks. Since "already selected" tasks join the active
    // union, each candidate check is O(number of its lock paths).
    let mut active_write: HashSet<String> = active
        .iter()
        .flat_map(|task| task.write_locks.iter().cloned())
        .collect();
    let mut active_ancestor: HashSet<String> = active
        .iter()
        .flat_map(|task| task.ancestor_locks.iter().cloned())
        .collect();

    for candidate in ready {
        let direct_conflict = candidate
            .write_locks
            .iter()
            .any(|lock| active_write.contains(lock) || active_ancestor.contains(lock))
            || candidate
                .ancestor_locks
                .iter()
                .any(|lock| active_write.contains(lock));

        if !direct_conflict {
            active_write.extend(candidate.write_locks.iter().cloned());
            active_ancestor.extend(candidate.ancestor_locks.iter().cloned());
            claimable.push(candidate.task_id.clone());
            active.push(candidate);
        } else {
            let blockers: Vec<String> = active
                .iter()
                .filter(|other| {
                    conflicts_with_task(
                        &candidate.write_locks,
                        &candidate.ancestor_locks,
                        &other.write_locks,
                        &other.ancestor_locks,
                    )
                })
                .map(|other| other.task_id.clone())
                .collect();
            conflicts.push((candidate.task_id, blockers));
        }
    }

    Plan {
        claimable,
        conflicts,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_dotted_symbols() {
        assert_eq!(
            normalize("src/order/pricing.py::PricingEngine.calc_discount").unwrap(),
            "src/order/pricing.py::PricingEngine::calc_discount"
        );
        assert_eq!(
            normalize("./src\\auth.py::AuthService::login").unwrap(),
            "src/auth.py::AuthService::login"
        );
    }

    #[test]
    fn rejects_traversal() {
        assert!(normalize("../../etc/passwd").is_err());
        assert!(normalize("src/a.py::../../evil").is_err());
    }

    #[test]
    fn disjoint_symbols_do_not_conflict() {
        assert!(!conflicts(
            "src/auth.py::AuthService::login",
            "src/auth.py::AuthService::logout"
        )
        .unwrap());
        assert!(conflicts("src/auth.py", "src/auth.py::AuthService::login").unwrap());
    }

    #[test]
    fn expands_ancestors() {
        let (write, ancestors) =
            expand_write_set(&["src/order/pricing.py::PricingEngine::calc".to_string()]).unwrap();
        assert_eq!(write, vec!["src/order/pricing.py::PricingEngine::calc"]);
        assert!(ancestors.contains(&"src".to_string()));
        assert!(ancestors.contains(&"src/order/pricing.py".to_string()));
        assert!(ancestors.contains(&"src/order/pricing.py::PricingEngine".to_string()));
        assert!(!ancestors.contains(&write[0]));
    }

    #[test]
    fn plans_disjoint_ready_set() {
        let tasks = vec![
            TaskSnapshot {
                task_id: "active".into(),
                write_locks: vec!["src/a.py".into()],
                ancestor_locks: vec!["src".into()],
                status: "LEASED".into(),
                priority: 1,
                seq: 0,
            },
            TaskSnapshot {
                task_id: "conflict".into(),
                write_locks: vec!["src/a.py::g".into()],
                ancestor_locks: vec!["src/a.py".into()],
                status: "READY".into(),
                priority: 100,
                seq: 1,
            },
            TaskSnapshot {
                task_id: "ok".into(),
                write_locks: vec!["src/b.py::h".into()],
                ancestor_locks: vec!["src/b.py".into()],
                status: "READY".into(),
                priority: 50,
                seq: 2,
            },
        ];
        let plan = plan_claimable(tasks);
        assert_eq!(plan.claimable, vec!["ok"]);
        assert_eq!(plan.conflicts[0].0, "conflict");
    }
}
