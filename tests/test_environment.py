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
from harbor_rayrun.environment import STAGING_DIR

BASE = "http://rayrun.test/v1"
IMAGE_REF = "hello-world@1"


class Recorder:
    """按 (method, 路径前缀) 匹配的 MockTransport + 请求记录。

    exec 是个例外：同一路径上会跑语义完全不同的命令（`pwd` 探测、`mkdir`、
    `cp -a` 搬运、`rm -rf` 清理），按路径路由区分不了 —— 故按**命令内容**分派，
    并把每条命令记进 `commands` 供断言。
    """

    def __init__(
        self,
        routes: dict[tuple[str, str], httpx.Response] | None = None,
        *,
        root: str = "/app",
        root_error: str | None = None,
    ) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.commands: list[str] = []
        # provider 用 argv 形式发 exec（`["bash","-c",command]`）：`commands` 记
        # **内层命令文本**，好让断言停在「命令内容」这一层；包壳形状记在这里。
        self.argv_bodies: list[list[str] | None] = []
        # guest 里被 `mkdir -p` 建出来的工作dir 相对路径。写文件**不会**报
        # 「这是目录」——它只 O_CREATE（guestd/internal/guest/file_ops.go:43），
        # 撞上目录时 guest 侧是 EISDIR ⇒ 500 file op io_error。这里把这层约束
        # 建模出来，否则「中转路径被预建成目录」这类缺陷在单测里完全隐形。
        self.dirs: set[str] = set()
        self.root = root
        # 非 None 时 pwd 探测失败（模拟 workdir 只读/不存在）。
        self.root_error = root_error

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body: dict = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}
            self.bodies.append(body)

        # 显式路由优先（想让某条命令返回特定应答的用例仍能覆盖默认分派）。
        for (method, prefix), response in self.routes.items():
            if request.method == method and request.url.path.startswith(prefix):
                return response
        if request.method == "POST" and request.url.path.endswith("/exec"):
            return self._exec(body)
        if request.method == "PUT" and "/files/" in request.url.path:
            rel = request.url.path.split("/files/", 1)[1]
            if rel in self.dirs:
                return httpx.Response(
                    500, json={"error": "internal_error", "message": "file op io_error"}
                )
            return httpx.Response(201, json={"path": rel, "size": 0})
        return httpx.Response(404, json={"error": "not_found"})

    def _exec(self, body: dict) -> httpx.Response:
        argv = body.get("argv")
        self.argv_bodies.append(argv)
        # argv 形式的内层命令 = 最后一项（`["bash","-c",command]`）。
        command = body.get("command") or (argv[-1] if argv else "")
        self.commands.append(command)
        # 命令是一条 `&&` 链：`mkdir -p` 可能出现在中间（旧的 `_stage_out` 是
        # `rm -rf … && mkdir -p …`），所以逐段找。参数里带中转目录的才是「
        # guest 里已存在的目录」—— `_stage_out` 给绝对路径（带 root 前缀）、
        # `_mkdirs` 给 workdir 相对路径，统一截到中转目录起点才能和文件 API 对上。
        for segment in command.split("&&"):
            segment = segment.strip()
            if not segment.startswith("mkdir -p "):
                continue
            for token in segment[len("mkdir -p "):].split():
                if STAGING_DIR in token:
                    self.dirs.add(token[token.index(STAGING_DIR):])
        if "pwd" in command and self.root_error is not None:
            return httpx.Response(200, json={
                "exec_id": "e", "exit_code": 1, "stdout": "", "stderr": self.root_error,
                "duration_ms": 1, "timed_out": False, "truncated": False,
            })
        if "pwd" in command:
            return exec_ok(self.root + "\n")
        return exec_ok()

    def paths(self, method: str) -> list[str]:
        return [r.url.path for r in self.requests if r.method == method]

    def file_puts(self) -> list[str]:
        return [p for p in self.paths("PUT") if "/files/" in p]

    def cp_commands(self) -> list[str]:
        return [c for c in self.commands if "cp -a" in c]


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

    assert caps.stream is False                         # 无 SSH / TCP 转发（Harbor 0.26）
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


