"""harbor-rayrun —— 把 Rayrun 沙箱云接成 Harbor 的评测环境。

Harbor 的 `BaseEnvironment.type()` 显式允许返回任意字符串（它的注释写明是为了让
仓库之外的第三方环境不必改 `EnvironmentType` 枚举），所以本 provider **不需要**
Harbor 上游改动即可用 `--env` 入口加载：

    uv pip install ./integrations/harbor-rayrun
    export RAYRUN_API_URL=https://api.rayrunos.com/v1   # 自托管：http://<daemon>:8080/v1
    export RAYRUN_TOKEN=rayrun_<token_id>_<secret>
    harbor run -t <task> -a oracle -e harbor_rayrun:RayrunEnvironment

环境变量沿用 CLI 的同名约定（`07` §2.5），不新造一套 key 体系。
"""

from harbor_rayrun.environment import RayrunEnvironment

__all__ = ["RayrunEnvironment"]
