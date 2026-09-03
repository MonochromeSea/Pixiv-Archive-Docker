# Pixiv Archive - Docker 版

一个本地运行的 Pixiv 图库归档与浏览工具：扫描你本地已下载的 Pixiv 图片，构建可搜索的作品数据库并生成缩略图，然后通过本地网页界面进行浏览、搜索、按作者/标签/收藏夹整理，并可联网同步作品的元数据（标题、标签、作者信息等）。

> 本镜像基于 [Tumber-ZM/Pixiv-Archive](https://github.com/Tumber-ZM/Pixiv-Archive) 项目构建，提供开箱即用的 Docker 部署体验。

---

## ✨ 功能特性

- **本地扫描入库**：递归扫描本地 Pixiv 下载目录，从文件名解析作品 ID，自动去重（SHA-256），写入 SQLite 索引
- **缩略图生成**：为库内作品自动生成统一尺寸的缩略图，供网格浏览快速加载
- **网页浏览界面**：网格/排序浏览、多图页查看原图、作品详情页
- **多种检索方式**：关键词搜索（标题/描述/作者/标签/Pixiv ID），按作者、标签、收藏夹筛选
- **作者/标签体系**：自动聚合作者列表与标签列表
- **收藏夹管理**：创建/删除收藏夹，批量加入作品
- **元数据同步**：通过 Pixiv refresh token 联网同步作品的标题、标签、作者名、创建日期等信息
- **局域网访问保护**：支持访问令牌（`?token=...`）防止未授权访问

---

## 🚀 快速开始

### 1. 拉取镜像

```bash
docker pull monomm/pixiv-archive:latest
```

### 2. 运行容器

**基础命令（仅数据持久化）**：
```bash
docker run -d --name pixiv-archive \
  -p 6814:6814 \
  -v /path/to/your/pixiv/data:/app/data \
  --restart unless-stopped \
  monomm/pixiv-archive:latest
```

**完整命令（包含图片目录映射）**：
```bash
docker run -d --name pixiv-archive \
  -p 6814:6814 \
  -v /path/to/your/pixiv/data:/app/data \
  -v /path/to/your/pictures:/app/pictures \
  -e PA_PICTURES_DIR=/app/pictures \
  --restart unless-stopped \
  monomm/pixiv-archive:latest
```

**参数说明**：
- `-p 6814:6814`：将容器端口映射到宿主机
- `-v /path/to/your/pixiv/data:/app/data`：挂载数据目录（持久化数据库、缩略图、配置文件等）
- `-v /path/to/your/pictures:/app/pictures`：**【可选】** 挂载你存放 Pixiv 图片的目录，方便扫描入库
- `-e PA_PICTURES_DIR=/app/pictures`：告诉程序图片目录的位置（与挂载路径一致）
- `--restart unless-stopped`：容器退出时自动重启

> 如果仅挂载数据目录，后续可以通过网页设置的“图库路径”手动指定图片目录。

### 3. 获取访问令牌

容器启动后，查看日志获取访问地址和令牌：

```bash
docker logs pixiv-archive --tail 50
```

输出示例：
```
Pixiv Archive running at http://0.0.0.0:6814
====================================================
局域网访问已开启（LAN mode）
  访问地址: http://172.17.0.3:6814/?token=xxxxxxxx
====================================================
```

### 4. 访问服务

在浏览器中打开：
```
http://你的服务器IP:6814/?token=你的令牌
```

---

## 🔧 环境变量配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PA_HOST` | 绑定地址 | `0.0.0.0` |
| `PA_PORT` | 服务端口 | `6814` |
| `PA_TOKEN` | 自定义访问令牌 | 自动生成 |
| `PA_PICTURES_DIR` | 图片扫描目录（需挂载对应卷） | `/app/data/pictures` |
| `PYTHONUNBUFFERED` | 日志实时输出 | `1` |

示例（自定义令牌 + 指定图片目录）：
```bash
docker run -d --name pixiv-archive \
  -p 6814:6814 \
  -e PA_TOKEN=mysecret123 \
  -e PA_PICTURES_DIR=/app/pictures \
  -v /path/to/data:/app/data \
  -v /path/to/pictures:/app/pictures \
  monomm/pixiv-archive:latest
```

---

## 📂 数据持久化

容器内数据存储在 `/app/data` 目录，包含：
- `archive.db`：SQLite 数据库
- `thumbnails/`：缩略图缓存
- `.env`：环境配置文件

**务必挂载此目录**，否则容器重建后数据将丢失。

图片目录（通过 `PA_PICTURES_DIR` 指定）建议挂载到容器内，以便程序直接扫描。

---

## 🔄 自动更新

本镜像通过 GitHub Actions 每日自动检查上游更新并构建，确保始终包含最新功能。

镜像标签：
- `latest`：最新稳定版（默认）
- `main`：主分支最新构建

---

## 🛠️ 常见问题

### Q：如何关闭局域网访问令牌？

移除 `--lan` 启动参数即可（本镜像默认已开启令牌保护，如需关闭请自行构建）。

### Q：端口被占用怎么办？

修改宿主机端口映射，例如 `-p 6815:6814`，然后访问 `http://IP:6815/?token=xxx`。

### Q：如何查看容器日志？

```bash
docker logs pixiv-archive --tail 100 -f
```

### Q：图片目录挂载后依然扫描不到图片？

- 检查环境变量 `PA_PICTURES_DIR` 是否与挂载路径一致。
- 进入容器确认挂载是否成功：`docker exec -it pixiv-archive ls /app/pictures`。
- 在网页设置的“图库路径”中重新指定目录。

---

## 📄 开源协议

GPL-3.0 License

## 🔗 相关链接

- [GitHub 仓库](https://github.com/MonochromeSea/Pixiv-Archive-Docker)
- [上游项目](https://github.com/Tumber-ZM/Pixiv-Archive)
```

---

## 📌 主要更新点

1. **快速开始**中增加了“完整命令”示例，包含 `/pictures` 映射和环境变量 `PA_PICTURES_DIR`。
2. **环境变量表格**新增 `PA_PICTURES_DIR` 项，说明用途和默认值。
3. **常见问题**补充了图片目录扫描失败的排查方法。

现在你可以直接复制以上内容，在 Docker Hub 仓库的设置中更新概述。😊
