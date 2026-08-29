# Pixiv Archive

一个本地运行的 Pixiv 图库归档与浏览工具：扫描你本地已下载的 Pixiv 图片，构建可搜索的作品数据库并生成缩略图，然后通过本地网页界面进行浏览、搜索、按作者/标签/收藏夹整理，并可联网同步作品的元数据（标题、标签、作者信息等）。

**v1.1 新增**：R18 显示模式切换（全部 / 不显示R18 / 只显示R18）、已删除作品快速筛选、作者头像与作者 ID 显示、设置界面分页化（基础设置 / 网络设置 + GitHub 仓库图标）、查看器与详情页的「在文件夹中打开」。

**v1.1.1（CPU 性能优化版）**：针对图库量大时高 CPU 占用问题专项优化——每页缩略图 200 → 100、瓷砖动画轻量化（入场交错收敛、闪烁改一次性、静止时零动画开销）、顶部徽章去除 backdrop-filter（去掉 GPU 合成层）、后端 R18/收藏判定改为非关联 `IN` 子查询。运行功能与显示内容**与 v1.1 完全一致**。

**v1.1.2（同步限速 + 图片查看器增强版）**：新增「同步间隔」设置（默认 800ms，防风控、可取消）、查看器与详情页**滚轮缩放 + 放大后拖拽平移**（双击还原，缩放范围 0.2–5×，缩放的显示区域限制在右侧图片面板内）、**网页自定义右键菜单**（作品瓷砖 / 查看器图片 / 空白处上下文操作，输入框保留原生菜单）、多图作品翻页按钮与左侧操作区对齐且置于图片图层之上。运行功能与显示内容**与 v1.1.1 完全兼容**。

> 本仓库只包含源码，**不包含任何个人数据**（`.env`、`archive.db`、图片、缩略图、元数据等均未打包）。请使用 `install.bat` 或直接分发 `Release` 内的 exe 初始化你自己的运行环境。

---

## 版本 1.1 新增功能

- **R18 显示模式切换**：顶栏「R18」按钮循环切换 全部显示 / 不显示R18 / 只显示R18。判定标准为作品的 **#R18 标签**（标签名/译名为 R-18 / R18 / 18R，不含 R-18G）。画廊、搜索、作者、标签、收藏夹各列表同步过滤，瓷砖右上角带 R18 角标；选择会保存在本地。
- **作者列表优化**：作者名左侧显示圆形作者头像（缓存于 `metadata/avatars/`，缺失时自动联网拉取），作者名右侧用小字显示作者 `ID`。
- **设置界面分页化**：设置面板拆为「基础设置」与「网络设置」两个分页标签，右侧新增 GitHub 仓库图标；小尺寸窗口下面板可滚动显示完整。
- **快速定位已删除作品**：顶栏「已删除」按钮一键筛选 `pixiv_status=deleted` 的作品，方便复查被删作品。
- **在文件夹中打开**：图片查看器与作品详情页新增「在文件夹中打开」按钮，调用系统资源管理器打开目录并选中对应图片文件。

---

## 版本 1.1.2 功能更新

**1. 同步限速（防风控）**
- 设置「网络设置」新增**同步间隔（毫秒）**，默认 `800`（范围 0–10000，`0`=不限速）。
- 后端每次写入元数据请求前按改间隔等待（0.1s 分片、可随时取消），风控激烈时明显降低被限风险；不影响扫描/收藏等其它功能。

**2. 查看器滚轮缩放 + 拖拽平移**
- 首页查看器与作品详情页的图片支持**鼠标滚轮缩放**。
- **双击还原**、换图/关闭时自动复位。
- 放大后**按住左键拖拽平移**，查看被裁剪区域。。

**3. 自定义右键菜单（网页内覆盖原生右键）**
- 在网页内用自定义菜单**完全替换**默认右键菜单：作品瓷砖（查看 / 收藏 / 删除 / 复制 PID）、查看器图片（打开文件夹 / 收藏 / 删除 / 上一·下一作品 / 关闭）、空白处（刷新 / 返回首页 / 切换主题 / 打开设置）。
- 输入框保留系统原生右键（便于复制粘贴）；网页无法控制浏览器外壳/操作系统级的右键菜单，属平台限制。

