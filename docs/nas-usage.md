# fnOS 字库管理与自动入库

本说明部署到字体共享目录的 `_自动入库/使用说明.md`；下方文件链接以该位置为准。

## WebUI 与 MCP 客户端

- 字库 WebUI：`http://NAS的IP:8765`，提供预览试字、搜索、对比、缺字检测、图片字体识别、收藏、标签、选集及上传待入库。
- HTTP MCP 入口：`http://NAS的IP:8765/mcp`，供支持 Streamable HTTP 的 MCP 客户端使用，需要在客户端配置 API Token；浏览器直接打开不作为连接测试。

WebUI 直接打开，无需登录。在 `http://NAS的IP:8765/settings` 可以修改、复制和重置 API Token，并查看通用 MCP 客户端配置。保存或重置后旧 Token 立即失效，请同步更新各客户端的 `FONT_MANAGER_API_TOKEN`。WebUI 与 MCP 使用同一套整理记录。WebUI 容器是 `local-fonts-manager`，自动入库容器是 `local-fonts-manager-ingest`。收藏、标签、备注和选集存储在管理器数据库中，整理操作本身不会移动字体文件。

图片识别使用容器内的本地 Tesseract OCR 和字体渲染，不会把图片发送到外部服务。镜像已包含英、简中、繁中、日文语言包；OCR 后可以修正文字再进行全库匹配，结果只是本地字库的相似度候选。识别任务较耗 CPU，同一时间只运行一个全库任务，可在弹窗中取消。字体详情中的“删除原文件”会永久删除 `完整包/` 内对应文件并清理整理关系，操作前需确认，且不能撤销。

## 日常投递

把字体文件或整个字体文件夹复制／拖入：

`\\NAS\Fonts\待入库`

映射盘路径也可以使用：`X:\Fonts\待入库`。Mac 可通过 SMB 打开 `Fonts/待入库`。

也可在 WebUI 的“添加字体”中拖入文件或文件夹，或点击选择文件夹。文件夹会递归读取所有子目录，支持 TTF / OTF / TTC / OTC / WOFF / WOFF2，单个文件最大 100 MiB。界面会显示相对路径；非字体文件会跳过，ZIP、RAR、7z 请先解压再导入。现有 worker 暂不自动分类 WOFF / WOFF2，它们通常进入待检查区；完全重复内容仍按去重规则处理。

WebUI 按文件逐个上传，先写入 `_管理器上传暂存/`，单个文件校验成功后独立投递到 `待入库/web_随机ID/原文件名`。上传不会保留原文件夹层级，同名不同内容会进入独立投递目录，不会互相覆盖；随后由 worker 等待稳定期、检查、去重并分类。文件夹尚在上传时，已投递的文件可能先开始入库。需要保留说明 / 授权附件、原始目录结构或投递超过 100 MiB 的文件时，可通过 SMB 投递整个目录。上传中断留下的暂存文件不会被 worker 入库；确认没有正在上传后才清理暂存残留。

复制完成后，程序每 15 秒检查一次；同一顶层文件夹连续 60 秒没有变化后，递归处理里面的文件。文件仍被写入时会等待重试。大批量字体还需要计算哈希和检查字体，处理时间随数量和大小增加。

请先解压压缩包，再投递字体文件夹。程序不会解压 ZIP、RAR、7z；通过 SMB 投递的压缩包按附件保存，WebUI 会跳过它们。空文件夹不会入库。

服务运行在 NAS 的 Docker 容器 **local-fonts-manager-ingest** 中，电脑关机后仍可运行。NAS 重启且 Docker 启动后会自动恢复；在 Docker 中手动停止后，需要手动启动。

## 处理结果

| 结果 | 保存位置 | 规则 |
| --- | --- | --- |
| 新字体 | `完整包/厂商/语言或字符集/字族/` | 读取字体内部信息，使用现有分类规则；通过关键结构检查及样本试绘后入库。 |
| 完全重复 | `_入库重复备份/批次/` | 大小和完整 SHA-256 与库内文件一致，原始副本移到可恢复备份。 |
| 有问题或暂不支持 | `_入库待检查/批次/` | 无法解析、结构异常、样本试绘失败、缺少 Unicode 映射或暂不支持的字体格式。 |
| 说明、授权、压缩包等 | `附件与原有脚本/自动入库附件/批次/` | 保留原文件及其相对目录。 |

同名字体只要内容不同就保留，发生文件名冲突时增加 `__来源_哈希` 后缀；仅大小写不同的目标名称也会避让。不同字重、版本、格式不会仅凭名称被去重。新增英文字体也按此规则入库；此前的英文精简属于一次性整理，不会对新投递字体自动套用主观删减。

