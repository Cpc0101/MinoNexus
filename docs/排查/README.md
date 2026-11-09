# 排查手册

面向跑批失败、空转、超时、登录异常的操作向文档。设计细节见 [Session 事件日志设计](../专题/Session事件日志设计.md)。

| 文档 | 内容 |
|---|---|
| [Session日志与数据库](Session日志与数据库.md) | 轨迹真源、`session_events` / `session_meta`、`mino.db` 路径 |
| [按ID查询日志](按ID查询日志.md) | 任务 `cr-…`、会话 `cr-…::case-…` 的查询路径与 SQL |
| [常见问题](常见问题.md) | 失败码、模型未配置、节点离线、定位缺失 |
| [Web适配](Web适配.md) | Web 槽位、DOM 快照、与 Android 的差异 |
| [MCP与LLM响应](MCP与LLM响应.md) | MCP 工具调用与 LLM 原始响应查询 |
| [Scout进程与实例边界](Scout进程与实例边界.md) | 本机只有一个 Scout；开发与生产实例分离 |

**日常改代码、agent 联调时不得直接写 `mino.db`**，见 [开发手册](../开发/开发手册.md)。

> 示例里的 `cr-9478d0b7d12d`、`case-7bff9406` 仅作格式说明，请换成真实 ID。
