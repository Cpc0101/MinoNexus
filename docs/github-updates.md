# Nexus 发布与升级

Nexus 与 Workbench 独立构建、独立部署。Nexus 的 wheel 和 Docker 镜像只包含服务端；Workbench 在自己的仓库构建静态文件并发布到静态站点或 CDN。Scout 继续使用自己的原生发布与安装能力。

Workbench 构建时用 `VITE_NEXUS_URL` 指向 Nexus。跨域部署时，Nexus 必须通过 `MINO_WORKBENCH_ORIGINS` 显式允许 Workbench 的完整 origin；不要信任任意来源。

## 构建和发布

- Nexus 的 main push、PR 和手动 CI：只运行 Nexus 自身的检查与 wheel 构建，不 checkout Workbench，也不需要读取 Workbench 私有仓库的令牌。
- Nexus 的 `v*` tag：先校验 tag 与 `mino_nexus/VERSION` 一致，再构建 wheel/sdist 并发布 Release；同一 wheel 再打成 AMD64/ARM64 Docker 镜像，发布 GHCR 多架构标签及分架构离线镜像包。
- Workbench 的 main push、PR 和手动运行：在 Workbench 仓库执行前端检查、构建和 Cloudflare Pages 部署；v* tag 独立发布静态压缩包。
- Nexus 的 `BUILD.json` 只记录 Nexus 版本、源码 commit 与 dirty 状态，不记录 Workbench build ID。两端发布版本可独立演进，不要求 tag 同步。

普通 push 不发布 Nexus Release。修改工作流必须提交并推送后才在 GitHub 生效。

## macOS

```sh
pipx install ./mino_nexus-X.Y.Z-py3-none-any.whl
mino-nexus service install
mino-nexus service status
```

升级前按运维安排停写并备份；执行 `mino-nexus service stop`，再 `pipx install --force ./新版.whl`，最后 `mino-nexus service start`。普通重启使用 `service restart`。

B4 启动前自动备份按用户要求暂缓；不存在“每次启动已自动备份”的保证。旧版无法读取迁移后的库时，恢复备份需要单独决定，可能丢失升级后写入的数据。

## Linux Docker

在 `docker-compose.yml` 所在目录设置实际发布的固定版本：

```sh
export MINO_NEXUS_VERSION=X.Y.Z
# 默认向宿主所有地址发布端口；仅本机反向代理访问时设为 127.0.0.1。
export MINO_BIND_ADDRESS=127.0.0.1
docker compose pull
docker compose up -d
```

现场构建：执行 `docker compose build`，再 `docker compose up -d --pull never`。Dockerfile 的默认 `source-wheel` 阶段只构建 Nexus；发布 CI 用 `WHEEL_STAGE=release-wheel` 安装已经构建的同一份 Nexus wheel。运行容器非 root，单进程、50104、数据卷 `/data`，禁用 mDNS。

反向代理需转发 Host、WebSocket Upgrade，并按 Workbench 的实际静态域名设置 `MINO_WORKBENCH_ORIGINS`。仅把真实代理地址写入 `MINO_PROXY_IPS`，不笼统信任所有来源。Scout 连接 `ws://服务器:50104/node`，TLS 代理使用 `wss://域名/node`。

AI 配置通过 Workbench 按账号保存，不从 `MINO_AI_API_KEY` 等旧环境变量读取共享密钥，不把密钥写进镜像或 Compose。

升级：停写并备份数据卷，修改固定 tag，再 `docker compose pull && docker compose up -d`。回滚：改回原 tag 后重建容器；需要恢复数据时先确认影响。不要执行 `docker compose down -v`，它会删除业务卷。同一卷只能供单个 Nexus 进程使用，启动锁会拒绝第二个新进程。

离线部署：校验对应架构清单，`gunzip -c mino-nexus-X.Y.Z-linux-amd64.tar.gz | docker load`，将镜像标记为 Compose 使用的无架构后缀固定 tag，随后 `docker compose up -d --pull never`。ARM64 使用 `arm64` 包。离线包本身的标签为 `X.Y.Z-amd64` 或 `X.Y.Z-arm64`，例如 `docker tag ghcr.io/<owner>/mino-nexus:X.Y.Z-amd64 ghcr.io/<owner>/mino-nexus:X.Y.Z`。

## 旧安装迁移

旧安装先停止 Nexus 与历史 Workbench 进程，备份后复制到新的目录，显式用新数据根启动并验收登录、历史记录和 Scout 重连。保留原目录和原版本安装包；源码移除旧安装器不意味着现场已经迁移。不要把运行中的 SQLite 主文件直接复制成备份。
