use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::mpsc::{self, Receiver},
    time::Duration,
};

struct Harness {
    child: Child,
    lines: Receiver<Value>,
    _dir: tempfile::TempDir,
    root: PathBuf,
}
impl Harness {
    fn start() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_owned();
        let mut harness = Self::spawn(root.clone(), dir);
        harness.send(json!({"type":"hello","proto":1,"request_id":"hello"}));
        assert_eq!(harness.response("hello")["ok"], true);
        harness
    }
    fn spawn(root: PathBuf, dir: tempfile::TempDir) -> Self {
        let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/cluster");
        let mut child = Command::new(env!("CARGO_BIN_EXE_donau-sidecar"))
            .args([
                "--journal",
                root.join("donau_sidecar.journal").to_str().unwrap(),
                "--poll-interval",
                "1",
            ])
            .env("DONAU_BIN_DIR", &fixtures)
            .env("MOCK_DONAU_DIR", root.join("mock"))
            .env("MOCK_EXECUTE", "0")
            .env("PATH", "/usr/bin:/bin")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (sender, lines) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let value: Value =
                    serde_json::from_str(&line.unwrap()).expect("stdout must only contain JSON");
                if sender.send(value).is_err() {
                    break;
                }
            }
        });
        Self {
            child,
            lines,
            _dir: dir,
            root,
        }
    }
    fn send(&mut self, request: Value) {
        writeln!(self.child.stdin.as_mut().unwrap(), "{request}").unwrap();
        self.child.stdin.as_mut().unwrap().flush().unwrap();
    }
    fn response(&self, id: &str) -> Value {
        loop {
            let value = self
                .lines
                .recv_timeout(Duration::from_secs(15))
                .expect("protocol response timeout");
            if value["request_id"] == id {
                return value;
            }
        }
    }
    fn submit(&self, id: &str) -> Value {
        json!({"type":"submit","id":id,"name":format!("smk_{id}"),"cwd":self.root,"logfile":self.root.join("job.log"),"exec_cmd":"true","run_uuid":"run","request_id":id})
    }
    fn shutdown(&mut self) {
        self.send(json!({"type":"shutdown","request_id":"stop"}));
        assert_eq!(self.response("stop")["ok"], true);
        assert!(self.child.wait().unwrap().success());
    }
}
impl Drop for Harness {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn submit_poll_diag_cancel_and_idempotence() {
    let mut harness = Harness::start();
    let request = harness.submit("one");
    harness.send(request.clone());
    let first = harness.response("one");
    let jobid = first["donau_jobid"].as_str().unwrap();
    assert_eq!(first["type"], "submitted");
    harness.send(request);
    assert_eq!(harness.response("one")["donau_jobid"], jobid);
    harness.send(json!({"type":"poll","ids":[jobid,"99999"],"request_id":"poll"}));
    let states = harness.response("poll");
    assert_eq!(states["states"][jobid], "PENDING");
    assert!(states["states"].get("99999").is_none());
    harness.send(json!({"type":"diag","id":jobid,"request_id":"diag"}));
    assert_eq!(harness.response("diag")["fields"]["jobExitCode"], "0");
    harness.send(json!({"type":"cancel","ids":[jobid],"request_id":"cancel"}));
    assert_eq!(harness.response("cancel")["type"], "canceled");
    harness.send(json!({"type":"poll","ids":[jobid],"request_id":"terminal"}));
    assert_eq!(harness.response("terminal")["states"][jobid], "CANCELLED");
    harness.shutdown();
}

#[test]
fn crash_restart_reuses_durable_submission() {
    let mut harness = Harness::start();
    let request = harness.submit("recovered");
    harness.send(request.clone());
    let first = harness.response("recovered");
    harness.child.kill().unwrap();
    harness.child.wait().unwrap();
    // Keep the same journal and mock scheduler directory across processes.
    let placeholder = tempfile::tempdir().unwrap();
    let dir = std::mem::replace(&mut harness._dir, placeholder);
    let mut restarted = Harness::spawn(harness.root.clone(), dir);
    restarted.send(json!({"type":"hello","proto":1,"request_id":"hello"}));
    assert_eq!(restarted.response("hello")["ok"], true);
    let mut request = request;
    request["run_uuid"] = "second-workflow-run".into();
    restarted.send(request);
    assert_eq!(
        restarted.response("recovered")["donau_jobid"],
        first["donau_jobid"]
    );
    restarted.shutdown();
}

#[test]
fn malformed_line_is_dropped_and_protocol_mismatch_is_fatal() {
    let mut harness = Harness::start();
    writeln!(harness.child.stdin.as_mut().unwrap(), "not json").unwrap();
    harness.send(json!({"type":"unknown","request_id":"bad"}));
    assert_eq!(harness.response("bad")["type"], "error");
    harness.shutdown();
    let dir = tempfile::tempdir().unwrap();
    let mut bad = Harness::spawn(dir.path().to_owned(), dir);
    bad.send(json!({"type":"hello","proto":2,"request_id":"badhello"}));
    assert_eq!(bad.response("badhello")["code"], "protocol_mismatch");
    assert!(!bad.child.wait().unwrap().success());
}

#[test]
fn stdin_eof_drains_pending_submission_to_journal() {
    let mut harness = Harness::start();
    harness.send(harness.submit("eof"));
    drop(harness.child.stdin.take());
    assert_eq!(harness.response("eof")["type"], "submitted");
    assert!(harness.child.wait().unwrap().success());
    let journal = std::fs::read_to_string(harness.root.join("donau_sidecar.journal")).unwrap();
    assert!(journal.contains("job_submitted"));
}
