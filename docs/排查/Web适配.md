# Web 网页槽位适配

Web 跑批使用 Scout **Playwright 槽**（`sn` 形如 `web…`、`platform=web`），`ctx.target_package` 为 **被测 URL**（如 `http://hitem3d-test.mathmagical.cn/`），**不是** Android 包名。下列能力若仍按「包名 + adb hierarchy」实现，会在 Web 上误判或空转。

## 1. 前台 / 前置「被测 App 已打开」

| 安卓 | Web |
|------|-----|
| `get_foreground_app` → 包名 | **无包名**；`hierarchy` 为 **DOM 快照**（`source=playwright`），根节点带 `page_url` |
| `run_guard_foreground` 比 `foreground_package == target_package` | 比 **origin / URL**（`nav_target_scope.web_page_matches_target`） |
| `recover_bring_target_app_foreground` → `launch_app(package=…)` | `launch_app(url=…)`；`bring_target` recovery 规则仅种子在 android/ios，Web 靠 launch + guard |

**典型症状（`cr-c29661ad39d7` 类）**

- T0/T1 `launch_app` 已成功打开 URL，但 `app_foreground` 仍为 `unknown`/`no`。
- 前置阶段模型反复 `launch_app`，被 `skip_repeat_launch_app` 拦截，提示「recover_bring…」——Web 上该 recovery 往往无效。
- history 里大量 `skip_repeat_launch_app → skipped`。

**排查**

```sql
-- 会话里所有 launch_app 调用与结果
SELECT seq, type, payload FROM session_events
 WHERE session_id LIKE 'cr-xxxxxxxx%'
   AND payload LIKE '%launch_app%'
 ORDER BY seq;
```

**代码位置**

- `navigation/atlas/nav_target_scope.py` — `web_page_matches_target`、`infer_foreground`（`platform=web`）
- `navigation/atlas/nav_capture_store.py` — 空 DOM 时 Web 降级
- `loop/recovery.py` — Web 不走 `get_foreground_app`
- `launch_app` pass 后 Web 写 `ctx.app_foreground=yes`

## 2. UI 几何 / 发码 / 登录链

| 能力 | 安卓 | Web |
|------|------|-----|
| `request_sms_code` | `ui_sms_request`：宽 **EditText** + 同行发送钮 | `ui_dom`：`<input>` / `textarea` + 同行钮；无 DOM 时 **文案 tap 发码** |
| 程序登录链 | 同上 | `ui_channel` 分流；禁止用 `edittext` 匹配 DOM |
| `action_fuse` 屏指纹 | hierarchy 指纹 | Web 常无指纹；双空指纹计无进展 |

**典型症状**

- Email 标签已点，程序链报「未找到邮箱输入框」——旧版用安卓规则扫 DOM。
- 反复点 Email tab、不 `input_text` / 不发码。

## 3. 观测与超时

| 项 | 说明 |
|----|------|
| `observe` / 截图 | `router_proxy` 默认 **8s**；Playwright 慢时整步 fail，文案可能带「adb」（协议通用模板，**不代表走了 adb**） |
| `hierarchy` | Scout `dom_hierarchy.dump_dom_nodes`；Nexus 空节点时 guard 依赖 `launch_confirmed` |
| NavFSM / 底栏 Tab | 许多 App 图仅为安卓包名配置；Web 渠道可能 **无 FSM 边**，需开环 `tap_element` |

## 4. 环境与账号

- `env_surface` × `env_profile` → `login.kind=email` / `otp.mode=gmail`（见 `OTP_GMAIL.md`）。
- history 应有 `【登录凭证·环境】`；无则 Nexus 未重启或旧版本。
- 号池须 **email** 账号；`gmail_inbox` + 插件 `gmail_otp` 应用专用密码。

## 5. 发布对齐清单

改 Web 相关行为时，至少核对：

1. **两仓协议**：`PROTOCOL.md` §4.4.2 Web `hierarchy` 行；`tests/fixtures/protocol` 若改形状需双仓同步。
2. **Scout 版本**：DOM hierarchy 需 **Scout ≥ 0.1.41**（`v0.1.41` tag 打包）。
3. **Nexus 版本**：渠道分流、`run_guard_foreground` 需 **Nexus ≥ 0.1.19**。
4. **Workbench**：项目环境 `web_url`、渠道 `env_surface`、Web 设备槽绑定。

## 6. 仍待完善（规划）

- `get_foreground_app` 在 Web 返回当前 `page.url`（可选协议扩展）。
- Recovery 目录增加 `bring_target_web` / `platforms: ["web"]` 种子。
- `compile_sms_send_hint` 对 `login.kind=email` 的 Web 文案。
- Web `screen_fingerprint` 用 DOM 摘要替代纯截图 hash，改善熔断。

---

相关：[常见问题](常见问题.md) · [执行链](../架构/执行链.md) · [脚本仓库](../架构/脚本仓库.md)
