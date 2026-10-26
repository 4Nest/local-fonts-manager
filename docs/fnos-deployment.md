# fnOS 切换与回滚

本项目统一部署 WebUI / MCP 和自动入库，替换旧 `font-inbox`。以下命令使用默认目录，若 `.env` 修改了路径、UID 或 GID，应同步调整。首次切换期间暂停向“待入库”投递文件，等待当前批次完成；已停止但未完成的操作会由原入库程序的恢复机制继续核对。

| 用途 | 默认位置 |
| --- | --- |
| 项目代码 | `/vol3/docker/local-fonts-manager/app` |
| 字体库 | `/path/to/Fonts` |
| WebUI / MCP 状态 | `/vol3/docker/local-fonts-manager/state` |
| 新 worker 状态 | `/vol3/docker/local-fonts-manager/ingest-state` |
| 旧 worker 状态 | `/vol3/docker/font-inbox/state` |
| 旧项目代码 | `/path/to/Fonts/_自动入库` |

新 `font-ingest` 服务使用 `legacy/ingest` 作为构建上下文，镜像为 `ghcr.io/4nest/local-fonts-manager-ingest:latest`，容器为 `local-fonts-manager-ingest`。WebUI / MCP 镜像为 `ghcr.io/4nest/local-fonts-manager-web:latest`。GitHub Actions 发布 linux/amd64 与 linux/arm64 多架构镜像，NAS 无需本地编译。Python 3.12 slim 基础镜像、`fonttools==4.62.1`、`Pillow==11.3.0`、配置和启动命令均保留。容器内字体根为 `/fonts`，状态目录为 `/state`；检查间隔仍为 15 秒，复制稳定等待仍为 60 秒。

## 首次切换

1. 将代码复制到项目目录，复制 `deploy.env.example` 为 `.env`，核对挂载路径，并在 `legacy/ingest/config.nas.json` 中设置用于状态展示的宿主机路径与 SMB 共享路径。公开 GHCR 镜像可直接拉取，拉取镜像可以在旧 worker 运行时提前完成：

   ```bash
   cd /vol3/docker/local-fonts-manager/app
   sudo docker compose --env-file .env config --quiet
   sudo docker compose --env-file .env pull
   ```

2. 暂停投递并确认旧服务没有正在处理的批次。关闭旧容器自动重启，再让旧 worker 正常退出；检查它确实已经停止。保留旧容器、镜像与项目代码用于回滚：

   ```bash
   sudo docker update --restart=no font-inbox
   sudo docker stop --time 120 font-inbox
   sudo docker inspect --format '{{.State.Status}} {{.State.ExitCode}} {{.HostConfig.RestartPolicy.Name}}' font-inbox
   ```

   预期状态为 `exited` 且重启策略为 `no`。若发生超时强制停止或已有恢复记录，不要删除数据库；迁移完整状态后由新 worker 执行恢复。确认没有其他入库进程后再继续，不能让新旧 worker 同时写同一字库。共享目录中的 `worker.lock` 是额外保护，不能代替切换时明确停止旧服务。

3. 在旧 worker 已停止、新 worker 尚未启动时，备份并迁移**完整**状态目录。包括 `index.sqlite3`、SQLite 日志文件以及其他状态文件，不要只复制主数据库：

   ```bash
   sudo mkdir -p /vol3/docker/local-fonts-manager/state
   sudo mkdir -p /vol3/docker/local-fonts-manager/ingest-state
   # 首次迁移要求目标为空；若并非空目录，先确认它是否已被新 worker 使用。
   sudo test -z "$(sudo ls -A /vol3/docker/local-fonts-manager/ingest-state)"
   sudo test -f /vol3/docker/font-inbox/state/index.sqlite3
   sudo test ! -e /vol3/docker/local-fonts-manager/font-inbox-state.before-switch
   sudo cp -a /vol3/docker/font-inbox/state /vol3/docker/local-fonts-manager/font-inbox-state.before-switch
   sudo cp -a /vol3/docker/font-inbox/state/. /vol3/docker/local-fonts-manager/ingest-state/
   sudo chown -R 1000:1001 /vol3/docker/local-fonts-manager/state /vol3/docker/local-fonts-manager/ingest-state
   ```

   逐条执行并检查结果；`test` 失败时停止迁移，避免覆盖已有新状态。备份目录名如已存在，应改用新的时间戳名称。字体库和 `_自动入库/入库记录` 仍留在原共享目录，新 worker 继续使用，不能清空。