---

## 版本 1.1.1 性能优化（CPU）

针对图库作品数量大、缩略图较多时的 CPU/GPU 占用做了专项优化，功能与显示内容完全不变：

- **每页缩略图数量 200 → 100**：DOM 与动画规模减半，分页功能不受影响。
- **入场动画轻量化**：交错由 `idx*12→360ms` 收敛为 `idx*5→100ms`，动画时长 `0.35s→0.22s`，保留波浪感。
- **消灭常驻闪烁 (shimmer)**：瓷砖骨架光从无限循环改为一次性（1.2s 后自动停止），静止时零动画开销。
- **徽章去掉 backdrop-filter**：缩略图角标改用纯半透明背景，去掉每块的 GPU 合成层。
- **后端 R18/收藏判定改为非关联子查询**：`EXISTS(... = a.id ...)` → `a.id IN (SELECT ...)`，避免对每行执行相关子查询；多图库下 SQLite 负载显著下降，`is_r18` / `is_favorited` 返回值不变。

---

## 功能特性

- **本地扫描入库**：递归扫描本地 Pixiv 下载目录，从文件名解析作品中对应的 Pixiv ID（兼容 `12345678_p0` 等多页命名），自动去重（按 SHA-256）、识别缺失文件，并写入 SQLite 索引（`archive.db`）。
- **缩略图生成**：为库内作品自动生成统一尺寸的缩略图（JPG），供网格浏览快速加载。
- **网页浏览界面**：网格/排序浏览、多图页查看原图、作品详情页（标题、描述、标签、作者）。
- **多种检索方式**：关键词搜索（标题 / 描述 / 作者 / 标签 / Pixiv ID），按作者、标签、收藏夹筛选。
- **作者 / 标签体系**：自动聚合作者列表与标签列表，查看某作者或某标签下的全部作品。
- **收藏夹管理**：创建/删除收藏夹，将作品批量加入收藏夹。
- **元数据同步**：通过 Pixiv refresh token 联网同步作品的标题、标签、作者名、创建日期、尺寸等信息；作品已删除时自动标记（`deleted`）。
- **局域网访问**：`--lan` 或 `Start-LAN.bat` 可开放局域网访问，通过访问令牌（`?token=...` / `X-Access-Token` 请求头 / Cookie）保护，防止未授权设备访问。
- **直连 / 代理自适应**：`PIXIV_MODE` 支持 `direct`（直连 IP 免 SNI）、`proxy`（走 Clash 类代理）、`auto`（自动尝试并回退）。
- **一键启动 / 一键打包**：`.bat` 脚本自动查找 Python、创建 venv、安装依赖；`build-exe.bat` 可从源码直接编译出单文件 exe。
- **桌面窗口模式**：`launcher.bat` 可选用 pywebview 原生窗口呈现，缺失时自动回退到浏览器。

---

## 目录结构

```
app/                 应用代码
  main.py             FastAPI 路由、页面与 API
  database.py         SQLite 连接与建表/迁移
  scanner.py          本地图片扫描与入库
  sync.py             Pixiv 元数据同步
  pixiv.py            Pixiv API 客户端（直连/代理）
  jobs.py             后台任务（扫描/同步）管理与进度
  thumbnails.py       缩略图生成
  templates/          页面模板（index.html、artwork.html、author.html、tag.html）
  static/             前端资源（style.css、pa_token.js、favicon.png）
assets/              图标（icon.png / icon.ico，由 scripts/make_icon.py 生成）
scripts/             辅助脚本（make_icon.py 生成图标）
Release/             产物目录（构建出的 PixivArchive.exe）
run.py               启动本地 HTTP 服务（浏览器模式）
launcher.py          桌面窗口 / 浏览器启动器（含托盘、端口避让、单实例锁）
browser_entry.py     exe 分发专用入口（强制自动打开浏览器）
Start.bat            启动服务（浏览器模式）
Start-LAN.bat        启动服务（局域网模式）
launcher.bat         桌面窗口模式
install.bat          一键初始化环境（创建 venv + 安装依赖 + 生成 .env）
build-exe.bat        一键从源码编译单文件 exe
build.ps1            PowerShell 构建脚本（等价于 build-exe.bat）
PixivArchive.spec    PyInstaller 打包配置（onefile / 无控制台 / 自动开浏览器）
requirements.txt     运行时依赖清单
requirements-build.txt  打包（PyInstaller）所需依赖
.env.template        环境变量模板（复制为 .env 后填写）
```

