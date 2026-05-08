# 慧盘 · Scheduler 容器
FROM debian:trixie-slim

ENV WORKDIR=/app \
    STARTUP=/start \
    TZ=Asia/Shanghai \
    UVDIR=/root/.local/bin/uv

# 阿里云镜像
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
# 时区
ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && \
apt-get autoremove -y && \
update-ca-certificates && \
git clone https://github.com/Xinjiang006/huipan.git $WORKDIR && \
mkdir -p /app/static/data /app/data /app/logs && \
# UV
curl  -LsSf https://astral.sh/uv/install.sh | sh && \
$UVDIR python install 3.14 && \
$UVDIR --directory $WORKDIR sync && \
# START script
echo '#!/bin/sh -e' >> $STARTUP && \
echo '### START HUIPAN...' >> $STARTUP && \
echo '/app/.venv/bin/python /app/run_scheduler.py' >> $STARTUP && \
echo 'wait -n' >> $STARTUP && \
echo 'exit $?' >> $STARTUP && \
chmod +x $STARTUP && rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache

ENTRYPOINT ["sh", "-c", "$STARTUP"]
