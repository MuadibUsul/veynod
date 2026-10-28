#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader},
    path::PathBuf,
    process::Command,
    thread,
    time::{Duration, Instant},
};

#[cfg(windows)]
use std::os::windows::process::CommandExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_notification::NotificationExt;

#[derive(Debug, Deserialize, Serialize)]
struct DesktopConfig {
    #[serde(default)]
    agents: Vec<String>,
    #[serde(default = "default_host")]
    host: String,
    #[serde(default)]
    runtime: Option<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default = "default_auto_wake")]
    auto_wake: bool,
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            agents: Vec::new(),
            host: default_host(),
            runtime: None,
            cwd: None,
            auto_wake: default_auto_wake(),
        }
    }
}

fn default_host() -> String {
    "https://veynod.com".to_string()
}

fn default_auto_wake() -> bool {
    true
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct CursorState {
    #[serde(default)]
    last_seq: HashMap<String, i64>,
}

fn home() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn veynod_dir() -> PathBuf {
    home().join(".veynod")
}

fn load_config() -> DesktopConfig {
    let path = veynod_dir().join("desktop.json");
    let mut config: DesktopConfig = fs::read_to_string(&path)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default();
    if config.host.trim().is_empty() {
        config.host = default_host();
    }
    if config.agents.is_empty() {
        // A sensible default while the config file is still being written.
        config.agents.push("zcode".to_string());
    }
    config.agents.sort();
    config.agents.dedup();
    config
}

fn save_config(config: &DesktopConfig) -> Result<(), String> {
    fs::create_dir_all(veynod_dir()).map_err(|error| error.to_string())?;
    let raw = serde_json::to_string_pretty(config).map_err(|error| error.to_string())?;
    fs::write(veynod_dir().join("desktop.json"), raw).map_err(|error| error.to_string())
}

fn load_state() -> CursorState {
    let path = veynod_dir().join("desktop-state.json");
    fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default()
}

fn save_state(state: &CursorState) {
    let path = veynod_dir().join("desktop-state.json");
    let _ = fs::create_dir_all(veynod_dir());
    if let Ok(raw) = serde_json::to_string_pretty(state) {
        let _ = fs::write(path, raw);
    }
}

fn read_key(agent: &str) -> Option<String> {
    let path = veynod_dir().join(format!("{agent}.key"));
    fs::read_to_string(path)
        .ok()
        .map(|raw| raw.trim().to_string())
        .filter(|key| !key.is_empty())
}

fn api_request(method: &str, host: &str, key: &str, path: &str, body: Option<&Value>) -> Result<Value, String> {
    let request = ureq::request(method, &format!("{}{}", host.trim_end_matches('/'), path))
        .set("Authorization", &format!("Bearer {key}"))
        .set("Accept", "application/json")
        .timeout(Duration::from_secs(20));
    let response = match body {
        Some(value) => request
            .set("Content-Type", "application/json")
            .send_string(&value.to_string()),
        None => request.call(),
    };
    match response {
        Ok(resp) => resp
            .into_json::<Value>()
            .map_err(|error| format!("unexpected response: {error}")),
        Err(ureq::Error::Status(code, resp)) => {
            let detail = resp.into_string().unwrap_or_default();
            Err(format!("HTTP {code}: {detail}"))
        }
        Err(error) => Err(format!("cannot reach Veynod: {error}")),
    }
}

fn first_key() -> Result<(String, String), String> {
    let config = load_config();
    let key = config
        .agents
        .iter()
        .find_map(|agent| read_key(agent))
        .ok_or_else(|| "no agent key on this computer yet".to_string())?;
    Ok((config.host, key))
}

fn log_line(message: &str) {
    use std::io::Write;
    let _ = fs::create_dir_all(veynod_dir());
    if let Ok(mut file) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(veynod_dir().join("desktop.log"))
    {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_secs())
            .unwrap_or(0);
        let _ = writeln!(file, "{stamp} {message}");
    }
}

/// Only these pokes are worth interrupting a person: a new assignment, a
/// rejection that needs rework, a mention, a directed message or an approval.
fn actionable(kind: &str) -> bool {
    matches!(
        kind,
        "task.assigned"
            | "task.review_rejected"
            | "task.escalated"
            | "task.awaiting_human"
            | "task.review_requested"
            | "mention.created"
            | "message.unread"
    )
}