4. 启动新项目并验证两个服务：

   ```bash
   cd /vol3/docker/local-fonts-manager/app
   sudo docker compose up -d
   sudo docker compose ps
   sudo docker compose logs --tail=100
   sudo docker compose exec -T font-ingest python /app/font_ingest.py status --config /app/config.json --state /state
   sudo docker inspect --format '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' font-inbox
   ```

   worker 首次启动会刷新索引并发布目录，随后状态应变为“等待投递”（有待处理文件时可能为“等待复制稳定”或处理中）；同时核对 `updated_at` 持续更新、没有异常，旧容器仍为 `exited no`。WebUI 使用 `http://NAS的IP:8765`，MCP 使用 `http://NAS的IP:8765/mcp`；直接打开网页，核对字体数、试字与入库状态；在“设置”中复制 API Token 配置 MCP 客户端。通过检查后恢复投递。不要从旧项目目录运行 `docker compose up` 或在 fnOS 界面启动旧容器。

## 网页与 API Token

WebUI 及其网页 API 直接访问。HTTP MCP 使用 Bearer API Token，可在 `/settings` 页面修改、复制或重置，并查看通用客户端配置。Token 保存在 `state/library.sqlite3` 的设置表中，容器重建后仍有效；旧的网页登录令牌不再需要。更改 Token 后，更新各 MCP 客户端的 `FONT_MANAGER_API_TOKEN` 并重新加载配置。主镜像包含本地 OCR 所需的 Tesseract 及英、简中、繁中、日文语言包，图片识别不依赖外部服务。

仅更新 WebUI / MCP 时，备份管理器数据库和当前代码后执行以下命令，入库 worker 继续运行：

```bash
cd /vol3/docker/local-fonts-manager/app
sudo docker compose pull fonts-manager
sudo docker compose up -d --no-deps fonts-manager
```

## 日常维护与备份

```bash
cd /vol3/docker/local-fonts-manager/app
sudo docker compose ps
sudo docker compose logs --tail=100 -f
# 单独停止 / 启动入库，不影响浏览与 MCP
sudo docker compose stop font-ingest
sudo docker compose start font-ingest
```

自动入库的结构化状态继续写入字体库 `_自动入库/运行状态.json`，完整异常记录位于新状态目录的 `worker.log`。WebUI / MCP 从共享目录读取入库状态，不依赖旧容器名。

完整备份前停止两个服务，分别备份管理器状态、入库状态和对应时点的字体目录。恢复必须将入库数据库与其记录的字体文件状态配套恢复。`index.sqlite3` 内有移动操作与中断恢复记录，不能当作可随意删除的缓存。

## 回滚到旧 font-inbox

新 worker 一旦运行，就可能刷新索引、恢复中断操作或移动待入库文件。**不能直接启动旧容器，也不能用切换前的旧数据库继续处理已经变化的字体库。** 新旧 worker 使用相同程序和数据库格式，回滚应先停新 worker，再把当前完整入库状态同步回旧服务。

1. 暂停投递，停止新 worker 并禁用它的自动重启；确认新旧容器均已停止：

   ```bash
   cd /vol3/docker/local-fonts-manager/app
   sudo docker update --restart=no local-fonts-manager-ingest
   sudo docker compose stop -t 120 font-ingest
   sudo docker inspect --format '{{.Name}} {{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' local-fonts-manager-ingest font-inbox
   ```

2. 保留当前旧状态作为备份，把停止后的新状态完整复制回原路径，再修正权限。以下备份名必须不存在；如已经使用过，应更换名称：

   ```bash
   sudo test ! -e /vol3/docker/font-inbox/state.before-rollback
   sudo mv /vol3/docker/font-inbox/state /vol3/docker/font-inbox/state.before-rollback
   sudo cp -a /vol3/docker/local-fonts-manager/ingest-state /vol3/docker/font-inbox/state
   sudo chown -R 1000:1001 /vol3/docker/font-inbox/state
   ```

   逐条执行并检查结果。保留新状态副本和原有记录，不要回退实际字体文件，也不要将管理器的 `state` 复制为入库状态。仅当确定新 worker 从未启动、字库未变化时，才可以保留旧状态直接启动。

3. 恢复旧容器自动运行并启动，检查它的状态文件与日志：

   ```bash
   sudo docker update --restart=unless-stopped font-inbox
   sudo docker start font-inbox
   sudo docker exec font-inbox python /app/font_ingest.py status --config /app/config.json --state /state
   sudo docker logs --tail=100 font-inbox
   ```

   WebUI 可继续运行；此时管理器上传由旧 worker 接手。回滚期间更新 WebUI 只能执行 `docker compose up -d --build fonts-manager`，不要执行不带服务名的 `up`，否则会重新启用新 worker。再次切换回新 worker 时，重复“先停当前 worker、迁移最新完整状态、再启另一 worker”的顺序。
