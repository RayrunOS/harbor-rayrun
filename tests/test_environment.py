"""RayrunEnvironment 的单测：把 provider 的**判断**钉住，而不是把 Harbor 跑起来。

刻意用**真的** `harbor.environments.base.BaseEnvironment`（不是 stub）—— 这样能力
声明、资源策略、目录辅助这些由基类消费的部分都是真的；网络那一层用
`httpx.MockTransport` 打桩。真机链路（真沙箱 + 真 exec）归 M4 验收。

覆盖的几条都是**会静默出错**的地方：能力虚报、PUBLIC 被悄悄降级成断网、
镜像缺失没有可操作的提示、超时没有可判定的退出码。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths

from harbor_rayrun import RayrunEnvironment
from harbor_rayrun.client import RayrunClient

BASE = "http://rayrun.test/v1"
IMAGE_REF = "hello-world@1"


class Recorder:
    """按 (method, 路径前缀) 匹配的 MockTransport + 请求记录。"""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.content:
            try:
                self.bodies.append(json.loads(request.content))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.bodies.append({})
        for (method, prefix), response in self.routes.items():
            if request.method == method and request.url.path.startswith(prefix):
                return response
        return httpx.Response(404, json={"error": "not_found"})

    def paths(self, method: str) -> list[str]:
        return [r.url.path for r in self.requests if r.method == method]


def exec_ok(stdout: str = "") -> httpx.Response:
    return httpx.Response(200, json={
        "exec_id": "e", "exit_code": 0, "stdout": stdout, "stderr": "",
        "duration_ms": 1, "timed_out": False, "truncated": False,
    })


def make_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rec: Recorder,
    *,
    network_policy: NetworkPolicy | None = None,
) -> RayrunEnvironment:
    monkeypatch.setenv("RAYRUN_API_URL", BASE)
    monkeypatch.setenv("RAYRUN_TOKEN", "rayrun_test_secret")
    env_dir = tmp_path / "environment"
    env_dir.mkdir(exist_ok=True)
    (env_dir / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir(exist_ok=True)

    # 测试缝隙：把 provider 内部构造的 client 换成打桩版（生产路径不变）。
    real = RayrunClient
    monkeypatch.setattr(
        "harbor_rayrun.environment.RayrunClient",
        lambda base_url, token: real(
            base_url, token, transport=httpx.MockTransport(rec.handler)
        ),
    )
    return RayrunEnvironment(
        environment_dir=env_dir,
        environment_name="hello-world",
        session_id="hello-world__abc123__env",
        trial_paths=TrialPaths(trial_dir=trial_dir),
        task_env_config=EnvironmentConfig(),
        network_policy=network_policy,
    )


def allowlist(hosts: list[str]) -> NetworkPolicy:
    return NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=hosts)


# ---------------------------------------------------------------- 能力声明
def test_capabilities_are_honest(tmp_path, monkeypatch):
    """声明即契约：虚报会让评测跑到一半假失败（`15` §6.3）。"""
    env = make_env(tmp_path, monkeypatch, Recorder())
    caps = env.capabilities

    assert caps.disable_internet is True            # egress.allow=[] 即断网
    assert caps.network_allowlist is True
    assert caps.network_allowlist_hostnames is True
    assert caps.network_allowlist_ipv4_cidrs is True
    # 以下都是「做不到就声明 False」——它们各自对应 Rayrun 的真实边界
    assert caps.network_allowlist_wildcard_hostnames is False   # 精确匹配，不做通配
    assert caps.network_allowlist_ipv4_addresses is False       # 不收裸 IP
    assert caps.dynamic_network_policy is False                 # egress 只在创建时提交
    assert caps.docker_compose is False
    assert caps.gpus is False and caps.tpus is False and caps.windows is False
    assert caps.mounted is False


def test_type_is_rayrun(tmp_path, monkeypatch):
    """`type()` 允许任意字符串（Harbor 显式为第三方 provider 设计）⇒ 无需上游改动。"""
    env = make_env(tmp_path, monkeypatch, Recorder())
    assert env.type() == "rayrun"


# ---------------------------------------------------------------- 网络策略
@pytest.mark.asyncio
async def test_public_network_maps_to_allow_all(tmp_path, monkeypatch):
    """PUBLIC → Rayrun 的显式「允许全部出网」档位（`egress.allow_all=true`）。

    Harbor 的 NetworkPolicy 默认就是 PUBLIC，所以这条映射决定了默认任务能不能跑；
    它必须是**精确执行**（不是静默近似成断网或一串枚举）。
    """
    rec = Recorder({
        ("GET", "/v1/agents"): httpx.Response(200, json={"agents": [{"name": "hello-world", "version": "1"}]}),
        ("POST", "/v1/sandboxes"): httpx.Response(201, json={"id": "sb-1", "status": "Running"}),
        ("POST", "/v1/sandboxes/sb-1/exec"): exec_ok(),
    })
    env = make_env(tmp_path, monkeypatch, rec)  # 默认 policy = PUBLIC

    await env.start(force_build=False)

    create = next(b for b in rec.bodies if "agent" in b)
    assert create["egress"] == {"allow": [], "allow_all": True}


@pytest.mark.asyncio
async def test_no_network_maps_to_empty_allowlist(tmp_path, monkeypatch):
    rec = Recorder({
        ("GET", "/v1/agents"): httpx.Response(200, json={"agents": [{"name": "hello-world", "version": "1"}]}),
        ("POST", "/v1/sandboxes"): httpx.Response(201, json={"id": "sb-1", "status": "Running", "ip": "10.42.0.6"}),
        ("POST", "/v1/sandboxes/sb-1/exec"): exec_ok(),
    })
    env = make_env(tmp_path, monkeypatch, rec, network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))

    await env.start(force_build=False)

    create = next(b for b in rec.bodies if "agent" in b)
    assert create["egress"] == {"allow": []}      # 空数组 = 完全断网


@pytest.mark.asyncio
async def test_allowlist_is_sorted_and_deduped(tmp_path, monkeypatch):
    rec = Recorder({
        ("GET", "/v1/agents"): httpx.Response(200, json={"agents": [{"name": "hello-world", "version": "1"}]}),
        ("POST", "/v1/sandboxes"): httpx.Response(201, json={"id": "sb-1", "status": "Running"}),
        ("POST", "/v1/sandboxes/sb-1/exec"): exec_ok(),
    })
    env = make_env(tmp_path, monkeypatch, rec,
                   network_policy=allowlist(["pypi.org", "files.pythonhosted.org", "pypi.org"]))

    await env.start(force_build=False)

    create = next(b for b in rec.bodies if "agent" in b)
    assert create["egress"]["allow"] == ["files.pythonhosted.org", "pypi.org"]
    assert create["agent"] == IMAGE_REF


# ---------------------------------------------------------------- 镜像
@pytest.mark.asyncio
async def test_missing_image_error_is_actionable(tmp_path, monkeypatch):
    """镜像缺失要给「能直接照做」的构建命令（Rayrun 不做镜像构建，M3.5）。"""
    rec = Recorder({("GET", "/v1/agents"): httpx.Response(200, json={"agents": []})})
    env = make_env(tmp_path, monkeypatch, rec, network_policy=allowlist(["pypi.org"]))

    with pytest.raises(ValueError) as exc:
        await env.start(force_build=False)

    message = str(exc.value)
    assert "build-task-image.sh" in message
    assert "--name hello-world" in message
    assert "rsync" in message


@pytest.mark.asyncio
async def test_validate_definition_rejects_compose(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch, Recorder())
    (env.environment_dir / "docker-compose.yaml").write_text("services: {}\n")

    with pytest.raises(ValueError) as exc:
        env._validate_definition()
    assert "docker-compose" in str(exc.value)


# ---------------------------------------------------------------- exec
@pytest.mark.asyncio
async def test_exec_timeout_maps_to_124(tmp_path, monkeypatch):
    """超时给 Harbor 一个可判定的失败码（Rayrun 契约里 exit_code 是 null）。"""
    rec = Recorder({
        ("POST", "/v1/sandboxes/sb-1/exec"): httpx.Response(200, json={
            "exec_id": "e", "exit_code": None, "stdout": "partial", "stderr": "",
            "duration_ms": 1000, "timed_out": True, "truncated": False,
        }),
    })
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    result = await env.exec("sleep 30", timeout_sec=1)

    assert result.return_code == 124
    assert result.stdout == "partial"


@pytest.mark.asyncio
async def test_exec_rejects_non_root_user(tmp_path, monkeypatch):
    """guest 内只有 root：明确拒绝好过静默降级。"""
    env = make_env(tmp_path, monkeypatch, Recorder())
    env._sandbox_id = "sb-1"

    with pytest.raises(ValueError) as exc:
        await env.exec("id", user="nobody")
    assert "root" in str(exc.value)


@pytest.mark.asyncio
async def test_exec_passes_resolved_cwd_and_env(tmp_path, monkeypatch):
    rec = Recorder({("POST", "/v1/sandboxes/sb-1/exec"): exec_ok("ok")})
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    await env.exec("pwd", cwd="/app", env={"FOO": "bar"})

    body = rec.bodies[-1]
    assert body["cwd"] == "/app"
    assert body["env"] == {"FOO": "bar"}
    assert body["user"] == "root"


# ---------------------------------------------------------------- 命名与文件
def test_sandbox_name_sanitized_and_bounded(tmp_path, monkeypatch):
    """Rayrun 要求活跃沙箱名唯一且 ≤64 字符。"""
    env = make_env(tmp_path, monkeypatch, Recorder())
    env.session_id = "task/with:weird chars__role__env" + "x" * 80

    name = env._sandbox_name()

    assert len(name) <= 64
    assert all(c.isalnum() or c in "._-" for c in name)


@pytest.mark.asyncio
async def test_upload_dir_mkdirs_first_then_uploads(tmp_path, monkeypatch):
    """写文件不建父目录（guestd 只 O_CREATE）⇒ 先一次性 mkdir -p 再逐个写。"""
    rec = Recorder({
        ("POST", "/v1/sandboxes/sb-1/exec"): exec_ok(),
        ("PUT", "/v1/sandboxes/sb-1/files"): httpx.Response(201, json={"path": "x", "size": 1}),
    })
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    src = tmp_path / "env"
    (src / "tests").mkdir(parents=True)
    (src / "Dockerfile").write_text("FROM x\n")
    (src / "tests" / "run.sh").write_text("echo hi\n")

    await env.upload_dir(src, "/app")

    mkdir_body = next(b for b in rec.bodies if "mkdir" in str(b.get("command", "")))
    assert "mkdir -p /app /app/tests" in mkdir_body["command"]
    puts = rec.paths("PUT")
    assert "sb-1/files/app/Dockerfile" in puts[0]
    assert "sb-1/files/app/tests/run.sh" in puts[1]
