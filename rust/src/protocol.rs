use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::io::{AsyncWriteExt, Stdout};
use tokio::sync::Mutex;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Submit {
    pub id: String,
    pub name: String,
    pub cwd: String,
    pub logfile: String,
    pub exec_cmd: String,
    pub run_uuid: String,
    #[serde(default = "one")]
    pub attempt: u64,
    #[serde(default)]
    pub argv: Option<Vec<String>>,
    #[serde(default)]
    pub queue: Option<String>,
    #[serde(default = "one")]
    pub threads: u64,
    #[serde(default = "memory")]
    pub mem_mb: u64,
    #[serde(default)]
    pub npu: u64,
    #[serde(default)]
    pub gpu: u64,
    #[serde(default)]
    pub runtime_sec: Option<u64>,
    #[serde(default)]
    pub nodes: Option<u64>,
    #[serde(default)]
    pub exclusive: bool,
    #[serde(default)]
    pub tag: Option<String>,
    #[serde(default)]
    pub account: Option<String>,
    #[serde(default)]
    pub mpi: Option<String>,
    #[serde(default)]
    pub priority: i64,
    #[serde(default)]
    pub extra_args: String,
    #[serde(default = "yes")]
    pub any_arch: bool,
}
fn one() -> u64 {
    1
}
fn memory() -> u64 {
    1024
}
fn yes() -> bool {
    true
}

impl Submit {
    pub fn args(&self) -> Result<Vec<String>> {
        if self.id.is_empty() || self.name.is_empty() || self.run_uuid.is_empty() {
            bail!("submit id, name and run_uuid must not be empty");
        }
        // Python sends the canonical argument vector, preserving every legacy edge case.
        if let Some(args) = &self.argv {
            if args.is_empty() {
                bail!("empty dsub argv");
            }
            return Ok(args.clone());
        }
        let mut args = vec![
            "-n".into(),
            self.name.chars().take(63).collect(),
            "-oo".into(),
            self.logfile.clone(),
            "--cwd".into(),
            self.cwd.clone(),
        ];
        for (flag, value) in [("-q", &self.queue)] {
            if let Some(value) = value {
                args.extend([flag.into(), value.clone()]);
            }
        }
        if self.any_arch {
            args.push("-aa".into());
        }
        if self.priority != 0 {
            args.extend(["-p".into(), self.priority.clamp(1, 9999).to_string()]);
        }
        if let Some(nodes) = self.nodes {
            args.extend(["-N".into(), nodes.to_string()]);
        }
        for (flag, value) in [("-A", &self.account), ("--mpi", &self.mpi)] {
            if let Some(value) = value {
                args.extend([flag.into(), value.clone()]);
            }
        }
        if self.exclusive {
            args.extend(["-x".into(), "job".into()]);
        }
        if let Some(tag) = &self.tag {
            args.extend(["--tag".into(), tag.clone()]);
        }
        let mut resources = format!("cpu={},mem={}MB", self.threads.max(1), self.mem_mb);
        if self.npu > 0 {
            resources += &format!(",npu={}", self.npu);
        }
        if self.gpu > 0 {
            resources += &format!(",gpu={}", self.gpu);
        }
        args.extend(["-R".into(), resources]);
        if let Some(seconds) = self.runtime_sec {
            args.extend(["-T".into(), seconds.to_string()]);
        }
        args.extend(split_extra_args(&self.extra_args)?);
        args.push(self.exec_cmd.clone());
        Ok(args)
    }
}

/// Match Python shlex.split(..., comments=False, posix=True), including its
/// deliberate differences from a POSIX shell (CR and escaped newlines).
fn split_extra_args(text: &str) -> Result<Vec<String>> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut quote = None;
    let mut escaped = false;
    let mut active = false;
    for ch in text.chars() {
        if escaped {
            if quote == Some('"') && ch != '"' && ch != '\\' {
                word.push('\\');
            }
            word.push(ch);
            escaped = false;
            active = true;
        } else if quote == Some('\'') {
            if ch == '\'' {
                quote = None;
            } else {
                word.push(ch);
            }
        } else if ch == '\\' {
            escaped = true;
            active = true;
        } else if let Some(current) = quote {
            if ch == current {
                quote = None;
            } else {
                word.push(ch);
            }
        } else if ch == '\'' || ch == '"' {
            quote = Some(ch);
            active = true;
        } else if [' ', '\t', '\r', '\n'].contains(&ch) {
            if active {
                words.push(std::mem::take(&mut word));
                active = false;
            }
        } else {
            word.push(ch);
            active = true;
        }
    }
    if escaped || quote.is_some() {
        bail!("unclosed quote or escape in extra_args");
    }
    if active {
        words.push(word);
    }
    Ok(words)
}

pub struct Output(pub Mutex<Stdout>);
impl Output {
    pub async fn send(&self, value: Value) -> Result<()> {
        let mut bytes = serde_json::to_vec(&value)?;
        bytes.push(b'\n');
        let mut stdout = self.0.lock().await;
        stdout.write_all(&bytes).await?;
        stdout.flush().await?;
        Ok(())
    }
    pub async fn error(&self, request: &Value, code: &str, msg: impl ToString) {
        let _ = self.send(json!({"type":"error", "request_id":request.get("request_id"), "code":code, "msg":msg.to_string()})).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shlex_compatibility_edges() -> Result<()> {
        assert_eq!(
            split_extra_args("a #literal '' b\rc")?,
            ["a", "#literal", "", "b", "c"]
        );
        assert_eq!(
            split_extra_args(r#""a\$b" 'c\d' e\ f"#)?,
            [r"a\$b", r"c\d", "e f"]
        );
        assert_eq!(split_extra_args("a\\\nb")?, ["a\nb"]);
        assert!(split_extra_args("dangling\\").is_err());
        assert!(split_extra_args("'unclosed").is_err());
        Ok(())
    }

    #[test]
    fn field_mapping_preserves_legacy_order_and_quoting() -> Result<()> {
        let request: Submit = serde_json::from_value(json!({
            "id":"id", "name":"smk_align", "cwd":"/work space", "logfile":"/log file",
            "exec_cmd":"echo 'hello world'", "run_uuid":"run", "queue":"q", "threads":4,
            "mem_mb":8000, "npu":1, "gpu":2, "runtime_sec":600, "nodes":2,
            "exclusive":true, "tag":"x=y", "account":"acct", "mpi":"openmpi",
            "priority":10001, "extra_args":"--env 'A=hello world'"
        }))?;
        assert_eq!(
            request.args()?,
            [
                "-n",
                "smk_align",
                "-oo",
                "/log file",
                "--cwd",
                "/work space",
                "-q",
                "q",
                "-aa",
                "-p",
                "9999",
                "-N",
                "2",
                "-A",
                "acct",
                "--mpi",
                "openmpi",
                "-x",
                "job",
                "--tag",
                "x=y",
                "-R",
                "cpu=4,mem=8000MB,npu=1,gpu=2",
                "-T",
                "600",
                "--env",
                "A=hello world",
                "echo 'hello world'"
            ]
        );
        Ok(())
    }
}
