use crate::{
    donau,
    metrics::Metrics,
    protocol::{Output, Submit},
    state::{now, Job, Store},
};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::Mutex;

#[derive(Default)]
pub struct PollCache {
    at: Option<Instant>,
    covered: HashSet<String>,
    states: HashMap<String, String>,
}
pub struct Scheduler {
    pub store: Mutex<Store>,
    pub output: Output,
    pub metrics: Mutex<Metrics>,
    cache: Mutex<PollCache>,
    identities: Mutex<HashMap<String, Arc<Mutex<()>>>>,
    pub poll_interval: Duration,
    orphans: Mutex<HashSet<String>>,
    pub recover_orphans: bool,
}
impl Scheduler {
    pub fn new(store: Store, output: Output, seconds: u64, recover_orphans: bool) -> Self {
        Self {
            store: Mutex::new(store),
            output,
            metrics: Mutex::new(Metrics::default()),
            cache: Mutex::new(PollCache::default()),
            identities: Mutex::new(HashMap::new()),
            poll_interval: Duration::from_secs(seconds),
            orphans: Mutex::new(HashSet::new()),
            recover_orphans,
        }
    }
    pub async fn refresh(&self, extra: &[String]) -> Result<HashMap<String, String>> {
        let mut cache = self.cache.lock().await;
        let ids: HashSet<String> = {
            let orphan_ids = self.orphans.lock().await;
            let store = self.store.lock().await;
            store
                .jobs
                .values()
                .filter(|job| !donau::terminal(&job.state))
                .filter(|job| self.recover_orphans || !orphan_ids.contains(&job.id))
                .filter_map(|job| job.donau_jobid.clone())
                .chain(extra.iter().cloned())
                .collect()
        };
        if cache
            .at
            .map(|at| at.elapsed() < self.poll_interval)
            .unwrap_or(false)
            && ids.is_subset(&cache.covered)
        {
            return Ok(cache.states.clone());
        }
        let start = Instant::now();
        let queried = donau::poll(&ids.iter().cloned().collect::<Vec<_>>()).await?;
        let mut store = self.store.lock().await;
        if let Err(error) = store.update(&queried, &ids) {
            tracing::error!(%error, "journal state update failed");
        }
        // Include already terminal jobs even when no longer queried by the interval.
        let mut states = queried;
        for job in store
            .jobs
            .values()
            .filter(|job| donau::terminal(&job.state))
        {
            if let Some(id) = &job.donau_jobid {
                states
                    .entry(id.clone())
                    .or_insert_with(|| job.state.clone());
            }
        }
        cache.at = Some(Instant::now());
        cache.covered = ids;
        cache.states = states.clone();
        self.metrics.lock().await.poll_ms = start.elapsed().as_secs_f64() * 1000.0;
        Ok(states)
    }
    pub async fn recover(&self) -> Result<Vec<Job>> {
        // An intent is deliberately never retried from absence in djob.
        let unresolved: Vec<_> = self
            .store
            .lock()
            .await
            .jobs
            .values()
            .filter(|job| job.donau_jobid.is_none() && !donau::terminal(&job.state))
            .cloned()
            .collect();
        for job in unresolved {
            if let Err(error) = self.reconcile(job).await {
                tracing::warn!(%error, "unresolved submission intent");
            }
        }
        if let Err(error) = self.refresh(&[]).await {
            tracing::warn!(%error, "recovery query failed; jobs remain unconfirmed");
        }
        let store = self.store.lock().await;
        let orphans: Vec<_> = store
            .jobs
            .values()
            .filter(|job| !donau::terminal(&job.state))
            .cloned()
            .collect();
        tracing::info!(
            active = orphans.len(),
            known = store.jobs.len(),
            "[recover] journal reconciled"
        );
        drop(store);
        self.orphans
            .lock()
            .await
            .extend(orphans.iter().map(|job| job.id.clone()));
        Ok(orphans)
    }
    async fn reconcile(&self, mut job: Job) -> Result<Job> {
        let matches: Vec<_> = donau::find_name(&job.name)
            .await?
            .into_iter()
            .filter(|(id, _)| !job.previous_ids.contains(id))
            .collect();
        if matches.len() != 1 {
            bail!(
                "ambiguous submission {} ({} name matches); inspect djob before retrying",
                job.id,
                matches.len()
            );
        }
        job.donau_jobid = Some(matches[0].0.clone());
        job.state = matches[0].1.clone();
        self.store
            .lock()
            .await
            .record("job_submitted", job.clone(), true)?;
        Ok(job)
    }
    pub async fn submit(&self, req: Submit) -> Result<String> {
        let args = req.args()?;
        self.orphans.lock().await.remove(&req.id);
        let identity_lock = self
            .identities
            .lock()
            .await
            .entry(req.id.clone())
            .or_default()
            .clone();
        let _identity_guard = identity_lock.lock().await;
        let previous = self.store.lock().await.jobs.get(&req.id).cloned();
        let mut previous_ids = Vec::new();
        if let Some(mut previous) = previous {
            if !donau::terminal(&previous.state)
                || (previous.run_uuid == req.run_uuid && previous.attempt == req.attempt)
            {
                if previous.donau_jobid.is_none() && !donau::terminal(&previous.state) {
                    previous = self.reconcile(previous).await?;
                }
                return previous
                    .donau_jobid
                    .ok_or_else(|| anyhow::anyhow!("previous submission failed; no job ID"));
            }
            previous_ids = previous.previous_ids;
            if let Some(id) = previous.donau_jobid {
                previous_ids.push(id);
            }
            // New attempt/run may intentionally rerun only after confirmed terminal state.
        }
        let mut job = Job {
            id: req.id.clone(),
            name: req.name.clone(),
            run_uuid: req.run_uuid.clone(),
            attempt: req.attempt,
            logfile: req.logfile.clone(),
            donau_jobid: None,
            previous_ids,
            state: "UNKNOWN".into(),
            phase: Default::default(),
            ts: now(),
        };
        // Durable intent precedes the external side effect. Disk failure stops submission.
        self.store
            .lock()
            .await
            .record("submit_intent", job.clone(), true)?;
        let start = Instant::now();
        for attempt in 0..3 {
            match donau::command("dsub", &args).await {
                Ok(text) => {
                    job.donau_jobid = donau::parse_jobid(&text);
                    if job.donau_jobid.is_none() {
                        return self
                            .reconcile(job)
                            .await?
                            .donau_jobid
                            .ok_or_else(|| anyhow::anyhow!("reconciled job has no ID"));
                    }
                    job.state = "SUBMITTED".into();
                    self.store
                        .lock()
                        .await
                        .record("job_submitted", job.clone(), true)?;
                    self.metrics
                        .lock()
                        .await
                        .submitted(start.elapsed().as_secs_f64() * 1000.0);
                    return job
                        .donau_jobid
                        .ok_or_else(|| anyhow::anyhow!("parsed job has no ID"));
                }
                Err(error) => {
                    if error
                        .downcast_ref::<tokio::time::error::Elapsed>()
                        .is_some()
                    {
                        // Killing a timed-out CLI cannot revoke a submission already accepted remotely.
                        return self
                            .reconcile(job)
                            .await?
                            .donau_jobid
                            .ok_or_else(|| anyhow::anyhow!("reconciled job has no ID"));
                    }
                    if attempt == 2 {
                        job.state = "FAILED".into();
                        self.store.lock().await.record("submit_failed", job, true)?;
                        return Err(error);
                    }
                    tokio::time::sleep(Duration::from_secs(2 * (attempt + 1))).await;
                }
            }
        }
        unreachable!("three submission attempts always return")
    }
    pub async fn handle(&self, request: Value) {
        let correlation = request.get("request_id").cloned().unwrap_or(Value::Null);
        let kind = request.get("type").and_then(Value::as_str).unwrap_or("");
        let result: Result<Value> = async {
            match kind {
                "submit" => {
                    let req: Submit = serde_json::from_value(request.clone())?;
                    match self.submit(req.clone()).await {
                        Ok(id) => {
                            let logfile = self.store.lock().await.jobs.get(&req.id).map(|job| job.logfile.clone()).unwrap_or_default();
                            Ok(json!({"type":"submitted","id":req.id,"donau_jobid":id,"logfile":logfile}))
                        },
                        Err(error) => Ok(json!({"type":"submit_failed","id":req.id,"error":error.to_string()})),
                    }
                }
                "poll" => {
                    let ids: Vec<String> = if let Some(ids) = request.get("ids") { serde_json::from_value(ids.clone())? } else { self.store.lock().await.active_ids() };
                    let states = self.refresh(&ids).await?;
                    let wanted: HashSet<_> = ids.iter().collect();
                    Ok(json!({"type":"states","states":states.into_iter().filter(|(id, _)| wanted.contains(id)).collect::<HashMap<_,_>>()}))
                }
                "cancel" => {
                    let ids: Vec<String> = serde_json::from_value(request.get("ids").cloned().unwrap_or(Value::Null))?;
                    if !ids.is_empty() {
                        let mut args = vec!["-y".into(), "--force".into()];
                        args.extend(ids.clone());
                        donau::command("dkill", &args).await?;
                        // dkill acceptance alone is not proof of a terminal state; poll verifies it.
                        self.cache.lock().await.at = None;
                    }
                    Ok(json!({"type":"canceled", "ids":ids}))
                }
                "diag" => {
                    let id = request.get("id").and_then(Value::as_str).ok_or_else(|| anyhow::anyhow!("diag requires id"))?;
                    Ok(json!({"type":"diag","id":id,"fields":donau::diagnostics(id).await}))
                }
                _ => bail!("unknown request type {kind}"),
            }
        }.await;
        match result {
            Ok(mut value) => {
                value["request_id"] = correlation;
                let _ = self.output.send(value).await;
            }
            Err(error) => self.output.error(&request, "request_failed", error).await,
        }
    }
}
