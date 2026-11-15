# harbor-rayrun —— Rayrun 的 Harbor provider

把 **Rayrun 沙箱云**接成 [Harbor](https://docs.harborframework.com) 的评测环境。Harbor 每个
trial 的隔离环境由 provider 实现；本包装上 Rayrun 的 HTTP API，用
`-e harbor_rayrun:RayrunEnvironment` 加载，不需要改 Harbor 上游。

平台仓库是 [RayrunOS/RayrunOS](https://github.com/RayrunOS/RayrunOS)。下面的安装、凭证、
镜像和网络说明都写在本文里，按本文即可跑起来。

## 它负责什么

| Harbor 契约 | 落到 Rayrun |
|---|---|
| `start(force_build)` | 校验镜像已存在 → `POST /v1/sandboxes`（201 即 Running） |
| `stop(delete=True/False)` | `DELETE /sandboxes/{id}` / `POST …/stop`（冷停：留盘、停表、IP 不变） |
| `exec(command, cwd, env, timeout_sec, user)` | `POST /sandboxes/{id}/exec`（同步；以 `["bash","-c",…]` 执行） |
| `upload_file` / `upload_dir` | `PUT /sandboxes/{id}/files/{path}`（先 `mkdir -p`：写文件不建父目录） |
| `download_file` / `download_dir` | `GET …/files/{path}`；列目录是单层的，provider 自己递归 |
| `is_dir` / `is_file` | 用基类默认实现（走 `exec` 的 `test -d/-f`），不覆写 |

本 provider **不**构建镜像、**不**做 SSH / 流式（`stream=False`）、**不**支持 GPU、TPU、
Windows 或 docker-compose。

## 安装

Harbor 与 provider 必须在同一个 Python 环境里。本包依赖 `harbor>=0.26,<0.27`。

```bash
uv pip install harbor-rayrun
# 或从 GitHub（HTTPS）：
uv pip install "git+https://github.com/RayrunOS/harbor-rayrun.git"
# 或在本仓库根目录：
uv pip install .
```

## 凭证

API 基址必须带 `/v1`。托管环境用 `https://api.rayrunos.com/v1`。
`https://dashboard.rayrunos.com/v1` 已返回 **410**，不是 API 入口。

Token 在控制台「设置 → 我的 API Token」签发，形如 `rayrun_<token_id>_<secret>`。

```bash
# 托管
export RAYRUN_API_URL=https://api.rayrunos.com/v1
# 自托管 / 本机 daemon
# export RAYRUN_API_URL=http://<daemon 主机>:8080/v1
export RAYRUN_TOKEN=rayrun_<token_id>_<secret>
```

缺任何一个，`preflight` 会在 trial 排队前直接退出。

## 跑

```bash
harbor run -t <task> -a oracle -e harbor_rayrun:RayrunEnvironment
```

本地任务目录用 `-p <任务目录>`，registry 上的单个任务用 `-t <org/name>`。`-a oracle`
跑的是任务自带的参考解。非交互再加 `-y`。

镜像名是 `<RAYRUN_IMAGE_PREFIX><任务目录名>@<RAYRUN_IMAGE_VERSION>`。两个变量都不设时，
前缀为空、版本为 `1`，例如任务目录 `regex-log` 对应镜像 `regex-log@1`。

```bash
export RAYRUN_IMAGE_PREFIX=tb-    # 可选
export RAYRUN_IMAGE_VERSION=1     # 可选，默认 1
```

名字对不上时，`start()` 会报错并列出当前环境里已有的镜像。

## 镜像

镜像要先存在于目标 Rayrun 环境。`start()` 用 `GET /v1/agents` 核对，缺失就报错，不静默换镜像。

### 托管：平台 builder

用 `POST /v1/builds` 在平台上构建。请求返回 **202**，再轮询 `GET /v1/builds/{id}`，
直到 `status` 为 `succeeded`。成功结果里的 `image_ref`（形如 `regex-log@1`）就是
provider 要的镜像名；若版本不是 `1`，把 `RAYRUN_IMAGE_VERSION` 设成这个版本号。

平台 builder 默认把镜像标成整根可写（Harbor 的 `/logs`、`/tests`、`/solution` 需要可写）。
**不要**把本地的 `rootfs.ext4` 或镜像目录传到托管环境。

只有 Dockerfile 文本时用 JSON。`COPY` 需要构建上下文时改用 `multipart/form-data`，
并带上 `context.tar.gz`。

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $RAYRUN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"regex-log",
       "dockerfile":"FROM ghcr.io/laude-institute/terminal-bench/regex-log:2.0\nRUN true",
       "egress":{"allow":["ghcr.io"]}}' \
  "$RAYRUN_API_URL/builds"
```

`name` 只含小写字母、数字和 `.` `_` `-`，最长 64。`egress.allow` 列出构建期要访问的
域名（基础镜像的 registry、apt / pip 源等）；基础镜像的 registry 会由服务端并入白名单。
构建期出网和 trial 出网是两套策略：trial 仍按下一节的 Harbor `NetworkPolicy` 映射。

跑完可删：`DELETE /v1/images/<name>@<version>`。

### 自托管：离线 builder

自托管者使用 Rayrun 平台树里的 `images/build-task-image.sh`（仓库
[RayrunOS/RayrunOS](https://github.com/RayrunOS/RayrunOS)，在有 docker 的机器上执行）。
**`--root-writable` 必需。** 以前那三个 `--writable-dir /logs`、`--writable-dir /tests`、
`--writable-dir /solution` 已经废弃，不要再加。

```bash
# 任务只给了 docker_image
images/build-task-image.sh --image <task.toml 里的 docker_image> \
    --name <任务目录名> --version 1 --out /tmp/task-images --root-writable

# 任务带有 environment/Dockerfile
images/build-task-image.sh --dockerfile <任务>/environment/Dockerfile \
    --name <任务目录名> --version 1 --out /tmp/task-images --root-writable
```

产出是 `<out>/<name>@<version>/`。把它放到本机 daemon 的镜像目录 `RAYRUN_IMAGES_DIR`
（缺省在数据目录下的 `images/`）。这条路只用于自托管，不要把这份目录传到托管环境。

### 为什么必须整根可写

沙箱里，清单外的目录默认只读。可写面是固定的几个子树
（`/usr` `/etc` `/var` `/opt` `/root` `/home` `/srv`）加上 `/workspace`。
Harbor 的 `/logs`（reward 写在 `/logs/verifier/reward.txt`）、`/tests`、`/solution`
都在清单外。少了可写面时，verifier 写不出 reward，表面症状只是「reward 文件不存在」。

`--root-writable` 在镜像里写下标记，guestd 用 overlay 加 pivot_root 换根，让 `/` 与
docker 的语义一致。任务镜像自己的 `WORKDIR`（常见是 `/app`）由 guestd 按 manifest
的 `[run].workdir` 补挂，不用在命令行再声明。

## 网络策略

Harbor 的 `NetworkPolicy` 原样映射到 Rayrun 的 `egress`，不做静默近似。

| Harbor `network_mode` | Rayrun `egress` |
|---|---|
| `no-network` | `allow: []`（完全断网） |
| `allowlist` | `allow: [...]`（域名精确匹配，外加 IPv4 CIDR；不做通配，不收裸 IP） |
| `public`（Harbor 的默认） | `allow_all: true`（显式档位，会记入审计） |

`public` 映射到 `egress.allow_all=true`，所以默认任务可以出网。这台沙箱开了全出网，
在 Rayrun 的 `sandbox.created` 记录里看得到（`payload.egress.allow_all`）。

## 依赖

- 运行时：`harbor>=0.26,<0.27`（`BaseEnvironment`）与 `httpx`。安装本包时会带上。
  刻意不依赖 Rayrun 的生成式 SDK：provider 只用沙箱、exec、文件和镜像列表这几条端点。
- 开发与测试：`pytest`、`pytest-asyncio`（`[project.optional-dependencies]` 的 `test`）。

Harbor 0.26 起，自定义环境必须继承 `BaseEnvironment`；`preflight` 覆写要接受
`kwargs=None` 并调用 `super().preflight(kwargs)`。本包已按这个契约实现。
能力位按已安装的 Harbor 0.26 `EnvironmentCapabilities` 声明，其中 `stream=False`
（不提供 SSH 执行和 TCP 转发）。

## 测试

```bash
make test        # 首次会建 .venv 并安装依赖（需要网络）
```

单测用 `httpx.MockTransport` 打桩，不打真网络。覆盖的是会静默出错的判断：能力虚报、
`public` 被降级成断网、镜像缺失没有可操作提示、超时没有可判定退出码、写文件不建父目录。

## 已知范围

单测钉的是 provider 自己的判断。端到端只在 **oracle、样本任务** 上对过：参考解能跑完
并写出 reward，说明建沙箱、传文件、exec、出网和回传 `/logs` 这条链是通的。
这不是「真实 agent 已可用」，也不是「完整 terminal-bench 已通过」。
