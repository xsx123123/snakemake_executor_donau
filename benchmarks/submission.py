"""Reproducible CLI baseline. Synthetic results are not cluster throughput.

Run: .venv/bin/python benchmarks/submission.py > benchmarks/baseline.json
"""
import concurrent.futures
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

CLI = str(Path(__file__).resolve().parents[1] / "tests/fixtures/dsub")


def submit():
    start = time.perf_counter()
    subprocess.check_output([CLI], text=True)
    return time.perf_counter() - start


def measure(count, workers, concurrent_callers):
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        if concurrent_callers:
            latencies = list(pool.map(lambda _: submit(), range(count)))
        else:
            # The proposed synchronous run_job -> submit -> result sequence.
            latencies = [pool.submit(submit).result() for _ in range(count)]
    wall = time.perf_counter() - wall_start
    ordered = sorted(latencies)
    return dict(count=count, workers=workers, concurrent_callers=concurrent_callers,
                wall_seconds=wall, parent_cpu_seconds=time.process_time() - cpu_start,
                throughput=count / wall,
                p50=statistics.median(ordered), p90=ordered[int(count * .90) - 1],
                p99=ordered[int(count * .99) - 1], maximum=max(ordered))


if __name__ == "__main__":
    results = [measure(n, 1, False) for n in (100, 500, 1000)]
    results += [measure(1000, 16, False), measure(1000, 16, True)]
    print(json.dumps({"kind": "synthetic CLI, no Donau cluster",
                      "delay_seconds": float(os.environ.get("MOCK_DSUB_DELAY", "0.02")),
                      "results": results}, indent=2))
