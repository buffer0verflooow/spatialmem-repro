.PHONY: help venv install install-kb run test test-unit test-int lint fmt bench bench-cost demo clean

PY := .venv/bin/python
PIP := .venv/bin/pip

help:
	@echo "make install      创建 venv 并安装基础依赖"
	@echo "make install-kb   额外安装 RAG 依赖（chromadb + sentence-transformers，较重）"
	@echo "make run          启动开发服务（mock 后端，无需 API key / Redis / MySQL）"
	@echo "make test         跑全部测试"
	@echo "make demo         起服务 + 假眼镜推流，验证端到端闭环"
	@echo "make bench        管线延迟压测（20 台并发）"
	@echo "make bench-cost   成本推算（先看脚本里的用法说明）"
	@echo "make lint / fmt   ruff 检查 / 自动修复"

venv:
	python3.11 -m venv .venv

install: venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"
	@echo "完成。cp .env.example .env 然后 make run"

install-kb:
	$(PIP) install -e ".[kb]"

run:
	.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	$(PY) -m pytest -q

test-unit:
	$(PY) -m pytest tests/unit -q

test-int:
	$(PY) -m pytest tests/integration -q

lint:
	.venv/bin/ruff check app tests scripts

fmt:
	.venv/bin/ruff check --fix app tests scripts
	.venv/bin/ruff format app tests scripts

bench:
	$(PY) scripts/bench_latency.py --devices 20 --frames-per-device 50

bench-mock-realistic:
	MOCK_LATENCY_MS=1200 $(PY) scripts/bench_latency.py --devices 20 --frames-per-device 10

bench-cost:
	$(PY) scripts/bench_cost.py --help

demo:
	@echo "另开一个终端跑 make run，然后："
	$(PY) scripts/fake_glasses.py --frames 12 --fps 2 --scene-change-every 3

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ *.egg-info
