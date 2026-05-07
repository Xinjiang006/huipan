#!/bin/bash
# 慧盘 · 打包部署脚本
# 用法: bash pack_deploy.sh
# 输出: huipan-deploy.tar.gz（拷到新机器解压即用）

set -e
echo "═══════════════════════════════════════"
echo "慧盘 · 打包部署"
echo "═══════════════════════════════════════"

DEPLOY_DIR="/tmp/huipan-deploy"
rm -rf "$DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"

# 1. 导出Docker镜像
echo "📦 导出Docker镜像..."
docker save huipan-scheduler nginx:alpine | gzip > "$DEPLOY_DIR/images.tar.gz"
echo "   ✅ images.tar.gz"

# 2. 生成部署用的docker-compose.yml（用image替代build）
echo "📋 生成配置文件..."
cat > "$DEPLOY_DIR/docker-compose.yml" << 'DCEOF'
# 慧盘 · Docker Compose（部署版）
services:
  scheduler:
    image: huipan-scheduler
    container_name: huipan-scheduler
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - ./static/data:/app/static/data
      - ./data:/app/data
      - ./config:/app/config
      - ./logs:/app/logs

  web:
    image: nginx:alpine
    container_name: huipan-web
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - ./static:/usr/share/nginx/html/static
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      - scheduler
DCEOF
cp ~/huipan/nginx.conf "$DEPLOY_DIR/"

# 3. 配置目录
mkdir -p "$DEPLOY_DIR/config"
cp ~/huipan/config/*.json "$DEPLOY_DIR/config/"
echo "   ✅ config/"

# 4. 创建空数据目录（新机器首次运行会自动填充）
mkdir -p "$DEPLOY_DIR/static/data"
mkdir -p "$DEPLOY_DIR/data"
mkdir -p "$DEPLOY_DIR/logs"

# 5. 复制前端文件
cp ~/huipan/static/index.html "$DEPLOY_DIR/static/"
echo "   ✅ static/index.html"

# 6. 可选：复制现有JSON数据（新机器立即有数据展示）
if [ -f ~/huipan/static/data/ashare_overview.json ]; then
    cp ~/huipan/static/data/*.json "$DEPLOY_DIR/static/data/" 2>/dev/null || true
    echo "   ✅ static/data/*.json (现有数据)"
fi

# 6. 启动脚本
cat > "$DEPLOY_DIR/start.sh" << 'EOF'
#!/bin/bash
# 慧盘 · 新机器启动脚本
set -e
echo "═══════════════════════════════════════"
echo "慧盘 · 部署启动"
echo "═══════════════════════════════════════"

# 加载Docker镜像
echo "📦 加载Docker镜像..."
docker load < images.tar.gz
echo "   ✅ 镜像加载完成"

# 启动
echo "🚀 启动服务..."
docker compose up -d

echo ""
echo "═══════════════════════════════════════"
echo "✅ 部署完成！"
echo "   前端: http://localhost:8080"
echo "   日志: docker compose logs -f"
echo "   手动采集: docker compose run scheduler python run_scheduler.py --run all"
echo "═══════════════════════════════════════"
EOF
chmod +x "$DEPLOY_DIR/start.sh"

# 7. 打包
echo "📦 打包..."
cd /tmp
tar czf ~/huipan-deploy.tar.gz -C /tmp huipan-deploy/
rm -rf "$DEPLOY_DIR"

SIZE=$(du -h ~/huipan-deploy.tar.gz | cut -f1)
echo ""
echo "═══════════════════════════════════════"
echo "✅ 打包完成: ~/huipan-deploy.tar.gz ($SIZE)"
echo ""
echo "部署到新机器："
echo "  scp ~/huipan-deploy.tar.gz user@新机器:~/"
echo "  # 在新机器上："
echo "  tar xzf huipan-deploy.tar.gz"
echo "  cd huipan-deploy"
echo "  bash start.sh"
echo "═══════════════════════════════════════"