def test_preflight_requires_credentials(monkeypatch):
    """Harbor 0.26：preflight(kwargs=None) 先走基类，再拦缺失的凭证。"""
    monkeypatch.delenv("RAYRUN_API_URL", raising=False)
    monkeypatch.delenv("RAYRUN_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc:
        RayrunEnvironment.preflight(None)

    message = str(exc.value)
    assert "RAYRUN_API_URL" in message
    assert "RAYRUN_TOKEN" in message
    assert "api.rayrunos.com/v1" in message


def test_preflight_accepts_kwargs_when_credentials_present(monkeypatch):
    monkeypatch.setenv("RAYRUN_API_URL", BASE)
    monkeypatch.setenv("RAYRUN_TOKEN", "rayrun_test_secret")

    RayrunEnvironment.preflight({})


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
    """镜像缺失要给出托管平台 builder 和自托管 --root-writable 命令。"""
    rec = Recorder({("GET", "/v1/agents"): httpx.Response(200, json={"agents": []})})
    env = make_env(tmp_path, monkeypatch, rec, network_policy=allowlist(["pypi.org"]))

    with pytest.raises(ValueError) as exc:
        await env.start(force_build=False)

    message = str(exc.value)
    assert "build-task-image.sh" in message
    assert "--name hello-world" in message
    assert "--root-writable" in message
    assert "POST /v1/builds" in message
    assert "rsync" not in message
    assert "--writable-dir" not in message


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


@pytest.mark.asyncio
async def test_exec_wraps_command_in_bash_like_docker_provider(tmp_path, monkeypatch):
    """shell 语义对齐 docker provider（`["bash","-c",command]`）。

    Rayrun exec 的 `command` 形式经 `/bin/sh`，而 Debian/Ubuntu 的 `/bin/sh` 是
    **dash** —— installed agent 的 `set -euo pipefail` 会在 dash 上直接语法级失败，
    agent 连安装都过不去。这条把「走 argv + bash 包壳」钉住。
    """
    rec = Recorder()
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    await env.exec("set -euo pipefail; echo hi")

    assert rec.argv_bodies[-1] == ["bash", "-c", "set -euo pipefail; echo hi"]
    # 不能退化成 `command` 形式：那正是被 dash 拒掉的那条路。
    assert "command" not in rec.bodies[-1]


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
    """写文件不建父目录（guestd 只 O_CREATE）⇒ 先一次性 mkdir -p 再逐个写。

    注意落点：写入走**中转目录下的相对路径**（文件 API 只认 workdir 相对），
    最后再一次性 `cp -a` 把内容并进真正的目标。
    """
    rec = Recorder({("PUT", "/v1/sandboxes/sb-1/files"): httpx.Response(201, json={"path": "x", "size": 1})})
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    src = tmp_path / "env"
    (src / "tests").mkdir(parents=True)
    (src / "Dockerfile").write_text("FROM x\n")
    (src / "tests" / "run.sh").write_text("echo hi\n")

    await env.upload_dir(src, "/tests")

    # `_stage_out` 建的是中转**根**目录（绝对路径），`_mkdirs` 建的是这批文件
    # 需要的子目录（workdir 相对）—— 按前缀把两条分开取。
    mkdir_cmd = next(c for c in rec.commands if c.startswith(f"mkdir -p {STAGING_DIR}/"))
    # 中转根目录下一个扁平子目录，含 tests/ 一层（父目录必须先建出来）
    assert mkdir_cmd.endswith("/tests")
    staging_rel = mkdir_cmd[len("mkdir -p "):].split()[0]
    puts = rec.file_puts()
    assert puts[0].endswith(f"/files/{staging_rel}/Dockerfile")
    assert puts[1].endswith("/tests/run.sh")
    # 内容并进 /tests（`/.` 而不是把中转目录本身搬过去）
    assert any(c.startswith("mkdir -p /tests && cp -a /app/.rayrun-harbor-xfer/") for c in rec.cp_commands())


# ---------------------------------------------------------------- 路径根翻译
#
# Rayrun 的文件 API 是 **workdir 相对**的（`06` §4.5：绝对路径一律 400），
# Harbor 用的却是容器绝对路径。这几条钉住两者的翻译 —— 走错了不会报错，
# 只会把文件写到别处（或 400 掉整个 trial）。
@pytest.mark.asyncio
async def test_upload_file_stages_then_moves(tmp_path, monkeypatch):
    """绝对目标 → 先写中转目录 → `cp -a` 到绝对路径（含父目录）。"""
    rec = Recorder({("PUT", "/v1/sandboxes/sb-1/files"): httpx.Response(201, json={"path": "x", "size": 1})})
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    local = tmp_path / "test.sh"
    local.write_text("echo hi\n")
    await env.upload_file(local, "/tests/test.sh")

    assert rec.file_puts()[0].startswith("/v1/sandboxes/sb-1/files/.rayrun-harbor-xfer/")
    assert any(
        c.startswith("mkdir -p /tests && cp -a /app/.rayrun-harbor-xfer/") and c.endswith(" /tests/test.sh")
        for c in rec.cp_commands()
    ), rec.commands


@pytest.mark.asyncio
async def test_upload_file_writes_to_a_path_that_is_not_a_directory(tmp_path, monkeypatch):
    """中转路径**不能**被预建成目录 —— 那会让 upload_file 整个 500。

    guestd 的写文件是 `O_CREATE|O_TRUNC|O_WRONLY`
    （`guestd/internal/guest/file_ops.go:43`），往一个已存在的**目录**上写会
    `EISDIR` ⇒ `500 file op io_error`。而 `_stage_out` 返回的路径恰恰就是
    upload_file 落文件的地方 —— 它一旦 `mkdir -p` 过，整条通路就废了。

    Recorder 建模了这条约束（PUT 撞上已知目录就返 500），所以这条用例在旧的
    `_stage_out` 下是**真的会红**的。真机证据：`docs/plans/harbor-integration.md`
    §13.13。
    """
    rec = Recorder()  # 不给 PUT 显式路由：让 Recorder 的 EISDIR 约束生效
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    local = tmp_path / "test.sh"
    local.write_text("echo hi\n")
    await env.upload_file(local, "/tests/test.sh")

    staged = rec.file_puts()[0].split("/files/", 1)[1]
    assert staged not in rec.dirs, f"中转路径被建成了目录：{staged}"
    # 只建中转**根**目录，`mkdir -p` 的参数里不该出现这个 uuid 子目录
    assert f"mkdir -p /app/{STAGING_DIR}" in rec.commands


@pytest.mark.asyncio
async def test_download_file_stages_from_absolute(tmp_path, monkeypatch):
    """绝对源 → 先 `cp -a` 进中转目录 → 再用相对路径读 → 清理。"""
    rec = Recorder({
        ("GET", "/v1/sandboxes/sb-1/files/"): httpx.Response(200, content=b"reward logs"),
    })
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    target = tmp_path / "out" / "oracle.txt"
    await env.download_file("/logs/agent/oracle.txt", target)

    assert target.read_bytes() == b"reward logs"
    assert any(c.endswith("&& cp -a /logs/agent/oracle.txt /app/.rayrun-harbor-xfer/" + c.split("/")[-1])
               for c in rec.cp_commands()), rec.commands
    # 中转目录必须被清掉（否则每笔传输都在任务的 workdir 里留垃圾）
    assert any(c.startswith("rm -rf /app/.rayrun-harbor-xfer/") for c in rec.commands)


@pytest.mark.asyncio
async def test_download_dir_recurses_within_staged_root(tmp_path, monkeypatch):
    """列目录是单层的 ⇒ 自己走递归，但递归的**根是中转目录**，不是 /logs。

    轨迹里同名文件多，相对结构必须保住（拍平会互相覆盖）。
    """
    def tree(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/exec"):
            return exec_ok("/app\n")  # pwd 探测
        # 列目录一律打在 /files?path=<…> 上（单层语义），读文件才走 /files/<path>。
        if request.url.path.endswith("/files"):
            listed = request.url.params.get("path", "")
            assert listed.startswith(".rayrun-harbor-xfer/"), f"递归的根不是中转目录：{listed}"
            if listed.count("/") == 1:  # 中转目录本身
                return httpx.Response(200, json={"entries": [
                    {"name": "sub", "type": "dir"},
                    {"name": "top.txt", "type": "file", "size": 2},
                ]})
            return httpx.Response(200, json={"entries": [{"name": "deep.txt", "type": "file", "size": 2}]})
        if "/files/" in request.url.path:
            return httpx.Response(200, content=b"hi")
        return httpx.Response(404, json={"error": "not_found"})

    env = make_env(tmp_path, monkeypatch, Recorder())
    # 构造之后再换：make_env 自己会 monkeypatch RayrunClient（覆盖上面这条），
    # 而 client 是在 __init__ 里建的。
    env._client = RayrunClient(BASE, "rayrun_test_secret", transport=httpx.MockTransport(tree))
    env._sandbox_id = "sb-1"

    target = tmp_path / "logs"
    await env.download_dir("/logs/agent", target)

    assert (target / "top.txt").read_bytes() == b"hi"
    assert (target / "sub" / "deep.txt").read_bytes() == b"hi"


@pytest.mark.asyncio
async def test_relative_paths_pass_through_unchanged(tmp_path, monkeypatch):
    """已经是相对路径的调用不该被再套一层中转（Harbor 也会传相对路径）。"""
    rec = Recorder({("PUT", "/v1/sandboxes/sb-1/files"): httpx.Response(201, json={"path": "x", "size": 1})})
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    local = tmp_path / "f.txt"
    local.write_text("x")
    await env.upload_file(local, "rel/f.txt")

    assert any(c.endswith(" rel/f.txt") for c in rec.cp_commands()), rec.commands


@pytest.mark.asyncio
async def test_readonly_workdir_error_is_actionable(tmp_path, monkeypatch):
    """workdir 只读/不存在时，报错必须指向可照做的修法。

    镜像的 workdir 不在可写子树里时，写文件只会以 io_error/EROFS 失败，
    而那个报错指不出原因。探测放在第一次用到文件 API 时（pwd 与可写性同一趟）。
    """
    rec = Recorder(root_error="touch: cannot touch '.rayrun-writecheck': Read-only file system")
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    local = tmp_path / "f.txt"
    local.write_text("x")
    with pytest.raises(RuntimeError) as exc:
        await env.upload_file(local, "/logs/f.txt")

    message = str(exc.value)
    assert "--root-writable" in message
    assert "--writable-dir" not in message
    assert "Read-only file system" in message
    assert "POST /v1/builds" in message


@pytest.mark.asyncio
async def test_exec_without_timeout_is_not_server_truncated(tmp_path, monkeypatch):
    """没给 timeout_sec 时必须**显式**传上限，不能落进 Rayrun 的 300s 服务端缺省。

    Harbor 对 verifier 的时限是在外层用 asyncio.wait_for 兜的（docker provider 里
    docker exec 无界），它不会把预算传进 exec。用服务端缺省 = 内层 300s 先杀，
    外层 900s 的预算形同虚设 —— 症状是 verifier 跑到第 5 分钟断掉、Harbor 只报
    「reward 文件不存在」。这条钉住「外层是唯一时限权威」。
    """
    rec = Recorder({("POST", "/v1/sandboxes/sb-1/exec"): exec_ok("ok")})
    env = make_env(tmp_path, monkeypatch, rec)
    env._sandbox_id = "sb-1"

    await env.exec("long-running-thing")            # 不给 timeout_sec
    await env.exec("short", timeout_sec=30)         # 给了就照给

    assert rec.bodies[0]["timeout_seconds"] == 3600
    assert rec.bodies[1]["timeout_seconds"] == 30


@pytest.mark.asyncio
async def test_failed_download_cleans_staging(tmp_path, monkeypatch):
    """源不存在（agent 什么都没写）时也要收摊 —— 否则每跑一次 trial 就在任务
    workdir 里留一个空目录，跑一百次就有一百个。

    这条不是异常路径上的边角：Harbor 每次都会 `download_dir("/logs/agent")`。
    """
    rec = Recorder()

    def failing_exec(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        argv = body.get("argv")
        cmd = body.get("command") or (argv[-1] if argv else "")
        if "cp -a" in cmd:
            rec.commands.append(cmd)
            return httpx.Response(200, json={
                "exec_id": "e", "exit_code": 1, "stdout": "",
                "stderr": "cp: cannot stat '/logs/agent': No such file or directory",
                "duration_ms": 1, "timed_out": False, "truncated": False,
            })
        return rec._exec(body)

    # 构造之后再换 client：make_env 自己会 monkeypatch RayrunClient（覆盖上面那条）。
    env = make_env(tmp_path, monkeypatch, Recorder())
    env._client = RayrunClient(BASE, "tok", transport=httpx.MockTransport(failing_exec))
    env._sandbox_id = "sb-1"

    with pytest.raises(Exception) as exc:
        await env.download_dir("/logs/agent", tmp_path / "out")
    assert "No such file or directory" in str(exc.value)

    # 清理命令发出去了（且指向中转目录）
    assert any(c.startswith("rm -rf /app/.rayrun-harbor-xfer/") for c in rec.commands), rec.commands
