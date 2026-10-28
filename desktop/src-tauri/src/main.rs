#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader},
    path::PathBuf,
    thread,
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager,
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_notification::NotificationExt;

#[derive(Debug, Default, Deserialize)]
struct DesktopConfig {
    #[serde(default)]
    agents: Vec<String>,
    #[serde(default = "default_host")]
    host: String,
}

fn default_host() -> String {
    "https://veynod.com".to_string()
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
        .setup(|app| {
            match app.autolaunch().enable() {
                Ok(_) => log_line("autostart enabled"),
                Err(error) => log_line(&format!("autostart failed: {error}")),
            }

            let open = MenuItem::with_id(app, "open", "Open Veynod", true, None::<&str>)?;
            let reload = MenuItem::with_id(app, "reload", "Reload console", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &reload, &quit])?;

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

            start_watchers(app.handle().clone());
            Ok(())
        })
        .on_page_load(|webview, payload| {
            if let tauri::webview::PageLoadEvent::Finished = payload.event() {
                // A desktop window should not present the web page's page-level
                // scrollbars. Lock the document to the viewport; panels inside
                // the dashboard keep their own scroll containers.
                let _ = webview.eval(
                    r#"(() => {
                        if (document.getElementById('veynod-native-shell')) return;
                        const style = document.createElement('style');
                        style.id = 'veynod-native-shell';
                        style.textContent = `
                            html, body {
                                overflow: hidden !important;
                                overscroll-behavior: none !important;
                                scrollbar-width: none !important;
                            }
                            ::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }
                            body { overscroll-behavior-y: none !important; }
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
