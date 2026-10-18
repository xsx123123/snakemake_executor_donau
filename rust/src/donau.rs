use anyhow::{bail, Context, Result};
use std::{
    collections::{HashMap, HashSet},
    path::PathBuf,
    time::Duration,
};
use tokio::{process::Command, time::timeout};

pub fn terminal(state: &str) -> bool {
    matches!(
        state.to_ascii_uppercase().as_str(),
        "FINISHED"
            | "SUCCEEDED"
            | "DONE"
            | "0"
            | "FAILED"
            | "ABORTED"
            | "TIMEOUT"
            | "NODE_FAIL"
            | "TERMINATED"
            | "EXIT"
            | "CANCELED"
            | "CANCELLED"
    )
}
pub fn success(state: &str) -> bool {
    matches!(
        state.to_ascii_uppercase().as_str(),
        "FINISHED" | "SUCCEEDED" | "DONE" | "0"
    )
}

pub async fn command(tool: &str, args: &[String]) -> Result<String> {
    let path = std::env::var_os("DONAU_BIN_DIR")
        .map(|p| PathBuf::from(p).join(tool))
        .unwrap_or_else(|| tool.into());
    let mut command = Command::new(path);
    command.args(args).kill_on_drop(true);
    let output = timeout(Duration::from_secs(30), command.output())
        .await
        .context("CLI timed out; submission outcome may be unknown")??;
    if !output.status.success() {
        bail!(
            "{tool} exited {}: {} {}",
            output.status,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

pub fn parse_jobid(text: &str) -> Option<String> {
    if let Some(start) = text.find('<') {
        if let Some(end) = text[start + 1..].find('>') {
            let candidate = &text[start + 1..start + 1 + end];
            if !candidate.is_empty() && candidate.bytes().all(|c| c.is_ascii_digit()) {
                return Some(candidate.into());
            }
        }
    }
    let candidate: String = text
        .trim_start()
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .collect();
    if candidate.is_empty() {
        None
    } else {
        Some(candidate)
    }
}

pub fn parse_states(text: &str) -> HashMap<String, String> {
    text.lines()
        .filter_map(|line| {
            let mut parts = line.split_ascii_whitespace();
            let id = parts.next()?;
            let state = parts.next()?;
            if id.bytes().all(|c| c.is_ascii_digit()) {
                Some((id.into(), state.into()))
            } else {
                None
            }
        })
        .collect()
}

pub async fn poll(ids: &[String]) -> Result<HashMap<String, String>> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let mut args = vec!["-o".into(), "jobId state".into(), "--no-header".into()];
    args.extend_from_slice(ids);
    let wanted: HashSet<_> = ids.iter().collect();
    let mut states = parse_states(&command("djob", &args).await?);
    states.retain(|id, _| wanted.contains(id));
    let missing: Vec<String> = ids
        .iter()
        .filter(|id| !states.contains_key(*id))
        .cloned()
        .collect();
    if !missing.is_empty() {
        let mut args = vec![
            "-D".into(),
            "-o".into(),
            "jobId state".into(),
            "--no-header".into(),
        ];
        args.extend(missing.iter().cloned());
        let wanted: HashSet<_> = missing.iter().collect();
        match command("djob", &args).await {
            Ok(text) => states.extend(
                parse_states(&text)
                    .into_iter()
                    .filter(|(id, _)| wanted.contains(id)),
            ),
            Err(error) => {
                tracing::warn!(%error, "historical poll failed; missing jobs stay UNKNOWN")
            }
        }
    }
    Ok(states)
}

pub async fn find_name(name: &str) -> Result<Vec<(String, String)>> {
    let mut matches = HashMap::new();
    for historical in [false, true] {
        let mut args = vec!["-o".into(), "jobId name state".into(), "--no-header".into()];
        if historical {
            args.insert(0, "-D".into());
        }
        let text = command("djob", &args).await?;
        for line in text.lines() {
            let parts: Vec<_> = line.split_ascii_whitespace().collect();
            if parts.len() >= 3 && parts[0].bytes().all(|c| c.is_ascii_digit()) && parts[1] == name
            {
                matches.insert(parts[0].to_string(), parts[2].to_string());
            }
        }
    }
    Ok(matches.into_iter().collect())
}

pub async fn diagnostics(id: &str) -> HashMap<String, String> {
    let mut fields = HashMap::new();
    if let Ok(text) = command("djob", &["-ll".into(), id.into()]).await {
        for line in text.lines() {
            let trimmed = line.trim();
            if let Some(end) = trimmed.find(char::is_whitespace) {
                let (key, value) = trimmed.split_at(end);
                if [
                    "exitMessage",
                    "jobExitCode",
                    "systemExitCode",
                    "totalMaxMem",
                    "jobWallclockDuration",
                    "execNodes",
                ]
                .contains(&key)
                {
                    fields.insert(key.into(), value.trim().into());
                }
            }
        }
    }
    fields
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parsing_and_terminals() {
        assert_eq!(parse_jobid("Job <123> submitted"), Some("123".into()));
        assert_eq!(parse_jobid(" 456 accepted"), Some("456".into()));
        assert_eq!(parse_jobid("garbage"), None);
        let states = parse_states("1 RUNNING\nError warning\n2 DONE\n");
        assert_eq!(states.len(), 2);
        for state in [
            "FINISHED",
            "SUCCEEDED",
            "DONE",
            "0",
            "FAILED",
            "ABORTED",
            "TIMEOUT",
            "NODE_FAIL",
            "TERMINATED",
            "EXIT",
            "CANCELED",
            "CANCELLED",
        ] {
            assert!(terminal(state));
        }
        for state in ["UNKNOWN", "RUNNING", "PENDING", ""] {
            assert!(!terminal(state));
        }
    }
}
