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

        # 生成一个唯一的运行ID，用于区分不同批次的任务

        self.run_uuid = str(uuid.uuid4())

        

        # 获取持久化目录并初始化日志

        log_dir = ".snakemake"

        if hasattr(self.workflow, "persistence") and self.workflow.persistence:

             log_dir = self.workflow.persistence.path

        

        setup_logger(log_dir)

        logger.info(f"Donau Executor initialized. Run UUID: {self.run_uuid}")



    def additional_general_args(self):

        # 强制 Snakemake 传递任务时每次只传一个 (由插件来处理投递)

        return "--executor cluster-generic --jobs 1"



    def run_job(self, job: JobExecutorInterface):

        """

        核心方法：构造 dsub 命令并提交任务

        """

        # --- 1. 准备日志路径和作业名 ---

        if job.is_group():

            log_folder = f"group_{{job.groupid}}"

            wildcards_str = "group"

        else:

            log_folder = f"rule_{{job.name}}"

            # 将 wildcards 展平为字符串

            wildcards_str = "_".join(f"{k}-{v}" for k, v in job.wildcards_dict.items()) or "unique"

            # 替换掉路径中的斜杠，防止创建文件夹失败

            wildcards_str = wildcards_str.replace("/", "-")



        # 作业名 (Job Name)

        jobname = f"smk_{{job.name}}_{self.run_uuid[:8]}"

        

        # 构造日志文件路径 (.snakemake/donau_logs/...)

        logfile = os.path.abspath(

            f".snakemake/donau_logs/{log_folder}/{wildcards_str}/{job.jobid}.log"

        )

        os.makedirs(os.path.dirname(logfile), exist_ok=True)



        # --- 2. 构造 dsub 命令 ---

        # 基础命令: -n 指定名字, -oo 覆盖输出

        cmd_parts = ["dsub", "-n", jobname, "-oo", logfile]



        # 指定当前工作目录

        cmd_parts.extend(["--cwd", self.workflow.workdir_init])



        # --- 资源映射 ---

        

        # 队列 (-q)

        if job.resources.get("queue"):

            cmd_parts.extend(["-q", str(job.resources.queue)])

        elif job.resources.get("partition"):

            cmd_parts.extend(["-q", str(job.resources.partition)])



        # 账户 (-A) - 新增

        if job.resources.get("account"):

            cmd_parts.extend(["-A", str(job.resources.account)])



        # MPI 类型 (--mpi) - 新增

        # 例如: resources: mpi="openmpi"

        if job.resources.get("mpi"):

            cmd_parts.extend(["--mpi", str(job.resources.mpi)])



        # CPU 和内存 (-R "cpu=X,mem=YMB")

        cpus = max(1, job.threads)

        mem_mb = job.resources.get("mem_mb", 1024)

        if job.resources.get("mem_mb_per_cpu"):

            mem_mb = job.resources.mem_mb_per_cpu * cpus

        

        # Donau 资源字符串格式: cpu=X,mem=YMB

        cmd_parts.extend(["-R", f"cpu={{cpus}},mem={{int(mem_mb)}}MB"])



        # 运行时间 (-T 秒)

        runtime_min = job.resources.get("runtime") or job.resources.get("time_min")

        if runtime_min:

            try:

                timeout_sec = int(runtime_min) * 60

                cmd_parts.extend(["-T", str(timeout_sec)])

            except ValueError:

                self.logger.warning(f"Invalid runtime value: {runtime_min}, skipping -T")



        # --- 执行的具体脚本 ---

        exec_job = self.format_job_exec(job)

        cmd_parts.append(exec_job)



        # 将列表拼接成字符串，用于打印日志或调试

        cmd_str = " ".join(shlex.quote(part) for part in cmd_parts)

        logger.debug(f"Submitting job {job.name} with command: {cmd_str}")



        # --- 3. 执行提交并解析 Job ID (带重试) ---

        try:

            output = self._run_cmd_with_retry(cmd_parts)

            logger.debug(f"Submission output for {job.name}: {output}")

        except subprocess.CalledProcessError as e:

            logger.error(f"Donau submission failed for {job.name}. Output: {e.output}")

            raise WorkflowError(

                f"Donau submission failed.\nCommand: {cmd_str}\nOutput: {e.output}"

            )



        # 解析输出: "Submit job <12345> successfully." 或 "32468352"

        match = re.search(r"<(\d+)>", output) or re.search(r"^\s*(\d+)", output)

            

        if not match:

            logger.error(f"Could not parse Job ID for {job.name}. Output: {output}")

            raise WorkflowError(

                f"Could not parse Donau Job ID from output:\n{output}"

            )



        external_jobid = match.group(1)

        logger.info(f"Job {job.name} submitted successfully with ID: {external_jobid}")

        

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

        """

        核心方法：批量查询任务状态

        """

        success_states = {"FINISHED", "SUCCEEDED", "DONE", "0"}

        fail_states = {"FAILED", "ABORTED", "TIMEOUT", "NODE_FAIL", "TERMINATED", "EXIT"}



        job_ids = {job.external_jobid for job in active_jobs}

        if not job_ids:

            return



        # 调用 djob 查询状态

        status_map = await self._get_donau_job_status_async(job_ids)

        logger.debug(f"Status check results for {len(job_ids)} jobs: {status_map}")



        for job in active_jobs:

            jid = job.external_jobid

            

            if jid not in status_map:

                logger.warning(f"Job {jid} ({job.name}) not found in Donau status query.")

                yield job

                continue



            state = status_map[jid].upper()



            if state in success_states:

                logger.info(f"Job {jid} ({job.name}) finished successfully (State: {state}).")

                self.report_job_success(job)

            elif state in fail_states:

                logger.error(f"Job {jid} ({job.name}) failed with state: {state}")

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

        logger.info(f"Cancelling {len(job_ids)} jobs: {job_ids}")

        

        try:

            # 取消操作通常不需要重试，尽力而为

            subprocess.run(["dkill", "-y", "--force"] + job_ids, check=False, stderr=subprocess.PIPE)

            logger.info("Cancellation command sent.")

        except Exception as e:

            logger.warning(f"Failed to cancel jobs: {e}")



    def _run_cmd_with_retry(self, cmd: List[str], retries: int = 3, delay: int = 2) -> str:

        """

        同步执行 shell 命令，带重试机制。

        用于 dsub 提交，因为提交必须是同步阻塞的以获取 Job ID。

        """

        for i in range(retries):

            try:

                return subprocess.check_output(

                    cmd, text=True, stderr=subprocess.STDOUT

                ).strip()

            except subprocess.CalledProcessError:

                if i == retries - 1:

                    raise # 最后一次尝试失败，抛出异常

                time.sleep(delay * (i + 1)) # 线性退避

        return ""



    async def _get_donau_job_status_async(self, job_ids: set) -> Dict[str, str]:

        """

        异步查询 djob 状态，避免阻塞主线程。

        """

        status_map = {}

        

        async def query_djob(additional_args: List[str], target_ids: set):

            if not target_ids:

                return

            cmd = ["djob", "-o", "jobid state", "--no-header"] + additional_args + list(target_ids)

            

            try:

                # 使用 asyncio 执行子进程

                process = await asyncio.create_subprocess_exec(

                    *cmd,

                    stdout=asyncio.subprocess.PIPE,

                    stderr=asyncio.subprocess.PIPE

                )

                stdout, stderr = await process.communicate()

                

                output = stdout.decode().strip()

                if output:

                    for line in output.split("\n"):

                        parts = line.split()

                        if len(parts) >= 2:

                            jid, state = parts[0], parts[1]

                            if jid in target_ids:

                                status_map[jid] = state

            except Exception as e:

                logger.debug(f"Error querying Donau status async: {e}")



        # 1. 查询活跃任务

        await query_djob([], job_ids)

        

        # 2. 查询已完成任务

        remaining = job_ids - set(status_map.keys())

        if remaining:

            await query_djob(["-D"], remaining)

        

        return status_map
