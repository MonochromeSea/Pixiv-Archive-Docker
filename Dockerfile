FROM python:3.11-slim

WORKDIR /app

# 安装必要的编译工具（某些依赖可能需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目核心文件
COPY run.py .
COPY app /app/app

# 创建数据目录（用于持久化 archive.db 和缩略图等）
RUN mkdir -p /app/data

# 声明容器运行时监听的端口（项目默认 6814）
EXPOSE 6814

# 设置默认环境变量
ENV PA_HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1

# 使用 -u 参数确保日志实时输出，--lan 开启局域网访问令牌保护
CMD ["python", "-u", "run.py", "--lan"]
