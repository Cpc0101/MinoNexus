import { listMembers, addMember, removeMember, ProjectAccessError, canReadRun } from "./project_access";
/**
 * MCP endpoint — auth and tool table match mino_nexus.api.rMcp + prompts/assistant/tools.
 * Tools dispatch to the same HTTP routes as mcp0.0.1 assistant tool YAML `route` fields.
 */

import type { Env } from "./env";
import { hubStub } from "./env";
import { listProjects, createApp, createProject, deleteApp, deleteProject } from "./auth";
import { listDevices, listScouts } from "./db";
import {
  ACCESS_PREFIX,
  OAuthPage,
  canonicalResource,
  publicOrigin,
  resolveAccessToken,
} from "./mcp_oauth";
import { resolveMcpToken } from "./mcp_tokens";
import { handlePluginRoutes } from "./plugin_routes";
import { startRun } from "./case_runner";
import { getRepoApps, saveRepoApps } from "./repo_apps";
import { getRun, listRuns, toTaskJson } from "./run_store";
import { cancelTask, deviceAvailability, TaskError } from "./task_scheduler";
import { artifactChunk, readEvents, readRecord, screenshotMetadata, listSessions } from "./record_proxy";
import { listAccounts, saveAccount, deleteAccount, importAccounts, AccountError } from "./account_pool";
import { getInstallStatus, issueInstallToken, unbindScout } from "./install";
import { requireOwnedProject, saveRepo } from "./script_repo";
import { getProjectEnv, saveProjectEnv } from "./project_env";

type JsonRpcId = string | number | null;

type JsonRpcRequest = {
  jsonrpc?: string;
  id?: JsonRpcId;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: unknown;
};

const PROTOCOL = "2025-03-26";
const PROTOCOLS = new Set(["2025-03-26", "2025-06-18", "2025-11-25"]);

