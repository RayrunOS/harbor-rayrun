"""harbor-rayrun —— 把 Rayrun 沙箱云接成 Harbor 的评测环境。

Harbor 的 `BaseEnvironment.type()` 允许返回任意字符串，所以本 provider 不需要
改 Harbor 上游，用 `--env` 入口加载即可：

    uv pip install harbor-rayrun
    # 或：uv pip install "git+https://github.com/RayrunOS/harbor-rayrun.git"
    export RAYRUN_API_URL=https://api.rayrunos.com/v1   # 自托管：http://<daemon>:8080/v1
    export RAYRUN_TOKEN=rayrun_<token_id>_<secret>
    harbor run -t <task> -a oracle -e harbor_rayrun:RayrunEnvironment

环境变量与 Rayrun CLI 同名。`dashboard.rayrunos.com/v1` 已返回 410，不是 API 入口。
"""

from harbor_rayrun.environment import RayrunEnvironment

__all__ = ["RayrunEnvironment"]