---

## 环境要求

- **操作系统**：Windows（桌面窗口模式依赖 WebView2 Runtime，缺失时自动回退浏览器）
- **Python**：3.10+（开发与验证环境为 3.11）
- **网络**：同步元数据 / 刷新直连 IP 需要能访问 pixiv（必要时搭配代理）

运行时依赖（`requirements.txt`）：`fastapi`、`hypercorn`、`pixivpy3`、`Pillow`、`Jinja2`、`python-dotenv`、`aiofiles`、`pywebview`、`pystray`。
构建依赖（`requirements-build.txt`）：`pyinstaller`。

### venv 里装的是什么？

`venv/` 只是普通 Python 虚拟环境（`python -m venv venv` 创建，再用 `venv\Scripts\python.exe -m pip install -r requirements.txt` 安装依赖）。里面没有任何个人数据，分发时不需要也不应该打包它——接收方运行 `install.bat` 即可自行创建。

---

## 使用教程

### 方式一：从源码运行

**第 1 步：初始化环境（仅首次）**

双击 `install.bat`，脚本会自动：
1. 查找系统 Python（`python` → `py`）；
2. 创建本地虚拟环境 `venv/`；
3. 安装 `requirements.txt` 全部依赖（带 `--trusted-host`，规避常见证书/中间人拦截）；
4. 若不存在 `.env`，自动从 `.env.template` 复制生成。

> 没有 Python？先到 https://www.python.org/downloads/ 安装，安装时勾选 "Add python.exe to PATH"，再运行 `install.bat`。

**第 2 步：配置 `.env`**

编辑根目录的 `.env`，至少填写两项：

```ini
PIXIV_REFRESH_TOKEN=你的refresh_token
IMAGE_SOURCE_DIR=D:\Pixiv
```

**第 3 步：启动**

| 场景 | 操作 | 说明 |
|------|------|------|
| 本机使用 | 双击 `Start.bat` | 浏览器模式，随后访问 http://127.0.0.1:6814 |
| 局域网使用 | 双击 `Start-LAN.bat` | 开放局域网，自动生成/使用访问令牌 |
| 桌面窗口 | 双击 `launcher.bat` | pywebview 原生窗口，缺失时回退浏览器 |

启动脚本会优先使用 `venv\Scripts\python.exe`，其次回退到 PATH 中已装好依赖的 `python` / `py`；找不到合适的 Python 时会给出提示。

**第 4 步：首次导入作品**

打开网页后，进入 **设置** 页确认 `本地图片目录`，然后点击 **扫描**。扫描会把该目录下所有 Pixiv 图片（文件名包含 7–10 位数字 ID）写入数据库并生成缩略图。

**第 5 步：同步元数据（可选）**

在 **设置** 填入 `PIXIV_REFRESH_TOKEN` 后点击 **同步**，为库内作品联网拉取标题、标签、作者等信息。

### 方式二：直接使用 exe

直接分发/使用 `Release\PixivArchive.exe`（或自己按"[构建方式](#构建方式)"一节编译）：

- 无需安装 Python，双击即可运行；
- 启动后**自动打开系统默认浏览器**访问 http://127.0.0.1:6814；
- 首次运行会在 exe 同目录自动生成数据文件：`archive.db`、`thumbnails/`、`.env`、日志；
- 托盘图标提供"显示窗口 / 退出"，退出后进程与端口会彻底释放；
- 支持命令行参数：`--lan` 开启局域网模式、`--port 7130` 指定端口、`--check` 冒烟自检。

### 界面与常用操作