/** Tool specs copied from mino_nexus/prompts/assistant/tools/*.yaml (mcp surface). */
const TOOLS = [
  {
    name: "agent_guide",
    title: "给其他 agent 的使用说明",
    description:
      "外部 agent 使用 Mino 时先调用。返回工具怎么分组调用，以及脚本、Git、设备分别由谁做。",
    inputSchema: { type: "object", properties: {} },
    annotations: {
      title: "给其他 agent 的使用说明",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "project_list",
    title: "项目与应用列表",
    description: "列出所有项目和其下的应用。",
    inputSchema: { type: "object", properties: {} },
    annotations: {
      title: "项目与应用列表",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "scout_list",
    title: "查询 Scout 节点",
    description: "列出当前账号可见的 Scout 节点，含 node_id、在线状态、上报版本。",
    inputSchema: { type: "object", additionalProperties: false, properties: {} },
    annotations: {
      title: "查询 Scout 节点",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "scout_install_token",
    title: "申请 Scout 安装命令",
    description:
      "为当前账号签发 15 分钟的 Scout 安装命令，返回 install_id、expires_at 和各系统 commands。把与本机系统匹配的那条命令交给本机执行，遵循客户端的本机执行权限，不要把 token 或完整命令写进对话。安装进度用 scout_install_status。",
    inputSchema: { type: "object", additionalProperties: false, properties: {} },
    annotations: {
      title: "申请 Scout 安装命令",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "scout_install_status",
    title: "查看 Scout 安装进度",
    description: "用 scout_install_token 返回的 install_id 查看安装是否完成、节点是否在线。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: { install_id: { type: "string" } },
      required: ["install_id"],
    },
    annotations: {
      title: "查看 Scout 安装进度",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "scout_uninstall",
    title: "卸载 Scout 并解除绑定",
    description:
      "删除该节点在当前账号下的绑定、设备和安装记录，并断开在线连接。返回本机卸载命令。执行与本机系统匹配的命令，遵循客户端的本机执行权限，不要把命令写进对话。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: { node_id: { type: "string", description: "来自 scout_list 的 node_id" } },
      required: ["node_id"],
    },
    annotations: {
      title: "卸载 Scout 并解除绑定",
      readOnlyHint: false,
      destructiveHint: true,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "device_list",
    title: "兼容设备列表",
    description: "实时查询设备。status 为 online/offline。",
    inputSchema: {
      type: "object",
      properties: {
        only_online: { type: "boolean" },
        project_id: { type: "string" },
        app_id: { type: "string" },
        env_surface: { type: "string" },
      },
    },
    annotations: {
      title: "兼容设备列表",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "app_create",
    title: "创建应用",
    description:
      "在项目下创建一个应用，返回它的 id。参数齐全且用户已要求该操作时直接调用，无需再次确认。",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
        name: { type: "string" },
        description: { type: "string" },
        platforms: {
          type: "string",
          description: "逗号分隔，取值 android / ios / web / mac / win",
        },
      },
      required: ["project_id", "name", "platforms"],
    },
    annotations: {
      title: "创建应用",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "project_create",
    title: "创建项目",
    description:
      "创建一个项目，返回项目 id。name 必填，description 可选。参数齐全且用户已要求该操作时直接调用，无需再次确认。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        name: { type: "string", description: "项目名称，1 到 200 字" },
        description: { type: "string", description: "项目说明，可空，最长 2000 字" },
      },
      required: ["name"],
    },
    annotations: {
      title: "创建项目",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "app_delete",
    title: "删除应用",
    description:
      "删除一个误建的应用，只删应用记录。app_id 来自 project_list。参数齐全且用户已要求该操作时直接调用，无需再次确认。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        app_id: { type: "string", description: "要删除的应用 id" },
      },
      required: ["app_id"],
    },
    annotations: {
      title: "删除应用",
      readOnlyHint: false,
      destructiveHint: true,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "project_delete",
    title: "删除项目",
    description:
      "删除一个项目及其下的应用记录。project_id 来自 project_list。参数齐全且用户已要求该操作时直接调用，无需再次确认。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        project_id: { type: "string", description: "要删除的项目 id" },
      },
      required: ["project_id"],
    },
    annotations: {
      title: "删除项目",
      readOnlyHint: false,
      destructiveHint: true,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "plugin_status",
    title: "查看插件安装状态",
    description:
      "查看本人 Scout 节点上的插件是否已安装、是否已配置，以及下载中心里的安装/更新能力。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: { node_id: { type: "string" } },
      required: [],
    },
    annotations: {
      title: "查看插件安装状态",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "node_channel_program",
    title: "安装或更新渠道程序",
    description:
      "把安装或更新渠道程序转成该用户当前在线节点的下载中心请求。允许 chromium、adb.keyboard、appium。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        node_id: { type: "string", minLength: 1, maxLength: 128 },
        tool_id: { type: "string", enum: ["chromium", "adb.keyboard", "appium"] },
        action: { type: "string", enum: ["install", "update"], default: "install" },
        operation_id: { type: "string", maxLength: 100 },
      },
      required: ["node_id", "tool_id"],
    },
    annotations: {
      title: "安装或更新渠道程序",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "plugin_install",
    title: "安装或更新插件",
    description:
      "在本人 Scout 节点上提交插件安装或更新任务。仅支持 cli.feishu、cli.meego。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        node_id: { type: "string", minLength: 1, maxLength: 128 },
        plugin: { type: "string", enum: ["cli.feishu", "cli.meego"] },
        action: { type: "string", enum: ["install", "update"], default: "install" },
        operation_id: { type: "string", maxLength: 100 },
        wait_ms: { type: "integer", minimum: 0, maximum: 60000, default: 0 },
      },
      required: ["node_id", "plugin"],
    },
    annotations: {
      title: "安装或更新插件",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "case_list",
    title: "项目需求列表",
    description:
      "列出项目已登记的需求：id、名称、渠道链接。这里没有用例正文。编写脚本前读链接，跑的时候用 case_run 的 script_targets。",
    inputSchema: {
      type: "object",
      properties: {
        project_id: { type: "string" },
      },
      required: ["project_id"],
    },
    annotations: {
      title: "项目用例列表",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "case_run",
    title: "跑用例",
    description:
      "在指定设备上执行脚本，返回任务 id。优先传 script_targets：[{requirement_id, case_ids}]。case_ids 为空则执行 tests/<需求ID>/ 下的全部脚本；指定了用例 ID 时路径是 tests/<需求ID>/<用例ID>.py。revision 填已 push 的 commit，不传则 Scout 取分支最新。",
    inputSchema: {
      type: "object",
      properties: {
        client_request_id: { type: "string", maxLength: 160 },
        project_id: { type: "string" },
        app_id: { type: "string" },
        env_surface: { type: "string" },
        env_profile: { type: "string" },
        sn: { type: "string" },
        case_ids: { type: "array", items: { type: "string" } },
        script_targets: {
          type: "array",
          items: {
            type: "object",
            properties: {
              requirement_id: { type: "string" },
              case_ids: { type: "array", items: { type: "string" } },
            },
            required: ["requirement_id"],
          },
        },
        revision: { type: "string" },
      },
      required: ["app_id"],
    },
    annotations: {
      title: "跑用例",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: false,
      openWorldHint: false,
    },
  },
  {
    name: "requirement_list",
    title: "项目需求列表",
    description: "列出项目已登记的需求：id、名称、渠道链接。与 case_list 相同。",
    inputSchema: {
      type: "object",
      properties: { project_id: { type: "string" } },
      required: ["project_id"],
    },
    annotations: {
      title: "项目需求列表",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "requirement_save",
    title: "登记需求",
    description: "保存需求 ID、名称、适用应用和需求/用例文档链接。app_ids 可关联多个应用；source_url 为需求文档，cases_url 为用例文档。ID 同时是脚本目录 tests/<需求ID>/。参数齐全且用户已要求该操作时直接调用，无需再次确认。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        project_id: { type: "string" },
        app_id: { type: "string" },
        app_ids: { type: "array", items: { type: "string" }, description: "适用应用 ID；空列表表示项目通用。与 app_id 二选一。" },
        cases_url: { type: "string" },
        requirement_id: { type: "string" },
        new_requirement_id: { type: "string", description: "兼容旧客户端的显式改名入口；已有需求优先使用 requirement_update。" },
        title: { type: "string" },
        source_url: { type: "string" },
      },
      required: ["project_id", "requirement_id", "title"],
    },
    annotations: {
      title: "登记需求",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "requirement_update",
    title: "修改需求",
    description: "修改已有需求的 ID、名称、适用应用及需求/用例链接。只更新传入字段；new_requirement_id 用于改名，脚本仓 tests/<旧ID>/ 需同步改名，Nexus 不修改仓库。参数齐全且用户已要求该操作时直接调用，无需再次确认。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        project_id: { type: "string" },
        app_id: { type: "string" },
        app_ids: { type: "array", items: { type: "string" }, description: "适用应用 ID；空列表表示项目通用。与 app_id 二选一。" },
        cases_url: { type: "string" },
        requirement_id: { type: "string" },
        new_requirement_id: { type: "string" },
        title: { type: "string" },
        source_url: { type: "string" },
      },
      required: ["project_id", "requirement_id"],
    },
    annotations: {
      title: "修改需求",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "project_script_repo_set",
    title: "设置项目脚本仓库",
    description:
      "保存项目的脚本仓库地址和分支。Nexus 只记地址，不拉取仓库，不保存 Git 令牌；Scout 在本机用自己的 git 凭据拉取。参数齐全且用户已要求该操作时直接调用，无需再次确认。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        project_id: { type: "string", minLength: 1 },
        url: { type: "string", description: "http/https/ssh/git 地址；不要带令牌" },
        branch: { type: "string" },
      },
      required: ["project_id", "url"],
    },
    annotations: {
      title: "设置项目脚本仓库",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "repo_apps_sync",
    title: "同步脚本仓库的应用清单",
    description:
      "脚本仓库根目录 config.yaml 的 apps 改动并 push 后必须调用。revision 填 push 后的 commit，branch 填分支。差异更新：apps 只填这次新增或修改的应用（取自 config.yaml，按 key 整项替换；每个应用要有 platform，身份字段只有 package / bundle_id / url，可带 envs；不要传 app_id、不要传 otp）。未带 app_id 时 Nexus 按 key 对齐或自动创建应用并回填。删除的应用 key 填进 remove；没出现的应用保持不变。不能含账号密码或 otp。Nexus 不读仓库，凭此知道项目能测哪些应用。参数齐全且用户已要求该操作时直接调用，无需再次确认。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        project_id: { type: "string", minLength: 1 },
        revision: { type: "string", description: "push 后的 commit" },
        branch: { type: "string" },
        apps: {
          type: "object",
          description:
            "本次新增或修改的应用（只含 platform 与 package/bundle_id/url/envs；不要 otp；app_id 可省略）",
        },
        remove: {
          type: "array",
          items: { type: "string" },
          description: "本次从 config.yaml 删除的应用 key",
        },
      },
      required: ["project_id"],
    },
    annotations: {
      title: "同步脚本仓库的应用清单",
      readOnlyHint: false,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "repo_apps_get",
    title: "查看脚本仓库的应用清单",
    description:
      "读取 Nexus 记录的项目脚本仓库应用清单（含同步的 commit、分支、同步人和时间）。没有同步过则 apps 为空。",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: { project_id: { type: "string", minLength: 1 } },
      required: ["project_id"],
    },
    annotations: {
      title: "查看脚本仓库的应用清单",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  {
    name: "task_get",
    title: "任务详情",
    description: "查看一次跑批任务的结果和失败原因。",
    inputSchema: {
      type: "object",
      properties: { task_id: { type: "string" } },
      required: ["task_id"],
    },
    annotations: {
      title: "任务详情",
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
  },
  ...[
    { name: "project_member_list", title: "项目成员", description: "查看项目所有者及成员。", properties: { project_id: { type: "string" } }, required: ["project_id"] },
    { name: "project_member_add", title: "添加项目成员", description: "所有者按已注册邮箱添加成员，成员可查看、编辑项目并使用自己的 Scout 执行。", properties: { project_id: { type: "string" }, email: { type: "string" } }, required: ["project_id", "email"] },
    { name: "project_member_remove", title: "移除项目成员", description: "所有者移除成员，撤销其项目访问权限。", properties: { project_id: { type: "string" }, user_id: { type: "string" } }, required: ["project_id", "user_id"] },
    { name: "task_list", title: "任务列表", description: "查询当前账号的持久任务摘要。", properties: { app_id: { type: "string" }, project_id: { type: "string" }, status: { type: "string" }, limit: { type: "integer", minimum: 1, maximum: 100 }, offset: { type: "integer", minimum: 0 } }, required: [] },
    { name: "task_cancel", title: "取消任务", description: "持久保存取消意图；仅在 Scout 确认停止后任务才显示 cancelled。", properties: { task_id: { type: "string" } }, required: ["task_id"] },
    { name: "session_list", title: "执行记录列表", description: "查询当前账号的记录索引；完整证据保存在 Scout。", properties: { run_id: { type: "string" }, app_id: { type: "string" }, case_id: { type: "string" }, limit: { type: "integer", minimum: 1, maximum: 100 } }, required: [] },
    { name: "session_record", title: "执行记录摘要", description: "经已认证节点连接读取 Scout 上的实时记录摘要。", properties: { session_id: { type: "string" } }, required: ["session_id"] },
    { name: "session_events", title: "执行证据", description: "读取 Scout 本地事件。from_seq 是排他游标，下一页使用 next_seq；完整大节点通过 session_artifact 读取；节点离线明确返回证据暂不可用。", properties: { session_id: { type: "string" }, from_seq: { type: "integer", minimum: 0 }, limit: { type: "integer", minimum: 1, maximum: 200 }, max_bytes: { type: "integer", minimum: 1024, maximum: 196608, default: 196608 } }, required: ["session_id"] },
    { name: "session_artifact", title: "完整证据分块", description: "读取已有证据的一个分块。大节点树从事件的 artifact_id 获取；下一块 offset=offset+bytes_read，直到 eof。UTF-8 完整时附 text，否则读取 data_base64。", properties: { session_id: { type: "string" }, artifact_id: { type: "string" }, offset: { type: "integer", minimum: 0, default: 0 }, length: { type: "integer", minimum: 1, maximum: 65536, default: 32768 } }, required: ["session_id", "artifact_id"] },
    { name: "session_screenshot", title: "截图证据", description: "读取已有截图的元数据和需登录的下载地址，不重新操作设备。用 session_events 找 seq 或 artifact_id。", properties: { session_id: { type: "string" }, seq: { type: "integer", minimum: 1 }, artifact_id: { type: "string" } }, required: ["session_id"] },
    { name: "resource_env_get", title: "项目环境配置", description: "读取项目在 Nexus 保存的环境、渠道、登录策略、验证码策略和自动新增账号序列。", properties: { project_id: { type: "string" } }, required: ["project_id"] },
    { name: "resource_env_set", title: "保存项目环境配置", description: "保存项目在 Nexus 的环境配置。登录方式、验证码策略、Gmail 收件箱和手机号/邮箱别名序列放在这里，不写入脚本仓；号池为空或没有匹配账号时 Nexus 按这里的策略自动补号。可只传要修改的顶层字段。", properties: { project_id: { type: "string" }, env: { type: "object", description: "要合并保存的项目环境字段", additionalProperties: true } }, required: ["project_id", "env"] },
    { name: "resource_account_list", title: "测试账号池", description: "查询当前项目的测试账号，不返回密码和验证码。", properties: { project_id: { type: "string" }, env: { type: "string" } }, required: ["project_id"] },
    { name: "resource_account_create", title: "新增测试账号", description: "向当前项目的 Nexus 测试账号池新增账号。环境与登录身份用于租用匹配。", properties: { project_id: { type: "string" }, env: { type: "string" }, display_name: { type: "string" }, phone: { type: "string" }, email: { type: "string" }, username: { type: "string" }, password: { type: "string" }, otp: { type: "string" }, kind: { type: "string" }, profile_id: { type: "string" }, note: { type: "string" }, facets: { type: "object", additionalProperties: true }, locked: { type: "boolean" } }, required: ["project_id"] },
    { name: "resource_account_update", title: "修改测试账号", description: "修改 Nexus 测试账号池中的账号；仅传要修改的字段。", properties: { project_id: { type: "string" }, account_id: { type: "string" }, env: { type: "string" }, display_name: { type: "string" }, phone: { type: "string" }, email: { type: "string" }, username: { type: "string" }, password: { type: "string" }, otp: { type: "string" }, kind: { type: "string" }, profile_id: { type: "string" }, note: { type: "string" }, facets: { type: "object", additionalProperties: true }, locked: { type: "boolean" } }, required: ["project_id", "account_id"] },
    { name: "resource_account_delete", title: "删除测试账号", description: "删除 Nexus 测试账号池中的空闲账号；被任务租用的账号不能删除。", properties: { project_id: { type: "string" }, account_id: { type: "string" } }, required: ["project_id", "account_id"] },
    { name: "resource_account_import", title: "批量导入测试账号", description: "批量导入 Nexus 测试账号。preview=true 时只返回匹配与变更预览，不写入。", properties: { project_id: { type: "string" }, rows: { type: "array", items: { type: "object", additionalProperties: true } }, default_env: { type: "string" }, on_duplicate: { type: "string", enum: ["skip", "merge", "overwrite_credentials"] }, preview: { type: "boolean" } }, required: ["project_id", "rows"] },
  ].map(tool => ({ name: tool.name, title: tool.title, description: tool.description,
    inputSchema: { type: "object", additionalProperties: false, properties: tool.properties, required: tool.required },
    annotations: {
      readOnlyHint: !["project_member_add", "project_member_remove", "task_cancel", "resource_env_set", "resource_account_create", "resource_account_update", "resource_account_delete", "resource_account_import"].includes(tool.name),
      destructiveHint: ["resource_account_delete", "project_member_remove"].includes(tool.name),
      idempotentHint: !["task_cancel", "resource_account_create", "resource_account_delete", "resource_account_import"].includes(tool.name),
      openWorldHint: false,
    } })),
] as const;

function rpcResult(id: JsonRpcId | undefined, result: unknown): Response {
  return Response.json({ jsonrpc: "2.0", id: id ?? null, result });
}

function rpcError(id: JsonRpcId | undefined, code: number, message: string): Response {
  return Response.json({
    jsonrpc: "2.0",
    id: id ?? null,
    error: { code, message },
  });
}

function unauthorized(request: Request, invalid: boolean): Response {
  const origin = publicOrigin(new URL(request.url).origin);
  const metadata = origin + "/.well-known/oauth-protected-resource/mcp";
  let challenge = `Bearer resource_metadata="${metadata}", scope="mcp"`;
  let detail = "需要授权";
  if (invalid) {
    challenge += ', error="invalid_token", error_description="The access token is invalid or expired"';
    detail = "令牌无效或已过期";
  }
  return Response.json(
    { detail },
    {
      status: 401,
      headers: { "WWW-Authenticate": challenge, "Cache-Control": "no-store" },
    },
  );
}

async function authenticate(
  request: Request,
  env: Env,
): Promise<{ userId: string } | Response> {
  const raw = request.headers.get("authorization") || "";
  const token = raw.toLowerCase().startsWith("bearer ") ? raw.slice(7).trim() : "";
  if (!token) return unauthorized(request, false);

  // Keep shared bearer for smoke tests only (tools/list/initialize).
  if (env.MCP_BEARER_TOKEN && token === env.MCP_BEARER_TOKEN) {
    return { userId: "system:probe" };
  }

  if (token.startsWith(ACCESS_PREFIX)) {
    try {
      const uid = await resolveAccessToken(
        env.DB,
        token,
        canonicalResource(new URL(request.url).origin),
      );
      return uid ? { userId: uid } : unauthorized(request, true);
    } catch (exc) {
      if (exc instanceof OAuthPage) return unauthorized(request, true);
      throw exc;
    }
  }

  if (token.startsWith("mino_mcp_")) {
    const uid = await resolveMcpToken(env.DB, token);
    return uid ? { userId: uid } : unauthorized(request, true);
  }

  return unauthorized(request, true);
}

function agentGuidePayload(): Record<string, unknown> {
  // Match mino_nexus.assistant.agent_guide.agent_guide()
  return {
    channel: "Mino MCP。工具名里的点写成下划线，例如 agent.guide 调用为 agent_guide。",
    first_call: "agent_guide",
    boundaries: [
      "脚本文本只由 agent 写在本机脚本仓并通过 Git 推送；MCP 只做查询、同步元数据与触发执行，不写代码。各仓库分工与 Scout 安装见 agent 技能 overview。",
      "改了 config.yaml 的 apps 后必须 push 并调用 repo_apps_sync（只传 platform 与包名/URL，不要传 otp；可不传 app_id，Nexus 按 key 对齐或自动创建应用）；选应用前可先调 repo_apps_get。Nexus 不保存 Git 令牌，也不拉取脚本仓库。",
      "开跑后整份脚本在 Scout 执行。账号、步骤和失败直接写入 Nexus。登录方式、验证码策略、Gmail 收件箱和自动新增序号由 Nexus 项目环境配置下发；号池为空或没有匹配账号时由 Nexus 自动补号。脚本仓 config.yaml 只保留应用 key、platform、package/bundle_id/url；otp.code 仅兼容旧仓库，不应作为新项目配置。模型在 Scout 本机完成。脚本里不要自己连接 adb、WDA、Playwright 或 Appium。",
    ],
    workbench: {
      url: "https://workbench.minocc.top",
      login_failure: "无法连接服务器",
    },
    flows: [
      {
        name: "安装或卸载本机 Scout",
        tools: ["scout_install_token", "scout_install_status", "scout_list", "scout_uninstall"],
        then: "安装先 scout_install_token 获取命令，再在本机执行匹配系统的命令，遵循客户端的本机执行权限，不要把 token 写进对话。用户要求卸载时调用 scout_uninstall 删除绑定，再执行返回的本机卸载命令。Nexus 不追加确认。",
      },
      { name: "确认能操作哪些项目", tools: ["project_list"] },
      {
        name: "新建项目或删除误建应用",
        tools: ["project_list", "project_create", "project_delete", "app_delete"],
        then: "参数齐全且用户已要求该操作时直接调用工具。Nexus 不追加确认；参数缺失时补齐参数，删除须对应用户明确要求的对象。",
      },
      {
        name: "编写或修改用例脚本",
        tools: [
          "project_list",
          "case_list",
          "device_list",
          "case_run",
          "task_get",
          "session_events",
          "session_screenshot",
        ],
        then: "使用 write-test-cases。先选一台 executable 设备。",
      },
      {
        name: "按需求链接编写并执行脚本",
        tools: ["case_list", "device_list", "case_run", "task_get"],
        then: "case_list 返回需求 id、名称和链接。从链接读用例，写成 tests/<需求ID>/<用例ID>.py 并 push。开跑前 plugin_status，缺的渠道程序先装完。case_run 传 script_targets。执行与汇报使用 agent 技能 run-test-cases。",
      },
      {
        name: "让节点安装渠道程序",
        tools: ["node_channel_program"],
        then: "只转发给该节点下载中心已有的安装请求。Nexus 不下载安装包，也不接受临时地址。",
      },
      {
        name: "查看一次执行",
        tools: ["task_list", "task_get", "session_list", "session_record", "session_events", "session_artifact", "session_screenshot"],
      },
      {
        name: "看资源够不够",
        tools: ["resource_overview", "resource_device_list", "resource_account_list"],
      },
    ],
  };
}

function mapDevices(raw: unknown[]): Record<string, unknown>[] {
  return raw.map((row) => {
    const d = (row || {}) as Record<string, unknown>;
    const online = Boolean(d.online);
    return {
      sn: String(d.device_id || ""),
      device_id: d.device_id,
      scout_id: d.scout_id,
      node_id: d.scout_id,
      platform: d.platform || undefined,
      status: online ? "online" : "offline",
      ready: Boolean(d.ready),
      executable: online && Boolean(d.ready),
    };
  });
}

function mapScouts(raw: unknown[], userId: string): Record<string, unknown>[] {
  return (raw as Array<Record<string, unknown>>)
    .filter((s) => !s.owner_user_id || s.owner_user_id === userId || userId === "system:probe")
    .map((s) => ({
      node_id: s.scout_id,
      scout_id: s.scout_id,
      online: Boolean(s.online),
      scout_version: s.scout_version || undefined,
      hostname:
        (s.host && typeof s.host === "object"
          ? (s.host as Record<string, unknown>).hostname
          : undefined) || s.scout_id,
      owner_user_id: s.owner_user_id || undefined,
      plugins_ready: s.plugins_ready,
      tool_tasks_version: Number(s.tool_tasks_version || 0),
      record_api_version: Number(s.record_api_version || 0),
    }));
}

async function callHttpTool(
  env: Env,
  userId: string,
  method: string,
  path: string,
  body?: Record<string, unknown>,
): Promise<{ ok: boolean; status: number; summary: string; data: unknown }> {
  const url = new URL(path, "https://nexus.minocc.top");
  const req = new Request(url.toString(), {
    method,
    headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const handled = await handlePluginRoutes(req, env, url.pathname, method, userId);
  if (!handled) {
    return { ok: false, status: 404, summary: "没有这个工具路由", data: {} };
  }
  const json = (await handled.json()) as {
    ok?: boolean;
    code?: number;
    msg?: string;
    data?: unknown;
  };
  return {
    ok: Boolean(json.ok) && handled.status < 400,
    status: Number(json.code || handled.status),
    summary: String(json.msg || ""),
    data: json.data,
  };
}

async function callTool(
  env: Env,
  userId: string,
  name: string,
  args: Record<string, unknown>,
  mutationScope: string,
  waitUntil: (promise: Promise<unknown>) => void,
): Promise<{ isError: boolean; content: Array<{ type: string; text: string }> }> {
  const pack = (ok: boolean, summary: string, data: unknown, status = 0) => {
    const body: Record<string, unknown> = { ok, summary, data };
    if (!ok && status) {
      const code =
        status === 401
          ? "auth_required"
          : status === 403
            ? "forbidden"
            : status === 404
              ? "not_found"
              : status === 409
                ? "conflict"
                : status === 422 || status === 400
                  ? "invalid_argument"
                  : status === 501
                    ? "tool_failed"
                    : "tool_failed";
      body.code = code;
      body.retryable = code === "conflict";
    }
    return {
      isError: !ok,
      content: [{ type: "text", text: JSON.stringify(body) }],
    };
  };

  if (userId === "system:probe") {
    return pack(false, "probe token 仅允许 initialize/ping/tools/list", {}, 403);
  }

  if (name === "agent_guide") {
    return pack(true, "", agentGuidePayload());
  }

  if (name.startsWith("project_member_")) {
    try {
      const project = String(args.project_id || "");
      if (name === "project_member_list") return pack(true, "", await listMembers(env.DB,project,userId));
      if (name === "project_member_add") return pack(true, "成员已添加", await addMember(env.DB,project,userId,String(args.email || "")));
      if (name === "project_member_remove") return pack(true, "成员已移除", await removeMember(env.DB,project,userId,String(args.user_id || "")));
    } catch (error) { if (error instanceof ProjectAccessError) return pack(false,error.message,{},error.http); throw error; }
  }
  if (name === "project_list") {
    return pack(true, "", await listProjects(env.DB, userId));
  }

  if (name === "scout_list") {
    return pack(true, "", {
      nodes: mapScouts(await listScouts(env.DB), userId),
    });
  }

  if (name === "scout_install_token") {
    const issued = await issueInstallToken(env.DB, userId, "https://nexus.minocc.top");
    return pack(true, "已签发安装命令", issued);
  }

  if (name === "scout_install_status") {
    const installId = String(args.install_id || "").trim();
    if (!installId) return pack(false, "缺少 install_id", {}, 422);
    const status = await getInstallStatus(env.DB, installId, userId);
    if (!status) return pack(false, "没有这次安装", {}, 404);
    return pack(true, "", status);
  }

  if (name === "scout_uninstall") {
    const nodeId = String(args.node_id || "").trim();
    if (!nodeId) return pack(false, "缺少 node_id", {}, 422);
    const unbound = await unbindScout(env.DB, userId, nodeId);
    if (!unbound) return pack(false, "没有这个节点", {}, 404);
    try {
      await hubStub(env).fetch(
        new Request("https://hub/detach", {
          method: "POST",
          body: JSON.stringify({ scout_id: nodeId }),
        }),
      );
    } catch {
      /* binding is already gone */
    }
    return pack(true, "已解除绑定", unbound);
  }

  if (name === "device_list") {
    const onlyOnline = args.only_online !== false;
    try { return pack(true, "", await deviceAvailability(env, userId, { only_online: onlyOnline, app_id: String(args.app_id || "") })); }
    catch (error) { if (error instanceof TaskError) return pack(false, error.message, { reason_code: error.code }, error.http); throw error; }
  }

  if (name === "app_create") {
    const projectId = String(args.project_id || "").trim();
    const appName = String(args.name || "").trim();
    const platforms = String(args.platforms || "").trim();
    const description = String(args.description || "").trim();
    if (!projectId) return pack(false, "缺少项目", {}, 422);
    if (!appName || appName.length > 200) return pack(false, "请填写 1 到 200 字的应用名称", {}, 422);
    if (!platforms) return pack(false, "请填写平台", {}, 422);
    try {
      const app = await createApp(
        env.DB,
        userId,
        projectId,
        appName,
        platforms,
        description,
      );
      return pack(true, "已创建", app);
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "创建失败", {}, 400);
    }
  }

  if (name === "project_create") {
    const projectName = String(args.name || "").trim();
    const description = String(args.description || "").trim();
    if (!projectName || projectName.length > 200) return pack(false, "请填写 1 到 200 字的项目名称", {}, 422);
    if (description.length > 2000) return pack(false, "说明最长 2000 字", {}, 422);
    try {
      const project = await createProject(env.DB, userId, projectName, description);
      return pack(true, "已创建", project);
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "创建失败", {}, 400);
    }
  }

  if (name === "app_delete") {
    const appId = String(args.app_id || "").trim();
    if (!appId) return pack(false, "缺少 app_id", {}, 422);
    const deleted = await deleteApp(env.DB, userId, appId);
    if (!deleted) return pack(false, "没有这个应用", {}, 404);
    return pack(true, "已删除", deleted);
  }

  if (name === "project_delete") {
    const projectId = String(args.project_id || "").trim();
    if (!projectId) return pack(false, "缺少 project_id", {}, 422);
    const deleted = await deleteProject(env.DB, userId, projectId);
    if (!deleted) return pack(false, "没有这个项目", {}, 404);
    return pack(true, "已删除", deleted);
  }

  if (name === "plugin_status") {
    const qs = args.node_id
      ? `?node_id=${encodeURIComponent(String(args.node_id))}`
      : "";
    const out = await callHttpTool(env, userId, "GET", `/runtime/plugins/status${qs}`);
    return pack(out.ok, out.summary, out.data, out.status);
  }

  if (name === "node_channel_program") {
    const operation_id =
      String(args.operation_id || "").trim() ||
      (mutationScope
        ? [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mutationScope)))]
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("")
            .slice(0, 32)
        : "");
    const out = await callHttpTool(env, userId, "POST", "/runtime/channel-programs/install", {
      node_id: args.node_id,
      tool_id: args.tool_id,
      action: args.action || "install",
      operation_id,
    });
    return pack(out.ok, out.summary, out.data, out.status);
  }

  if (name === "plugin_install") {
    const operation_id =
      String(args.operation_id || "").trim() ||
      (mutationScope
        ? [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(mutationScope)))]
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("")
            .slice(0, 32)
        : "");
    const out = await callHttpTool(env, userId, "POST", "/runtime/plugins/install", {
      node_id: args.node_id,
      plugin: args.plugin,
      action: args.action || "install",
      operation_id,
      wait_ms: args.wait_ms || 0,
    });
    return pack(out.ok, out.summary, out.data, out.status);
  }

  if (name === "requirement_save" || name === "requirement_update") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    const { saveRequirement, updateRequirement } = await import("./requirements");
    try {
      const input: import("./requirements").RequirementInput = { project_id, requirement_id: String(args.requirement_id || "") };
      for (const key of ["title", "source_url", "cases_url", "app_id", "new_requirement_id"] as const) {
        if (args[key] !== undefined) input[key] = String(args[key]);
      }
      if (args.app_ids !== undefined) input.app_ids = args.app_ids;
      const requirement = await (name === "requirement_update" ? updateRequirement : saveRequirement)(env.DB, input);
      return pack(true, name === "requirement_update" ? "需求已修改" : "需求已保存", { requirement, ...(requirement.id !== input.requirement_id ? { script_directory_rename: { from: `tests/${input.requirement_id}/`, to: `tests/${requirement.id}/` } } : {}) });
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "保存失败", {}, 422);
    }
  }

  if (name === "case_list" || name === "requirement_list") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    const { listRequirements } = await import("./requirements");
    const requirements = await listRequirements(env.DB, project_id);
    return pack(true, "", { requirements, items: requirements, count: requirements.length, total: requirements.length });
  }

  if (name === "project_script_repo_set") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    try {
      const saved = await saveRepo(
        env.DB,
        project_id,
        String(args.url || ""),
        String(args.branch || ""),
      );
      return pack(true, "脚本仓库已保存", {
        url: saved.public_url,
        branch: saved.branch,
        updated_at: saved.updated_at,
      });
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "保存失败", {}, 422);
    }
  }

  if (name === "repo_apps_get") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    return pack(true, "", await getRepoApps(env.DB, project_id));
  }

  if (name === "repo_apps_sync") {
    const project_id = String(args.project_id || "").trim();
    if (!project_id) return pack(false, "缺少 project_id", {}, 422);
    if (!(await requireOwnedProject(env.DB, project_id, userId))) {
      return pack(false, "没有这个项目", {}, 404);
    }
    try {
      const doc = await saveRepoApps(env.DB, {
        project_id,
        revision: args.revision,
        branch: args.branch,
        apps: args.apps,
        remove: args.remove,
        synced_by: `mcp:${userId}`,
        owner_user_id: userId,
      });
      return pack(true, "已同步", doc);
    } catch (err) {
      return pack(false, err instanceof Error ? err.message : "同步失败", {}, 422);
    }
  }

  try {
    if (name === "task_list") return pack(true, "", await listRuns(env.DB, userId, { app_id: String(args.app_id || ""), project_id: String(args.project_id || ""), status: String(args.status || ""), limit: Number(args.limit || 30), offset: Number(args.offset || 0) }));
    if (name === "task_cancel") return pack(true, "已记录取消请求", toTaskJson(await cancelTask(env, userId, String(args.task_id || ""))));
    if (name === "session_list") return pack(true, "", await listSessions(env, userId, { run_id: String(args.run_id || ""), app_id: String(args.app_id || ""), case_id: String(args.case_id || ""), limit: Number(args.limit || 30) }));
    if (name === "session_record") return pack(true, "", await readRecord(env, userId, String(args.session_id || ""), "record.status"));
    if (name === "session_events") return pack(true, "", await readEvents(env, userId, String(args.session_id || ""), Number(args.from_seq || 0), Number(args.limit || 100), Math.min(196608, Number(args.max_bytes || 196608))));
    if (name === "session_artifact") return pack(true, "", await artifactChunk(env, userId, String(args.session_id || ""), String(args.artifact_id || ""), Number(args.offset ?? 0), Number(args.length ?? 32768)));
    if (name === "session_screenshot") return pack(true, "", await screenshotMetadata(env, userId, String(args.session_id || ""), Number(args.seq || 0), String(args.artifact_id || "")));
    if (name.startsWith("resource_")) {
      const project_id = String(args.project_id || "").trim();
      if (!project_id) return pack(false, "缺少 project_id", {}, 422);
      if (name === "resource_env_get") return pack(true, "", await getProjectEnv(env.DB, userId, project_id));
      if (name === "resource_env_set") {
        if (!args.env || typeof args.env !== "object" || Array.isArray(args.env)) return pack(false, "env 必须是对象", {}, 422);
        return pack(true, "项目环境配置已保存", await saveProjectEnv(env.DB, userId, project_id, args.env));
      }
      if (name === "resource_account_list") return pack(true, "", await listAccounts(env.DB, userId, project_id, String(args.env || "")));
      if (name === "resource_account_create") return pack(true, "测试账号已新增", { account: await saveAccount(env.DB, userId, project_id, args) });
      if (name === "resource_account_update") {
        const account_id = String(args.account_id || "").trim();
        if (!account_id) return pack(false, "缺少 account_id", {}, 422);
        return pack(true, "测试账号已修改", { account: await saveAccount(env.DB, userId, project_id, args, account_id) });
      }
      if (name === "resource_account_delete") {
        const account_id = String(args.account_id || "").trim();
        if (!account_id) return pack(false, "缺少 account_id", {}, 422);
        await deleteAccount(env.DB, userId, project_id, account_id);
        return pack(true, "测试账号已删除", { account_id });
      }
      if (name === "resource_account_import") return pack(true, "", await importAccounts(env.DB, userId, project_id, args));
    }
  } catch (error) {
    if (error instanceof ProjectAccessError) return pack(false,error.message,{},error.http);
    if (error instanceof TaskError || error instanceof AccountError) return pack(false, error.message, { reason_code: error.code }, error.http);
    throw error;
  }

  if (name === "task_get") {
    const task_id = String(args.task_id || "").trim();
    if (!task_id) return pack(false, "缺少 task_id", {}, 422);
    const doc = await getRun(env.DB, task_id);
    if (!doc || !await canReadRun(env.DB, userId, doc)) return pack(false, "没有这个任务", {}, 404);
    return pack(true, "", toTaskJson(doc));
  }

  if (name === "case_run") {
    const out = await startRun(env, {
      userId,
      app_id: String(args.app_id || ""),
      sn: String(args.sn || ""),
      case_ids: args.case_ids,
      script_targets: args.script_targets,
      revision: String(args.revision || ""),
      env_profile: String(args.env_profile || ""),
      env_surface: String(args.env_surface || ""),
      project_id: String(args.project_id || ""),
      client_request_id: String(args.client_request_id || ""),
      waitUntil,
    });
    if (!out.ok) {
      return pack(false, out.msg, { reason_code: out.reason_code }, out.status);
    }
    return pack(true, out.reused ? "沿用已有任务" : "已开始执行", {
      task_id: out.task_id,
      run_id: out.run_id,
      id: out.run_id,
      status: out.status,
      total: out.total,
      sn: out.sn,
      report_url: (await getRun(env.DB, out.run_id))?.report_url || "",
    });
  }

  return pack(false, `没有这个工具：${name}`, {});
}

