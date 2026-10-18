"""Exercise actual legacy plugin batch submission against the synthetic CLI."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from loguru import logger
from snakemake_executor_plugin_donau import Executor

logger.remove()
cli = str(Path(__file__).resolve().parents[1] / "tests/fixtures/dsub")
results = []
for workers in (1, 16):
    obj = object.__new__(Executor)
    obj._submit_concurrency = workers
    obj._submit_pool = ThreadPoolExecutor(max_workers=workers)
    obj.run_job_pre = lambda job: None
    obj._build_submit_request = lambda job: {
        "cmd": [sys.executable, cli], "name": "bench", "logfile": "unused"}
    reported = []
    obj.report_job_submission = reported.append
    start = time.perf_counter()
    obj.run_jobs([SimpleNamespace(name="bench") for _ in range(1000)])
    wall = time.perf_counter() - start
    obj._submit_pool.shutdown()
    assert len(reported) == 1000
    results.append({"workers": workers, "count": len(reported), "wall_seconds": wall,
                    "throughput": 1000 / wall})
print(json.dumps({"kind": "synthetic actual plugin run_jobs, 20ms CLI sleep",
                  "results": results}, indent=2))
