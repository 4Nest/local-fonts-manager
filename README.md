# 字库 · Local Fonts Manager

为 fnOS 上的字体库提供 WebUI、通用 MCP 和自动入库。字体文件保留在 NAS，WebUI 与 MCP 客户端共用本地 SQLite 索引、收藏、标签、备注和选集；独立的入库 worker 负责检查、去重、分类与中断恢复。

支持搜索字族 / 文件名 / 内部别名，按厂商、语言、格式、收藏和选集筛选；实时试字、字号与底色调整、2–6 款对比、字体详情、TTC / OTC 多字面、缺字检测、原文件下载与删除、批量整理，以及拖拽文件 / 文件夹上传待入库。图片字体识别支持剪贴板粘贴、本地 OCR、文本校正和全库字形相似度排序。字体按需提取 WOFF2 子集，不需要把整个字体库下载到浏览器。缺字显示为 □，不会悄悄用系统字体补齐。

## 在 Mac 上运行

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。字体目录通过 `--root` 或 `FONT_MANAGER_ROOT` 指定。

```bash
cd local-fonts-manager
uv sync --extra dev
uv run fonts-manager web --root /path/to/Fonts
```

打开 <http://127.0.0.1:8765>。第一次启动自动读取 NAS 的 `字体目录.html`，后续直接使用本地索引；在界面点击“刷新目录”更新。若直接在文件夹中增删字体，选择“完整扫描”。快速刷新会忽略目录中已不存在的文件；两种刷新都会保留仍存在的字体及其整理记录，扫描失败不会清空已有索引。

图片 OCR 另需本机 Tesseract 和语言数据；不需要 OCR、只想手动输入文字匹配时无需安装：

```bash
brew install tesseract tesseract-lang
```

容器镜像已包含 English、简体中文、繁体中文和日文语言包。

```bash
# 指定其他挂载位置、状态目录与端口
uv run fonts-manager web --root /path/to/Fonts --state .state --port 8765
# 单独建立 / 刷新快速索引
uv run fonts-manager index
```

环境变量见 [.env.example](.env.example)。CLI 读取进程环境变量，不会自动加载 `.env`；Docker Compose 会自动读取。WebUI 直接打开，无需登录；HTTP MCP 使用独立的 API Token，在网页“设置”中修改、复制或重置。Token 保存在状态数据库中，重启后继续有效。

## 部署到 fnOS

将本项目代码放入 NAS，例如 `/vol3/docker/local-fonts-manager/app`。Compose 包含 `fonts-manager`（容器 `local-fonts-manager`，WebUI / MCP）和 `font-ingest`（容器 `local-fonts-manager-ingest`，自动入库）。新 worker 复用 `legacy/ingest/Dockerfile`，保留原来的依赖版本、15 秒检查周期、60 秒稳定等待、分类规则与恢复机制。GitHub Actions 自动测试并发布 linux/amd64、linux/arm64 多架构 GHCR 镜像，NAS 无需编译。

**从现有 `font-inbox` 切换时，先按 [fnOS 切换与回滚说明](docs/fnos-deployment.md) 停止旧 worker、迁移完整状态目录，再启动新 worker。两个 worker 不得同时处理同一字库。** 下面的初始化步骤也适用于全新部署；已有服务必须先完成上述迁移。

```bash
cd /vol3/docker/local-fonts-manager/app
cp deploy.env.example .env
# 编辑 .env，核对字体 / 状态路径及运行账户。
# 按部署说明设置 config.nas.json 中用于展示的宿主机与 SMB 路径。
sudo mkdir -p /vol3/docker/local-fonts-manager/state /vol3/docker/local-fonts-manager/ingest-state
sudo chown 1000:1001 /vol3/docker/local-fonts-manager/state /vol3/docker/local-fonts-manager/ingest-state
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

直接打开 `http://NAS的IP:8765`。设置页面为 `http://NAS的IP:8765/settings`，支持显示、复制、修改和重置 API Token；保存或重置后旧 Token 立即失效，需要同步更新客户端。HTTP MCP 位于 `http://NAS的IP:8765/mcp`，通过 Bearer API Token 认证。请在 `.env` 中设置实际字体目录；管理器数据默认放在 `/vol3/docker/local-fonts-manager/state`，入库索引与恢复日志默认放在 `/vol3/docker/local-fonts-manager/ingest-state`。两种数据库分别保存，不可混用。SQLite 不要放在 SMB 网络盘上。

