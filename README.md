[English](README.md) | [中文](README.zh-CN.md)

# harbor-rayrun

## What Rayrun is

[Rayrun](https://rayrunos.com/docs/) is a sandbox cloud for agent workloads. Each sandbox is its own lightweight virtual machine (the docs describe it as a Firecracker microVM). It is not a shared machine that stays up between runs.

This repository is `harbor-rayrun`. It lets [Harbor](https://docs.harborframework.com) use those sandboxes. A Harbor trial is one run of one task. This provider talks to Rayrun over HTTP and starts a new sandbox for that trial.

Load it with `-e harbor_rayrun:RayrunEnvironment`.

This is a custom Harbor sandbox, not one of the sandboxes shipped inside Harbor. It does not appear in `harbor environment list`. Harbor loads it by import path (`module:Class`), the same way it loads any other sandbox that lives outside the Harbor tree. See Harbor's [custom sandboxes](https://docs.harborframework.com/sandboxes/custom-sandboxes) and Rayrun's [Harbor guide](https://rayrunos.com/docs/integrations/harbor/).

## What you can do with it

For each trial, this provider can:

- start a sandbox, and stop or delete it when the trial ends
- run commands inside the sandbox
- upload files into the sandbox and download files back out
- apply the task's Harbor network policy: no network, an allowlist, or the public internet

It does not:

- build the task image inside Harbor. `--force-build` does not build a Rayrun image. The image must already exist in that Rayrun environment.
- give Harbor SSH or TCP forwarding. Harbor calls that capability `stream`; this provider sets `stream=False`. Rayrun's own console and CLI can still open a terminal. Rayrun's SSH is shell and exec only: it does not forward ports. See [交互式终端与 SSH](https://rayrunos.com/docs/executable/terminal-ssh/).
- support GPU, TPU, Windows, or docker-compose. A task that ships `docker-compose.yaml` (several containers in one file) is rejected.

## Install

Harbor and this package must be in the same Python environment. The package needs Python 3.12 or newer. It depends on `harbor>=0.26,<0.27`.

```bash
uv pip install harbor-rayrun
```

From this Git repository, over HTTPS:

```bash
uv pip install "git+https://github.com/RayrunOS/harbor-rayrun.git"
```

From a checkout of this repository: `uv pip install .`

Rayrun's Harbor guide still lists `harbor>=0.23,<0.24`. Install the 0.26 line this package declares. An older Harbor cannot load it.

## Run a first trial

### Set the API address and the token

```bash
export RAYRUN_API_URL=https://api.rayrunos.com/v1
export RAYRUN_TOKEN=rayrun_<token_id>_<secret>
```

The hosted API base is `https://api.rayrunos.com/v1`. The `/v1` suffix is required. Create the token in the console under **设置 → 我的 API Token** (Settings → My API token). The plaintext is shown once, and it looks like `rayrun_<token_id>_<secret>`.

The console is [https://dashboard.rayrunos.com](https://dashboard.rayrunos.com). Do not use that host as `RAYRUN_API_URL`. `https://dashboard.rayrunos.com/v1` returns 410. It is not the API.

How these two variables work is in [安装与配置](https://rayrunos.com/docs/cli/install/). For a self-hosted Rayrun, set `RAYRUN_API_URL` to that instance's `/v1` address. The same page is the reference. This package's preflight text also shows `http://<daemon host>:8080/v1` as a self-hosted example. The install guide does not prescribe that port. In that guide, `127.0.0.1:8080` is an old CLI default, not the hosted API.

If either variable is missing, preflight exits before Harbor queues the trial.

### The image has to exist first

`harbor run` does not build the image. The image must already be in that Rayrun environment. The name is in [Image name](#image-name) below.

Hosted Rayrun: build with `POST /v1/builds`. The call returns **202**. Poll `GET /v1/builds/{id}` and read `image_ref`. Do not upload a local `rootfs.ext4` or an image directory. The public API has no upload for that file. The request is in [Build an image on hosted Rayrun](#build-an-image-on-hosted-rayrun), and the field list is on [镜像与构建](https://rayrunos.com/docs/api/images/).

Self-hosted Rayrun: the [Harbor guide](https://rayrunos.com/docs/integrations/harbor/) says to use `images/build-task-image.sh` from the Rayrun platform tree (the private RayrunOS/RayrunOS repository), on a machine that has Docker. That page does not list the script's flags. If the image is missing, this provider prints a command. Copy that command. It passes `--root-writable`. Do not add the older `--writable-dir /logs`, `--writable-dir /tests`, and `--writable-dir /solution`.

The Harbor guide still says Rayrun does not build images, and that hosted users should mail [support@rayrunos.com](mailto:support@rayrunos.com) to have an image prepared. The API reference documents building a Dockerfile yourself with `POST /v1/builds`. Follow the API reference for the request.

### Start the trial

```bash
harbor run -t <task> -a oracle -e harbor_rayrun:RayrunEnvironment
```

- `-t <task>` selects a registry task, such as `org/name`.
- `-p <task-dir>` selects a task directory on your machine. The Harbor guide uses `-p`.
- `-a oracle` runs the task's reference solution (`solution/solve.sh`). It does not call a model.
- `-y` skips the confirmation prompt.
- `-e harbor_rayrun:RayrunEnvironment` loads this provider.

A local task, with no prompt:

```bash
harbor run -p ./my-task -a oracle -y -e harbor_rayrun:RayrunEnvironment
```

In a job config, set `environment.import_path` to the same `harbor_rayrun:RayrunEnvironment` string.

### Image name

The provider looks up this name:

```text
<RAYRUN_IMAGE_PREFIX><task-dir>@<RAYRUN_IMAGE_VERSION>
```

The prefix defaults to empty. The version defaults to `1`. A task directory named `regex-log` needs an image named `regex-log@1`.

```bash
export RAYRUN_IMAGE_PREFIX=tb-   # optional
export RAYRUN_IMAGE_VERSION=1    # optional, default 1
```

`<task-dir>` is the task directory name. Harbor passes that name in as the environment name. If `GET /v1/agents` does not list the image, `start()` stops and prints the images that do exist. It does not pick a different one. The list endpoint is on [镜像与构建](https://rayrunos.com/docs/api/images/).

## Details you can skip the first time

### Build an image on hosted Rayrun

`POST /v1/builds` is asynchronous. **202** means the job was accepted and `status` is `pending` (queued or already running). The server checks the Dockerfile, prepares `FROM`, merges egress, and stores the build context before it returns that 202.

Poll the job:

```bash
curl -sS -H "Authorization: Bearer $RAYRUN_TOKEN" \
  "$RAYRUN_API_URL/builds/<id>"
```

The body has `status`, `failure_reason`, and `image_ref`. Use `image_ref` as the image name this provider looks up. If it is not `<task-dir>@1`, set `RAYRUN_IMAGE_PREFIX` and `RAYRUN_IMAGE_VERSION` to match. What each `status` means is on the API page, under 单个构建作业: [镜像与构建](https://rayrunos.com/docs/api/images/).

`name` is required. It may contain only lowercase letters, digits, and `.` `_` `-`, and it is at most 64 characters. `dockerfile` is the Dockerfile text, at most 256 KiB. `FROM` must be a public registry image. `FROM` a Rayrun image returns 400 `unsupported_base`.

Send JSON when you only have the Dockerfile text. If the Dockerfile `COPY`s files, send `multipart/form-data` and include `context.tar.gz`. In that form, `egress` and `build_args` are JSON strings.

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $RAYRUN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"regex-log","dockerfile":"FROM public.example/image:tag\nRUN true","egress":{"allow":["public.example"]}}' \
  "$RAYRUN_API_URL/builds"
```

`egress` on this call is optional. The object is documented with the sandbox API: `allow`, `deny`, `allow_all`, and `bandwidth_mbps`. See [沙箱](https://rayrunos.com/docs/api/sandboxes/). Build-time egress is not the trial's network policy. The trial still uses the table below.

`$RAYRUN_API_URL` already ends in `/v1`. The path above is `/builds`, not `/v1/builds` a second time.

To delete an image you built: `DELETE /v1/images/{ref}`. Take `ref` from `GET /v1/images`.

### Build an image for a self-hosted Rayrun

The [Harbor guide](https://rayrunos.com/docs/integrations/harbor/) names `images/build-task-image.sh` in the Rayrun platform tree and does not list its flags. The tree is the private RayrunOS/RayrunOS repository. Run the script on a machine with Docker.

When the image is missing, this provider prints a command like the one below. `--root-writable` is there because Harbor has to write `/logs`, `/tests`, and `/solution`:

```bash
# task ships environment/Dockerfile
images/build-task-image.sh --dockerfile <task>/environment/Dockerfile \
    --name <task-dir> --version 1 --out /tmp/task-images --root-writable

# task.toml names a prebuilt image (docker_image)
images/build-task-image.sh --image <docker_image from task.toml> \
    --name <task-dir> --version 1 --out /tmp/task-images --root-writable
```

Put the output where that self-hosted daemon reads images. The error text calls that directory `RAYRUN_IMAGES_DIR`. This path is only for a self-hosted daemon. Do not upload it to hosted Rayrun.

Do not pass `--writable-dir /logs`, `--writable-dir /tests`, or `--writable-dir /solution`. Those three flags are obsolete. This package does not print them.

### Why `/logs`, `/tests`, and `/solution` must be writable

Harbor writes the reward to `/logs/verifier/reward.txt`. It uploads tests to `/tests` and the reference solution to `/solution`. If those paths are not writable, the verifier cannot write the reward file. Harbor then only reports that the file is missing.

The public builds page does not document a writable-root switch on `POST /v1/builds`. This package's errors, when the image is missing or the sandbox workdir is read-only, tell you to rebuild with a writable root: hosted, with `POST /v1/builds`; self-hosted, with `images/build-task-image.sh --root-writable`.

### Network policy

Rayrun's own default is no outbound network. The [quickstart](https://rayrunos.com/docs/quickstart/) says the allow list is empty unless you set one. Harbor's default network mode is `public`. This provider does not leave a `public` Harbor task on that no-network default. It maps the task's policy as written. It does not quietly swap one mode for another.

| Harbor `network_mode` | `egress` this provider sends |
|---|---|
| `no-network` | `allow: []` (no outbound traffic) |
| `allowlist` | `allow: [...]` (the hosts the task listed) |
| `public` (Harbor's default) | `allow_all: true` |

This provider tells Harbor that an allowlist may contain exact hostnames and IPv4 CIDR ranges, and that it does not accept wildcards, bare IP addresses, or IPv6. Rayrun's full `Egress` object also has `deny` and `bandwidth_mbps`. This provider does not set those. The object, including how allow and deny interact, is on the [sandbox API](https://rayrunos.com/docs/api/sandboxes/). Creating a sandbox can also merge the image's recommended allow list. That merge is Rayrun's, not an extra step in this package.

Egress is set once, when the sandbox is created. Rayrun can replace it later with `PUT /v1/sandboxes/{id}/egress`. This provider does not call that endpoint (`dynamic_network_policy` is false).

## What has been checked

Unit tests mock the HTTP client (`httpx.MockTransport`). They do not call a live Rayrun. They cover the failures that would otherwise pass silently: a capability this provider does not have, `public` turned into no network, a missing image with no command you can copy, a timeout with no exit code Harbor can read, and a file write that does not create the parent directory.

```bash
make test
```

The first run creates a `.venv` and installs dependencies. It needs a network path to a package index.

End-to-end runs so far are oracle on sample tasks only. That is not a real-agent result, and it is not a full terminal-bench pass. Rayrun's Harbor guide describes an oracle reward of 1.0 as the check that a reference solution finished. This repository does not report that score as a result.
