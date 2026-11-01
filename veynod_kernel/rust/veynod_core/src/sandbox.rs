//! Hardened command supervisor for the deterministic gate.
//!
//! Not a security sandbox: it adds wall-clock timeouts, process-tree kill
//! (setsid/killpg on Unix, taskkill /T on Windows), merged output capture and a
//! bounded output tail so one runaway test cannot pin the kernel forever.

use std::collections::{HashMap, VecDeque};
use std::io::Read;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub struct RunResult {
    pub returncode: i32,
    pub output: String,
    pub timed_out: bool,
    pub duration_ms: u64,
}

struct LimitedBuffer {
    data: VecDeque<u8>,
    limit: usize,
    truncated: bool,
}

impl LimitedBuffer {
    fn new(limit: usize) -> Self {
        Self {
            data: VecDeque::new(),
            limit: limit.max(1),
            truncated: false,
        }
    }

    fn write(&mut self, chunk: &[u8]) {
        for byte in chunk {
            if self.data.len() >= self.limit {
                self.data.pop_front();
                self.truncated = true;
            }
            self.data.push_back(*byte);
        }
    }

    fn finish(&self) -> String {
        let body =
            String::from_utf8_lossy(&self.data.iter().copied().collect::<Vec<u8>>()).to_string();
        if self.truncated {
            format!("[output truncated to last {} bytes]\n{}", self.limit, body)
        } else {
            body
        }
    }
}

fn pump<R: Read + Send + 'static>(
    mut reader: R,
    buffer: Arc<Mutex<LimitedBuffer>>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut chunk = [0u8; 8192];
        loop {
            match reader.read(&mut chunk) {
                Ok(0) => break,
                Ok(count) => {
                    if let Ok(mut guard) = buffer.lock() {
                        guard.write(&chunk[..count]);
                    }
                }
                Err(_) => break,
            }
        }
    })
}

#[cfg(windows)]
fn kill_tree(child: &mut Child) {
    let pid = child.id().to_string();
    let _ = Command::new("taskkill")
        .args(["/PID", &pid, "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let _ = child.kill();
}

#[cfg(unix)]
fn kill_tree(child: &mut Child) {
    let pid = child.id() as i32;
    // The shell was started as a session leader, so the negative pid targets
    // the whole process group.
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
    }
    let _ = child.kill();
}

pub fn run_command(
    command: &str,
    cwd: &str,
    timeout_seconds: f64,
    extra_env: &HashMap<String, String>,
    output_limit: usize,
) -> Result<RunResult, String> {
    #[cfg(windows)]
    let mut process = {
        use std::os::windows::process::CommandExt;
        let mut cmd = Command::new("cmd.exe");
        // raw_arg preserves the exact command string, including inner quotes.
        // With normal args Windows would re-quote and cmd.exe would mangle
        // commands such as `python -c "print(1)"`.
        cmd.raw_arg("/D");
        cmd.raw_arg("/S");
        cmd.raw_arg("/C");
        cmd.raw_arg(format!("\"{command}\""));
        // CREATE_NO_WINDOW: don't flash a console for every gate command.
        cmd.creation_flags(0x0800_0000);
        cmd
    };

    #[cfg(unix)]
    let mut process = {
        use std::os::unix::process::CommandExt;
        let mut cmd = Command::new("/bin/sh");
        cmd.args(["-c", command]);
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        cmd
    };

    process
        .current_dir(cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (key, value) in extra_env {
        process.env(key, value);
    }

    let started = Instant::now();
    let mut child = process
        .spawn()
        .map_err(|error| format!("failed to spawn command: {error}"))?;

    let buffer = Arc::new(Mutex::new(LimitedBuffer::new(output_limit)));
    let stdout_reader = child
        .stdout
        .take()
        .map(|stdout| pump(stdout, Arc::clone(&buffer)));
    let stderr_reader = child
        .stderr
        .take()
        .map(|stderr| pump(stderr, Arc::clone(&buffer)));

    let timeout = Duration::from_secs_f64(timeout_seconds.max(0.05));
    let deadline = Instant::now() + timeout;
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {}
            Err(error) => return Err(format!("failed to wait for command: {error}")),
        }
        if Instant::now() >= deadline {
            timed_out = true;
            kill_tree(&mut child);
            break child.wait().ok();
        }
        thread::sleep(Duration::from_millis(10));
    };

    if let Some(handle) = stdout_reader {
        let _ = handle.join();
    }
    if let Some(handle) = stderr_reader {
        let _ = handle.join();
    }

    let returncode = if timed_out {
        124
    } else {
        status.and_then(|value| value.code()).unwrap_or(1)
    };
    let output = buffer
        .lock()
        .map(|guard| guard.finish())
        .unwrap_or_else(|_| String::new());

    Ok(RunResult {
        returncode,
        output,
        timed_out,
        duration_ms: started.elapsed().as_millis() as u64,
    })
}
