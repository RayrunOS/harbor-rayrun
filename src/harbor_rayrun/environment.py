"""Rayrun 沙箱云的 Harbor provider（`docs/plans/harbor-integration.md` M3）。

Harbor 把每个 trial 跑在一个隔离环境里，环境由 provider 实现。本文件把那个环境
接到 **Rayrun 的 HTTP API**（控制面）—— 与 E2B/Daytona 那类 provider 不同，这里
**不走沙箱的入向**（不拨端口、不需要公网域名），所以不依赖 `docs/13` 的服务发布。

契约面（`15` §2.1）与 Rayrun 端点（`06` §4）的映射：

    start(force_build)           → 校验镜像存在 + POST /sandboxes（201 即 Running）
    stop(delete=True)            → DELETE /sandboxes/{id}
    stop(delete=False)           → POST /sandboxes/{id}/stop  ← ADR-005 冷停：留盘停表
    exec(...)                    → POST /sandboxes/{id}/exec  ← M1 新落的原语（ADR-007）
    upload_file / upload_dir     → PUT  /sandboxes/{id}/files/{path}
    download_file / download_dir → GET …/files/{path} + GET …/files（单层列目录）
    is_dir / is_file             → 基类默认实现（走 exec 的 test -d/-f），不覆写

**已知边界（诚实声明，不是缺陷；`15` §6.3「声明即契约」）**：

1. **不支持 PUBLIC 出网**：Rayrun 的 egress 是「精确白名单」模型，**没有「允许全部」
   档位**（`06` §4.3）。Harbor 的 `NetworkPolicy` 默认是 `PUBLIC`，这类任务会在建沙箱
   之前**明确报错** —— 绝不静默降级成断网（那会让任务以为能联网却莫名失败），也绝不
   假装白名单能覆盖「全部」。要跑这类任务：任务声明 `network_mode = "allowlist"`
   （列出所需域名）或 `"no-network"`。⚠️ Harbor 对 `PUBLIC` 没有能力位可拦，
   所以这道闸必须在本 provider 里。
2. **不支持 docker-compose 任务**（`capabilities.docker_compose=False`，Harbor 会在
   开跑前按声明裁剪任务）。
3. **无 GPU / TPU / Windows**；**不支持运行期改网络策略**（`dynamic_network_policy=False`）。
4. **镜像不由 Harbor 构建**（也刻意不给 provider 加构建能力：那等于把「构建任意
   Dockerfile」的 RCE 面开在产品面上，`15` §5.1 方案 A 已否决）。镜像来自离线 builder
   （`images/build-task-image.sh`）；缺失时 `start()` 报错并附一条可直接复制的构建命令。
5. 单次 exec 的输出上限 4 MiB（超出置 `truncated`，会打 warn）；命令超时按
   `timeout(1)` 惯例给 Harbor 返回 124（Rayrun 契约里超时的 `exit_code` 是 null）。
"""

from __future__ import annotations

import os
import shlex
import uuid
from pathlib import Path, PurePosixPath
from typing import override

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.definition import (
    COMPOSE_FILE_NAME,
    require_agent_environment_definition,
)
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import EnvironmentPaths, TrialPaths

from harbor_rayrun.client import RayrunClient

# env 名沿用 CLI 的既有约定（`07` §2.5），不新造一套 key 体系。
ENV_API_URL = "RAYRUN_API_URL"
ENV_TOKEN = "RAYRUN_TOKEN"
# 镜像名约定：`<prefix><environment_name>@<version>`。builder 的 `--name` 由使用者
# 决定、未必等于 Harbor 的任务名，所以两者都可覆盖。
ENV_IMAGE_PREFIX = "RAYRUN_IMAGE_PREFIX"
ENV_IMAGE_VERSION = "RAYRUN_IMAGE_VERSION"

# 任务没声明资源时的缺省（Rayrun 的下限是 1 核 / 512 MB，这里取更贴近评测的档位）。
DEFAULT_CPUS = 2
DEFAULT_MEMORY_MB = 2048

# 命令超时给 Harbor 的退出码：照 timeout(1) 惯例，与 Rayrun CLI 同口径（`07` §2.4）。
EXIT_CODE_TIMEOUT = 124

