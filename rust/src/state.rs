use crate::donau;
use anyhow::{bail, Context, Result};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
const MAX_BYTES: u64 = 50 * 1024 * 1024;
const RETENTION: u64 = 7 * 86400;

fn first_attempt() -> u64 {
    1
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Lifecycle {
    Submitted,
    Active,
    Finished,
    Failed,
    Cancelled,
    #[default]
    Unknown,
}
impl Lifecycle {
    fn from_scheduler(state: &str) -> Self {
        match state.to_ascii_uppercase().as_str() {
            "SUBMITTED" => Self::Submitted,
            "UNKNOWN" | "" => Self::Unknown,
            "CANCELED" | "CANCELLED" => Self::Cancelled,
            other if donau::success(other) => Self::Finished,
            other if donau::terminal(other) => Self::Failed,
            _ => Self::Active,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Job {
    pub id: String,
    pub name: String,
    pub run_uuid: String,
    #[serde(default = "first_attempt")]
    pub attempt: u64,
    #[serde(default)]
    pub logfile: String,
    #[serde(default)]
    pub previous_ids: Vec<String>,
    pub donau_jobid: Option<String>,
    pub state: String,
    #[serde(default)]
    pub phase: Lifecycle,
    pub ts: u64,
}
#[derive(Deserialize, Serialize)]
struct Event {
    event: String,
    job: Job,
}

pub struct Store {
    pub jobs: HashMap<String, Job>,
    path: PathBuf,
    file: File,
    _lock: File,
    dirty: bool,
    pub healthy: bool,
    rotation_at: u64,
}
impl Store {
    pub fn open(path: &Path) -> Result<Self> {
        let path = &std::path::absolute(path)?;
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent)?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.with_extension("lock"))?;
        lock.try_lock_exclusive()
            .context("another sidecar owns the journal")?;
        let mut jobs = HashMap::new();
        if path.exists() {
            let bytes = fs::read(path)?;
            let lines: Vec<_> = bytes.split(|byte| *byte == b'\n').collect();
            for (index, line) in lines.iter().enumerate() {
                if line.is_empty() {
                    continue;
                }
                match serde_json::from_slice::<Event>(line) {
                    Ok(mut event) => {
                        event.job.phase = Lifecycle::from_scheduler(&event.job.state);
                        jobs.insert(event.job.id.clone(), event.job);
                    }
                    Err(error) if index + 1 == lines.len() && !bytes.ends_with(b"\n") => {
                        tracing::warn!(%error, "discarding torn final journal record")
                    }
                    Err(error) => bail!("corrupt journal record {}: {error}", index + 1),
                }
            }
        }
        let file = OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .open(path)?;
        let mut store = Self {
            jobs,
            path: path.into(),
            file,
            _lock: lock,
            dirty: false,
            healthy: true,
            rotation_at: MAX_BYTES,
        };
        // Snapshot also removes any torn tail before further appends.
        store.compact(false)?;
        Ok(store)
    }
    pub fn record(&mut self, event: &str, mut job: Job, durable: bool) -> Result<()> {
        if !self.healthy {
            bail!("journal is unhealthy; refusing untracked submission");
        }
        job.ts = now();
        job.phase = Lifecycle::from_scheduler(&job.state);
        let result = (|| -> Result<()> {
            let mut bytes = serde_json::to_vec(&Event {
                event: event.into(),
                job: job.clone(),
            })?;
            bytes.push(b'\n');
            self.file.write_all(&bytes)?;
            self.dirty = true;
            if durable {
                self.flush()?;
            }
            Ok(())
        })();
        if result.is_err() {
            self.healthy = false;
        }
        result?;
        self.jobs.insert(job.id.clone(), job);
        if self.file.metadata()?.len() > self.rotation_at {
            if let Err(error) = self.compact(true) {
                self.healthy = false;
                return Err(error);
            }
        }
        Ok(())
    }
    pub fn flush(&mut self) -> Result<()> {
        if self.dirty {
            if let Err(error) = self.file.sync_all() {
                self.healthy = false;
                return Err(error.into());
            }
            self.dirty = false;
        }
        Ok(())
    }
    fn compact(&mut self, rotate: bool) -> Result<()> {
        self.jobs.retain(|_, job| {
            !donau::terminal(&job.state) || now().saturating_sub(job.ts) <= RETENTION
        });
        let temp = self.path.with_extension("snapshot.tmp");
        let mut output = File::create(&temp)?;
        for job in self.jobs.values() {
            serde_json::to_writer(
                &mut output,
                &Event {
                    event: "snapshot".into(),
                    job: job.clone(),
                },
            )?;
            output.write_all(b"\n")?;
        }
        output.sync_all()?;
        if rotate {
            // Hard-link before atomic replace: current journal always exists after a crash.
            let archived =
                self.path
                    .with_extension(format!("journal.{}.{}", now(), std::process::id()));
            if !archived.exists() {
                fs::hard_link(&self.path, archived)?;
            }
        }
        fs::rename(temp, &self.path)?;
        File::open(self.path.parent().unwrap_or_else(|| Path::new(".")))?.sync_all()?;
        self.file = OpenOptions::new().append(true).open(&self.path)?;
        // A live checkpoint can exceed 50 MiB at 100k jobs. Budget another
        // segment of appended events instead of rewriting that checkpoint on
        // every subsequent record (which would turn submission into O(n^2) IO).
        self.rotation_at = self.file.metadata()?.len().saturating_add(MAX_BYTES);
        self.dirty = false;
        let prefix = format!(
            "{}.",
            self.path.file_name().unwrap_or_default().to_string_lossy()
        );
        for entry in fs::read_dir(self.path.parent().unwrap_or_else(|| Path::new(".")))? {
            let entry = entry?;
            if entry.file_name().to_string_lossy().starts_with(&prefix) {
                let age = entry
                    .metadata()?
                    .modified()?
                    .elapsed()
                    .unwrap_or_default()
                    .as_secs();
                if age > RETENTION {
                    fs::remove_file(entry.path())?;
                }
            }
        }
        Ok(())
    }
    pub fn active_ids(&self) -> Vec<String> {
        self.jobs
            .values()
            .filter(|job| !donau::terminal(&job.state))
            .filter_map(|job| job.donau_jobid.clone())
            .collect()
    }
    pub fn update(
        &mut self,
        states: &HashMap<String, String>,
        covered: &std::collections::HashSet<String>,
    ) -> Result<()> {
        let jobs: Vec<_> = self
            .jobs
            .values()
            .filter(|job| !donau::terminal(&job.state))
            .filter(|job| {
                job.donau_jobid
                    .as_ref()
                    .map(|id| covered.contains(id))
                    .unwrap_or(false)
            })
            .cloned()
            .collect();
        for mut job in jobs {
            let state = job
                .donau_jobid
                .as_ref()
                .and_then(|id| states.get(id))
                .cloned()
                .unwrap_or_else(|| "UNKNOWN".into());
            if job.state != state {
                job.state = state;
                let terminal = donau::terminal(&job.state);
                self.record(
                    if terminal {
                        "job_finished"
                    } else {
                        "job_state"
                    },
                    job,
                    terminal,
                )?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_corrupt_complete_record_and_failed_writes() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("donau_sidecar.journal");
        fs::write(&path, b"not json\n")?;
        assert!(Store::open(&path).is_err());
        fs::write(&path, b"")?;
        let mut store = Store::open(&path)?;
        store.file = OpenOptions::new().write(true).open("/dev/full")?;
        let job = Job {
            id: "a".into(),
            name: "smk_a".into(),
            run_uuid: "run".into(),
            attempt: 1,
            logfile: "log".into(),
            previous_ids: Vec::new(),
            donau_jobid: None,
            state: "UNKNOWN".into(),
            phase: Lifecycle::Unknown,
            ts: now(),
        };
        assert!(store.record("submit_intent", job, true).is_err());
        assert!(!store.healthy);
        assert!(store.jobs.is_empty());
        Ok(())
    }

    #[test]
    fn failed_rotation_stops_new_writes_but_preserves_durable_record() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("donau_sidecar.journal");
        let mut store = Store::open(&path)?;
        let occupied = dir.path().join("occupied");
        fs::create_dir(&occupied)?;
        store.path = occupied;
        store.rotation_at = 0;
        let job = Job {
            id: "a".into(),
            name: "smk_a".into(),
            run_uuid: "run".into(),
            attempt: 1,
            logfile: "log".into(),
            previous_ids: Vec::new(),
            donau_jobid: Some("1".into()),
            state: "SUBMITTED".into(),
            phase: Lifecycle::Submitted,
            ts: now(),
        };
        assert!(store.record("job_submitted", job.clone(), true).is_err());
        assert!(!store.healthy);
        assert!(store.record("job_state", job, true).is_err());
        drop(store);
        assert_eq!(Store::open(&path)?.active_ids(), vec!["1"]);
        Ok(())
    }

    #[test]
    #[cfg(unix)]
    fn oversized_live_checkpoint_does_not_rotate_on_every_append() -> Result<()> {
        use std::os::unix::fs::MetadataExt;
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("donau_sidecar.journal");
        let mut store = Store::open(&path)?;
        // Model the total serialized live state size without 100k subprocesses.
        let mut job = Job {
            id: "large".into(),
            name: "smk_large".into(),
            run_uuid: "run".into(),
            attempt: 1,
            logfile: "x".repeat(MAX_BYTES as usize + 1),
            previous_ids: Vec::new(),
            donau_jobid: Some("1".into()),
            state: "SUBMITTED".into(),
            phase: Lifecycle::Submitted,
            ts: now(),
        };
        store.record("job_submitted", job.clone(), true)?;
        let checkpoint_inode = store.file.metadata()?.ino();
        job.id = "small".into();
        job.logfile = "small.log".into();
        job.donau_jobid = Some("2".into());
        store.record("job_submitted", job, true)?;
        assert_eq!(store.file.metadata()?.ino(), checkpoint_inode);
        assert_eq!(store.active_ids().len(), 2);
        Ok(())
    }

    #[test]
    fn rotation_keeps_live_state_and_expires_archives() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("donau_sidecar.journal");
        let expired = dir.path().join("donau_sidecar.journal.expired");
        let old = File::create(&expired)?;
        old.set_times(
            fs::FileTimes::new()
                .set_modified(SystemTime::now() - std::time::Duration::from_secs(RETENTION + 60)),
        )?;
        let mut store = Store::open(&path)?;
        store.file.set_len(MAX_BYTES + 1)?;
        let job = Job {
            id: "a".into(),
            name: "smk_a".into(),
            run_uuid: "run".into(),
            attempt: 1,
            logfile: "log".into(),
            previous_ids: Vec::new(),
            donau_jobid: Some("1".into()),
            state: "SUBMITTED".into(),
            phase: Lifecycle::Submitted,
            ts: now(),
        };
        store.record("job_submitted", job, true)?;
        assert!(store.file.metadata()?.len() < MAX_BYTES);
        assert!(!expired.exists());
        drop(store);
        assert_eq!(Store::open(&path)?.active_ids(), vec!["1"]);
        Ok(())
    }

    #[test]
    fn journal_lock_replay_and_torn_tail() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("donau_sidecar.journal");
        let mut store = Store::open(&path)?;
        assert!(Store::open(&path).is_err());
        store.record(
            "job_submitted",
            Job {
                id: "a".into(),
                name: "smk_a".into(),
                run_uuid: "run".into(),
                attempt: 1,
                logfile: "log".into(),
                donau_jobid: Some("1".into()),
                previous_ids: Vec::new(),
                state: "SUBMITTED".into(),
                phase: Lifecycle::Submitted,
                ts: now(),
            },
            true,
        )?;
        drop(store);
        OpenOptions::new()
            .append(true)
            .open(&path)?
            .write_all(b"{\"torn\"")?;
        let store = Store::open(&path)?;
        assert_eq!(store.active_ids(), vec!["1"]);
        drop(store);
        assert!(Store::open(&path).is_ok());
        Ok(())
    }
}