- **扫描**：把本地图片目录中识别出的作品加入库并生成缩略图（可设置取消/重置）。
- **同步**：联网为库内作品补充元数据；缺失/失效 token 会在界面给出明确提示。
- **搜索**：顶部搜索框按 标题/描述/作者/标签/Pixiv ID 全文检索；可按作者、标签、收藏夹组合筛选，支持多种排序。
- **收藏夹**：在作品详情或列表页批量加入/移除收藏夹。
- **作者 / 标签页**：`/author/<id>`、`/tag/<名称>` 页面聚合作者、标签下的全部作品。
- **局域网访问**：非本机访问需携带令牌。启动时会打印访问地址，形如 `http://<局域网IP>:6814/?token=xxxx`；也可在设置中自定义 `PA_ACCESS_TOKEN`。
- **R18 模式与已删除筛选**：顶栏「R18」循环切换 全部 / 隐藏R18 / 仅R18；「已删除」按钮快速只看被删作品。两者会同步作用于搜索、作者、标签、收藏夹与跨页浏览。
- **在文件夹中打开**：查看器与作品详情页的「在文件夹中打开」按钮，可在资源管理器中定位并选中图片文件。

### 环境变量一览（`.env`）

| 变量 | 说明 |
|------|------|
| `PIXIV_REFRESH_TOKEN` | Pixiv refresh token（同步元数据必需） |
| `IMAGE_SOURCE_DIR` | 本地 Pixiv 图片目录（扫描必需） |
| `PIXIV_MODE` | `direct` 直连 / `proxy` 代理 / `auto` 自动（默认 auto） |
| `PIXIV_PROXY` | 代理地址，如 `http://127.0.0.1:7890` |
| `THUMBNAIL_SIZE` | 缩略图边长，默认 400 |
| `THUMBNAIL_DIR` / `METADATA_DIR` | 缩略图 / 元数据存放目录名 |
| `PA_PORT` / `PA_HOST` | 服务端口 / 绑定地址，默认 6814 / 127.0.0.1 |
| `PA_ACCESS_TOKEN` | 局域网访问令牌（可自定义固定值；留空则自动生成） |
| `PIXIV_IP_*` | 直连模式下的 pixiv 域名 → IP 覆盖（网页设置中可刷新） |

---

## 构建方式

### 一键脚本：`build-exe.bat`（推荐）

双击 `build-exe.bat`，脚本会自动：
1. 查找 Python 并创建 `venv/`（如不存在）；
2. 安装运行时依赖 `requirements.txt` + 构建依赖 `requirements-build.txt`（PyInstaller）；
3. 运行 `scripts/make_icon.py` 重新生成图标；
4. 执行 PyInstaller（`--clean --noconfirm`）输出到 `Release\`。

产物：**`Release\PixivArchive.exe`**（单文件、无控制台窗口）。

特点：
- 独立运行，exe 自带全部依赖与页面资源，无需目标机器安装 Python；
- 启动自动打开系统默认浏览器；
- 首次运行在 exe 所在目录生成 `archive.db`、`thumbnails/`、`.env`；
- 支持 `--lan` / `--port` 等命令行参数。

### PowerShell 脚本：`build.ps1`

与 `build-exe.bat` 等价，适合命令行环境：

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

（旧版脚本，产物同样输出到 `Release\`。）

### 手动构建步骤（等价命令）

```bat
:: 1. 准备环境
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt

:: 2. 生成图标
venv\Scripts\python.exe scripts\make_icon.py