字体挂载允许写入，是为了上传到“待入库”、由 worker 入库，以及从字体详情中显式删除原文件。收藏、标签、备注、选集整理本身不会移动或删除字体。在“添加字体”中可拖入文件或文件夹，也可选择文件夹；文件夹会递归读取所有子目录，支持 TTF / OTF / TTC / OTC / WOFF / WOFF2，单个文件最大 100 MiB。非字体文件会跳过；ZIP、RAR、7z 请先解压再导入。WOFF / WOFF2 可上传，但现有 worker 暂不自动分类这些格式，会放入待检查区；完全重复内容仍按去重规则处理。

上传界面显示文件相对路径，文件逐个上传；每个文件先写入 `_管理器上传暂存/`，校验成功后独立投递到 `待入库/web_随机ID/原文件名`。原文件夹层级不会保留到待入库目录，同名不同内容会进入独立投递目录，不会互相覆盖。每个投递目录达到 60 秒稳定等待期后，由 `font-ingest` 按 15 秒检查周期处理，检查字体、按完整内容去重并分类；文件夹尚在上传时，已投递的文件可能先开始入库。需要保留附件、原始目录结构或投递更大文件时，可直接通过 SMB 投递整个目录。上传时若服务意外停止，暂存目录里的残留不会进入入库服务；确认没有正在上传后可手动清理暂存目录。

状态备份：停止两个服务后，分别备份整个 `FONT_STATE_PATH` 和 `FONT_INGEST_STATE_PATH`（含 SQLite 数据），并与字体文件的备份配套保存；恢复时整体放回并恢复运行账户权限。WebUI 的“导出整理记录”另提供便于查看的 JSON，数据库备份是完整恢复方式。管理器 `previews/` 是可重建的缓存，可在停止服务后删除。

更新代码后执行 `docker compose pull && docker compose up -d` 使用 Actions 发布的新镜像；只有本地修改镜像内容时才需要 `docker compose up -d --build`。日志用 `docker compose logs --tail=100 -f` 查看；入库异常还记录在 `FONT_INGEST_STATE_PATH/worker.log`。部署替换旧 `font-inbox`，Samba 设置保持现状。不要再从旧目录启动 `font-inbox`；回滚须先停止新 worker，并将最新入库状态同步回旧服务，详见部署说明。

## 删除字体

字体详情底部的“删除原文件”会永久删除 `完整包/` 中对应的 NAS 文件，并清理其收藏、标签、备注和选集关系；TTC / OTC 以整个文件为单位删除。操作需要二次确认，不能撤销，也不会删除字体根目录之外的文件。目录中的对应路径会标记为“已删除”，防止快速刷新或自动入库的历史记录把它重新加入；以后同一路径确有新文件入库时，目录会恢复为“保留”。

## 图片识别字体

在侧栏点击“图片识别”，粘贴、拖放或选择一张文字图片。推荐先 OCR，再核对并修正文字，最后开始全库匹配；OCR 不可用或结果不准时，可以直接手动输入文字。图片仅在本机 / NAS 内存中处理，不进入字体入库目录，也不发送到第三方服务。PNG / JPEG / WebP 最大 12 MiB、2400 万像素，匹配文本最多 100 字。

匹配先按 cmap 排除缺字字体，遍历 TTC / OTC 字面，再用低分辨率字形掩膜初筛和高分辨率对齐精排。后台任务显示进度并支持取消，结果为当前字体库中的 Top 12 候选，可查看同文真实预览并进入详情。全库任务同一时间只运行一个，超时为 15 分钟，结果保留 30 分钟。