fn notification_body(event: &Value) -> String {
    let kind = event.get("kind").and_then(Value::as_str).unwrap_or("event");
    let task = event.get("task_key").and_then(Value::as_str).unwrap_or("");
    let doc = event.get("doc_key").and_then(Value::as_str).unwrap_or("");
    let note = event.get("note").and_then(Value::as_str).unwrap_or("");
    let actor = event.get("actor").and_then(Value::as_str).unwrap_or("");
    let mut parts = vec![kind.to_string()];
    if !task.is_empty() {
        parts.push(task.to_string());
    } else if !doc.is_empty() {
        parts.push(doc.to_string());
    }
    if !actor.is_empty() {
        parts.push(format!("by {actor}"));
    }
    if !note.is_empty() {
        parts.push(note.chars().take(120).collect());
    }
    parts.join(" · ")
}

/// A live `task.assigned` poke is the Loop's cue: enqueue a wake for the local
/// harness so nobody has to send "start work" by hand. The queue dedupes by
/// input_id, so a reconnect or a repeated poke cannot start the same task twice.
fn auto_wake(app: &AppHandle, agent: &str, event: &Value) {
    let config = load_config();
    if !config.auto_wake {
        return;
    }
    let (Some(runtime), Some(cwd)) = (config.runtime.clone(), config.cwd.clone()) else {
        log_line(&format!("{agent}: task.assigned but no runtime/project is saved; skipped auto-wake"));
        return;
    };
    let task_key = event.get("task_key").and_then(Value::as_str).unwrap_or("");
    if task_key.is_empty() {
        return;
    }
    let title = event.get("title").and_then(Value::as_str).unwrap_or("");
    let prompt = format!(
        "Veynod 任务指派：{task_key}{title_part}。\n请先读取任务（get_task）、确认验收项与上下文，具备条件就认领并开始做；不要停在“等待开工”。这条唤起由本地 Loop 在任务指派时自动发出，幂等键 task:{task_key}。",
        title_part = if title.is_empty() { String::new() } else { format!("（{title}）") }
    );
    let Ok(mut command) = sidecar_command(app) else {
        log_line(&format!("{agent}: auto-wake skipped, sidecar missing"));
        return;
    };
    match command
        .arg("--enqueue")
        .arg(&runtime)
        .arg(&cwd)
        .arg(task_key)
        .arg("normal")
        .arg(&prompt)
        .env("VEYNOD_AGENT", agent)
        .env("VEYNOD_INPUT_ID", format!("task:{agent}:{task_key}"))
        .spawn()
    {
        Ok(_) => log_line(&format!("{agent}: auto-wake queued for {task_key}")),
        Err(error) => log_line(&format!("{agent}: auto-wake failed for {task_key}: {error}")),
    }
}

fn stream_agent(app: AppHandle, agent: String, key: String, host: String, start_seq: i64) {
    let mut last = start_seq;
    let mut backoff = 1u64;
    // On the very first connection the server replays unacknowledged events.
    // Do not turn that backlog into a notification storm.
    let silent_until = if start_seq == 0 {
        Some(Instant::now() + Duration::from_secs(12))
    } else {
        None
    };
    let mut last_notification = Instant::now() - Duration::from_secs(60);
    loop {
        let url = format!("{host}/v1/inbox/stream?last_event_id={last}");
        let response = ureq::get(&url)
            .set("Authorization", &format!("Bearer {key}"))
            .set("Accept", "text/event-stream")
            .timeout(Duration::from_secs(3600))
            .call();
        match response {
            Ok(resp) => {
                backoff = 1;
                log_line(&format!("{agent}: stream connected at seq {last}"));
                let reader = BufReader::new(resp.into_reader());
                for line in reader.lines() {
                    let Ok(line) = line else { break };
                    let Some(data) = line.strip_prefix("data:") else {
                        continue;
                    };
                    let Ok(event) = serde_json::from_str::<Value>(data.trim()) else {
                        continue;
                    };
                    let seq = event
                        .get("event_seq")
                        .or_else(|| event.get("seq"))
                        .and_then(Value::as_i64)
                        .unwrap_or(0);
                    if seq > last {
                        last = seq;
                        let mut state = load_state();
                        state.last_seq.insert(agent.clone(), last);
                        save_state(&state);
                    }
                    let kind = event.get("kind").and_then(Value::as_str).unwrap_or("");
                    if kind == "task.assigned" && !silent_until.is_some_and(|until| Instant::now() < until) {
                        auto_wake(&app, &agent, &event);
                    }
                    if !actionable(kind) {
                        continue;
                    }
                    if silent_until.is_some_and(|until| Instant::now() < until) {
                        continue;
                    }
                    if last_notification.elapsed() < Duration::from_secs(4) {
                        continue;
                    }
                    last_notification = Instant::now();
                    let body = notification_body(&event);
                    log_line(&format!("{agent}: notify seq {seq}: {body}"));
                    let _ = app
                        .notification()
                        .builder()
                        .title(format!("Veynod · {agent}"))
                        .body(body)
                        .show();
                }
                log_line(&format!("{agent}: stream ended at seq {last}"));
            }
            Err(error) => {
                log_line(&format!("{agent}: stream error: {error}"));
            }
        }
        // The server closes a stream after about an hour; reconnect promptly.
        thread::sleep(Duration::from_secs(backoff));
        backoff = (backoff * 2).min(30);
    }
}

