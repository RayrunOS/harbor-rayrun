"""Rayrun HTTP API 的薄客户端（provider 专用）。

只覆盖 provider 真正用到的端点：沙箱生命周期（create/get/stop/delete）、
`exec`、文件读写与列目录、镜像列表。刻意不做成通用 SDK —— `openapi.yaml`
生成的 SDK 是独立立项（`docs/15` §10-Q7），provider 只要一层能对上的薄客户端。

契约来源：`docs/06` §4.3/§4.4.1/§4.5/§4.6；认证与错误 envelope 见 §1.5/§2。
所有非 2xx 一律抛 `RayrunAPIError`（带 code + message），provider 侧不再做二次
翻译 —— 错误码是全产品统一的（`06` §2）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import AsyncIterator, BinaryIO
from urllib.parse import quote

import httpx


class RayrunAPIError(RuntimeError):
    """Rayrun 返回的错误响应（`{"error": code, "message": ...}`，`06` §1.5）。"""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = status
        self.code = code
        self.message = message
        detail = f"{code}: {message}" if message else code
        super().__init__(f"rayrun API {status} {detail}")


@dataclass(frozen=True)
class Sandbox:
    id: str
    status: str
    ip: str | None
    name: str | None


@dataclass(frozen=True)
class CommandResult:
    """`POST /sandboxes/{id}/exec` 的 200 响应（`06` §4.4.1）。

    `exit_code` 为 None 当且仅当 `timed_out` —— 契约明确不伪造 128+9。
    """

    stdout: str
    stderr: str
    exit_code: int | None
    duration_ms: int
    timed_out: bool
    truncated: bool


class RayrunClient:
    """Rayrun daemon 的异步客户端。`base_url` 需含 `/v1`（与 CLI 约定一致）。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` 是**测试缝隙**（与 guestd 的 runCmd / netOps 注入同款范式）：
        单测传 `httpx.MockTransport` 打桩，生产传 None 走真实网络。"""
        if not base_url:
            raise ValueError("RAYRUN_API_URL 为空")
        if not token:
            raise ValueError("RAYRUN_TOKEN 为空")
        # read 超时关掉：exec 同步阻塞到命令结束，可能是分钟级（`06` §4.4.1 的
        # timeout_seconds 最长 3600s，权威超时在 daemon 侧）。connect/write 仍要
        # 有界，否则网络不通时会挂死。
        timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _encode_path(path: str) -> str:
        """把沙箱内的相对路径编进 URL（保留 `/`，其余按段转义）。"""
        return quote(path.lstrip("/"), safe="/")

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        code, message = "unknown", ""
        try:
            body = resp.json()
            code = str(body.get("error") or code)
            message = str(body.get("message") or "")
        except Exception:  # noqa: BLE001 —— 非 JSON 响应（如 nginx 的 HTML 413）
            message = resp.text[:200]
        raise RayrunAPIError(resp.status_code, code, message)

    # ---------------------------------------------------------------- 只读
    async def agents(self) -> list[dict]:
        """`GET /v1/agents`（读宿主机镜像目录，非数据库）。"""
        resp = await self._client.get("/agents")
        self._raise_for_status(resp)
        return resp.json().get("agents", [])

    async def get_sandbox(self, sandbox_id: str) -> Sandbox:
        resp = await self._client.get(f"/sandboxes/{sandbox_id}")
        self._raise_for_status(resp)
        body = resp.json()
        return Sandbox(
            id=body["id"], status=body["status"], ip=body.get("ip"), name=body.get("name")
        )

    async def list_files(self, sandbox_id: str, path: str = "") -> list[dict]:
        """`GET /v1/sandboxes/{id}/files?path=` —— **单层**列目录（`12` §4.2：无递归语义）。"""
        resp = await self._client.get(
            f"/sandboxes/{sandbox_id}/files", params={"path": path} if path else None
        )
        self._raise_for_status(resp)
        return resp.json().get("entries", [])

    async def read_file(self, sandbox_id: str, path: str) -> bytes:
        resp = await self._client.get(
            f"/sandboxes/{sandbox_id}/files/{self._encode_path(path)}"
        )
        self._raise_for_status(resp)
        return resp.content

    # ---------------------------------------------------------------- 写
    async def create_sandbox(
        self,
        *,
        name: str,
        agent: str,
        cpu_cores: int,
        memory_mb: int,
        egress_allow: list[str] | None = None,
        egress_allow_all: bool = False,
        workspace_mb: int | None = None,
        max_lifetime_seconds: int | None = None,
    ) -> Sandbox:
        """`POST /v1/sandboxes`（201 = 已 Running，同步端点，`06` §4.3）。

        `egress_allow=[]` 即**完全断网**；`egress_allow_all=True` 是显式的「允许
        全部出网」（2026-09-27 起，`06` §4.3）—— 两者**互斥**，同时给会被 daemon
        以 400 拒绝（这里也不构造那种请求）。
        """
        if egress_allow_all and egress_allow:
            raise ValueError("egress_allow_all 与 egress_allow 互斥（daemon 会 400）")
        egress: dict = {"allow": list(egress_allow or [])}
        if egress_allow_all:
            egress["allow_all"] = True
        body: dict = {
            "name": name,
            "agent": agent,
            "cpu_cores": cpu_cores,
            "memory_mb": memory_mb,
            "egress": egress,
        }
        if workspace_mb is not None:
            body["workspace_mb"] = workspace_mb
        if max_lifetime_seconds is not None:
            body["max_lifetime_seconds"] = max_lifetime_seconds
        resp = await self._client.post("/sandboxes", json=body)
        self._raise_for_status(resp)
        data = resp.json()
        return Sandbox(
            id=data["id"],
            status=data["status"],
            ip=data.get("ip"),
            name=data.get("name"),
        )

    async def write_file(self, sandbox_id: str, path: str, data: bytes) -> int:
        """`PUT /v1/sandboxes/{id}/files/{path}`（不设大小上限，`06` §4.5）。"""
        return await self.write_file_stream(sandbox_id, path, io.BytesIO(data))

    async def write_file_stream(self, sandbox_id: str, path: str, stream: BinaryIO) -> int:
        """同上，但**流式**发送（逐 1 MiB 块）。

        两个理由：大文件不整份进 provider 的内存；daemon 本就是边读 body 边分块
        发往 guest（`12` D19），先攒全再发没有意义。
        """

        # 必须是**异步**生成器：httpx 的 AsyncClient 拒收同步 request stream
        # （"Attempted to send an sync request with an AsyncClient instance"）。
        async def _chunks() -> AsyncIterator[bytes]:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    return
                yield chunk

        resp = await self._client.put(
            f"/sandboxes/{sandbox_id}/files/{self._encode_path(path)}",
            content=_chunks(),
            headers={"Content-Type": "application/octet-stream"},
        )
        self._raise_for_status(resp)
        return int(resp.json().get("size", 0))

    async def exec(
        self,
        sandbox_id: str,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        user: str | None = None,
    ) -> CommandResult:
        """`POST /v1/sandboxes/{id}/exec` —— **同步**：响应即命令结束（`06` §4.4.1）。"""
        body: dict = {"command": command}
        if cwd:
            body["cwd"] = cwd
        if env:
            body["env"] = env
        if timeout_seconds:
            body["timeout_seconds"] = timeout_seconds
        if user:
            body["user"] = user
        resp = await self._client.post(f"/sandboxes/{sandbox_id}/exec", json=body)
        self._raise_for_status(resp)
        data = resp.json()
        return CommandResult(
            stdout=data.get("stdout") or "",
            stderr=data.get("stderr") or "",
            exit_code=data.get("exit_code"),
            duration_ms=int(data.get("duration_ms") or 0),
            timed_out=bool(data.get("timed_out")),
            truncated=bool(data.get("truncated")),
        )

    # ---------------------------------------------------------------- 生命周期
    async def stop_sandbox(self, sandbox_id: str) -> None:
        """`POST /v1/sandboxes/{id}/stop`（ADR-005 冷停：杀进程、留盘、停表）。"""
        resp = await self._client.post(f"/sandboxes/{sandbox_id}/stop")
        if resp.status_code == 404:
            return  # 幂等：没了就是停好了
        self._raise_for_status(resp)

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """`DELETE /v1/sandboxes/{id}`（204；Terminated 幂等，从未存在 404）。"""
        resp = await self._client.delete(f"/sandboxes/{sandbox_id}")
        if resp.status_code == 404:
            return
        self._raise_for_status(resp)
