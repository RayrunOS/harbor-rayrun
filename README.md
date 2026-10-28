# harbor-rayrun —— Rayrun 的 Harbor provider

把 **Rayrun 沙箱云**接成 [Harbor](https://docs.harborframework.com) 的评测环境：Harbor 每个
trial 的隔离环境由 provider 实现，本包把它落到 Rayrun 的 HTTP API 上。

设计真相源：`docs/plans/harbor-integration.md`（M3）；外部契约取证见 `docs/15` §2.1。

## 它负责什么

| Harbor 契约 | 落到 Rayrun |
|---|---|
| `start(force_build)` | 校验镜像在宿主机上 → `POST /v1/sandboxes`（201 即 Running） |
| `stop(delete=True/False)` | `DELETE /sandboxes/{id}` / `POST …/stop`（**ADR-005 冷停**：留盘、停表、IP 不变） |
| `exec(command, cwd, env, timeout_sec, user)` | `POST /sandboxes/{id}/exec`（**同步**；M1 新落的原语，ADR-007） |
| `upload_file` / `upload_dir` | `PUT /sandboxes/{id}/files/{path}`（先 `mkdir -p` —— guestd 写文件不建父目录） |
| `download_file` / `download_dir` | `GET …/files/{path}`；列目录是**单层**的，provider 自己走递归 |
| `is_dir` / `is_file` | 用基类默认实现（走 `exec` 的 `test -d/-f`），不覆写 |

**它不负责什么**：不构建镜像（Rayrun 不做镜像构建，见下）、不做 SSH/流式（`stream` 档位未开）、
不支持 GPU / TPU / Windows / docker-compose。

## 用法

```bash
# 1. 装（Harbor 与 provider 在同一个 Python 环境里）
uv pip install ./integrations/harbor-rayrun

# 2. 凭证（沿用 CLI 的 env 名，docs/07 §2.5）
# 托管环境（2026-10-28 起）：https://api.rayrunos.com/v1
# 自托管 / dev：           http://<daemon 主机>:8080/v1
# ⚠️ dashboard.rayrunos.com/v1 已硬性 410，不是 API 入口
export RAYRUN_API_URL=https://api.rayrunos.com/v1
export RAYRUN_TOKEN=rayrun_<token_id>_<secret>

# 3. 跑（provider 用 --env 的 module:Class 入口，不需要改 Harbor 上游）
harbor run -t <task> -a oracle -e harbor_rayrun:RayrunEnvironment
```

镜像名默认取 `<environment_name>@1`，可用两个环境变量覆盖：
`RAYRUN_IMAGE_PREFIX`（前缀，例如 `tb-`）与 `RAYRUN_IMAGE_VERSION`。

## 两条必须先知道的前提

**1. 镜像要先在 Rayrun 宿主机上备好。** Rayrun 的镜像是**离线 builder** 的产物
（`images/build-task-image.sh`，见 `docs/plans/harbor-integration.md` §10）—— 容器内构建
Dockerfile 是明确不做的（RCE 面）。镜像缺失时 `start()` 会直接报错并附一条**可直接复制**的
构建命令，不猜、不静默。

**构建时必须带上三个 `--writable-dir`**：

```bash
images/build-task-image.sh --dockerfile <任务的 Dockerfile> \
    --name <name> --version 1 --out /tmp/task-images \
    --writable-dir /logs --writable-dir /tests --writable-dir /solution
```

这不是可选优化。沙箱里**清单外的目录一律只读**（ADR-006：可写面是固定 7 个子树
`/usr /etc /var /opt /root /home /srv` + `/workspace` 盘），而 Harbor 的
`/logs`（reward 写在这里）`/tests` `/solution` 全在清单外 ⇒ 少了它们，verifier 连
`/logs/verifier/reward.txt` 都写不出来，症状只是「reward 文件不存在」。同一原因，
任务镜像自己的 `WORKDIR`（典型 `/app`）也要可写 —— 那一条由 guestd 按 manifest 的
`[run].workdir` **自动**补挂，不需要在命令行重复声明。见 `docs/plans/harbor-integration.md`
§13.3 与 ADR-006 修订。

**2. 网络策略的映射是精确的**（2026-09-27 起）：

| Harbor `NetworkPolicy` | Rayrun `egress` |
|---|---|
| `no-network` | `allow: []`（完全断网） |
| `allowlist` | `allow: [...]`（域名精确匹配 + IPv4 CIDR） |
| `public`（**Harbor 的默认**） | `allow_all: true`（显式档位，Rayrun 侧会进审计） |

三者都**精确执行**、不做静默近似。`public` 那一档曾经只能被拒绝（Rayrun 当时没有「允许
全部」），为此产品侧补了 `egress.allow_all`（`06` §4.3）—— 于是默认任务也能跑，而「这台
沙箱开了全出网」在 Rayrun 的 journal（`sandbox.created.payload.egress.allow_all`）里看得见。

## 依赖

- 运行时：`harbor>=0.23,<0.24`（提供 `BaseEnvironment` 抽象面）+ `httpx`（打 Rayrun API）。
  **刻意不引 Rayrun 的生成式 SDK** —— provider 只用 8 个端点，薄客户端足够；SDK 生成是独立
  立项（`docs/15` §10-Q7），将来若落地，换掉 `client.py` 即可、provider 契约不变。
- 开发/测试：`pytest` + `pytest-asyncio`（见 `[project.optional-dependencies]`）。

## 测试

```bash
make test        # 首次会建 .venv 并从 PyPI 镜像装依赖（需要网络）
```

单测用 `httpx.MockTransport` 打桩，覆盖的都是**会静默出错**的地方：能力虚报、PUBLIC 被悄悄
降级、镜像缺失没有可操作提示、超时没有可判定退出码、写文件不建父目录。基线用**真**
`BaseEnvironment`（不是 stub），所以能力声明的字段名是**对着装好的 harbor 版本**核过的 ——
`main` 分支与 0.23.0 的 `EnvironmentCapabilities` 字段并不相同（0.23.0 没有 `stream`），
pydantic 会静默忽略未知字段，写错不会报错。

## 与真机验收的分工

单测钉 provider 的判断；**真沙箱链路**（建沙箱 → exec → 文件 → 销毁）归
`docs/plans/harbor-integration.md` 的 **M4**（Harbor 端到端 reward 比对）。
