<div align="center">

# 字库

### Local Fonts Manager

**部署在 NAS 上的字体预览、整理、识别与 MCP 工具**

[![Tests & Docker](https://github.com/4Nest/local-fonts-manager/actions/workflows/docker.yml/badge.svg)](https://github.com/4Nest/local-fonts-manager/actions/workflows/docker.yml)
[![GHCR](https://img.shields.io/badge/GHCR-amd64%20%C2%B7%20arm64-2496ED?logo=docker&logoColor=white)](https://github.com/4Nest/local-fonts-manager/pkgs/container/local-fonts-manager-web)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)

</div>

字体留在自己的 NAS，通过浏览器完成预览、整理和图片识别，也可以让任意支持 Streamable HTTP 的 MCP 客户端使用字体库。

## 功能一览

| 能力 | 说明 |
| --- | --- |
| **预览与搜索** | 自定义试字、缺字提示、TTC / OTC 多字面、厂商 / 语言 / 格式筛选 |
| **字体整理** | 收藏、标签、备注、选集、批量操作与原文件下载 |
| **图片识别** | 粘贴图片 → 本地 OCR → 修正文字 → 全库字形相似度排序 |
| **文件管理** | 拖入文件或文件夹待入库；经确认后删除原字体文件 |
| **自动入库** | 独立 worker 检查、去重、分类，并支持中断恢复 |
| **MCP 接入** | 搜索、缺字检测、真实样张、收藏标签与选集等标准 MCP 工具 |

## 快速部署

镜像由 GitHub Actions 自动测试并发布，支持 **linux/amd64** 与 **linux/arm64**，NAS 无需编译。

> [!IMPORTANT]
> 如果正在运行旧 `font-inbox`，请先阅读 [fnOS 切换与回滚说明](docs/fnos-deployment.md)，迁移完整状态后再启动新 worker。两个 worker 不能同时处理同一字库。

```bash
git clone https://github.com/4Nest/local-fonts-manager.git
cd local-fonts-manager
cp deploy.env.example .env

# 编辑 .env，设置真实字体目录、状态目录和运行账户。
# 按部署说明同步修改 legacy/ingest/config.nas.json 中的展示路径。
sudo mkdir -p /vol3/docker/local-fonts-manager/state
sudo mkdir -p /vol3/docker/local-fonts-manager/ingest-state
sudo chown 1000:1001 /vol3/docker/local-fonts-manager/{state,ingest-state}

sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

| 入口 | 地址 |
| --- | --- |
| WebUI | `http://NAS的IP:8765` |
| 设置 | `http://NAS的IP:8765/settings` |
| MCP | `http://NAS的IP:8765/mcp` |

> [!WARNING]
> WebUI 及网页 API 不设置登录。请仅在可信局域网使用，不要直接暴露到公网。字体挂载可写，用于上传、自动入库和显式删除。

### 公开镜像

- WebUI / MCP：[`local-fonts-manager-web`](https://github.com/4Nest/local-fonts-manager/pkgs/container/local-fonts-manager-web)
- 自动入库：[`local-fonts-manager-ingest`](https://github.com/4Nest/local-fonts-manager/pkgs/container/local-fonts-manager-ingest)

日常更新：

```bash
sudo docker compose pull
sudo docker compose up -d
sudo docker compose logs --tail=100 -f
```

## 图片字体识别

在侧栏选择 **图片识别**，粘贴、拖放或选择一张文字图片：

1. 使用本机 Tesseract OCR，也可以直接手动输入文字。
2. 核对并修正匹配文本。
3. 后台渲染字库中的相同文字，进行初筛、对齐和精排。
4. 查看 Top 12 同文预览与相似度，并跳转到字体详情。

图片不会发送给第三方。清晰、单一字体的文字区域效果最好；复杂背景、描边阴影、透视、混排及可变字体非默认轴会降低准确度。相似度是启发式排序，不是确定性的字体鉴定。

## MCP 客户端

在 **设置 → MCP 客户端接入 → 查看连接配置** 复制标准 `mcpServers` JSON。配置包含 MCP 地址和 Bearer API Token，不限定特定客户端。

- Hermes 示例：[HTTP](docs/hermes-http.yaml) · [stdio](docs/hermes-stdio.yaml)
- Token 保存在状态数据库中，容器重建后仍有效。
- 重置 Token 后，需要同步更新所有 MCP 客户端。

## 本地开发

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。OCR 依赖可通过 `brew install tesseract tesseract-lang` 安装；只手动输入文字匹配时无需安装。

```bash
uv sync --extra dev
uv run fonts-manager web --root /path/to/Fonts
```

打开 <http://127.0.0.1:8765>。CLI 读取进程环境变量，不会自动加载 `.env`；配置示例见 [.env.example](.env.example)。

## 删除与备份

字体详情中的 **删除原文件** 会永久删除 `完整包/` 中对应文件，并清理收藏、标签和选集关系；TTC / OTC 按整个文件删除，操作不可撤销。目录删除标记可避免历史记录让字体重新出现。

停止服务后，完整备份字体库、`FONT_STATE_PATH` 和 `FONT_INGEST_STATE_PATH`，包括 SQLite 日志文件。两个状态目录用途不同，不要混用；SQLite 不应放在 SMB 网络盘上。

## 验证

```bash
uv sync --extra dev
uv run pytest
uv run ruff check font_manager tests
node --test tests/test_upload_queue.cjs

# Linux 入库环境
uv run pytest legacy/ingest/test_ingest.py
```

更多操作与故障恢复见 [NAS 使用说明](docs/nas-usage.md)和 [fnOS 部署文档](docs/fnos-deployment.md)。

---

界面工作流参考 [op200/windows_font_manager](https://github.com/op200/windows_font_manager)，界面与代码独立实现；MCP 基于[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)。
