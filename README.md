# ☁️ 云端同步

让 AstrBot 的文件可以稳稳地放到云端，也能在需要时取回来。

`astrbot_plugin_rclone` 基于 [rclone](https://rclone.org/) 工作，在 AstrBot 原生插件页面里配置网盘、创建同步任务、预览变更和查看执行进度。它适合备份插件数据、导出文件、媒体目录等普通文件，也支持把云端内容下载到本机的专用目录。

插件只做单向同步：每个任务都明确选择一个方向，不会偷偷进行双向合并，也不会保存历史版本。

[功能亮点](#features) · [快速开始](#quick-start) · [同步方式](#modes) · [命令与模型工具](#commands) · [数据与安全](#data) · [开发验证](#development)

<a id="features"></a>

## 🌟 功能亮点

| 能力 | 说明 |
| --- | --- |
| 四种同步方式 | 上传更新、镜像到云端、下载更新、镜像到本地，方向和删除行为都清楚写在任务里 |
| 原生插件页面 | 管理 rclone、存储连接、同步任务和执行记录，不需要额外搭建管理服务 |
| 连接配置向导 | 根据当前 rclone 的配置元数据生成表单，支持候选项、密码字段、高级选项和 OAuth 授权 |
| 官方 GUI | 需要更完整的 rclone 能力时，可以临时启动官方网页界面 |
| 预览与进度 | 执行 dry-run，先看新增、更新和删除；同步时显示文件数、字节数、速度和删除数 |
| 定时任务 | 间隔按分钟设置，支持手动执行、取消和重启后的到期补跑 |
| 聊天命令 | 使用英文 `/rclone` 命令查看、创建、编辑、预览、执行和删除任务 |
| 可选模型工具 | 默认关闭；打开后模型才可以查询和管理同步任务 |

<a id="quick-start"></a>

## ⚡ 快速开始

### 准备环境

| 项目 | 要求 |
| --- | --- |
| AstrBot | `>=4.27,<5` |
| Python | `3.12+` |
| rclone | `>=1.74`，需要包含 `gui` 命令和官方 GUI 资源 |

### 安装插件

把插件目录放进 `data/plugins/astrbot_plugin_rclone/`，或者在 AstrBot WebUI 上传插件 ZIP。依赖在 `requirements.txt` 中；如果 AstrBot 没有自动安装，可以在 AstrBot 使用的 Python 环境执行：

```bash
python -m pip install -r data/plugins/astrbot_plugin_rclone/requirements.txt
```

### 安装 rclone

打开 AstrBot 插件详情中的「云端同步」，在「使用引导」的安装步骤点击「下载并安装 rclone」。插件会根据 **AstrBot 所在的机器或容器**选择稳定版，校验官方 SHA-256 后保存到插件私有目录的 `bin/` 下。

也可以自己安装 rclone，然后在页面填写 `rclone` 或可执行文件的绝对路径。Windows 路径必须指向 `.exe` 文件。Docker 部署时请持久化 AstrBot 的 `data` 目录，否则插件下载的程序和配置会随容器重建丢失。

自动安装支持 Windows、Linux 和 macOS 的常见 amd64、386、arm64 架构，以及 Linux arm-v6、arm-v7。平台不支持或无法访问官方下载源时，直接从 [rclone 下载页](https://rclone.org/downloads/) 安装即可。

### 添加连接并创建任务

1. 在页面填写 rclone 程序位置，保存连接设置。
2. 点击「添加存储」，选择后端并按向导完成授权；也可以使用终端配置或官方 GUI。
3. 选择连接名称，填写「云端同步根目录」，保存后点击「检测并连接」。
4. 点击「新建任务」，选择同步模式，填写本机路径和云端子目录。
5. 先点「预览变更」，确认结果后再保存和执行。

所有任务共用当前连接和同步根目录。普通网盘的根目录可以写成 `AstrBotSync`；S3、R2 等对象存储需要写成 `桶名/AstrBotSync`。

<a id="modes"></a>

## 🔄 同步方式

| 模式 | 方向 | 同名文件 | 目标端多余文件 |
| --- | --- | --- | --- |
| 上传更新 | 本机 → 云端 | 按本机来源更新 | 保留 |
| 镜像到云端 | 本机 → 云端 | 按本机来源更新 | 删除 |
| 下载更新 | 云端 → 本机 | 按云端来源更新 | 保留 |
| 镜像到本地 | 云端 → 本机 | 按云端来源更新 | 删除 |

“更新”会按来源内容更新变化的文件，目标端同名文件可能被覆盖；它不是只补缺失文件，也不会按两端修改时间做合并。镜像模式会删除目标端多余文件，建议给镜像任务使用专用目录。

任务路径有以下规则：

- 上传模式的本机路径可以是文件或目录。上传单个文件时，云端会保留这个文件名，插件不会把同级其他文件一起上传。
- 下载模式的本机路径必须是目录，不存在时会在实际执行时创建；预览不会创建目录。
- 相对本机路径以页面显示的 AstrBot 运行根目录为基准；Docker 中填写容器内的路径。
- 本机路径及父目录不能经过符号链接或目录联接。上传目录内部的链接会跳过，下载目标内部出现链接会直接报错。
- 不能把下载目标设为文件系统根目录或插件私有目录。插件私有目录会在同步时自动排除。
- 云端子目录必须是非空相对路径，使用 `/` 分隔，不能包含 `..` 或冒号。

排除规则每行一条，使用 [rclone glob 规则](https://rclone.org/filtering/)，例如 `*.tmp`、`cache/**`、`__pycache__/**`。预览会显示将修改哪一端以及新增、更新、删除、未变数量，最多列出 200 项，不会修改文件。

执行间隔按分钟设置：`0` 表示仅手动执行，`1440` 表示每天执行一次。取消同步不会回滚已经完成的文件变更；运行中的 SQLite 等数据库请先导出或创建快照，普通文件同步不提供数据库在线一致性保证。

## 🧩 配置存储

配置向导使用 rclone 自己提供的后端和字段信息，因此 OneDrive、Google Drive、WebDAV、SFTP、S3、R2 等后端都可以沿用 rclone 的配置方式。

OneDrive 等 OAuth 后端可能需要在有浏览器的电脑执行向导显示的 `rclone authorize ...` 命令，再把授权结果粘贴回页面。两端建议使用相同版本的 rclone。敏感值不会回显，留空会保留原值；需要彻底清除时可使用官方 GUI。

官方 GUI 的使用方式：

1. 点击「官方 GUI」，选择监听地址和 GUI/API 端口。
2. 点击「启动官方 GUI」，插件会使用同一份私有 `rclone.conf` 启动 `rclone gui --no-open-browser`。
3. 把页面显示的登录地址复制到浏览器，在官方界面完成配置。
4. 返回插件点击「结束配置并停止服务」，保存连接设置后重新检测连接。

默认只监听 `127.0.0.1`。服务器或 Docker 环境需要映射 GUI/API 端口，远程访问时请填写浏览器实际能访问的 HTTP(S) 地址，并通过 HTTPS 或反向代理保护登录地址。GUI 启动 15 分钟后会自动停止，配置期间同步和预览会暂停。

<a id="commands"></a>

## 💬 命令与模型工具

### `/rclone` 命令

命令名和参数使用英文。任务可以用任务 ID 或精确任务名称引用。

```text
/rclone list
/rclone show <task>
/rclone status [task]
/rclone create <JSON>
/rclone edit <task> <JSON>
/rclone delete <task> confirm
/rclone preview <task>
/rclone run <task> [confirm]
/rclone cancel <task>
```

创建任务需要完整提供以下字段：`name`、`mode`、`local_path`、`remote_path`、`exclude`、`enabled` 和 `interval_minutes`。

```text
/rclone create {"name":"插件备份","mode":"upload","local_path":"data/plugin_data","remote_path":"plugin-data","exclude":["*.tmp"],"enabled":true,"interval_minutes":1440}
```

`edit` 接受部分字段的 JSON 更新。`delete` 只删除任务配置，不删除本机或云端文件；镜像模式执行 `run` 时必须在末尾加 `confirm`。`preview` 只做 dry-run。

### 模型工具

配置项「启用同步任务模型工具」默认关闭。关闭时这些工具不会暴露给模型，也不会加入模型工具提示词上下文；开启后提供：

| 工具 | 用途 |
| --- | --- |
| `rclone_list_tasks` | 列出同步任务 |
| `rclone_get_task` / `rclone_get_status` | 查看任务或运行状态 |
| `rclone_create_task` / `rclone_update_task` | 创建或编辑任务 |
| `rclone_delete_task` | 删除任务配置 |
| `rclone_preview_task` | 预览同步变更 |
| `rclone_run_task` / `rclone_cancel_task` | 执行或取消任务 |

删除任务和镜像执行都要求模型明确传入 `confirm=true`。

<a id="data"></a>

## 🔐 数据与安全

插件私有数据位于 AstrBot 数据目录下的 `plugin_data/astrbot_plugin_rclone/`：

- `state.json`：连接、任务和最近 200 条已结束执行记录。
- `rclone.conf`：正式连接配置，可能包含授权令牌或密钥。
- `rclone.edit.conf`：尚未提交的配置向导内容。
- `bin/`：页面下载的 rclone 程序。
- `work/`：单文件同步和预览使用的临时目录，完成后清理。

每次同步都显式使用这份私有 rclone 配置，不读取系统默认配置，也会过滤继承的 `RCLONE_*` 环境覆盖。配置文件当前不加密，请限制 AstrBot 数据目录的访问权限。

页面管理接口只接受已登录 WebUI 的 JWT 和用户绑定的操作凭据，不接受 API Key 或页面资源令牌。

同步使用 `rclone copy` 或 `rclone sync --delete-after`。传输不是原子事务，失败时可能已经有部分文件更新；重新执行会重新比较并继续。插件不生成 ZIP、版本目录、校验清单或历史备份。

### 从旧版切换

旧镜像版或更早备份版的 `state.json` 不会自动迁移，也不保留旧字段和旧 API 别名。遇到旧格式、未知字段或损坏状态时插件会直接报错。升级时请先停止旧版，备份并移出原 `state.json`，保留 `rclone.conf`，然后重新创建任务。

<a id="development"></a>

## 🧪 开发验证

在插件目录执行：

```bash
ruff format --check .
ruff check .
node --check pages/sync/app.js
node --check pages/sync/config.js
pytest -q
```

测试覆盖四种同步模式、dry-run、排除规则、单文件上传、删除范围、失败和取消、动态配置、JWT/CSRF、模型工具开关以及插件页面交互。真实 rclone 测试优先使用 `tests/tools/rclone.exe`，缺少测试二进制时会跳过对应场景。

遇到问题或有改进建议，欢迎提交 Issue 或 Pull Request。
