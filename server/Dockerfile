# linksee-server — 智能眼镜后端服务
#
# 构建时用 --build-arg EXTRAS 控制可选依赖：
#   docker build . -t linksee-server                         # 仅基础依赖（无 MySQL/Chroma/Face）
#   docker build --build-arg EXTRAS=db . -t linksee-server   # + MySQL 持久化
#   docker build --build-arg EXTRAS=db,kb,face .             # 全部可选依赖
#
# docker-compose 默认装 db,kb，覆盖用 COMPOSE_EXTRAS 环境变量。

FROM python:3.11-slim

ARG EXTRAS=db,kb
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Pillow / OpenCV 的系统库
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo libz1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先创建最小的包结构，让 pip install -e 能解析 setuptools 的包发现
RUN mkdir -p app && touch app/__init__.py

# 先拷依赖声明，利用 Docker 层缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# 仅当 EXTRAS 非空时才安装可选依赖
RUN if [ -n "${EXTRAS}" ]; then \
      pip install --no-cache-dir -e ".[${EXTRAS}]"; \
    else \
      pip install --no-cache-dir -e .; \
    fi

# 复制应用代码（覆盖上面创建的占位 app/__init__.py）
COPY app/ ./app/
COPY scripts/ ./scripts/

# 创建数据目录并设置权限
RUN groupadd -r linksee && useradd -r -g linksee linksee \
    && mkdir -p /app/data/kb && chown -R linksee:linksee /app
USER linksee

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python3 -c "import httpx; httpx.get('http://localhost:8000/healthz').raise_for_status()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
