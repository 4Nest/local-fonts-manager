# fnOS 部署与维护

本项目通过两个容器提供 WebUI / MCP 和自动入库。GitHub Actions 发布 linux/amd64 与 linux/arm64 多架构镜像，NAS 无需本地编译。

| 用途 | 默认位置 |
| --- | --- |
| 项目代码 | `/vol3/docker/local-fonts-manager/app` |
| 字体库 | 在 `.env` 中设置 |
| WebUI / MCP 状态 | `/vol3/docker/local-fonts-manager/state` |
| 入库 worker 状态 | `/vol3/docker/local-fonts-manager/ingest-state` |

两个状态目录用途不同，不要混用。启动前确认没有其他入库程序指向同一字库，避免并发移动文件。

## 首次部署

1. 获取代码并准备配置：

   ```bash
   sudo mkdir -p /vol3/docker/local-fonts-manager
   cd /vol3/docker/local-fonts-manager
   sudo git clone https://github.com/4Nest/local-fonts-manager.git app
   cd app
   sudo cp deploy.env.example .env
   ```

2. 编辑 `.env`，设置 `FONT_LIBRARY_PATH`、状态路径、端口和运行账户。编辑 `legacy/ingest/config.nas.json`，设置用于状态展示的宿主机路径与 SMB 共享路径；容器内字体根保持 `/fonts`。

3. 创建状态目录并修正权限。以下示例使用默认路径及 `1000:1001`，如修改配置应同步调整：

   ```bash
   sudo mkdir -p /vol3/docker/local-fonts-manager/state
   sudo mkdir -p /vol3/docker/local-fonts-manager/ingest-state
   sudo chown -R 1000:1001 /vol3/docker/local-fonts-manager/state
   sudo chown -R 1000:1001 /vol3/docker/local-fonts-manager/ingest-state
   ```

4. 拉取并启动公开镜像：

   ```bash
   cd /vol3/docker/local-fonts-manager/app
   sudo docker compose config --quiet
   sudo docker compose pull
   sudo docker compose up -d
   sudo docker compose ps
   ```

## 验证

```bash
cd /vol3/docker/local-fonts-manager/app
sudo docker compose logs --tail=100
sudo docker compose exec -T font-ingest \
  python /app/font_ingest.py status --config /app/config.json
curl -fsS http://127.0.0.1:8765/api/status
curl -fsS http://127.0.0.1:8765/api/recognition/languages
```

worker 启动后会刷新索引并发布目录，空闲状态为“等待投递”；有待处理文件时可能显示“等待复制稳定”或处理中。核对 `updated_at` 持续更新且没有异常。

WebUI 使用 `http://NAS的IP:8765`，MCP 使用 `http://NAS的IP:8765/mcp`。直接打开网页，核对字体数、试字与入库状态；在“设置”中复制 API Token 配置 MCP 客户端。

## 更新

更新全部服务：

```bash
cd /vol3/docker/local-fonts-manager/app
sudo git pull --ff-only
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

仅更新 WebUI / MCP，入库 worker 继续运行：

```bash
sudo docker compose pull fonts-manager
sudo docker compose up -d --no-deps fonts-manager
```

## 日常维护

```bash
cd /vol3/docker/local-fonts-manager/app
sudo docker compose logs --tail=100 -f
# 以下命令只影响自动入库，不影响浏览与 MCP
sudo docker compose stop font-ingest
sudo docker compose start font-ingest
sudo docker compose restart font-ingest
```

自动入库的结构化状态写入字体库 `_自动入库/运行状态.json`，完整异常记录位于入库状态目录的 `worker.log`。WebUI / MCP 从共享目录读取入库状态。

## 备份与恢复

完整备份前停止两个服务，分别备份管理器状态、入库状态和对应时点的字体目录：

```bash
cd /vol3/docker/local-fonts-manager/app
sudo docker compose stop
# 备份 FONT_STATE_PATH、FONT_INGEST_STATE_PATH 与字体库后：
sudo docker compose start
```

SQLite 主数据库及日志文件都应保留。恢复时必须将入库数据库与其记录的字体文件状态配套恢复；`index.sqlite3` 包含移动操作与中断恢复记录，不能当作可随意删除的缓存。WebUI 的“导出整理记录”只用于查看，完整恢复依赖状态目录备份。
