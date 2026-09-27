# rayrun/integrations/harbor-rayrun —— 构建配方（make -C integrations/harbor-rayrun <target>）
#
# 这是仓库里唯一的 Python 包（provider 侧），所以它的「构建」= 建 venv + 装依赖，
# 「测试」= pytest。**刻意不接进顶层 `make test`**：那会让整仓测试依赖网络与 PyPI
# 镜像（Go/web/site 都不依赖），代价大于收益。要跑就显式跑这一条。

VENV     ?= .venv
PYTHON   ?= python3
# 与仓库其它地方一致：国内直连 PyPI 不稳，走清华镜像（可用 PIP_INDEX 覆盖）。
PIP_INDEX ?= https://pypi.tuna.tsinghua.edu.cn/simple

.PHONY: venv test lint clean

venv: $(VENV)/.installed

$(VENV)/.installed: pyproject.toml src/harbor_rayrun/*.py
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q -i $(PIP_INDEX) -e '.[test]'
	@touch $@

test: venv ## 单测（httpx.MockTransport 打桩，不打真网络）
	$(VENV)/bin/python -m pytest tests -q

lint: venv ## 语法/导入自检（没有引入 ruff：不为一个包加新工具链）
	$(VENV)/bin/python -m compileall -q src tests
	$(VENV)/bin/python -c "import harbor_rayrun; print('import OK:', harbor_rayrun.RayrunEnvironment.__name__)"

clean:
	rm -rf $(VENV) .pytest_cache src/*.egg-info src/harbor_rayrun/__pycache__ tests/__pycache__
