[English](README.md) | [中文](README.zh-CN.md)

# harbor-rayrun

## Rayrun 是什么

[Rayrun](https://rayrunos.com/docs/) 是面向 Agent 的沙箱云。每个沙箱是一台单独的轻量虚拟机。它不是一台大家共用、一直开着的机器。

这个仓库是 `harbor-rayrun`。它让 [Harbor](https://docs.harborframework.com) 能用这些沙箱。Harbor 里的一次 trial，就是一个任务跑一遍。本 provider 通过 HTTP 调用 Rayrun，为这一次 trial 单独开一台沙箱。

加载方式：`-e harbor_rayrun:RayrunEnvironment`。

这是 Harbor 的自定义沙箱，不是 Harbor 自带的那一批。`harbor environment list` 里看不到它。Harbor 用导入路径（`module:Class`）加载它，和加载任何树外沙箱一样。见 Harbor 的 [自定义沙箱](https://docs.harborframework.com/sandboxes/custom-sandboxes)，以及 Rayrun 的 [Harbor 指南](https://rayrunos.com/docs/integrations/harbor/)。

## 你可以用它做什么

对每一次 trial，这个 provider 可以：

- 开一台沙箱，trial 结束时停掉或删掉
- 在沙箱里执行命令
- 把文件传进沙箱，再把文件取回来
- 按任务的 Harbor 网络策略来：完全断网、白名单，或允许公网

它不做这些事：

- 不在 Harbor 里面构建任务镜像。`--force-build` 不会去构建 Rayrun 镜像。镜像必须事先就在那套 Rayrun 环境里。
- 不向 Harbor 提供 SSH，也不做 TCP 转发。Harbor 把这项能力叫 `stream`，这里设为 `stream=False`。Rayrun 自己的控制台和 CLI 仍然可以开终端。Rayrun 的 SSH 只做 shell 和 exec，不做端口转发。见 [交互式终端与 SSH](https://rayrunos.com/docs/executable/terminal-ssh/)。
- 不支持 GPU、TPU、Windows，也不支持 docker-compose。任务如果带了 `docker-compose.yaml`（一个文件里描述多台容器），会被拒绝。

## 安装

Harbor 和这个包装在同一个 Python 环境里。需要 Python 3.12 或更新版本。依赖是 `harbor>=0.26,<0.27`。

```bash
uv pip install harbor-rayrun
```

从本 Git 仓库用 HTTPS 安装：

```bash
uv pip install "git+https://github.com/RayrunOS/harbor-rayrun.git"
```

已经克隆了本仓库的话，在仓库根目录执行 `uv pip install .`。

Rayrun 的 Harbor 指南仍写着 `harbor>=0.23,<0.24`。请按本包声明的 0.26 系列来装。更旧的 Harbor 加载不了这个包。

## 第一次跑起来

### 设置 API 地址和 Token

```bash
export RAYRUN_API_URL=https://api.rayrunos.com/v1
export RAYRUN_TOKEN=rayrun_<token_id>_<secret>
```

托管环境的 API 基址是 `https://api.rayrunos.com/v1`，末尾的 `/v1` 不能省。Token 在控制台 **设置 → 我的 API Token** 里创建。明文只显示一次，形状是 `rayrun_<token_id>_<secret>`。

控制台是 [https://dashboard.rayrunos.com](https://dashboard.rayrunos.com)。不要把这个主机名当成 `RAYRUN_API_URL`。`https://dashboard.rayrunos.com/v1` 返回 410，不是 API。

这两个变量的说明在 [安装与配置](https://rayrunos.com/docs/cli/install/)。自托管的 Rayrun，把 `RAYRUN_API_URL` 设成那套实例自己的 `/v1` 地址，仍以该页为准。本包 preflight 的报错里还有一行示例：`http://<daemon 主机>:8080/v1`。安装文档没有规定这个端口。文档里的 `127.0.0.1:8080` 是旧版 CLI 的默认值，不是托管 API。

缺了其中一个变量，preflight 会在 Harbor 把 trial 排进队列之前直接退出。

### 镜像要先放在 Rayrun 里

`harbor run` 不会构建镜像。镜像必须已经在那套 Rayrun 环境里。名字见下面的 [镜像名](#镜像名)。

托管环境：用 `POST /v1/builds` 构建。调用返回 **202**。再轮询 `GET /v1/builds/{id}`，读返回里的 `image_ref`。不要上传本地的 `rootfs.ext4`，也不要上传镜像目录。公开 API 没有这个上传入口。请求示例在 [在托管环境上构建镜像](#在托管环境上构建镜像)，字段表在 [镜像与构建](https://rayrunos.com/docs/api/images/)。

自托管：镜像仍须事先就在那套实例里，`harbor run` 不会构建它。本包不带镜像构建器。自托管的镜像准备以公开的 [Harbor 指南](https://rayrunos.com/docs/integrations/harbor/) 为准。不要把本地 `rootfs.ext4` 或镜像目录传到托管环境。

Harbor 指南仍写着 Rayrun 不做镜像构建，托管用户要发邮件到 [support@rayrunos.com](mailto:support@rayrunos.com) 请对方备好镜像。API 参考已经写了用 `POST /v1/builds` 自己提交 Dockerfile。请求以 API 参考为准。

### 启动这次评测

```bash
harbor run -t <task> -a oracle -e harbor_rayrun:RayrunEnvironment
```

- `-t <task>` 选注册表上的任务，例如 `org/name`。
- `-p <任务目录>` 选本机的任务目录。Harbor 指南用的是 `-p`。
- `-a oracle` 跑任务自带的参考解（`solution/solve.sh`）。它不调用模型。
- `-y` 跳过确认提示。
- `-e harbor_rayrun:RayrunEnvironment` 加载本 provider。

本地任务、不要提示时：

```bash
harbor run -p ./my-task -a oracle -y -e harbor_rayrun:RayrunEnvironment
```

写在 job 配置里时，把 `environment.import_path` 设成同一个字符串 `harbor_rayrun:RayrunEnvironment`。

### 镜像名

provider 查找的名字是：

```text
<RAYRUN_IMAGE_PREFIX><任务目录名>@<RAYRUN_IMAGE_VERSION>
```

前缀默认是空的，版本默认是 `1`。任务目录叫 `regex-log`，对应的镜像就是 `regex-log@1`。

```bash
export RAYRUN_IMAGE_PREFIX=tb-   # 可选
export RAYRUN_IMAGE_VERSION=1    # 可选，默认 1
```

任务目录名就是 Harbor 传进来的环境名。`GET /v1/agents` 里没有这张镜像时，`start()` 会停下来，并列出已经有的镜像，不会偷偷换一张。列表接口在 [镜像与构建](https://rayrunos.com/docs/api/images/)。

## 细节（第一次可以先跳过）

### 在托管环境上构建镜像

`POST /v1/builds` 是异步的。**202** 表示作业已受理，`status` 为 `pending`（在排队，或已经在跑）。返回 202 之前，服务端会先校验 Dockerfile、处理 `FROM`、合并 egress、把构建上下文落盘。

轮询这一次作业：

```bash
curl -sS -H "Authorization: Bearer $RAYRUN_TOKEN" \
  "$RAYRUN_API_URL/builds/<id>"
```

响应里有 `status`、`failure_reason` 和 `image_ref`。`image_ref` 就是本 provider 要找的镜像名。如果它不是 `<任务目录名>@1`，就把 `RAYRUN_IMAGE_PREFIX` 和 `RAYRUN_IMAGE_VERSION` 设成对得上的值。各个 `status` 的含义以 API 页的「单个构建作业」为准，这里不另列一份：[镜像与构建](https://rayrunos.com/docs/api/images/)。

`name` 必填。只能是小写字母、数字，以及 `.` `_` `-`，最长 64 个字符。`dockerfile` 是 Dockerfile 原文，最大 256 KiB。`FROM` 必须是公共仓库里的镜像。`FROM` 一张 Rayrun 镜像会返回 400 `unsupported_base`。

只有 Dockerfile 文本时用 JSON。Dockerfile 里有 `COPY` 时，改用 `multipart/form-data`，并带上 `context.tar.gz`。这种提交里，`egress` 和 `build_args` 是 JSON 字符串。

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $RAYRUN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"regex-log","dockerfile":"FROM public.example/image:tag\nRUN true","egress":{"allow":["public.example"]}}' \
  "$RAYRUN_API_URL/builds"
```

这次请求里的 `egress` 可以不写。这个对象的字段在沙箱 API 里：`allow`、`deny`、`allow_all`、`bandwidth_mbps`。见 [沙箱](https://rayrunos.com/docs/api/sandboxes/)。构建时的出网策略不是 trial 的网络策略。trial 仍按下文的表来映射。

`$RAYRUN_API_URL` 已经带了 `/v1`。上面的路径是 `/builds`，不要再写成 `/v1/builds`。

删掉自己构建的镜像：`DELETE /v1/images/{ref}`。`ref` 从 `GET /v1/images` 取。

### 在自托管环境上构建镜像

本包不带镜像构建器。自托管的镜像准备以公开的 [Harbor 指南](https://rayrunos.com/docs/integrations/harbor/) 为准。

不要把本地 `rootfs.ext4` 或镜像目录传到托管环境。公开 API 没有这个上传入口。托管环境上的镜像用上一节的 `POST /v1/builds` 构建。

### 为什么 `/logs`、`/tests`、`/solution` 必须可写

Harbor 把得分写在 `/logs/verifier/reward.txt`。测试在 `/tests`，参考解上传到 `/solution`。这几个路径不能写时，校验写不出得分文件，Harbor 只会报这个文件不存在。

公开的构建文档没有在 `POST /v1/builds` 上写一个「整根可写」开关。镜像缺失，或沙箱工作目录只读时，本包的报错会让你重新构建，使 `/logs`、`/tests`、`/solution` 可以写入。托管环境的这次重建走 `POST /v1/builds`。不要上传本地 `rootfs.ext4`。自托管的镜像准备以公开的 [Harbor 指南](https://rayrunos.com/docs/integrations/harbor/) 为准。

### 网络策略

Rayrun 自己的默认是不出网。[快速开始](https://rayrunos.com/docs/quickstart/) 写明：不另设的话，出向白名单是空的。Harbor 的默认网络模式是 `public`。本 provider 不会把 Harbor 的 `public` 任务留在 Rayrun 的断网默认上。它按任务写明的策略来映射，不会悄悄换成另一种。

| Harbor `network_mode` | 本 provider 发出的 `egress` |
|---|---|
| `no-network` | `allow: []`（不出网） |
| `allowlist` | `allow: [...]`（任务列出的主机） |
| `public`（Harbor 的默认） | `allow_all: true` |

本 provider 向 Harbor 声明：白名单可以是精确主机名和 IPv4 CIDR，不接受通配符、裸 IP，也不接受 IPv6。Rayrun 的完整 `Egress` 对象还有 `deny` 和 `bandwidth_mbps`。本 provider 不设置这两项。字段，以及 allow 与 deny 怎么一起生效，见 [沙箱 API](https://rayrunos.com/docs/api/sandboxes/)。创建沙箱时，Rayrun 还可能并入镜像自带的推荐白名单。那是 Rayrun 做的，不是本包多出来的一步。

出网策略只在创建沙箱时提交一次。Rayrun 之后可以用 `PUT /v1/sandboxes/{id}/egress` 整份替换。本 provider 不调用这个接口（`dynamic_network_policy` 为 false）。

## 目前核对过的范围

单元测试把 HTTP 客户端打成了桩（`httpx.MockTransport`），不会打到真的 Rayrun。覆盖的是那些会悄悄错过去的情况：声明了本 provider 并没有的能力、把 `public` 变成断网、镜像缺失时没有可照抄的命令、超时却没有 Harbor 能读的退出码、写文件时不建父目录。

```bash
make test
```

第一次运行会创建 `.venv` 并安装依赖，需要能访问包索引。

端到端目前只在 oracle 和样本任务上对过。这不是某个真实 agent 的结果，也不是完整的 terminal-bench 通过记录。Rayrun 的 Harbor 指南把 oracle 的 reward = 1.0 写成「参考解跑完」的信号。本仓库不把这个分数当作已有结果来报。
