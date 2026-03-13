FROM python:3.11-slim

WORKDIR /app

# 安装必要的系统依赖（包括 OpenCV 所需的库）
RUN apt-get update && apt-get install -y \
    cron \
    dos2unix \
    libgl1 \
    libglib2.0-0 \
    libxcb1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY euser_renew.py .
COPY entrypoint.sh .

# 解决 Windows 换行符问题并设置执行权限
RUN dos2unix /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# 设置时区为中国
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 入口脚本
ENTRYPOINT ["/app/entrypoint.sh"]
