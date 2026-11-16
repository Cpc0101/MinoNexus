# 开发与正式部署

| 组件 | 开发 | 正式 |
| --- | --- | --- |
| Nexus | localhost:50101，`--dev` | `--prod`，默认 0.0.0.0:50104 |
| Workbench | Vite localhost:60101 | 前端仓库独立构建并部署到静态站点或 CDN |
| Scout | 连接对应开发 Nexus | 连接正式 Nexus，容器场景用服务器 IP 或域名 |

开发数据根是 `~/.mino-dev/{scout,nexus,workbench}`，正式数据根是 `~/.mino/{scout,nexus}`。Nexus 的 `mino.db` 在对应 `nexus/` 目录根上。Docker 使用 `/data`。Workbench 正式产物是独立静态站点，不写入 Nexus 数据目录。两种环境禁止共用或互相包含目录，环境标记会校验。旧安装目录不自动迁移。

源码默认仍是开发模式；wheel 的生产启动必须用 `--prod` 或 `service` 子命令。开发 Workbench 只允许连接 localhost:50101，默认经 Vite proxy；正式构建通过 `VITE_NEXUS_URL` 指向 Nexus，不自动发现其他 Nexus。

正式 CORS 默认不允许跨源，开发源可用 localhost/127.0.0.1:60101。反向代理可在 Nexus 上设置明确的 `MINO_WORKBENCH_ORIGINS`，WS 认证仍必须通过。不要把正式凭据或数据库复制到开发环境。

现有 PyCharm 进程仍由 IDE 管理。Docker 的升级通过固定 tag 与数据卷完成。Workbench 独立部署，不随 Nexus wheel 或镜像升级；没有在线自更新。
