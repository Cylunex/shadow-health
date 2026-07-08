# shadow-health 生产镜像（设计文档 §7.3）
# 注意：static/app.css 在开发机构建后随仓库分发（tools/tailwindcss.exe 不进镜像）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /srv/app

COPY pyproject.toml uv.lock ./
RUN pip install uv && uv export --no-dev --no-hashes -o requirements.txt \
    && pip install -r requirements.txt && pip uninstall -y uv

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY migrations ./migrations
COPY alembic.ini ./

EXPOSE 8000

# 先跑迁移再起服务（启动内含 DB 重试循环）
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
