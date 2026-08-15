.PHONY: help venv install run test lint fmt clean

PY := .venv/bin/python
PIP := .venv/bin/pip

help:
	@echo "make install  创建 venv 并安装依赖"
	@echo "make run      启动服务（mock 后端，无需 API key）"
	@echo "make test     跑全部测试"
	@echo "make lint/fmt ruff 检查 / 自动修复"

venv:
	python3.11 -m venv .venv

install: venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"
	@echo "完成。cp .env.example .env 然后 make run"

run:
	.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

test:
	.venv/bin/python -m pytest tests -q

lint:
	.venv/bin/ruff check app tests

fmt:
	.venv/bin/ruff format app tests && .venv/bin/ruff check --fix app tests

clean:
	rm -rf .venv .pytest_cache .ruff_cache
