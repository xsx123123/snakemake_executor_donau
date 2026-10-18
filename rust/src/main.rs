mod donau;
mod metrics;
mod protocol;
mod scheduler;
mod state;

use anyhow::{bail, Result};
use clap::Parser;
use serde_json::{json, Value};
use std::{path::PathBuf, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    sync::{mpsc, Mutex, Semaphore},
    task::JoinSet,
};

#[derive(Parser)]
#[command(
    version,
    about = "Persistent Donau CLI proxy; stdout is JSON Lines only"
)]
struct Args {
    #[arg(long, default_value_t=32, value_parser=clap::value_parser!(u16).range(1..=64))]
    submit_concurrency: u16,
    #[arg(long, default_value_t=5, value_parser=clap::value_parser!(u64).range(1..))]
    poll_interval: u64,
    #[arg(long, default_value_t=30, value_parser=clap::value_parser!(u64).range(1..))]
    heartbeat_interval: u64,
    #[arg(long, default_value = "donau_sidecar.journal")]
    journal: PathBuf,
    #[arg(long, default_value_t = false)]
    recover_orphans: bool,
}

#[tokio::main]
async fn main() {
    let code = match run().await {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("donau-sidecar: {error:#}");
            1
        }
    };
    // tokio's blocking stdin read cannot be cancelled during runtime destruction.
    std::process::exit(code);
}

async fn run() -> Result<()> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_ansi(false)
        .init();
    let args = Args::parse();
    let output = protocol::Output(Mutex::new(tokio::io::stdout()));
    let (sender, mut receiver) = mpsc::channel::<Value>(1024);
    let input = tokio::spawn(async move {
        let mut lines = BufReader::new(tokio::io::stdin()).lines();
        while let Some(line) = lines.next_line().await? {
            match serde_json::from_str::<Value>(&line) {
                Ok(value) if value.is_object() => {
                    if sender.send(value).await.is_err() {
                        break;
                    }
                }
                Ok(_) => tracing::warn!("discarding non-object protocol message"),
                Err(error) => tracing::warn!(%error, "discarding malformed JSON message"),
            }
        }
        Ok::<(), std::io::Error>(())
    });
    let hello = tokio::time::timeout(Duration::from_secs(30), receiver.recv()).await?;
    let hello = hello.ok_or_else(|| anyhow::anyhow!("EOF before hello"))?;
    if hello.get("type").and_then(Value::as_str) != Some("hello")
        || hello.get("proto").and_then(Value::as_u64) != Some(1)
    {
        output
            .error(&hello, "protocol_mismatch", "expected hello proto=1")
            .await;
        bail!("protocol mismatch");
    }
    let store = match state::Store::open(&args.journal) {
        Ok(store) => store,
        Err(error) => {
            output.error(&hello, "journal_unavailable", &error).await;
            return Err(error);
        }
    };
    let scheduler = Arc::new(scheduler::Scheduler::new(
        store,
        output,
        args.poll_interval,
        args.recover_orphans,
    ));
    // Handshake must not wait for potentially slow recovery queries.
    scheduler
        .output
        .send(json!({"type":"hello","proto":1,"ok":true,"request_id":hello.get("request_id")}))
        .await?;
    // Recovery precedes any new submit, so identities cannot race reconciliation.
    let orphans = scheduler.recover().await?;
    scheduler
        .output
        .send(json!({"type":"recovered","jobs":orphans,"recover_orphans":args.recover_orphans}))
        .await?;
    let poll_scheduler = scheduler.clone();
    let periodic = tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(args.poll_interval));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            if let Err(error) = poll_scheduler.refresh(&[]).await {
                tracing::warn!(%error, "batch poll failed");
            }
        }
    });
    let flush_scheduler = scheduler.clone();
    let flush_task = tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(1));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            if let Err(error) = flush_scheduler.store.lock().await.flush() {
                tracing::error!(%error, "journal flush failed");
            }
        }
    });
    let heartbeat_scheduler = scheduler.clone();
    let heartbeat_task = tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(args.heartbeat_interval));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        interval.tick().await;
        loop {
            interval.tick().await;
            let value = {
                let store = heartbeat_scheduler.store.lock().await;
                heartbeat_scheduler.metrics.lock().await.heartbeat(&store)
            };
            let _ = heartbeat_scheduler.output.send(value).await;
        }
    });
    let semaphore = Arc::new(Semaphore::new(args.submit_concurrency as usize));
    let mut workers = JoinSet::new();
    let mut shutdown = None;
    while let Some(request) = receiver.recv().await {
        while let Some(result) = workers.try_join_next() {
            if let Err(error) = result {
                tracing::error!(%error, "request task failed");
            }
        }
        if request.get("type").and_then(Value::as_str) == Some("shutdown") {
            shutdown = Some(request);
            break;
        }
        // Bound ALL request tasks, not just active child processes.
        let permit = semaphore.clone().acquire_owned().await?;
        scheduler.metrics.lock().await.q_depth = receiver.len();
        let scheduler = scheduler.clone();
        workers.spawn(async move {
            let _permit = permit;
            scheduler.handle(request).await;
        });
    }
    // EOF gives outstanding CLI operations up to 60 seconds to persist their results.
    let drain = async {
        while let Some(result) = workers.join_next().await {
            if let Err(error) = result {
                tracing::error!(%error, "request task failed");
            }
        }
    };
    if tokio::time::timeout(Duration::from_secs(60), drain)
        .await
        .is_err()
    {
        workers.abort_all();
        while workers.join_next().await.is_some() {}
        tracing::warn!("EOF grace expired; unresolved intents remain for recovery");
    }
    flush_task.abort();
    heartbeat_task.abort();
    let _ = flush_task.await;
    let _ = heartbeat_task.await;
    periodic.abort();
    let _ = periodic.await;
    scheduler.store.lock().await.flush()?;
    if let Some(request) = shutdown {
        scheduler
            .output
            .send(json!({"type":"shutdown","ok":true,"request_id":request.get("request_id")}))
            .await?;
    }
    input.abort();
    Ok(())
}