# 调用方没给 timeout_sec 时用的命令超时 = Rayrun exec 的上限（`06` §4.4.1）。
#
# 为什么不是「不传，让服务端用缺省」：Rayrun 的 exec **有**服务端缺省 300s，而
# Harbor 对 verifier 的时限是在**外层**用 asyncio.wait_for 兜的（docker provider
# 里 docker exec 本身无界）。照缺省走会让「外层给 900s、内层 300s 就杀」的截断
# 静默发生 —— 表现是 verifier 跑到第 5 分钟被打断、Harbor 只报 reward 文件不存在
#（2026-09-27 M4 实测踩到，`docs/plans/harbor-integration.md` §13.6）。
# 取上限 ⇒ **外层成为唯一时限权威**，语义与 docker provider 对齐。
DEFAULT_EXEC_TIMEOUT_SEC = 3600

# 沙箱名上限（`06` §4.3：同客户内活跃沙箱名唯一，≤ 64 字符）。
MAX_SANDBOX_NAME = 64

# 文件传输的中转目录（相对 workdir）。文件 API 只认 workdir 相对路径，Harbor
# 的路径全在 workdir 之外，所以每笔传输都要在此落一次脚。点号开头 + 每笔一个
# 随机子目录：与任务自己的文件不会撞名，也不会被任务的 `ls` 当成它的产物。
STAGING_DIR = ".rayrun-harbor-xfer"

# Harbor 约定的落盘路径（**从它的 EnvironmentPaths 派生**，不在这里抄一份 ——
# Harbor 改目录时这里跟着走）。它们在 workdir 之外，而沙箱里清单外的目录一律
# 只读（ADR-006 的 overlay 只覆盖固定的 7 个子树）⇒ 任务镜像必须把这三个目录
# 声明为可写，否则 verifier 连 `/logs/verifier/reward.txt` 都写不出来。
_HARBOR_PATHS = EnvironmentPaths()
HONORED_WRITABLE_DIRS: tuple[str, ...] = (
    str(_HARBOR_PATHS.logs_dir),
    str(_HARBOR_PATHS.tests_dir),
    str(_HARBOR_PATHS.solution_dir),
)
WRITABLE_HINT = (
    "  ⚠ --writable-dir 是**必需**的（不是可选优化）：这几个目录在沙箱里默认只读，"
    "详见 docs/plans/harbor-integration.md §13.3"
)


