# CLAUDE.md — MinoNexus

本仓全部硬约束只有一份：[AGENTS.md](AGENTS.md)。**读完那份即可，本文不重复内容。**

原因：两份各自维护的约束文档已经漂移过一次（模型配置那一节描述相反），保持单一真源。

---

## 只在这里保留的两条工具特化提示

1. **文档目录只有 `docs/架构/` `docs/接口/` `docs/排查/` `docs/开发/` `docs/专题/`。** 历史方案已删除，不要再引用 `docs/架构/V1…V9`、`docs/更新日志/`、`docs/基础框架/`、`docs/排查手册/`、`docs/scout/`、`docs/归档/` —— 这些路径都不存在了。要考古用 `git log --all -- docs/`。
2. **不要为自证修复写测试。** 跑批与登录问题一律查 `session_events` / `session_meta`，缺字段就补 session 写入。详见 [Session 日志与数据库](docs/排查/Session日志与数据库.md)。
