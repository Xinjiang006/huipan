#!/bin/bash
# 慧盘 API 启动脚本
# 用法：./start_api.sh

cd /app
uvicorn api.router:app \
  --host 0.0.0.0 \
  --port 8000 \
  --reload \
  --log-level info
