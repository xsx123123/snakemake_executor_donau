use crate::{donau, state::Store};
use serde_json::{json, Value};
use std::collections::VecDeque;

#[derive(Default)]
pub struct Metrics {
    pub submits: VecDeque<f64>,
    pub poll_ms: f64,
    pub q_depth: usize,
}
impl Metrics {
    pub fn submitted(&mut self, ms: f64) {
        if self.submits.len() == 10000 {
            self.submits.pop_front();
        }
        self.submits.push_back(ms);
    }
    pub fn heartbeat(&self, store: &Store) -> Value {
        let mut latency: Vec<_> = self.submits.iter().copied().collect();
        latency.sort_by(f64::total_cmp);
        let percentile = |fraction: f64| {
            latency
                .get(((latency.len().saturating_sub(1)) as f64 * fraction) as usize)
                .copied()
                .unwrap_or(0.0)
        };
        let mut active = 0;
        let mut running = 0;
        let mut pending = 0;
        let mut finished = 0;
        let mut failed = 0;
        for job in store.jobs.values() {
            if donau::terminal(&job.state) {
                if donau::success(&job.state) {
                    finished += 1;
                } else {
                    failed += 1;
                }
            } else {
                active += 1;
                if job.state == "RUNNING" {
                    running += 1;
                } else {
                    pending += 1;
                }
            }
        }
        json!({"type":"heartbeat", "active":active,"running":running,"pending":pending,"finished":finished,"failed":failed,"q_depth":self.q_depth,"p50_submit_ms":percentile(0.5),"p99_submit_ms":percentile(0.99),"poll_ms":self.poll_ms})
    }
}
