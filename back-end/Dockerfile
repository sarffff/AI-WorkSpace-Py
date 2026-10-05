# 后端容器镜像。文档§5 的部署行写的是 FastAPI + Docker + K8s，这一层是它的第一步。
#
# 3.11 而不是更新的版本：faiss-cpu 与 pdfplumber 的轮子在哪个 Python 上有货，
# 决定这个镜像能不能在别人的机器上装起来。仓库其他地方要求 3.11+，取最低那档最稳。
FROM python:3.11-slim

# 编译型依赖的兜底：faiss-cpu / pdfplumber 走轮子，但 pip 解析失败时会退回源码编译，
# 那时候没有 gcc 只会得到一个看不懂的报错。只要 40MB，值。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：改代码不该重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 不用 root 跑。这个进程会读用户机器上的目录（本机文件工具）并对外发 HTTP，
# 容器逃逸的半径值得用一行 useradd 换掉。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown appuser:appuser /data
USER appuser

# 检查点必须落在卷上：默认值指向 back-end/ 目录（开发期直接跑），容器里那个路径
# 随镜像一起没了，挂起的工单会在下一次部署时凭空消失。/data 是唯一该用的位置。
#
# 价目表同理：model_prices.json 被 .dockerignore 排除（它是本地配置、也进了
# .gitignore），没有它 pricing 命中不了，单工单成本上限就只能退化成"按调用次数
# 拦"。要那道界生效就把它挂进 /app/model_prices.json。
ENV TICKET_CHECKPOINT_DB=/data/ticket_checkpoints.db \
    VECTOR_STORE=qdrant \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

EXPOSE 8000

# 健康检查打在已有的 /health 上。多 worker 部署时注意两条启动告警：
# 向量库不能还是 memory，工单检查点也不能留在本地文件（见 main.py 的启动自检）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 迁移**不在启动时跑**：副本数大于 1 时每个容器都 ALTER 一次同一条链，
# MySQL 的 DDL 非事务性会让中途失败的库停在半升级状态（见 migration_chain 测试记的
# 那次事故）。正确姿势是一次性作业：
#     docker compose run --rm api python -m alembic upgrade head
# 开发期那条路径（run.js 先 check_schema_drift 再 upgrade）保持不变。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