export async function handleMcp(
  request: Request,
  env: Env,
  ctx?: ExecutionContext,
): Promise<Response> {
  if (request.method === "GET") {
    const auth = await authenticate(request, env);
    if (auth instanceof Response) return auth;
    return new Response(null, { status: 405, headers: { Allow: "POST" } });
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  const auth = await authenticate(request, env);
  if (auth instanceof Response) return auth;
  const { userId } = auth;

  const headerProtocol = request.headers.get("mcp-protocol-version") || "";
  if (headerProtocol && !PROTOCOLS.has(headerProtocol)) {
    return Response.json({ detail: `仅支持 MCP ${[...PROTOCOLS].join("、")}` }, { status: 400 });
  }
  const protocol = PROTOCOLS.has(headerProtocol) ? headerProtocol : PROTOCOL;

  let body: JsonRpcRequest | JsonRpcRequest[];
  try {
    body = (await request.json()) as JsonRpcRequest | JsonRpcRequest[];
  } catch {
    return Response.json(
      { jsonrpc: "2.0", id: null, error: { code: -32700, message: "不是合法 JSON" } },
      { status: 400 },
    );
  }

  const items = Array.isArray(body) ? body : [body];
  const out: unknown[] = [];

  for (const msg of items) {
    if (!msg || typeof msg !== "object") {
      out.push({ jsonrpc: "2.0", id: null, error: { code: -32600, message: "无效请求" } });
      continue;
    }
    const method = String(msg.method || "");
    const id = msg.id;
    const probe = userId === "system:probe";

    // Responses from confirmation requests sent before a deployment are no
    // longer actionable. Acknowledge them without replaying any tool call.
    if (!method && msg.id != null && (msg.result !== undefined || msg.error !== undefined)) continue;

    if (probe && !["initialize", "ping", "tools/list"].includes(method)) {
      out.push({
        jsonrpc: "2.0",
        id: id ?? null,
        error: { code: -32601, message: "probe token 仅允许 initialize/ping/tools/list" },
      });
      continue;
    }

    if (method === "initialize") {
      const offered = String(msg.params?.protocolVersion || "");
      out.push({
        jsonrpc: "2.0",
        id: id ?? null,
        result: {
          protocolVersion: PROTOCOLS.has(offered) ? offered : protocol,
          capabilities: { tools: {} },
          serverInfo: { name: "mino-nexus", version: "0.0.1-cloudflare" },
        },
      });
      continue;
    }
    if (method === "notifications/initialized") continue;
    if (method === "ping") {
      out.push({ jsonrpc: "2.0", id: id ?? null, result: {} });
      continue;
    }
    if (method === "tools/list") {
      out.push({ jsonrpc: "2.0", id: id ?? null, result: { tools: TOOLS } });
      continue;
    }
    if (method === "tools/call") {
      const name = String(msg.params?.name || "");
      const args = (msg.params?.arguments || {}) as Record<string, unknown>;
      const token = (request.headers.get("authorization") || "").slice(7);
      const scopeBuf = await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(token + ":" + String(id ?? "")),
      );
      const scope = [...new Uint8Array(scopeBuf)]
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
      const waitUntil = (promise: Promise<unknown>) =>
        ctx ? ctx.waitUntil(promise) : void promise.catch(() => null);
      const result = await callTool(env, userId, name, args, `mcp:${scope}:${id}`, waitUntil);
      out.push({ jsonrpc: "2.0", id: id ?? null, result });
      continue;
    }
    out.push({
      jsonrpc: "2.0",
      id: id ?? null,
      error: { code: -32601, message: `不支持的方法 ${method}` },
    });
  }

  if (!out.length) return new Response(null, { status: 202, headers: { "MCP-Protocol-Version": protocol } });
  const response = Response.json(Array.isArray(body) ? out : out[0]);
  response.headers.set("MCP-Protocol-Version", protocol);
  return response;
}
