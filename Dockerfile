FROM python:3.11-slim

WORKDIR /app

# 安装编译工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 确保 python-dotenv 已安装（如果 requirements.txt 中没有，则手动安装）
RUN pip install --no-cache-dir python-dotenv

# 复制项目代码
COPY run.py .
COPY app /app/app

# 创建持久化数据目录
RUN mkdir -p /app/data

# ---------- 环境变量（默认值，敏感项留空） ----------
ENV PIXIV_REFRESH_TOKEN=""
ENV IMAGE_SOURCE_DIR="/app/data/images"
ENV PIXIV_MODE="auto"
ENV PIXIV_PROXY=""
ENV THUMBNAIL_SIZE="400"
ENV THUMBNAIL_DIR="thumbnails"
ENV METADATA_DIR="metadata"
ENV PA_PORT="6814"
ENV PA_HOST="0.0.0.0"
ENV PA_ACCESS_TOKEN=""
ENV PYTHONUNBUFFERED=1

# 创建 entrypoint.sh（含软链接 + 加载 .env 覆盖环境变量）
RUN echo '#!/bin/bash\n\
set -e\n\
cd /app\n\
\n\
# 迁移旧数据（如果存在）\n\
if [ ! -f /app/data/archive.db ] && [ -f /app/archive.db ]; then\n\
    echo "Migrating existing data to /app/data..."\n\
    cp -a /app/archive.db /app/.env /app/thumbnails /app/metadata /app/data/ 2>/dev/null || true\n\
fi\n\
\n\
# 确保目标目录存在\n\
mkdir -p /app/data/thumbnails /app/data/metadata\n\
\n\
# 创建软链接\n\
ln -sf /app/data/archive.db /app/archive.db\n\
ln -sf /app/data/.env /app/.env 2>/dev/null || true\n\
ln -sf /app/data/thumbnails /app/thumbnails\n\
ln -sf /app/data/metadata /app/metadata\n\
\n\
# 创建加载环境变量的 Python 脚本（覆盖环境变量）\n\
cat > /app/load_env.py << "EOF"\n\
import os\n\
from dotenv import load_dotenv\n\
# 加载 .env 文件，覆盖已有环境变量\n\
load_dotenv("/app/.env", override=True)\n\
# 用新环境变量启动主程序\n\
os.execvp("python", ["python", "-u", "run.py", "--lan"])\n\
EOF\n\
\n\
# 执行该脚本（它会加载 .env 并启动 run.py）\n\
exec python /app/load_env.py\n\
' > /entrypoint.sh && chmod +x /entrypoint.sh

EXPOSE 6814

ENTRYPOINT ["/entrypoint.sh"]
