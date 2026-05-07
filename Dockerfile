# 慧盘 · Scheduler 容器（优化体积）
FROM python:3.11-slim

# 阿里云镜像
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

# 时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 装编译依赖 → pip install → 删编译工具（一层完成，减小体积）
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libxml2-dev libxslt1-dev \
    && pip install --no-cache-dir -r requirements.txt \
       -i https://mirrors.aliyun.com/pypi/simple/ \
       --trusted-host mirrors.aliyun.com \
    && apt-get purge -y gcc g++ \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache

# 只复制代码
COPY collector/ collector/
COPY scheduler/ scheduler/
COPY storage/ storage/
COPY config/ config/
COPY compute/ compute/
COPY data_io/ data_io/
COPY utils/ utils/
COPY sources/ sources/
COPY tools/ tools/
COPY run_scheduler.py .
COPY config.py .

# 数据目录（运行时挂载）
RUN mkdir -p /app/static/data /app/data /app/logs

CMD ["python", "run_scheduler.py"]
