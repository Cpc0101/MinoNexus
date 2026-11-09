# Prompt 规范

Nexus 侧的 LLM 调用（助手、用例编译、知识抽取、`llm_client`）都在本仓，prompt 正文的唯一真源是 `mino_nexus/prompts/`。

**用例脚本里的 `case.llm` / `case.vlm` 不在本仓执行** —— 那里用节点自己保存的地址、模型名和密钥在 Scout 本机直连，Nexus 不参与、也不保存这些密钥。

## 1. 真源分工

| 内容 | 真源 | 说明 |
|---|---|---|
| **prompt 正文、技能、角色、编排** | `mino_nexus/prompts/` | git 是唯一真源。`jobs/`、`skills/`、`roles/`、`stack/` |
| **加载** | `platform/llm/repo.py` | 默认只在进程启动时校验并加载。`MINO_PROMPTS_RELOAD=1` 时，每次读取比对整棵 `prompts/` 的内容哈希，变了就重新加载并校验 |
| **渲染** | `platform/llm/render.py` | 块 → messages |
| **槽值** | `ai/job_slots.py` | 槽值生产者，运行时注入，不写进 job 模板 |

```
mino_nexus/
├── platform/llm/repo.py    读 prompts/、校验、指纹；list_assistant_tools() 也从这里取
├── platform/llm/render.py  块 → messages
├── ai/job_slots.py         槽值生产者（Python）
└── prompts/                prompt 与 assistant/tools/*.yaml。不放 Python
```

## 2. 活 job 列表

`mino_nexus/prompts/jobs/` 下 39 个 job。使用面分三类：

| 类别 | job | 入口 |
|---|---|---|
| 用例与评审 | `case-step-compile` · `draft_cases` · `draft_gate` · `draft_sign` · `map_cases` · `req-qa-bm` · `version-qa-bm` · `analyze_req` · `draft_mindmap` · `report-writer` | `casework/*` · `casework/qa/qa_cover` |
| 知识 / 图谱 | `propose_atlas` · `nav-atlas-morph` · `nav-widget-state` · `knowledge-extract` · `knowledge-extract-batch` · `knowledge-reviewer` · `doc-keeper` · `doc-learn` · `app-intel-ask` | `knowledge/*` · `ai/app_intel` |
| 观察 / 校验 | `inspect-session` · `inspect-checkbox-state` · `inspect-login-consent-scope` · `assert-vision` · `agent-vision-assert` · `agent-vision-exec` · `agent-vision-plan` · `agent-text-assert` · `classify_fail` | `ai/planner.py` · `loop/vision_*.py` |
| 助手 / IM | `conductor` · `im-dialogue` · `im-defect` · `test-engineer-chat` · `product-expert` · `intent-choice` · `account-facet-*` · `pick_regression` | `ai/roles_catalog.py` |

`agent-decide` 这个 job 文件仍在（`dispatch_log` 的历史记录会引用它），但**逐步决策已经删除**，不会再产生新的 `agent-decide` 调度。

**定位不是纯 VLM**：`action_space/locator_chain.py` 先走结构链（DOM / hierarchy 指纹），未命中才看图兜底。不要把 CLIP / OCR 引回 Nexus。

## 3. 改 prompt 或助手工具 yaml 的流程

1. 直接修改 `mino_nexus/prompts/` 中的当前文件（含 `prompts/assistant/tools/*.yaml`）；不维护数字版本、不审批、不发布、不切换 A/B。
2. 默认必须重启 Nexus。加载器只在启动时解析；格式错、引用断了会拒绝启动并说明原因。未设置 `MINO_PROMPTS_RELOAD` 时，改完 yaml 不会进入 `list_assistant_tools()`，MCP `tools/list` 仍是旧表。
3. 开发时设 `MINO_PROMPTS_RELOAD=1` 后再启动。之后每次取 job、技能或助手工具都会比对 `prompts/` 的内容哈希（不是只看最大 mtime），有变化就整表重载并重新校验。下一次 MCP `tools/list` 或助手调用能看到新工具，不必再重启进程。该模式下 MCP `initialize` 的 `tools.listChanged` 为 true，支持该通知的客户端会重新拉列表；不支持的客户端要自己再调一次 `tools/list`。生产环境不要开这个开关。
4. 提交。生产环境重启 Nexus 后生效。
5. **禁止**在 `mino_nexus/**/*.py` 里写 system prompt 字面量（`scripts/verify_no_prompt_literals.py`，中英文都拦）。

Git 保存提示词修改历史；`dispatch_log` 记录实际使用内容的 `prompt_sha`。兼容历史记录的数字字段不参与选择或准入。

Workbench 的 Jobs / 技能 / 角色 / 编排页只读，展示当前内容、sha 和文件路径。

历史修改通过 Git 回溯。运行时只加载当前文件，不应用历史升级补丁。

## 4. 铁律

| # | 规则 | 为什么 |
|---|---|---|
| 1 | 坐标一律 **0–1000 千分比** | Nexus 不知道分辨率 |
| 2 | prompt 里只出现菜单里**有**的能力 | 否则白跑 fallback |
| 3 | 观察阶段不准动手 | 校验阶段不能改界面 |
| 4 | 看图决策 ≠ 视觉校验 | 两个 job，两套 prompt |
| 5 | 结构化输出过 `schemas.py` | LLM 会乱填 |
| 6 | 配置 key 必须是已知闭集 | 禁止在 prompt 里写 JSON 表达式 |
| 7 | NavFSM 的 assist 正文由 `nav_compiler` 现编，**不进 prompt 模板** | 被测 App 的文案属于 `nav_fsm*` 表，硬编码进 job 就没法按 app 隔离 |

## 5. 成本与落痕

每次 LLM 调用经 `dispatch_log.record_llm` 落痕；`dispatch.bind(job=...)` 必须带 job id，**禁止**再从 system 文本反推 job。

本地校验（不进 CI）：

```bash
python scripts/verify_no_prompt_literals.py
```