重复备份和待检查区不会自动清空。这里的检查是结构和少量字符试绘，不代表逐字渲染或全部软件兼容性保证。旧字体库的问题字体不会因此被自动替换。单个字体可从重复备份或待检查区复制出来人工使用；再次投递完全重复的文件仍会进入重复备份。

## 查看进度与记录

- WebUI：`http://NAS的IP:8765`，查看入库状态并整理字体；新入库完成后，可点击“刷新目录”更新管理器索引。
- [自动入库记录](../自动入库记录.html)：最近 2,000 个处理结果，可搜索并打开对应文件。
- [运行状态](运行状态.json)：当前阶段、最后更新时间、库内数量和最近批次。正常空闲阶段是“等待投递”。
- [完整批次记录](入库记录/)：每批 Markdown 汇总和 JSONL 详细记录，包括 SHA-256、分类、移动路径和等待重试原因。
- [字体目录](../字体目录.html)：原目录保留历史记录，新入库字体会追加进来。

状态文件是快照。如果最后更新时间长时间没有刷新，请到 fnOS 的 Docker 页面检查 `local-fonts-manager-ingest` 是否运行。待入库文件一直未被处理时，先看是否还在复制、是否存在 `.part`／`.tmp` 等临时文件，以及状态中的警告。

## 启停与维护

在 fnOS 的 **Docker → 容器 → local-fonts-manager-ingest** 中启动、停止或重启自动入库。已启用自动运行，不需要在电脑上运行启动脚本。

SSH 管理命令：

```bash
cd /vol3/docker/local-fonts-manager/app
sudo docker compose ps
# 以下命令仅影响自动入库，WebUI / MCP 继续运行。
sudo docker compose stop font-ingest
sudo docker compose start font-ingest
sudo docker compose restart font-ingest
sudo docker compose logs --tail=100 font-ingest
sudo docker compose exec -T font-ingest python /app/font_ingest.py status --config /app/config.json --state /state
```

项目程序位于 `/vol3/docker/local-fonts-manager/app`，入库源代码与配置位于其中的 `legacy/ingest/`。`legacy/ingest/config.nas.json` 定义分类区路径、检查间隔和稳定等待时间。修改入库代码或配置后，在项目目录执行 `sudo docker compose up -d --build font-ingest` 重建 worker。原有依赖版本和入库行为保留；基础镜像来自 Amazon ECR 的 Docker Official Images 镜像源。

字体库通过 `.env` 中的 `FONT_LIBRARY_PATH` 挂载到容器内 `/fonts`。默认运行账户为 `1000:1001`，请按字体库所属用户调整。入库容器只挂载字体目录和自己的状态目录，没有开放网络端口；WebUI / MCP 由独立管理器容器提供，使用 8765 端口。

共享目录 `_自动入库` 现在用于**运行记录与旧代码存档**。其中的 `运行状态.json`、`入库记录/` 和 `worker.lock` 继续由新 worker 使用，旧 Python 文件、旧 Compose 配置及历史恢复脚本作为存档保留。不要在该目录运行旧 `docker compose up`，不要启动旧 `font-inbox` 容器，也不要直接执行历史一次性整理脚本。

## 状态、写锁与恢复

入库数据库和程序日志保存在 NAS 本地：

`/vol3/docker/local-fonts-manager/ingest-state/`

其中 `index.sqlite3` 保存索引和中断恢复日志，`worker.log` 记录异常，`status.json` 保存状态快照。共享目录中的 JSONL 同时保留每次移动前后的记录。意外重启时，程序会核对未完成操作及 SHA-256 后恢复。

worker 在共享目录 `_自动入库/worker.lock` 上持有操作系统文件锁，防止同一字库同时运行多个入库进程。锁文件存在本身不表示 worker 正在运行；不要通过删除锁文件来绕过锁，也不要同时运行新旧服务。切换时必须先停止当前 worker，迁移完整状态，再启动另一个 worker。

WebUI / MCP 的索引、收藏、标签、备注和选集另存于 `/vol3/docker/local-fonts-manager/state/`，不能与入库状态目录混用。备份整个字体目录前，应在项目目录执行 `sudo docker compose stop`，同时备份字体目录、上述两个完整状态目录，再执行 `sudo docker compose start`。SQLite 的日志文件也属于状态备份；不要只复制主数据库，不要在仍有未完成批次时删除或重建入库数据库。

旧服务的 `/vol3/docker/font-inbox/state/` 是切换时保留的旧状态。新 worker 开始处理后，旧数据库可能已落后于实际字库。回滚必须先停止新 worker，再把当前完整 `ingest-state` 同步回旧状态路径，然后启动旧服务。不能直接启动旧容器或仅恢复旧数据库。详细步骤见项目内 `/vol3/docker/local-fonts-manager/app/docs/fnos-deployment.md`。
