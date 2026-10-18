"""
Snakemake Executor Plugin for Huawei Donau Scheduler
Core Executor Implementation
"""
import os
import re
import subprocess
import shlex
import uuid
import time
import asyncio
import hashlib
import json
import threading
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import List, Generator, Dict, Optional

from snakemake_interface_executor_plugins.executors.base import SubmittedJobInfo
from snakemake_interface_executor_plugins.executors.remote import RemoteExecutor
from snakemake_interface_executor_plugins.jobs import JobExecutorInterface
from snakemake_interface_common.exceptions import WorkflowError

from .logging import setup_logger, logger
from .recovery import LegacyRecovery
from .sidecar_client import SidecarClient, SidecarUnavailable, SubmissionFailed, SidecarRequestError, find_sidecar

# 状态轮询心跳间隔（秒）默认值；可用 --donau-heartbeat-interval 覆盖。
# 轮询循环正常运行时也会周期性输出一条日志，
# 以便区分“作业在正常运行（沉默）”和“主进程挂死（沉默）”。
STATUS_HEARTBEAT_INTERVAL = 600

# 作业失败时从 `djob -ll` 输出中提取的诊断字段
_DIAGNOSTIC_FIELDS = (
    "exitMessage",
    "jobExitCode",
    "systemExitCode",
    "totalMaxMem",
    "jobWallclockDuration",
    "execNodes",
)


