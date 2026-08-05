FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# MoviePilot 风格运行参数
# PUID/PGID:容器内进程用户/组 ID(0=root;改非 0 会在启动时降权运行)
# SUPERUSER:管理员账号(预留,当前仅记录,通知功能未启用)
ENV LITE_CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1 \
    PUID=0 \
    PGID=0 \
    SUPERUSER=stark

VOLUME ["/config", "/data"]
EXPOSE 8900

CMD ["python", "-m", "src.main", "--config", "/config/config.yaml"]
