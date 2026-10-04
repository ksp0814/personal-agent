mod commands {
    use std::collections::HashMap;
    use std::io::{BufRead, BufReader, Write};
    use std::path::PathBuf;
    use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
    use std::sync::{Arc, Mutex};
    use std::thread;

    #[cfg(windows)]
    use std::os::windows::process::CommandExt;

    #[cfg(windows)]
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    use serde::Serialize;
    use serde_json::Value;
    use tauri::{AppHandle, Emitter, Manager, State};

    pub struct TerminalState(pub Mutex<HashMap<String, ManagedTerminal>>);

    pub struct BridgeState(Mutex<HashMap<String, Arc<Mutex<BridgeProcess>>>>);

    struct BridgeProcess {
        child: Child,
        stdin: ChildStdin,
        stdout: BufReader<ChildStdout>,
    }

    pub struct ManagedTerminal {
        child: Child,
        stdin: ChildStdin,
        output: Arc<Mutex<String>>,
    }

    const TERMINAL_BUFFER_LIMIT: usize = 64 * 1024;

    #[derive(Clone, Debug, Serialize)]
    pub struct TerminalEvent {
        pub session_id: String,
        pub event: String,
        pub data: Option<String>,
        pub message: Option<String>,
    }

    impl Default for TerminalState {
        fn default() -> Self {
            Self(Mutex::new(HashMap::new()))
        }
    }

    impl Default for BridgeState {
        fn default() -> Self {
            Self(Mutex::new(HashMap::new()))
        }
    }

    impl TerminalState {
        pub fn stop_all(&self) {
            if let Ok(mut terminals) = self.0.lock() {
                for (_, mut terminal) in terminals.drain() {
                    let _ =
                        write_control(&mut terminal.stdin, serde_json::json!({"command": "stop"}));
                    let _ = terminal.child.kill();
                    let _ = terminal.child.wait();
                }
            }
        }
    }

    impl BridgeState {
        pub fn stop_all(&self) {
            if let Ok(mut bridges) = self.0.lock() {
                for (_, bridge) in bridges.drain() {
                    if let Ok(mut bridge) = bridge.lock() {
                        let _ = bridge.child.kill();
                        let _ = bridge.child.wait();
                    }
                }
            }
        }
    }

    #[tauri::command]
    pub fn bridge_request(
        app: AppHandle,
        state: State<'_, BridgeState>,
        workspace: String,
        request: Value,
    ) -> Result<Value, String> {
        let request_json = serde_json::to_string(&request)
            .map_err(|error| format!("요청을 직렬화하지 못했습니다: {error}"))?;
        let bridge = get_bridge(&app, &state, &workspace)?;
        let response = send_bridge_request(&bridge, &request_json)?;
        if response.get("ok") != Some(&Value::Bool(true)) {
            return Err(response["error"]
                .as_str()
                .unwrap_or("Python 브리지 요청 실패")
                .to_string());
        }
        Ok(response["result"].clone())
    }

    fn get_bridge(
        app: &AppHandle,
        state: &State<'_, BridgeState>,
        workspace: &str,
    ) -> Result<Arc<Mutex<BridgeProcess>>, String> {
        let mut bridges = state
            .0
            .lock()
            .map_err(|_| "Python 브리지 상태 잠금에 실패했습니다.".to_string())?;
        if let Some(existing) = bridges.get(workspace) {
            let mut bridge = existing
                .lock()
                .map_err(|_| "Python 브리지 잠금에 실패했습니다.".to_string())?;
            if bridge
                .child
                .try_wait()
                .map_err(|error| format!("Python 브리지 상태를 확인하지 못했습니다: {error}"))?
                .is_none()
            {
                drop(bridge);
                return Ok(Arc::clone(existing));
            }
            drop(bridge);
            bridges.remove(workspace);
        }
        let bridge = Arc::new(Mutex::new(spawn_bridge(app, workspace)?));
        bridges.insert(workspace.to_string(), Arc::clone(&bridge));
        Ok(bridge)
    }

    fn spawn_bridge(app: &AppHandle, workspace: &str) -> Result<BridgeProcess, String> {
        let mut command = if let Some(bridge) = bundled_bridge_path(app) {
            let mut command = Command::new(bridge);
            command.arg(workspace);
            command
        } else {
            let project_root = project_root()?;
            let python_path = project_root.join("src");
            let mut command = Command::new(python_executable(&project_root));
            command.env("PYTHONPATH", &python_path).args([
                "-m",
                "personal_agent.bridge",
                workspace,
            ]);
            command
        };
        configure_child_process(&mut command);
        let mut child = command
            .current_dir(workspace)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|error| format!("Python 브리지를 시작하지 못했습니다: {error}"))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "Python 브리지 stdin을 열지 못했습니다.".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "Python 브리지 stdout을 열지 못했습니다.".to_string())?;
        Ok(BridgeProcess {
            child,
            stdin,
            stdout: BufReader::new(stdout),
        })
    }

    fn send_bridge_request(
        bridge: &Arc<Mutex<BridgeProcess>>,
        request: &str,
    ) -> Result<Value, String> {
        let mut bridge = bridge
            .lock()
            .map_err(|_| "Python 브리지 잠금에 실패했습니다.".to_string())?;
        writeln!(bridge.stdin, "{request}")
            .map_err(|error| format!("Python 브리지 요청을 전달하지 못했습니다: {error}"))?;
        bridge
            .stdin
            .flush()
            .map_err(|error| format!("Python 브리지 요청을 flush하지 못했습니다: {error}"))?;
        let mut response = String::new();
        let bytes = bridge
            .stdout
            .read_line(&mut response)
            .map_err(|error| format!("Python 브리지 응답을 읽지 못했습니다: {error}"))?;
        if bytes == 0 {
            return Err("Python 브리지가 종료되었습니다.".to_string());
        }
        serde_json::from_str(&response)
            .map_err(|error| format!("Python 브리지 응답을 해석하지 못했습니다: {error}"))
    }

    fn project_root() -> Result<PathBuf, String> {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .canonicalize()
            .map_err(|error| format!("프로젝트 루트를 찾지 못했습니다: {error}"))
    }

    fn python_executable(root: &PathBuf) -> PathBuf {
        let virtualenv_python = if cfg!(windows) {
            root.join(".venv").join("Scripts").join("python.exe")
        } else {
            root.join(".venv").join("bin").join("python")
        };
        if virtualenv_python.is_file() {
            virtualenv_python
        } else {
            PathBuf::from("python")
        }
    }

    fn configure_child_process(_command: &mut Command) {
        #[cfg(windows)]
        _command.creation_flags(CREATE_NO_WINDOW);
    }

    fn bundled_bridge_path(app: &AppHandle) -> Option<PathBuf> {
        if cfg!(debug_assertions) {
            return None;
        }
        let executable = if cfg!(windows) {
            "personal-agent-bridge.exe"
        } else {
            "personal-agent-bridge"
        };
        let candidates = [
            app.path()
                .resource_dir()
                .ok()
                .map(|path| path.join("resources").join(executable)),
            app.path()
                .resource_dir()
                .ok()
                .map(|path| path.join(executable)),
            Some(
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("resources")
                    .join(executable),
            ),
        ];
        candidates.into_iter().flatten().find(|path| path.is_file())
    }

    fn write_control(stdin: &mut ChildStdin, payload: Value) -> Result<(), String> {
        writeln!(stdin, "{payload}")
            .map_err(|error| format!("터미널 명령을 전달하지 못했습니다: {error}"))?;
        stdin
            .flush()
            .map_err(|error| format!("터미널 명령을 flush하지 못했습니다: {error}"))
    }

    #[tauri::command]
    pub fn start_terminal(
        app: AppHandle,
        state: State<'_, TerminalState>,
        session_id: String,
        workspace: String,
    ) -> Result<bool, String> {
        // Keep start idempotent across concurrent Tauri commands. The process
        // spawn path does not wait for terminal output while this lock is held.
        let mut terminals = state
            .0
            .lock()
            .map_err(|_| "터미널 상태 잠금에 실패했습니다.".to_string())?;
        if terminals.contains_key(&session_id) {
            return Ok(true);
        }
        let mut command = if let Some(bridge) = bundled_bridge_path(&app) {
            let mut command = Command::new(bridge);
            command.args(["--terminal", workspace.as_str()]);
            command
        } else {
            let root = project_root()?;
            let mut command = Command::new(python_executable(&root));
            command.env("PYTHONPATH", root.join("src")).args([
                "-m",
                "personal_agent.terminal_bridge",
                workspace.as_str(),
            ]);
            command
        };
        configure_child_process(&mut command);
        let mut child = command
            .current_dir(&workspace)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|error| format!("Python 터미널 브리지를 시작하지 못했습니다: {error}"))?;
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| "터미널 stdin을 열지 못했습니다.".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "터미널 stdout을 열지 못했습니다.".to_string())?;
        write_control(&mut stdin, serde_json::json!({"command": "start"}))?;
        let output_buffer = Arc::new(Mutex::new(String::new()));
        let buffer_for_reader = Arc::clone(&output_buffer);

        let session_for_reader = session_id.clone();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().flatten() {
                let parsed: Value = match serde_json::from_str(&line) {
                    Ok(value) => value,
                    Err(error) => {
                        let _ = app.emit(
                            "terminal-event",
                            TerminalEvent {
                                session_id: session_for_reader.clone(),
                                event: "error".to_string(),
                                data: None,
                                message: Some(format!("터미널 이벤트 해석 실패: {error}")),
                            },
                        );
                        continue;
                    }
                };
                if parsed["event"] == "output" {
                    if let Some(data) = parsed["data"].as_str() {
                        if let Ok(mut buffer) = buffer_for_reader.lock() {
                            buffer.push_str(data);
                            if buffer.len() > TERMINAL_BUFFER_LIMIT {
                                let trim_to = buffer.len() - TERMINAL_BUFFER_LIMIT;
                                if let Some((boundary, _)) =
                                    buffer.char_indices().find(|(index, _)| *index >= trim_to)
                                {
                                    buffer.drain(..boundary);
                                }
                            }
                        }
                    }
                }
                let _ = app.emit(
                    "terminal-event",
                    TerminalEvent {
                        session_id: session_for_reader.clone(),
                        event: parsed["event"].as_str().unwrap_or("error").to_string(),
                        data: parsed["data"].as_str().map(str::to_string),
                        message: parsed["message"].as_str().map(str::to_string),
                    },
                );
            }
        });

        terminals.insert(
            session_id,
            ManagedTerminal {
                child,
                stdin,
                output: output_buffer,
            },
        );
        Ok(false)
    }

    #[tauri::command]
    pub fn terminal_buffer(
        state: State<'_, TerminalState>,
        session_id: String,
    ) -> Result<String, String> {
        let terminals = state
            .0
            .lock()
            .map_err(|_| "터미널 상태 잠금에 실패했습니다.".to_string())?;
        let terminal = terminals
            .get(&session_id)
            .ok_or_else(|| "터미널 세션이 없습니다.".to_string())?;
        terminal
            .output
            .lock()
            .map(|buffer| buffer.clone())
            .map_err(|_| "터미널 출력 잠금에 실패했습니다.".to_string())
    }

    #[tauri::command]
    pub fn write_terminal(
        state: State<'_, TerminalState>,
        session_id: String,
        text: String,
    ) -> Result<(), String> {
        let mut terminals = state
            .0
            .lock()
            .map_err(|_| "터미널 상태 잠금에 실패했습니다.".to_string())?;
        let terminal = terminals
            .get_mut(&session_id)
            .ok_or_else(|| "터미널 세션이 없습니다.".to_string())?;
        write_control(
            &mut terminal.stdin,
            serde_json::json!({"command": "write", "text": text}),
        )
    }

    #[tauri::command]
    pub fn resize_terminal(
        state: State<'_, TerminalState>,
        session_id: String,
        columns: u16,
        rows: u16,
    ) -> Result<(), String> {
        let mut terminals = state
            .0
            .lock()
            .map_err(|_| "터미널 상태 잠금에 실패했습니다.".to_string())?;
        let terminal = terminals
            .get_mut(&session_id)
            .ok_or_else(|| "터미널 세션이 없습니다.".to_string())?;
        write_control(
            &mut terminal.stdin,
            serde_json::json!({"command": "resize", "columns": columns, "rows": rows}),
        )
    }

    #[tauri::command]
    pub fn stop_terminal(
        state: State<'_, TerminalState>,
        session_id: String,
    ) -> Result<(), String> {
        let mut terminals = state
            .0
            .lock()
            .map_err(|_| "터미널 상태 잠금에 실패했습니다.".to_string())?;
        if let Some(mut terminal) = terminals.remove(&session_id) {
            let _ = write_control(&mut terminal.stdin, serde_json::json!({"command": "stop"}));
            let _ = terminal.child.kill();
            let _ = terminal.child.wait();
        }
        Ok(())
    }
}

use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(commands::TerminalState::default())
        .manage(commands::BridgeState::default())
        .invoke_handler(tauri::generate_handler![
            commands::bridge_request,
            commands::start_terminal,
            commands::terminal_buffer,
            commands::write_terminal,
            commands::resize_terminal,
            commands::stop_terminal
        ])
        .build(tauri::generate_context!())
        .expect("error while building Personal Agent");
    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(state) = app.try_state::<commands::TerminalState>() {
                state.stop_all();
            }
            if let Some(state) = app.try_state::<commands::BridgeState>() {
                state.stop_all();
            }
        }
    });
}