class Executor(RemoteExecutor):
    def __post_init__(self):
        # 生成一个唯一的运行ID
        self.run_uuid = str(uuid.uuid4())

        # 使用当前工作目录初始化日志（优先尊重 Snakefile 中的 workdir 设置）
        workdir = os.getcwd()
        log_dir = getattr(self.executor_settings, "log_dir", None)
        setup_logger(workdir, log_dir)

        self._last_status_heartbeat = 0.0
        concurrency = getattr(self.executor_settings, "submit_concurrency", 16)
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 64:
            raise WorkflowError("donau-submit-concurrency must be between 1 and 64")
        self._submit_concurrency = concurrency
        self._submit_pool = ThreadPoolExecutor(max_workers=concurrency)

        self._sidecar = None
        self._legacy_recovery = None
        self._backend_lock = threading.Lock()
        self._journal_path = Path(log_dir or workdir).resolve() / "donau_sidecar.journal"
        self._mode = os.environ.get("DONAU_MODE") or getattr(self.executor_settings, "sidecar_mode", None) or getattr(self.executor_settings, "mode", "auto")
        if self._mode not in ("auto", "legacy", "sidecar"):
            raise WorkflowError("DONAU_MODE must be auto, legacy or sidecar")
        if self._mode != "legacy":
            path = find_sidecar(getattr(self.executor_settings, "sidecar_path", None))
            try:
                if not path:
                    raise SidecarUnavailable("donau-sidecar binary not found")
                self._sidecar = SidecarClient(
                    path, self._journal_path, concurrency,
                    getattr(self.executor_settings, "recover_orphans", False))
            except (OSError, SidecarUnavailable) as error:
                logger.warning("Sidecar unavailable; using legacy CLI: {}", error)

        logger.info(f"Donau Executor initialized. Run UUID: {self.run_uuid}")
        logger.debug(f"Workflow workdir: {workdir}")

    def shutdown(self):
        try:
            self._submit_pool.shutdown(wait=True)
            if getattr(self, "_sidecar", None) is not None:
                self._sidecar.close()
            if getattr(self, "_legacy_recovery", None) is not None:
                self._legacy_recovery.close()
        finally:
            super().shutdown()

    def run_jobs(self, jobs):
        """Bound outstanding submissions; callbacks stay on the scheduler thread."""
        pending = {}
        errors = []

        def collect():
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                job, request = pending.pop(future)
                try:
                    self._report_submission(job, request, future.result())
                except Exception as error:
                    errors.append(error)

        try:
            for job in jobs:
                if errors:
                    break
                if len(pending) >= self._submit_concurrency:
                    collect()
                    if errors:
                        break
                self.run_job_pre(job)
                request = self._build_submit_request(job)
                future = self._submit_pool.submit(self._submit_worker, request)
                pending[future] = (job, request)
        finally:
            # Account for every already accepted submission, including after an error.
            while pending:
                collect()
        if errors:
            raise errors[0]

    def run_job(self, job: JobExecutorInterface):
        request = self._build_submit_request(job)
        external_jobid = self._submit_pool.submit(self._submit_worker, request).result()
        self._report_submission(job, request, external_jobid)

    def _build_submit_request(self, job):
        # --- 1. 准备日志路径和作业名 ---
        if job.is_group():
            log_folder = f"group_{job.groupid}"
            wildcards_str = "group"
        else:
            log_folder = f"rule_{job.name}"
            wildcards_str = "_".join(f"{k}-{v}" for k, v in job.wildcards_dict.items()) or "unique"
            wildcards_str = wildcards_str.replace("/", "-")

        # 作业名携带 wildcard、jobid 和运行标识，保证在 djob 列表中可区分、
        # 跨多次运行不冲突；超长时逐步裁减（保留 jobid 这一最关键信息）。
        jobname = f"smk_{job.name}_{wildcards_str}_{job.jobid}_{self.run_uuid[:6]}"
        if len(jobname) > 63:
            jobname = f"smk_{job.name}_{job.jobid}_{self.run_uuid[:6]}"
        if len(jobname) > 63:
            jobname = f"smk_{job.name[:40]}_{job.jobid}_{self.run_uuid[:6]}"[:63]

        logfile = os.path.abspath(
            f".snakemake/donau_logs/{log_folder}/{wildcards_str}/{job.jobid}.log"
        )
        os.makedirs(os.path.dirname(logfile), exist_ok=True)

        # --- 2. 构造 dsub 命令 ---
        cmd_parts = ["dsub", "-n", jobname, "-oo", logfile]
        cmd_parts.extend(["--cwd", os.getcwd()])

        # --- 基础资源映射 ---
        # 队列 (-q)：作业 resources 优先，其次插件默认队列 (--donau-default-queue)
        queue = job.resources.get("queue") or job.resources.get("partition")
        if not queue:
            queue = getattr(self.executor_settings, "default_queue", None)
        if queue:
            cmd_parts.extend(["-q", str(queue)])

        # 任意架构 (-aa): 多瑙集群硬性要求 -q 必须与 -aa 一起提交，
        # 否则作业将永远处于 PENDING 状态，无法被调度。
        if getattr(self.executor_settings, "any_arch", True):
            cmd_parts.append("-aa")

        # 优先级 (-p): Donau 范围 [1, 9999]
        if job.priority != 0:
            # 将 Snakemake 优先级映射到 1-9999
            p_val = max(1, min(9999, int(job.priority)))
            cmd_parts.extend(["-p", str(p_val)])

        # 副本数/节点数 (-N)
        # 支持 resources: nodes=X 或 replica=X
        nnodes = job.resources.get("nodes") or job.resources.get("replica")
        if nnodes:
            cmd_parts.extend(["-N", str(nnodes)])

        # 账户 (-A)
        if job.resources.get("account"):
            cmd_parts.extend(["-A", str(job.resources.account)])

        # MPI 类型 (--mpi)
        if job.resources.get("mpi"):
            cmd_parts.extend(["--mpi", str(job.resources.mpi)])

        # 排他模式 (-x)
        # 支持 resources: exclusive=1
        if job.resources.get("exclusive"):
            cmd_parts.extend(["-x", "job"])

        # 自定义标签 (--tag)
        # 支持 resources: tag="key=value"
        if job.resources.get("tag"):
            cmd_parts.extend(["--tag", str(job.resources.tag)])

        # CPU、内存与加速卡 (-R)
        cpus = max(1, job.threads)
        mem_mb = job.resources.get("mem_mb", 1024)
        if job.resources.get("mem_mb_per_cpu"):
            mem_mb = job.resources.mem_mb_per_cpu * cpus

        res_req = f"cpu={cpus},mem={int(mem_mb)}MB"
        # NPU/GPU 加速卡：resources npu=X / gpu=X
        if job.resources.get("npu"):
            res_req += f",npu={int(job.resources.npu)}"
        if job.resources.get("gpu"):
            res_req += f",gpu={int(job.resources.gpu)}"

        cmd_parts.extend(["-R", res_req])

        # 运行时间 (-T)
        runtime_min = job.resources.get("runtime") or job.resources.get("time_min")
        if runtime_min:
            try:
                timeout_sec = int(runtime_min) * 60
                cmd_parts.extend(["-T", str(timeout_sec)])
            except ValueError:
                logger.warning(f"Invalid runtime value: {runtime_min}, skipping -T")

        # 任意额外的 dsub 参数透传（逃生口）：
        # 作业级 resources.dsub_extra 优先于插件级 --donau-submit-extra-args 设置
        extra_args = job.resources.get("dsub_extra") or getattr(
            self.executor_settings, "submit_extra_args", None
        )
        if extra_args:
            cmd_parts.extend(shlex.split(str(extra_args)))

        # --- 执行的具体脚本 ---
        exec_job = self.format_job_exec(job)
        cmd_parts.append(exec_job)

        cmd_str = " ".join(shlex.quote(part) for part in cmd_parts)
        logger.debug(f"Submitting job {job.name} with command: {cmd_str}")

        # Stable across workflow restarts; independent of transient Snakemake job IDs.
        outputs = sorted(os.path.abspath(str(output)) for output in job.output)
        identity = {"cwd": os.getcwd(), "outputs": outputs}
        if not outputs:
            identity.update(rule=job.name, wildcards=sorted(
                getattr(job, "wildcards_dict", {}).items()))
        request_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return {"cmd": cmd_parts, "logfile": logfile, "name": job.name,
                "sidecar": {"id": request_id, "name": jobname, "cwd": os.getcwd(),
                            "logfile": logfile, "exec_cmd": exec_job,
                            "run_uuid": self.run_uuid, "attempt": getattr(job, "attempt", 1),
                            "argv": cmd_parts[1:]}}

    def _submit_worker(self, request):
        sidecar = getattr(self, "_sidecar", None)
        if sidecar is not None:
            try:
                external_jobid = sidecar.submit(request["sidecar"])
                request["logfile"] = request["sidecar"]["logfile"]
                logger.info("Job {} submitted successfully (ID: {})", request["name"], external_jobid)
                return external_jobid
            except (SubmissionFailed, SidecarRequestError) as error:
                raise WorkflowError("Donau sidecar submission failed: {}".format(error))
            except SidecarUnavailable as error:
                # Recover from the durable journal before allowing any CLI retry.
                logger.warning("Sidecar unavailable; reconciling journal before legacy fallback: {}", error)
                with self._backend_lock:
                    if self._sidecar is not None:
                        self._sidecar.close()
                        self._sidecar = None
        journal = getattr(self, "_journal_path", None)
        if journal is not None and journal.exists() and journal.stat().st_size:
            with self._backend_lock:
                if self._legacy_recovery is None:
                    try:
                        self._legacy_recovery = LegacyRecovery(journal)
                    except (OSError, ValueError) as error:
                        raise WorkflowError("Donau journal recovery unavailable: {}".format(error))
            jid = self._legacy_recovery.submit(
                request["sidecar"], lambda: self._legacy_submit_worker(request))
            request["logfile"] = request["sidecar"]["logfile"]
            return jid
        return self._legacy_submit_worker(request)

    def _legacy_submit_worker(self, request):
        cmd_parts = request["cmd"]
        cmd_str = " ".join(shlex.quote(part) for part in cmd_parts)
        name = request["name"]
        try:
            output = self._run_cmd_with_retry(cmd_parts)
            logger.debug(f"Submission output for {name}: {output}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Donau submission failed for {name}. Output: {e.output}")
            raise WorkflowError(
                f"Donau submission failed.\nCommand: {cmd_str}\nOutput: {e.output}"
            )

        match = re.search(r"<(\d+)>", output) or re.search(r"^\s*(\d+)", output)
        if not match:
            logger.error(f"Could not parse Job ID for {name}. Output: {output}")
            raise WorkflowError(f"Could not parse Donau Job ID from output:\n{output}")

        external_jobid = match.group(1)
        logger.info(f"Job {name} submitted successfully (ID: {external_jobid})")

        return external_jobid

    def _report_submission(self, job, request, external_jobid):
        self.report_job_submission(
            SubmittedJobInfo(job, external_jobid=external_jobid,
                             aux={"logfile": request["logfile"]})
        )

    async def check_active_jobs(
        self,
        active_jobs: List[SubmittedJobInfo]
    ) -> Generator[SubmittedJobInfo, None, None]:
        success_states = {"FINISHED", "SUCCEEDED", "DONE", "0"}
        fail_states = {
            "FAILED", "ABORTED", "TIMEOUT", "NODE_FAIL",
            "TERMINATED", "EXIT", "CANCELED", "CANCELLED",
        }

        job_ids = {job.external_jobid for job in active_jobs}
        if not job_ids:
            return

        status_map = None
        if getattr(self, "_sidecar", None) is not None:
            try:
                response = await self._sidecar_async({"type": "poll", "ids": list(job_ids)})
                status_map = response["states"]
            except (SidecarUnavailable, SidecarRequestError, KeyError) as error:
                logger.warning("Sidecar poll unavailable; using legacy djob: {}", error)
        if status_map is None:
            status_map = await self._get_donau_job_status_async(job_ids)

        # 轮询心跳：轮询循环正常工作时也周期性输出一条日志，
        # 以便区分“作业正常运行中”与“主进程挂死”（两者都表现为无日志）。
        # 间隔可用 --donau-heartbeat-interval 调整，设为 0 可关闭。
        heartbeat_interval = getattr(
            self.executor_settings, "heartbeat_interval", STATUS_HEARTBEAT_INTERVAL
        ) or 0
        now = time.time()
        if heartbeat_interval > 0 and now - self._last_status_heartbeat >= heartbeat_interval:
            self._last_status_heartbeat = now
            if status_map is None:
                logger.info(
                    f"[heartbeat] {len(active_jobs)} job(s) active, "
                    "status query failed (djob error), will retry next round"
                )
            else:
                state_counts = Counter(s.upper() for s in status_map.values())
                states_str = ", ".join(
                    f"{k}={v}" for k, v in sorted(state_counts.items())
                )
                logger.info(
                    f"[heartbeat] {len(active_jobs)} job(s) active, "
                    f"queried {len(status_map)} state(s): {states_str}"
                )

        if status_map is None:
            for job in active_jobs:
                yield job
            return

        failed_ids = [job.external_jobid for job in active_jobs
                      if status_map.get(job.external_jobid, "").upper() in fail_states]
        diagnostic_limit = asyncio.Semaphore(self._submit_concurrency)

        async def diagnose(jid):
            async with diagnostic_limit:
                return await self._get_job_diagnostics(jid)

        diagnostics = dict(zip(failed_ids, await asyncio.gather(
            *(diagnose(jid) for jid in failed_ids)
        )))

        for job in active_jobs:
            jid = job.external_jobid
            if jid not in status_map:
                # 查询不到状态 ≠ 作业成功。作业可能仍在运行，
                # 保持活跃并等待下一轮轮询（由 init_seconds_before_status_checks
                # 和 seconds_between_status_checks 控制节奏）。
                logger.debug(
                    f"Job {job.job.name} (ID: {jid}) state unknown, keeping it active."
                )
                yield job
                continue

            state = status_map[jid].upper()
            if state in success_states:
                logger.info(f"Job {job.job.name} (ID: {jid}) finished successfully (State: {state}).")
                self.report_job_success(job)
            elif state in fail_states:
                # 失败时自动取回 djob -ll 诊断信息，让用户无需手动排查死因
                diag = diagnostics.get(jid, {})
                diag_str = ""
                if diag:
                    diag_lines = "\n".join(f"  {k}: {v}" for k, v in diag.items())
                    diag_str = f"\nDonau job details (djob -ll):\n{diag_lines}"
                hint = self._failure_hint(state, diag)
                if hint:
                    diag_str += f"\nHint: {hint}"
                logger.info(f"Job {job.job.name} (ID: {jid}) failed (State: {state}).{diag_str}")
                self.report_job_error(
                    job,
                    msg=f"Donau job {jid} failed with state {state}.{diag_str}",
                    aux_logs=[job.aux.get("logfile")],
                )
            else:
                yield job

    def cancel_jobs(self, active_jobs: List[SubmittedJobInfo]):
        if not active_jobs:
            return
        job_ids = [j.external_jobid for j in active_jobs]
        logger.info(f"Cancelling {len(job_ids)} Donau job(s): {', '.join(job_ids)}")
        if getattr(self, "_sidecar", None) is not None:
            try:
                response = self._sidecar.request({"type": "cancel", "ids": job_ids})
                if response.get("type") == "canceled":
                    return
            except (SidecarUnavailable, SidecarRequestError) as error:
                logger.warning("Sidecar cancel unavailable; using legacy dkill: {}", error)
        try:
            result = subprocess.run(
                ["dkill", "-y", "--force"] + job_ids,
                check=False, capture_output=True, text=True, timeout=30,
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                logger.warning(
                    f"dkill exited with code {result.returncode}: {output}"
                )
            else:
                logger.info(f"dkill output: {output or '(no output)'}")
        except Exception as e:
            logger.warning(f"Failed to cancel jobs: {e}")

    def _run_cmd_with_retry(self, cmd: List[str], retries: int = 3, delay: int = 2) -> str:
        for i in range(retries):
            try:
                return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
            except subprocess.CalledProcessError:
                if i == retries - 1:
                    raise
                time.sleep(delay * (i + 1))
        return ""

    async def _sidecar_async(self, message):
        loop = asyncio.get_running_loop()
        sidecar = self._sidecar
        if sidecar is None:
            raise SidecarUnavailable("sidecar has switched to legacy")
        return await loop.run_in_executor(None, sidecar.request, message)

    async def _get_job_diagnostics(self, jid: str) -> Dict[str, str]:
        """
        作业失败时从 `djob -ll <jid>` 提取关键诊断字段（exitMessage、
        退出码、峰值内存、运行时长、执行节点等）。任何失败都返回空 dict，
        诊断是尽力而为的附加信息，绝不影响失败状态本身的上报。
        """
        if getattr(self, "_sidecar", None) is not None:
            try:
                response = await self._sidecar_async({"type": "diag", "id": str(jid)})
                return response.get("fields", {})
            except (SidecarUnavailable, SidecarRequestError):
                pass
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "djob", "-ll", str(jid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode != 0:
                return {}
        except Exception as e:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            logger.debug(f"Could not fetch djob -ll diagnostics for {jid}: {e}")
            return {}

        diag = {}
        for line in stdout.decode(errors="replace").splitlines():
            m = re.match(r"^\s*([A-Za-z]+)[ \t]+(.*\S)[ \t]*$", line)
            if m and m.group(1) in _DIAGNOSTIC_FIELDS:
                diag[m.group(1)] = m.group(2)
        return diag

    @staticmethod
    def _failure_hint(state: str, diag: Dict[str, str]) -> str:
        """根据失败状态和诊断信息给出可操作的处理建议。"""
        text = " ".join(diag.values()).lower()
        if state == "TIMEOUT" or "timeout" in text:
            return (
                "job hit the walltime limit, consider increasing "
                "resources.runtime (minutes)"
            )
        if "mem limit" in text or "errcode: 143" in text:
            return (
                "job exceeded its memory limit, consider increasing "
                "resources.mem_mb"
            )
        exit_code = diag.get("jobExitCode")
        if exit_code and exit_code != "0":
            return (
                f"job command exited with code {exit_code}, "
                "check the job log for details"
            )
        return ""

    async def _get_donau_job_status_async(self, job_ids: set) -> Optional[Dict[str, str]]:
        """
        批量查询作业状态，返回 {jobid: state}。

        查询策略（与 README 设计一致）：
        1. 单次批量查询活跃队列：
           `djob -o "jobId state" --no-header <jid...>`（不带 -D），
           可覆盖 PENDING / RUNNING / 近期完成的作业；
        2. 活跃队列查不到的 ID，再批量追加 `-D` 查询已完成/历史作业数据库：
           `djob -D -o "jobId state" --no-header <jid...>`；
        3. 两轮都查不到 → 不放入 status_map，调用方将其保持活跃继续等待，
           绝不视为成功（作业可能仍在运行）。

        仅当第一轮查询整体失败（djob 不可用等）时才返回 None，
        调用方据此保留全部作业，避免误判。
        """

        async def _query(ids, extra_args: List[str]) -> Dict[str, str]:
            """单次批量 djob 查询，返回 {jobid: state}。"""
            cmd = ["djob"] + extra_args + ["-o", "jobId state", "--no-header"]
            cmd += [str(j) for j in ids]
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                raise RuntimeError(
                    f"djob {' '.join(extra_args)} failed: {stderr.decode().strip()}"
                )

            result = {}
            for line in stdout.decode().splitlines():
                parts = line.strip().split()
                # 有效状态行形如 "<jobid> <STATE>"，首列必须是纯数字 ID；
                # 其余行（Error、分隔线、提示信息等）全部跳过
                if len(parts) >= 2 and parts[0].isdigit():
                    result[parts[0]] = parts[1]
            return result

        try:
            status_map = await _query(job_ids, [])
        except Exception as e:
            logger.debug(f"djob batch query failed: {e}")
            return None

        missing = set(job_ids) - set(status_map)
        if missing:
            try:
                recovered = await _query(missing, ["-D"])
                status_map.update(recovered)
                logger.debug(
                    f"djob -D recovered {len(recovered)}/{len(missing)} finished job(s)"
                )
            except Exception as e:
                # 第二轮失败不致命：缺失的作业由调用方保持活跃继续等待
                logger.debug(f"djob -D batch query failed: {e}")

        return status_map