class RayrunEnvironment(BaseEnvironment):
    """把 Rayrun 沙箱接成 Harbor 的评测环境。"""

    @classmethod
    @override
    def preflight(cls) -> None:
        """开跑前校验凭证（Harbor 在 job 启动时调用；失败即 SystemExit）。"""
        missing = [k for k in (ENV_API_URL, ENV_TOKEN) if not os.environ.get(k)]
        if missing:
            raise SystemExit(
                f"Rayrun provider 需要 {' 与 '.join(missing)}。\n"
                "  托管：export RAYRUN_API_URL=https://api.rayrunos.com/v1\n"
                "  自托管：export RAYRUN_API_URL=http://<daemon 主机>:8080/v1\n"
                "  export RAYRUN_TOKEN=rayrun_<token_id>_<secret>\n"
                "（与 CLI 同名，见 docs/07 §2.5）"
            )

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            *args,
            **kwargs,
        )
        self._client = RayrunClient(
            base_url=self._require_env(ENV_API_URL),
            token=self._require_env(ENV_TOKEN),
        )
        self._sandbox_id: str | None = None
        # 文件 API 的根（= manifest workdir），首次用到时问 guest 一次。
        self._root: str | None = None

    # ------------------------------------------------------------ 契约面
    @staticmethod
    @override
    def type() -> str:
        # 返回普通字符串而非 EnvironmentType 枚举：Harbor 的 type() 显式允许任意
        # 字符串，正是为了让仓库外的第三方 provider 不必改上游枚举（`15` §2.1）。
        return "rayrun"

    @classmethod
    @override
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # 创建期硬分配（`06` §4.3 的 cpu_cores / memory_mb 即上限），不做 request 档。
        return EnvironmentResourceCapabilities(
            cpu_limit=True,
            memory_limit=True,
            cpu_request=False,
            memory_request=False,
        )

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        # 字段名**逐条对着装好的 harbor 版本核过**（0.23.0 里没有 `stream` —— 那
        # 是 main 分支新加的；pydantic 会静默忽略未知字段，写错了不会报错）。
        return EnvironmentCapabilities(
            gpus=False,
            tpus=False,
            windows=False,
            mounted=False,
            docker_compose=False,
            disable_internet=True,                      # egress.allow=[] = 完全断网
            network_allowlist=True,                     # 域名精确匹配 + IPv4 CIDR
            network_allowlist_hostnames=True,
            network_allowlist_wildcard_hostnames=False,  # 精确匹配，刻意不做通配（`01` §9.3）
            network_allowlist_ipv4_addresses=False,      # 只收域名或 CIDR，不收裸 IP
            network_allowlist_ipv6_addresses=False,
            network_allowlist_ipv4_cidrs=True,
            network_allowlist_ipv6_cidrs=False,
            dynamic_network_policy=False,                # egress 只在创建时提交一次
        )

    @override
    def _validate_definition(self) -> None:
        """开跑前的任务校验（纯查文件，不联网）。"""
        if (self.environment_dir / COMPOSE_FILE_NAME).exists():
            raise ValueError(
                f"Rayrun 不支持 docker-compose 任务（{self.environment_dir}/{COMPOSE_FILE_NAME}）。\n"
                "  可行路径：把该任务的环境改写成单个 Dockerfile，再用离线 builder 产成 Rayrun 镜像。"
            )
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
        )

    # ------------------------------------------------------------ 生命周期
    @override
    async def start(self, force_build: bool) -> None:
        """建沙箱。

        `force_build` 对 Rayrun 无意义（镜像不由 provider 构建，见类文档第 4 条）——
        刻意不静默忽略它：镜像不存在时直接报错并给出构建命令。

        顺序有讲究：**先算 egress（PUBLIC 会在这里抛错）→ 再校验镜像 → 最后建沙箱**，
        免得为一个注定要拒绝的任务白建一台 VM。
        """
        if self._sandbox_id is not None:
            raise RuntimeError(f"环境已启动（sandbox={self._sandbox_id}），不要重复 start")

        egress_allow, egress_allow_all = self._egress()
        image_ref = self._image_ref()
        await self._require_image(image_ref)

        cpus = self._effective_cpus or DEFAULT_CPUS
        memory_mb = self._effective_memory_mb or DEFAULT_MEMORY_MB
        if not self._effective_cpus or not self._effective_memory_mb:
            self.logger.debug(
                "任务未声明资源，使用缺省 cpu=%s memory_mb=%s", cpus, memory_mb
            )

        sandbox = await self._client.create_sandbox(
            name=self._sandbox_name(),
            agent=image_ref,
            cpu_cores=int(cpus),
            memory_mb=int(memory_mb),
            egress_allow=egress_allow,
            egress_allow_all=egress_allow_all,
        )
        self._sandbox_id = sandbox.id
        self.logger.debug(
            "rayrun sandbox %s（image=%s ip=%s egress=%s）",
            sandbox.id,
            image_ref,
            sandbox.ip,
            "allow_all" if egress_allow_all else (egress_allow or "[]（断网）"),
        )

        # 基类助手：建挂载点目录（走我们实现的 exec）；「预构建镜像」类任务还会把
        # environment/ 传进 workdir（与 E2B provider 同款调用）。
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()

    @override
    async def stop(self, delete: bool) -> None:
        """`delete=True` → 销毁；`delete=False` → 冷停（留盘、停表、IP 不变）。"""
        if self._sandbox_id is None:
            return
        sandbox_id, self._sandbox_id = self._sandbox_id, None
        try:
            if delete:
                await self._client.delete_sandbox(sandbox_id)
            else:
                await self._client.stop_sandbox(sandbox_id)
        finally:
            await self._client.aclose()

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        sandbox_id = self._require_sandbox()
        resolved_user = self._resolve_user(user)
        # guest 内只有 root（guestd 是 PID 1）：明确拒绝其它 user，好过静默降级。
        if resolved_user not in (None, "root", 0, "0"):
            raise ValueError(f"Rayrun 只支持以 root 执行命令（收到 user={resolved_user!r}）")

        # **shell 语义必须对齐 docker provider**（`["bash","-c",command]`，
        # `environments/docker/docker_unix.py:281`）：installed agent 全都写
        # `set -euo pipefail` 之类的 bash 特性，而 Rayrun exec 的 `command` 形式
        # 归一为 `["/bin/sh","-c",…]`（`daemon/internal/app/exec.go:193`）——
        # Debian/Ubuntu 的 `/bin/sh` 是 **dash**，当场 `set: Illegal option -o
        # pipefail`，agent 连装都装不上。走 `argv` 而不是 `command` 是为了少一层
        # `/bin/sh -c` 与随之而来的引号转义（exec 端点原生支持 argv，`06` §4.4.1）。
        result = await self._client.exec(
            sandbox_id,
            argv=["bash", "-c", command],
            cwd=cwd,
            env=self._merge_env(env),
            timeout_seconds=timeout_sec or DEFAULT_EXEC_TIMEOUT_SEC,
            user="root",
        )
        # 超时：Rayrun 契约里 exit_code 为 null（不伪造 128+9），这里给 Harbor 一个
        # 可判定的失败码（与 CLI 的 124 同口径）。
        return_code = result.exit_code if result.exit_code is not None else EXIT_CODE_TIMEOUT
        if result.truncated:
            self.logger.warning(
                "exec 输出触顶被截断（4 MiB 上限，stdout+stderr 合计）：%s", command[:80]
            )
        return ExecResult(stdout=result.stdout, stderr=result.stderr, return_code=return_code)

    # ------------------------------------------------------------ 文件
    #
    # **路径根**：Rayrun 的文件 API 是 **workdir 相对**的（`06` §4.5 —— 绝对路径
    # 与 `..` 逃逸一律 400 `invalid_path` 并落审计，那是有意的安全边界），而
    # Harbor 用的是**容器绝对路径**（`EnvironmentPaths` 把 `/logs` `/tests`
    # `/solution` 写死在里面）。两者之间必须翻译，否则上传/下载全部 400。
    #
    # 翻译以「中转目录」收口：先把目标用 `cp -a` 挪进 workdir 下的中转目录，
    # 再用文件 API 读写，完事删掉。**刻意不做「本来就在 workdir 里就直通」的
    # 优化** —— Harbor 传给文件 API 的路径全都落在 workdir 之外（它自己的三个
    # 目录），优化出来的分支在真实调用上一次都不会跑，却要多养一条路径。
    # 代价是每次传输多一次 guest 内 `cp`（本地盘，与跨网传输不是一个量级）。
    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        sandbox_id = self._require_sandbox()
        rel = await self._stage_out()
        try:
            with open(source_path, "rb") as fh:
                # guestd 写文件不建父目录（file_ops.go 只 O_CREATE）—— 中转目录
                # 已由 _stage_out 建好，文件名是扁平的，不需要再建父目录。
                await self._client.write_file_stream(sandbox_id, rel, fh)
            await self._move(rel, target_path)
        finally:
            await self._unstage(rel)

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        sandbox_id = self._require_sandbox()
        root = Path(source_dir)
        files = [p for p in sorted(root.rglob("*")) if p.is_file()]
        if not files:
            return
        rel = await self._stage_out()
        try:
            # 一次性把所有需要的目录建出来：guestd 的写文件不建父目录，逐个文件
            # 一次 exec 会慢得多（一个 environment/ 目录可能有几十上百个文件）。
            # 这些是**相对路径** —— exec 的缺省 cwd 就是 workdir，正好对上。
            dirs = sorted({str(PurePosixPath(rel) / p.relative_to(root).parent) for p in files})
            await self._mkdirs(sandbox_id, dirs)
            for path in files:
                sub = path.relative_to(root)
                with open(path, "rb") as fh:
                    await self._client.write_file_stream(sandbox_id, f"{rel}/{sub}", fh)
            # 内容**并进**目标目录（不是把中转目录本身搬过去）：Harbor 对
            # upload_dir 的语义是「把 source 的内容放到 target」。
            guest_root = await self._file_root()
            await self._run(
                f"mkdir -p {shlex.quote(target_dir)} && "
                f"cp -a {guest_root}/{rel}/. {shlex.quote(target_dir)}/"
            )
        finally:
            await self._unstage(rel)

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        sandbox_id = self._require_sandbox()
        rel = await self._stage_in(source_path)
        try:
            data = await self._client.read_file(sandbox_id, rel)
        finally:
            await self._unstage(rel)
        local = Path(target_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        sandbox_id = self._require_sandbox()
        rel = await self._stage_in(source_dir)
        try:
            local_root = Path(target_dir)
            src_root = PurePosixPath(rel)
            # 列目录是**单层**的（`12` §4.2：无递归语义）⇒ 自己走一遍广度优先，
            # 并保留相对目录结构（轨迹里同名文件多，拍平会互相覆盖）。
            queue = [src_root]
            while queue:
                current = queue.pop(0)
                for entry in await self._client.list_files(sandbox_id, str(current)):
                    remote = current / entry["name"]
                    if entry.get("type") == "dir":
                        queue.append(remote)
                        continue
                    local_path = local_root / remote.relative_to(src_root)
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(await self._client.read_file(sandbox_id, str(remote)))
        finally:
            await self._unstage(rel)

    # ---- 路径翻译（中转目录） ----------------------------------------
    async def _file_root(self) -> str:
        """文件 API 的根 = manifest `[run].workdir`（`06` §4.5）。

        用 `pwd` 问 guest 而不是读 manifest：manifest 不过 HTTP，provider 侧拿
        不到；而 exec 的缺省 cwd 恰好就是 workdir（`10` §3.9），问一次即可，
        不需要为 provider 新增接口。顺带在同一趟里验证**它可写** —— 写不了的话
        每次传输都会以 `io_error` 失败，而那个报错完全指不出「镜像的 workdir
        只读，要用 --writable-dir 重建」（`docs/plans/harbor-integration.md` §13.3）。
        """
        if self._root is None:
            # 刻意不经 _run：它的通用报错指不出这里该给的那条修法。
            result = await self._client.exec(
                self._require_sandbox(),
                "touch .rayrun-writecheck && rm -f .rayrun-writecheck && pwd",
                timeout_seconds=60,
                user="root",
            )
            root = result.stdout.strip()
            if result.exit_code != 0 or not root.startswith("/"):
                raise RuntimeError(
                    f"沙箱 workdir 不可写或不可用（exit={result.exit_code}）：{result.stderr.strip()[:200]}\n"
                    "  任务镜像的 workdir 若不在默认可写的子树里（/usr /etc /var /opt /root /home /srv），"
                    "重建镜像时要声明：\n"
                    "    images/build-task-image.sh … --writable-dir <workdir>\n"
                    "  见 docs/plans/harbor-integration.md §13.3 与 ADR-006 修订。"
                )
            self._root = root
        return self._root

    async def _stage_in(self, source: str) -> str:
        """把 guest 内的绝对路径（文件或目录）复制进 workdir 下的中转目录，
        返回**相对路径**（可直接喂文件 API）。调用方负责 `_unstage`。

        复制失败（比如源目录不存在）时**在这里**收摊再抛：调用方的 finally 拿不到
        返回值，谁都不会替它清 —— 而这**不是**异常路径上的边角情况，
        `download_dir("/logs/agent")`（agent 一个字都没写时）每次都会走到。
        """
        rel = await self._stage_out()
        root = await self._file_root()
        try:
            await self._run(f"rm -rf {root}/{rel} && cp -a {shlex.quote(source)} {root}/{rel}")
        except Exception:
            try:
                await self._unstage(rel)
            except Exception:  # noqa: BLE001 —— 清理失败不能盖掉原始错误
                pass
            raise
        return rel

    async def _stage_out(self) -> str:
        """分配一个空的、位于 workdir 下的中转相对路径。

        **只建父目录**（`STAGING_DIR` 本身），返回的路径**刻意保持不存在** ——
        中转路径是什么形状由调用方决定：`upload_file` 要在它上面放一个**文件**，
        而 guestd 的写文件是 `O_CREATE|O_TRUNC|O_WRONLY`
        （`guestd/internal/guest/file_ops.go:43`），往一个已存在的**目录**上写会
        `EISDIR` ⇒ 500 `file op io_error`。`upload_dir` / `_stage_in` 要的是目录，
        它们自己会 `mkdir -p`（前者经 `_mkdirs` 一次性建齐，后者靠 `cp -a` 顺带建）。

        名字带 uuid ⇒ 不会撞上上一轮遗留的中转路径，不需要先 `rm -rf`。
        """
        root = await self._file_root()
        await self._run(f"mkdir -p {root}/{STAGING_DIR}")
        return f"{STAGING_DIR}/{uuid.uuid4().hex[:12]}"

    async def _unstage(self, rel: str) -> None:
        await self._run(f"rm -rf {await self._file_root()}/{rel}")

    async def _move(self, rel: str, target: str) -> None:
        """把中转路径搬到 guest 内的绝对目标（含父目录）。"""
        root = await self._file_root()
        parent = str(PurePosixPath(target).parent)
        mkdir = f"mkdir -p {shlex.quote(parent)} && " if parent not in ("", ".") else ""
        await self._run(f"{mkdir}cp -a {root}/{rel} {shlex.quote(target)}")

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _require_env(name: str) -> str:
        value = os.environ.get(name, "")
        if not value:
            raise ValueError(f"{name} 未设置（preflight 应已拦下；这里防直接构造）")
        return value

    def _require_sandbox(self) -> str:
        if self._sandbox_id is None:
            raise RuntimeError("沙箱尚未启动（先 start()）")
        return self._sandbox_id

    async def _run(self, command: str, timeout_sec: int = 120):
        """跑一条 guest 内命令，非 0 退出即抛（中转目录的搬运全部经这里）。"""
        result = await self._client.exec(
            self._require_sandbox(), command, timeout_seconds=timeout_sec, user="root"
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"guest 命令失败（exit={result.exit_code}）：{command[:120]}\n  {result.stderr.strip()[:200]}"
            )
        return result

    def _image_ref(self) -> str:
        prefix = os.environ.get(ENV_IMAGE_PREFIX, "")
        version = os.environ.get(ENV_IMAGE_VERSION, "1")
        return f"{prefix}{self.environment_name}@{version}"

    def _sandbox_name(self) -> str:
        """沙箱名：Rayrun 要求同客户内活跃名唯一且 ≤64 字符。

        由 Harbor 的 `session_id`（`{trial_name}__{role}__env`）派生 —— 它本身含
        trial 的随机后缀，天然唯一；非 [A-Za-z0-9._-] 的字符换成 `-`。
        """
        safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in self.session_id)
        return safe[:MAX_SANDBOX_NAME] or "harbor-trial"

    def _egress(self) -> tuple[list[str], bool]:
        """把 Harbor 的 NetworkPolicy 映射成 Rayrun 的 `egress`（`06` §4.3）。

        - `no-network` → `allow=[]`（完全断网）
        - `allowlist`  → `allow=[...]`
        - `public`     → **`allow_all=true`**（显式档位，2026-09-27 起）。

        三种模式都**精确执行**，不做任何静默近似 —— 早先 Rayrun 没有「允许全部」
        档位时，PUBLIC 只能被拒绝；补上那一档之后，Harbor 的默认策略（PUBLIC）
        才真正跑得起来，且「这台沙箱开了全出网」在 Rayrun 的审计里看得见。
        """
        policy: NetworkPolicy = self.network_policy
        if policy.network_mode == NetworkMode.NO_NETWORK:
            return [], False
        if policy.network_mode == NetworkMode.PUBLIC:
            return [], True
        return sorted(set(policy.allowed_hosts)), False

    async def _require_image(self, image_ref: str) -> None:
        """镜像必须已在 Rayrun 宿主机上（Rayrun 不做镜像构建，见类文档第 4 条）。"""
        agents = await self._client.agents()
        existing = {f"{a.get('name')}@{a.get('version')}" for a in agents}
        if image_ref in existing:
            return
        name, _, version = image_ref.partition("@")
        available = "、".join(sorted(existing)) or "（宿主机镜像目录为空）"
        # 镜像里的 source 两种形态都要能照做：Dockerfile（本地可构建）与
        # docker_image（预构建，任务只给了引用）。
        source = (
            f"--image {shlex.quote(self.task_env_config.docker_image)}"
            if self.task_env_config.docker_image
            else f"--dockerfile {self.environment_dir}/Dockerfile"
        )
        raise ValueError(
            f"Rayrun 宿主机上没有镜像 {image_ref}。\n"
            "  在**有 docker 的构建机**上产一张（离线 builder，见 docs/plans/harbor-integration.md §10）：\n"
            f"    images/build-task-image.sh {source} \\\n"
            f"        --name {name} --version {version or '1'} --out /tmp/task-images \\\n"
            f"        {' '.join(f'--writable-dir {d}' for d in HONORED_WRITABLE_DIRS)}\n"
            f"    rsync -a --sparse /tmp/task-images/{image_ref}/ <宿主机>:$RAYRUN_IMAGES_DIR/\n"
            f"  {WRITABLE_HINT}\n"
            f"  宿主机现有镜像：{available}"
        )

    async def _mkdirs(self, sandbox_id: str, dirs: list[str]) -> None:
        quoted = " ".join(shlex.quote(d) for d in dirs)
        result = await self._client.exec(sandbox_id, f"mkdir -p {quoted}", timeout_seconds=60)
        if result.exit_code != 0:
            raise RuntimeError(f"mkdir -p 失败（exit={result.exit_code}）：{result.stderr[:200]}")
