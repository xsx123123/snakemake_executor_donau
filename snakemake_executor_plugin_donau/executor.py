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
from typing import List, Generator, Dict, Optional

from snakemake_interface_executor_plugins.executors.base import SubmittedJobInfo
from snakemake_interface_executor_plugins.executors.remote import RemoteExecutor
from snakemake_interface_executor_plugins.jobs import JobExecutorInterface
from snakemake_interface_common.exceptions import WorkflowError

from .logging import setup_logger, logger

class Executor(RemoteExecutor):
    def __post_init__(self):
        # 生成一个唯一的运行ID
        self.run_uuid = str(uuid.uuid4())
        
        # 使用工作目录初始化日志
        workdir = self.workflow.workdir_init or "."
        setup_logger(workdir)
        
        logger.info(f"Donau Executor initialized. Run UUID: {self.run_uuid}")

    def run_job(self, job: JobExecutorInterface):
        """
        核心方法：构造 dsub 命令并提交任务
        """
        # --- 1. 准备日志路径和作业名 ---
        if job.is_group():
            log_folder = f"group_{job.groupid}"
            wildcards_str = "group"
        else:
            log_folder = f"rule_{job.name}"
            wildcards_str = "_".join(f"{k}-{v}" for k, v in job.wildcards_dict.items()) or "unique"
            wildcards_str = wildcards_str.replace("/", "-")

        jobname = f"smk_{job.name}_{self.run_uuid[:8]}"
        
        logfile = os.path.abspath(
            f".snakemake/donau_logs/{log_folder}/{wildcards_str}/{job.jobid}.log"
        )
        os.makedirs(os.path.dirname(logfile), exist_ok=True)

        # --- 2. 构造 dsub 命令 ---
        cmd_parts = ["dsub", "-n", jobname, "-oo", logfile]
        cmd_parts.extend(["--cwd", self.workflow.workdir_init])

        # --- 基础资源映射 ---
        # 队列 (-q)
        if job.resources.get("queue"):
            cmd_parts.extend(["-q", str(job.resources.queue)])
        elif job.resources.get("partition"):
            cmd_parts.extend(["-q", str(job.resources.partition)])

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

        # CPU 和内存 (-R)
        cpus = max(1, job.threads)
        mem_mb = job.resources.get("mem_mb", 1024)
        if job.resources.get("mem_mb_per_cpu"):
            mem_mb = job.resources.mem_mb_per_cpu * cpus
        
        cmd_parts.extend(["-R", f"cpu={cpus},mem={int(mem_mb)}MB"])

        # 运行时间 (-T)
        runtime_min = job.resources.get("runtime") or job.resources.get("time_min")
        if runtime_min:
            try:
                timeout_sec = int(runtime_min) * 60
                cmd_parts.extend(["-T", str(timeout_sec)])
            except ValueError:
                logger.warning(f"Invalid runtime value: {runtime_min}, skipping -T")

        # --- 执行的具体脚本 ---
        exec_job = self.format_job_exec(job)
        cmd_parts.append(exec_job)

        cmd_str = " ".join(shlex.quote(part) for part in cmd_parts)
        logger.debug(f"Submitting job {job.name} with command: {cmd_str}")

        # --- 3. 执行提交并解析 Job ID ---
        try:
            output = self._run_cmd_with_retry(cmd_parts)
            logger.debug(f"Submission output for {job.name}: {output}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Donau submission failed for {job.name}. Output: {e.output}")
            raise WorkflowError(
                f"Donau submission failed.\nCommand: {cmd_str}\nOutput: {e.output}"
            )

        match = re.search(r"<(\d+)>", output) or re.search(r"^\s*(\d+)", output)
        if not match:
            logger.error(f"Could not parse Job ID for {job.name}. Output: {output}")
            raise WorkflowError(f"Could not parse Donau Job ID from output:\n{output}")

        external_jobid = match.group(1)
        logger.info(f"Job {job.name} submitted successfully (ID: {external_jobid})")
        
        self.report_job_submission(
            SubmittedJobInfo(
                job,
                external_jobid=external_jobid,
                aux={"logfile": logfile}
            )
        )

    async def check_active_jobs(
        self,
        active_jobs: List[SubmittedJobInfo]
    ) -> Generator[SubmittedJobInfo, None, None]:
        success_states = {"FINISHED", "SUCCEEDED", "DONE", "0"}
        fail_states = {"FAILED", "ABORTED", "TIMEOUT", "NODE_FAIL", "TERMINATED", "EXIT"}

        job_ids = {job.external_jobid for job in active_jobs}
        if not job_ids:
            return

        status_map = await self._get_donau_job_status_async(job_ids)
        
        if status_map is None:
            for job in active_jobs:
                yield job
            return

        for job in active_jobs:
            jid = job.external_jobid
            if jid not in status_map:
                self.report_job_success(job)
                continue

            state = status_map[jid].upper()
            if state in success_states:
                self.report_job_success(job)
            elif state in fail_states:
                self.report_job_error(
                    job, 
                    msg=f"Donau job {jid} failed with state {state}",
                    aux_logs=[job.aux.get("logfile")]
                )
            else:
                yield job

    def cancel_jobs(self, active_jobs: List[SubmittedJobInfo]):
        if not active_jobs:
            return
        job_ids = [j.external_jobid for j in active_jobs]
        try:
            subprocess.run(["dkill", "-y", "--force"] + job_ids, check=False, stderr=subprocess.PIPE)
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

    async def _get_donau_job_status_async(self, job_ids: set) -> Optional[Dict[str, str]]:
        status_map = {}
        error_occurred = False

        async def query_djob(additional_args: List[str], target_ids: set):
            nonlocal error_occurred
            if not target_ids:
                return
            cmd = ["djob", "-o", "jobid state", "--no-header"] + additional_args + list(target_ids)
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    logger.debug(f"djob command failed: {' '.join(cmd)}\nStderr: {stderr.decode()}")
                    error_occurred = True
                    return

                output = stdout.decode().strip()
                if output:
                    for line in output.split("\n"):
                        parts = line.split()
                        if len(parts) >= 2:
                            jid, state = parts[0], parts[1]
                            if jid in target_ids:
                                status_map[jid] = state
            except Exception as e:
                logger.debug(f"Error querying Donau status: {e}")
                error_occurred = True

        await query_djob([], job_ids)
        
        remaining = job_ids - set(status_map.keys())
        if remaining and not error_occurred:
            await query_djob(["-D"], remaining)

        if error_occurred:
            return None

        return status_map