相似度是启发式排序，不是确定性的字体鉴定。清晰、单一字体的文字区域效果最好；复杂背景、描边阴影、透视、异常字距和多字体混排会降低准确度。可变字体目前只比较默认轴，复杂文字塑形也受 Pillow 排版能力限制。

## MCP 客户端连接

设置页提供通用的 Streamable HTTP MCP 地址、Bearer Token 和客户端配置示例，不限于某一个应用。以 Hermes 为例，可把 [HTTP 配置](docs/hermes-http.yaml) 合并到运行 Hermes 的机器上 `~/.hermes/config.yaml`，替换 NAS 地址。在 Hermes 的 `~/.hermes/.env` 中加入：

```dotenv
FONT_MANAGER_API_TOKEN=从字库设置页面复制的Token
```

重新加载 Hermes 的 MCP 配置或重启 Hermes；工具会自动发现。其他客户端可在 WebUI“设置 → MCP 客户端接入 → 查看连接配置”复制通用 JSON 示例。服务使用标准 **Streamable HTTP MCP**；本机子进程方式见 [stdio 配置](docs/hermes-stdio.yaml)。

可从能访问 NAS 的机器验证连接（令牌通过环境变量提供）：

```bash
uv run python scripts/check_mcp.py http://NAS的IP:8765/mcp
```

工具包括：

| 工具 | 用途 |
| --- | --- |
| `library_status` | 查看连接、索引、分类、标签与选集 |
| `search_fonts` | 搜索、筛选、分页，返回稳定的 `font_id` |
| `font_details` | 字体内部名称、字重、版本、版权、多字面与可变轴 |
| `check_characters` | 按 cmap 检查实际字符覆盖，列出缺字 |
| `preview_font` | 直接返回真实字体 PNG 图片 |
| `organize_fonts` | 批量收藏、替换标签 / 备注、加入 / 移出选集 |
| `list_collections` / `create_collection` / `remove_collection` | 管理虚拟选集 |
| `inbox_status` | 查询 NAS 入库服务和待处理项目 |
| `refresh_index` | 后台快速刷新或完整扫描 |

例如：“找几款简体中文字体，检查‘山海之间’是否缺字，用相同文本做样张对比，把喜欢的加入‘字幕候选’选集。”

标签筛选支持 API / MCP 的 `tag` 参数；更新时 `tags` 和 `note` 替换原值，省略字段保持原值。`tags: []` 清空标签。整理操作每次最多 200 个字体。缺字检测依据 Unicode cmap，不是整段排版 / 连字 / 复杂文字塑形的保证。可变字体目前预览默认轴位置；`.fon` 可被索引，但通常无法由 fontTools 解析或预览。

## 目录说明

- `font_manager/`：WebUI、字体索引、图片识别与 MCP 服务。
- `legacy/ingest/`：自动入库 worker、测试、Docker 配置与分类参考。
- `tests/`：管理器与前端上传队列测试。

历史一次性整理脚本和内部部署记录不随公开仓库发布。Compose 仅运行 `legacy/ingest` 中的持续入库程序，不执行一次性整理脚本。

界面功能与工作流参考 [op200/windows_font_manager WebUI](https://github.com/op200/windows_font_manager/tree/main/windows-font-manager-webui)，未复制其源码或静态资源。新界面使用独立的 HTML / CSS / JavaScript，不依赖 Windows 字体安装接口，也不需要 Node 构建。MCP 基于 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)，Hermes 配置遵循其 [官方 MCP 文档](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/)。

## 验证

```bash
uv sync --extra dev
uv run pytest
uv run ruff check font_manager tests
node --check font_manager/static/app.js
node --check font_manager/static/recognition.js
node --check font_manager/static/upload-queue.js
node --test tests/test_upload_queue.cjs
# Linux 入库环境另运行：
uv run pytest legacy/ingest/test_ingest.py
```

单元与集成测试使用临时字体库，不向真实 NAS 投递测试文件。