:: 3. PyInstaller 打包（onefile，输出到 Release）
venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath Release --workpath build PixivArchive.spec
```

### 打包配置说明（`PixivArchive.spec`）

- 入口为 `browser_entry.py`：强制 `PA_BROWSER=1` 走 `launcher.main()`，因此 exe 启动即自动打开浏览器，而非桌面窗口；
- `console=False` 无控制台窗口；`onefile` 单文件输出；
- 已精简打包范围：不收集 `webview` / `pythonnet` / `clr`（浏览器模式不需要），以减小体积；仍保留 `pixivpy3`、`Pillow`、`pydantic`、`pystray`；
- `app/templates`、`app/static` 会随包内嵌（运行时解压到临时目录）。

---

## 数据与隐私

- 本仓库/产物**不包含任何个人数据**：不含 `.env`（token）、`archive.db`（作品记录）、图片、缩略图、元数据或日志。
- 运行后仅在你的机器上生成以下本地数据（源码运行于项目根目录，exe 运行于 exe 同目录）：
  - `archive.db`：作品索引（SQLite）
  - `thumbnails/`、`metadata/`：缩略图与元数据 JSON
  - `pixiv_archive.log`：运行日志
  - `.env`：你的配置（token 等，仅本机保存）
- 局域网模式请务必妥善保管访问令牌；任何持有该地址与令牌的设备都能查看/管理你的图库。

---

## 常见问题（FAQ）

**启动时报 `ModuleNotFoundError`？**
说明当前 Python 缺少依赖。运行 `install.bat` 创建 venv 后，用 `Start.bat` 重新启动；或改用 `Release\PixivArchive.exe`。

**服务启动但浏览器打不开 / 页面 404？**\n确认访问地址端口与启动日志一致（默认 6814）。`launcher.bat` / exe 在端口被占用时会自动避让到 +1 端口（最多 20 次）；而 `Start.bat`（`run.py`）会直接绑定设定端口，被占用则启动失败——此时请关闭占用端口的程序或在 `.env` 设置 `PA_PORT`。

**扫描后没有缩略图？**
确认 `IMAGE_SOURCE_DIR` 目录存在且可读、文件名确实包含 Pixiv ID（7–10 位数字）、图片格式属于 jpg/jpeg/png/webp/gif/bmp。

**同步失败 / 认证失败？**
确认 `.env` 中 `PIXIV_REFRESH_TOKEN` 有效未过期，且网络可访问 pixiv；可在设置中切换直连 / 代理模式（`PIXIV_MODE` / `PIXIV_PROXY`）。

**局域网下其他设备无法访问？**
确认使用 `Start-LAN.bat`（绑定了 0.0.0.0）、本机防火墙放行对应端口，并在访问 URL 末尾保留 `?token=...`。

**托盘图标点"退出"后进程还在？**
旧版本存在此问题（退出事件先在托盘回调里被阻塞）；当前版本已修复：退出事件先置位再停止托盘，并强制结束进程。请重新构建/获取最新 exe。

**杀毒软件报毒？**
PyInstaller 单文件 exe 偶见误报，属常见现象；可改用目录形态（onedir）打包以降低误报概率。

---

## 命令行参数速查

| 参数 | 适用脚本 | 说明 |
|------|----------|------|
| `--lan` | `Start-LAN.bat` / exe / `run.py` | 开放局域网访问（绑定 0.0.0.0，自动生成令牌） |
| `--port <端口>` | 任意启动方式 | 指定端口（默认 6814，被占自动避让+1） |
| `--host <地址>` | `run.py` | 指定绑定地址 |
| `--browser` | `launcher.bat` | 强制浏览器模式 |
| `--check` | `launcher.py` | 冒烟自检后退出（写入 `pixiv_archive_check.txt`） |

---

## 许可证

本项目以 **GNU GPL v3** 发布，见仓库根目录 [`LICENSE`](LICENSE)。

采用 GPL v3 的原因：本项目的直连模块（`app/direct_connect.py`，免 SNI / 直连 IP 方案）参考了开源项目 [PixEz](https://github.com/Notsfsssf/Pix-EzViewer)（及其 Flutter 版 [pixez-flutter](https://github.com/Notsfsssf/pixez-flutter)，GPL-3.0）的实现思路，为避免合规风险，整体以 GPL-3.0 发布其源码。在此感谢 PixEz 作者 **Notsfsssf** 提供的直连方案。

- 本仓库内的**代码**：GPL-3.0
- **依赖的第三方库**：各自许可证见 [`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES)，与 GPL-3.0 兼容
- **图片版权**：作品版权归相应 Pixiv 作者所有；本工具仅作本地归档，请尊重原作者权利并遵守 Pixiv 使用条款