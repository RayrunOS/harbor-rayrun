"""RayrunClient 的单测（用 httpx.MockTransport 打桩，不打真网络）。

钉的是**线级契约**：请求体形状、路径编码、错误 envelope → 异常、以及
`exec` 的超时语义（`exit_code` 为 null）。契约来源 `docs/06` §4.3/§4.4.1/§4.5。
"""

from __future__ import annotations

import json

import httpx
import pytest

from harbor_rayrun.client import RayrunAPIError, RayrunClient

BASE = "http://rayrun.test/v1"


class Recorder:
    """记录请求并按 (method, path) 返回预置响应的 MockTransport。"""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"error": "not_found"})
        return self.routes[key]

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_client(rec: Recorder) -> RayrunClient:
    return RayrunClient(BASE, "rayrun_test_secret", transport=httpx.MockTransport(rec.handler))


@pytest.mark.asyncio
async def test_exec_maps_response_fields():
    rec = Recorder({
        ("POST", "/v1/sandboxes/sb-1/exec"): httpx.Response(200, json={
            "exec_id": "e-1", "exit_code": 3, "stdout": "o\n", "stderr": "e\n",
            "duration_ms": 42, "timed_out": False, "truncated": False,
        }),
    })
    result = await make_client(rec).exec("sb-1", "echo o; echo e >&2; exit 3")

    assert (result.exit_code, result.stdout, result.stderr) == (3, "o\n", "e\n")
    assert result.timed_out is False and result.truncated is False
    body = json.loads(rec.last.content)
    assert body == {"command": "echo o; echo e >&2; exit 3"}


@pytest.mark.asyncio
async def test_exec_timeout_exit_code_is_none():
    """超时的 `exit_code` 是 null（契约明确不伪造 128+9）→ 客户端如实映射成 None。"""
    rec = Recorder({
        ("POST", "/v1/sandboxes/sb-1/exec"): httpx.Response(200, json={
            "exec_id": "e-2", "exit_code": None, "stdout": "partial", "stderr": "",
            "duration_ms": 1000, "timed_out": True, "truncated": False,
        }),
    })
    result = await make_client(rec).exec("sb-1", "sleep 30", timeout_seconds=1)

    assert result.exit_code is None and result.timed_out is True
    assert json.loads(rec.last.content)["timeout_seconds"] == 1


@pytest.mark.asyncio
async def test_error_envelope_becomes_exception():
    """错误 envelope（`06` §1.5）→ RayrunAPIError，带 code 与 message。"""
    rec = Recorder({
        ("POST", "/v1/sandboxes/sb-1/exec"): httpx.Response(409, json={
            "error": "exec_unsupported",
            "message": "sandbox image does not support exec (guestd too old)",
        }),
    })
    with pytest.raises(RayrunAPIError) as exc:
        await make_client(rec).exec("sb-1", "true")

    assert exc.value.status == 409
    assert exc.value.code == "exec_unsupported"
    assert "guestd too old" in exc.value.message


@pytest.mark.asyncio
async def test_error_non_json_body_is_surfaced():
    """非 JSON 的错误体（如 nginx 的 HTML 413）也要能用 —— 截断进 message。"""
    rec = Recorder({("GET", "/v1/agents"): httpx.Response(502, text="<html>bad gateway</html>")})
    with pytest.raises(RayrunAPIError) as exc:
        await make_client(rec).agents()

    assert exc.value.status == 502
    assert "bad gateway" in exc.value.message


@pytest.mark.asyncio
async def test_create_sandbox_body_shape():
    """创建请求体照 `06` §4.3：egress.allow 是数组（空数组 = 断网）。"""
    rec = Recorder({
        ("POST", "/v1/sandboxes"): httpx.Response(201, json={
            "id": "sb-9", "status": "Running", "ip": "10.42.0.6", "name": "t1",
        }),
    })
    sandbox = await make_client(rec).create_sandbox(
        name="t1", agent="tb-py@1", cpu_cores=2, memory_mb=2048,
        egress_allow=["pypi.org"], workspace_mb=4096,
    )

    assert (sandbox.id, sandbox.status) == ("sb-9", "Running")
    body = json.loads(rec.last.content)
    assert body["agent"] == "tb-py@1"
    assert body["egress"] == {"allow": ["pypi.org"]}
    assert body["workspace_mb"] == 4096


@pytest.mark.asyncio
async def test_file_path_encoding_keeps_slashes():
    """路径按段转义但**保留 `/`**（沙箱内相对路径，不是查询参数）。"""
    rec = Recorder({
        ("PUT", "/v1/sandboxes/sb-1/files/app/my dir/a b.py"): httpx.Response(
            201, json={"path": "app/my dir/a b.py", "size": 3}
        ),
        ("GET", "/v1/sandboxes/sb-1/files/app/x.txt"): httpx.Response(200, content=b"hi"),
    })
    client = make_client(rec)

    await client.write_file("sb-1", "app/my dir/a b.py", b"abc")
    assert rec.last.url.raw_path == b"/v1/sandboxes/sb-1/files/app/my%20dir/a%20b.py"

    assert await client.read_file("sb-1", "/app/x.txt") == b"hi"  # 前导 / 被剥掉


@pytest.mark.asyncio
async def test_delete_and_stop_are_idempotent_on_404():
    """清理路径不该因 404 炸掉：已销毁 / 从未存在都算「没了」。"""
    rec = Recorder({})  # 全部路由回 404
    client = make_client(rec)

    await client.delete_sandbox("sb-gone")
    await client.stop_sandbox("sb-gone")

    assert [r.method for r in rec.requests] == ["DELETE", "POST"]