fn start_watchers(app: AppHandle) {
    let config = load_config();
    let state = load_state();
    for agent in config.agents {
        let Some(key) = read_key(&agent) else {
            log_line(&format!(
                "{agent}: no key at ~/.veynod/{agent}.key; offline"
            ));
            continue;
        };
        let start = *state.last_seq.get(&agent).unwrap_or(&0);
        log_line(&format!(
            "{agent}: watching {} from seq {}",
            config.host, start
        ));
        let handle = app.clone();
        let host = config.host.clone();
        thread::spawn(move || stream_agent(handle, agent, key, host, start));
    }
}

/// Tauri returns Windows extended-length paths (`\\?\E:\...`). Node cannot
/// use that form as a script argument, so strip the prefix before spawning.
fn plain_path(path: &std::path::Path) -> PathBuf {
    #[cfg(windows)]
    {
        let raw = path.to_string_lossy();
        if let Some(rest) = raw.strip_prefix(r"\\?\UNC\") {
            return PathBuf::from(format!(r"\\{rest}"));
        }
        if let Some(rest) = raw.strip_prefix(r"\\?\") {
            return PathBuf::from(rest);
        }
    }
    path.to_path_buf()
}

/// Tauri keeps the `..` segment of a resource path, so an installed Windows
/// build stores the sidecar under `_up_/sidecar` while a plain resource dir
/// uses `sidecar`. Check both before falling back to the dev checkout.
fn sidecar_root(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(resources) = app.path().resource_dir() {
        for candidate in [
            resources.join("sidecar"),
            resources.join("_up_").join("sidecar"),
        ] {
            if candidate.join("bridge.mjs").is_file() {
                return Some(plain_path(&candidate));
            }
        }
    }
    let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("sidecar");
    if dev.join("bridge.mjs").is_file() {
        return Some(plain_path(&dev));
    }
    None
}

fn node_binary(app: &AppHandle) -> String {
    if let Some(root) = sidecar_root(app) {
        let bundled = root.join("runtime").join("node.exe");
        if bundled.is_file() {
            return bundled.to_string_lossy().into_owned();
        }
    }
    std::env::var("VEYNOD_NODE").unwrap_or_else(|_| "node".to_string())
}

fn sidecar_script(app: &AppHandle) -> Result<PathBuf, String> {
    sidecar_root(app)
        .map(|root| root.join("bridge.mjs"))
        .ok_or_else(|| "the OAR sidecar (desktop/sidecar/bridge.mjs) is missing".into())
}

fn sidecar_dir(app: &AppHandle) -> Result<PathBuf, String> {
    sidecar_script(app)?
        .parent()
        .map(PathBuf::from)
        .ok_or_else(|| "the OAR sidecar has no directory".to_string())
}

