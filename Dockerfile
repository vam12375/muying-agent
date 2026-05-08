# 多阶段构建：第一阶段装依赖，第二阶段做运行时镜像，最终镜像不带编译工具链
# ===== Stage 1: builder =====
FROM python:3.11-slim AS builder

# 关闭 pyc 与 stdout 缓冲，让日志立即可见
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 仅复制依赖描述先装库，让上层代码改动不破坏 pip 层缓存
COPY requirements.txt .
RUN pip install --user -r requirements.txt


# ===== Stage 2: runtime =====
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/agent/.local/bin:${PATH}"

# 创建非 root 用户：容器逃逸时降低提权风险
RUN groupadd --system agent \
    && useradd --system --gid agent --create-home --shell /usr/sbin/nologin agent

WORKDIR /app

# 把 builder 阶段装好的依赖原样搬过来；只搬 .local，不带编译工具链
COPY --from=builder --chown=agent:agent /root/.local /home/agent/.local

# 业务代码：明确只复制 app/，不带测试与 venv
COPY --chown=agent:agent app/ ./app/

USER agent

EXPOSE 8001

# K8s 探针建议在 manifest 里配 /livez 与 /readyz；
# Docker 单机也用 /livez 做最小存活检查（不依赖 muying-mall）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/livez', timeout=3).status==200 else 1)" || exit 1

# 单 worker 适合本地 / 小流量；生产请通过 --workers 或 gunicorn+UvicornWorker 扩容
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