/// A console-subsystem binary started without a console gets one. The client
/// is a windowed app, so every Node process it starts must ask Windows for no
/// console; the preload repeats that for the runtime CLI children.
fn hidden_node(app: &AppHandle, dir: &std::path::Path) -> Command {
    let mut command = Command::new(node_binary(app));
    #[cfg(windows)]
    {
        if dir.join("hide-console.mjs").is_file() {
            command.arg("--import").arg("./hide-console.mjs");
        }
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command.current_dir(dir);
    command
}

fn sidecar_command(app: &AppHandle) -> Result<Command, String> {
    let script = sidecar_script(app)?;
    let dir = script
        .parent()
        .map(PathBuf::from)
        .ok_or_else(|| "the OAR sidecar has no directory".to_string())?;
    let mut command = hidden_node(app, &dir);
    command.arg(script);
    Ok(command)
}

fn daemon_command(app: &AppHandle) -> Result<Command, String> {
    let dir = sidecar_dir(app)?;
    let daemon = dir.join("daemon.mjs");
    if !daemon.is_file() {
        return Err("the wake daemon (desktop/sidecar/daemon.mjs) is missing".into());
    }
    let mut command = hidden_node(app, &dir);
    command.arg(daemon);
    Ok(command)
}

#[derive(Serialize)]
struct SetupState {
    configured: bool,
    agents: Vec<String>,
}

#[tauri::command]
fn setup_state() -> SetupState {
    let config = load_config();
    let configured = config.agents.iter().any(|agent| read_key(agent).is_some());
    SetupState { configured, agents: config.agents }
}

#[tauri::command]
fn local_runtimes(app: AppHandle) -> Result<Value, String> {
    let mut command = sidecar_command(&app)?;
    let output = command
        .arg("--inventory")
        .output()
        .map_err(|error| format!("cannot start Node for the runtime inventory: {error}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("the runtime inventory was not JSON: {error}"))
}

#[tauri::command]
fn test_wake(app: AppHandle, runtime: String, cwd: String, prompt: Option<String>) -> Result<Value, String> {
    let mut command = sidecar_command(&app)?;
    let output = command
        .arg("--wake")
        .arg(runtime)
        .arg(cwd)
        .arg(prompt.unwrap_or_else(|| "Reply with the single word OK.".to_string()))
        .output()
        .map_err(|error| format!("cannot start Node for the wake test: {error}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("the wake result was not JSON: {error}"))
}

#[tauri::command]
#[allow(clippy::too_many_arguments)]
fn wake_enqueue(app: AppHandle, runtime: String, cwd: String, task_key: Option<String>,
                urgent: bool, prompt: Option<String>, agent: Option<String>,
                expected_fencing_token: Option<i64>, input_id: Option<String>) -> Result<Value, String> {
    let mut command = sidecar_command(&app)?;
    command
        .arg("--enqueue")
        .arg(runtime)
        .arg(cwd)
        .arg(task_key.unwrap_or_default())
        .arg(if urgent { "urgent" } else { "normal" })
        .arg(prompt.unwrap_or_else(|| "Reply with the single word OK.".to_string()));
    if let Some(agent) = agent {
        command.env("VEYNOD_AGENT", agent);
    }
    if let Some(token) = expected_fencing_token {
        command.env("VEYNOD_FENCING", token.to_string());
    }
    if let Some(input_id) = input_id {
        command.env("VEYNOD_INPUT_ID", input_id);
    }
    let output = command
        .output()
        .map_err(|error| format!("cannot queue the wake: {error}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    serde_json::from_slice(&output.stdout).map_err(|error| error.to_string())
}

#[derive(Serialize)]
struct LocalSettings {
    agents: Vec<String>,
    runtime: Option<String>,
    cwd: Option<String>,
    auto_wake: bool,
}

#[tauri::command]
fn local_settings() -> LocalSettings {
    let config = load_config();
    LocalSettings {
        agents: config.agents,
        runtime: config.runtime,
        cwd: config.cwd,
        auto_wake: config.auto_wake,
    }
}

#[tauri::command]
fn save_local_settings(runtime: Option<String>, cwd: Option<String>, auto_wake: bool) -> Result<(), String> {
    let mut config = load_config();
    config.runtime = runtime.filter(|value| !value.trim().is_empty());
    config.cwd = cwd.filter(|value| !value.trim().is_empty());
    config.auto_wake = auto_wake;
    save_config(&config)?;
    log_line(&format!(
        "settings: runtime {:?}, project {:?}, auto-wake {auto_wake}",
        config.runtime, config.cwd
    ));
    Ok(())
}

#[tauri::command]
fn wake_queue() -> Result<Value, String> {
    let queue = veynod_dir().join("wake-queue.json");
    let receipts = veynod_dir().join("wake-receipts.jsonl");
    let items: Value = fs::read_to_string(queue)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_else(|| json!([]));
    let tail = |path: PathBuf, count: usize| -> Vec<Value> {
        let mut values = Vec::new();
        if let Ok(raw) = fs::read_to_string(path) {
            for line in raw.lines().rev().take(count).collect::<Vec<_>>().into_iter().rev() {
                if let Ok(value) = serde_json::from_str::<Value>(line) {
                    values.push(value);
                }
            }
        }
        values
    };
    let recent = tail(receipts, 20);
    let audit = tail(veynod_dir().join("wake-audit.jsonl"), 20);
    Ok(json!({ "items": items, "receipts": recent, "audit": audit }))
}

#[tauri::command]
fn local_inbox() -> Result<Value, String> {
    let (host, key) = first_key()?;
    api_request("GET", &host, &key, "/v1/inbox?status=unacked&limit=100", None)
}

#[tauri::command]
fn ack_inbox(up_to_event_seq: i64) -> Result<Value, String> {
    let (host, key) = first_key()?;
    let body = json!({ "up_to_event_seq": up_to_event_seq });
    api_request("POST", &host, &key, "/v1/inbox/ack", Some(&body))
}

#[tauri::command]
fn local_reviews() -> Result<Value, String> {
    let (host, key) = first_key()?;
    api_request("GET", &host, &key, "/v1/tasks?status=REVIEWING&limit=200", None)
}

#[tauri::command]
fn task_detail(task_key: String) -> Result<Value, String> {
    let (host, key) = first_key()?;
    api_request(
        "GET",
        &host,
        &key,
        &format!("/v1/tasks/{}", task_key),
        None,
    )
}

#[tauri::command]
fn review_task(task_key: String, decision: String, feedback: Option<String>,
               items: Option<Value>, expected_version: Option<i64>) -> Result<Value, String> {
    let (host, key) = first_key()?;
    let body = json!({
        "decision": decision,
        "feedback": feedback,
        "items": items.unwrap_or_else(|| json!([])),
        "expected_version": expected_version,
    });
    api_request(
        "POST",
        &host,
        &key,
        &format!("/v1/tasks/{task_key}/review"),
        Some(&body),
    )
}

#[tauri::command]
fn local_status() -> Result<Value, String> {
    let config = load_config();
    let key = config.agents.iter().find_map(|agent| read_key(agent));
    let Some(key) = key else {
        return Ok(json!({ "configured": false, "agents": [], "tasks": [] }));
    };
    let host = config.host.trim_end_matches('/').to_string();
    let fetch = |path: &str| -> Result<Value, String> {
        let response = ureq::get(&format!("{host}{path}"))
            .set("Authorization", &format!("Bearer {key}"))
            .timeout(Duration::from_secs(15))
            .call()
            .map_err(|error| format!("cannot reach Veynod: {error}"))?;
        response.into_json::<Value>().map_err(|error| error.to_string())
    };
    Ok(json!({
        "configured": true,
        "agents": fetch("/v1/agents?limit=200")?,
        "tasks": fetch("/v1/tasks?limit=200")?,
    }))
}

#[tauri::command]
fn describe_runtime(app: AppHandle, runtime: String) -> Result<Value, String> {
    let mut command = sidecar_command(&app)?;
    let output = command
        .arg("--describe")
        .arg(runtime)
        .output()
        .map_err(|error| format!("cannot start Node for the runtime details: {error}"))?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("the runtime details were not JSON: {error}"))
}

#[tauri::command]
fn test_agent(key: String, host: Option<String>) -> Result<Value, String> {
    let host = host.unwrap_or_else(default_host).trim_end_matches('/').to_string();
    let response = ureq::get(&format!("{host}/v1/whoami"))
        .set("Authorization", &format!("Bearer {}", key.trim()))
        .timeout(Duration::from_secs(15))
        .call()
        .map_err(|error| format!("cannot reach Veynod: {error}"))?;
    response
        .into_json::<Value>()
        .map_err(|error| format!("unexpected whoami response: {error}"))
}

#[tauri::command]
fn save_agent(app: AppHandle, key: String) -> Result<Value, String> {
    let key = key.trim().to_string();
    let me = test_agent(key.clone(), None)?;
    let agent_id = me
        .get("agent_id")
        .and_then(Value::as_str)
        .or_else(|| me.get("agent_signature").and_then(Value::as_str))
        .ok_or_else(|| "whoami did not return an agent identity".to_string())?
        .to_string();
    fs::create_dir_all(veynod_dir()).map_err(|error| error.to_string())?;
    fs::write(veynod_dir().join(format!("{agent_id}.key")), &key)
        .map_err(|error| error.to_string())?;
    let mut config = load_config();
    if !config.agents.contains(&agent_id) {
        config.agents.push(agent_id.clone());
    }
    let raw = serde_json::to_string_pretty(&config).map_err(|error| error.to_string())?;
    fs::write(veynod_dir().join("desktop.json"), raw).map_err(|error| error.to_string())?;
    log_line(&format!("setup: saved agent {agent_id}"));
    let _ = app;
    Ok(me)
}

#[tauri::command]
fn open_dashboard(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("main") {
        window
            .eval("window.location.href = 'https://veynod.com/dashboard/';")
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn open_inbox(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("inbox") {
        let _ = window.show();
        let _ = window.set_focus();
        return Ok(());
    }
    WebviewWindowBuilder::new(&app, "inbox", WebviewUrl::App("inbox.html".into()))
        .title("Veynod · Inbox & approvals")
        .inner_size(980.0, 760.0)
        .build()
        .map(|_| ())
        .map_err(|error| error.to_string())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--hidden"]),
        ))
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            setup_state, local_settings, save_local_settings, local_runtimes, describe_runtime,
            local_status, wake_enqueue, wake_queue, local_inbox, ack_inbox, local_reviews,
            task_detail, review_task, test_wake, test_agent, save_agent, open_dashboard,
            open_inbox
        ])
        .setup(|app| {
            match app.autolaunch().enable() {
                Ok(_) => log_line("autostart enabled"),
                Err(error) => log_line(&format!("autostart failed: {error}")),
            }

            let open = MenuItem::with_id(app, "open", "Open Veynod", true, None::<&str>)?;
            let inbox = MenuItem::with_id(app, "inbox", "Inbox & approvals", true, None::<&str>)?;
            let status = MenuItem::with_id(app, "status", "Local status", true, None::<&str>)?;
            let reload = MenuItem::with_id(app, "reload", "Reload console", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &inbox, &status, &reload, &quit])?;

            let icon = app
                .default_window_icon()
                .cloned()
                .expect("the bundled window icon is missing");
            TrayIconBuilder::new()
                .tooltip("Veynod")
                .icon(icon)
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "inbox" => {
                        if let Some(window) = app.get_webview_window("inbox") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        } else {
                            let _ = WebviewWindowBuilder::new(
                                app, "inbox", WebviewUrl::App("inbox.html".into()))
                                .title("Veynod · Inbox & approvals")
                                .inner_size(980.0, 760.0)
                                .build();
                        }
                    }
                    "status" => {
                        if let Some(window) = app.get_webview_window("status") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        } else {
                            let _ = WebviewWindowBuilder::new(
                                app, "status", WebviewUrl::App("status.html".into()))
                                .title("Veynod · Local status")
                                .inner_size(1024.0, 760.0)
                                .build();
                        }
                    }
                    "reload" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.eval("window.location.reload()");
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            if std::env::args().any(|arg| arg == "--status") {
                let _ = WebviewWindowBuilder::new(
                    app, "status", WebviewUrl::App("status.html".into()))
                    .title("Veynod · Local status")
                    .inner_size(1024.0, 760.0)
                    .build();
            }
            if std::env::args().any(|arg| arg == "--inbox") {
                let _ = WebviewWindowBuilder::new(
                    app, "inbox", WebviewUrl::App("inbox.html".into()))
                    .title("Veynod · Inbox & approvals")
                    .inner_size(980.0, 760.0)
                    .build();
            }
            match daemon_command(app.handle()) {
                Ok(mut command) => {
                    let _ = command
                        .spawn()
                        .map_err(|error| log_line(&format!("wake daemon failed: {error}")));
                    log_line("wake daemon started");
                }
                Err(error) => log_line(&format!("wake daemon skipped: {error}")),
            }
            start_watchers(app.handle().clone());
            Ok(())
        })
        .on_page_load(|webview, payload| {
            if let tauri::webview::PageLoadEvent::Finished = payload.event() {
                // A desktop window should not show page-level scrollbars, but
                // the dashboard is taller than the window: clipping it hides
                // whole panels. Keep vertical scrolling, hide the bar itself;
                // panels inside the dashboard keep their own scroll containers.
                let _ = webview.eval(
                    r#"(() => {
                        if (document.getElementById('veynod-native-shell')) return;
                        const style = document.createElement('style');
                        style.id = 'veynod-native-shell';
                        style.textContent = `
                            html, body {
                                overflow-x: hidden !important;
                                /* overscroll-behavior:none also swallows the
                                   mouse wheel in WebView2. Keep the default. */
                                overscroll-behavior: auto !important;
                                scrollbar-width: none !important;
                            }
                            html { overflow-y: auto !important; }
                            body { overflow-y: visible !important; }
                            ::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }
                        `;
                        document.head && document.head.appendChild(style);
                    })();"#,
                );
            }
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Veynod desktop");
}
