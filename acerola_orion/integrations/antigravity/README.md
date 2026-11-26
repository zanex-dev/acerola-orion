# Acerola-Orion · Antigravity 配套 Skills、Plugins & Hooks 套件

本目录内置于 `acerola-orion` 公开包（`acerola_orion/integrations/antigravity/`），提供与 **Acerola-Orion** 桌面星盘、EOQ 上下文水位守卫、任务拆解看板、对话防误删保险箱（Orion-Vault）及版本冷库（Version-Vault）端到端联动的 **纯白帽 Antigravity 技能、插件与生命周期钩子模板**。

可通过 `python -m acerola_orion companion` 一键装配至本机 `~/.gemini/config/` 与目标工程工作区，也可参考下方映射手动按需部署。

## 1. 目录结构与核心模块映射

| 目录 / 文件 | 对应 `acerola_orion` 模块 | 作用说明 |
|---|---|---|
| `hooks/eoq_guard_config.json` | `plugins/eoq_hook.py` + `plugins/eoq_plugin.py` | EOQ 上下文水位阈值、豁免词、锁控工具与放行目录默认配置 |
| `hooks/hooks.template.json` | `plugins/eoq_hook.py` + `vault_hook.py` | `eoq-context-guard`（`PreInvocation` / `PreToolUse` / `PostToolUse`）与 `orion-chat-vault`（`Stop`）钩子模板 |
| `hooks/docs/eoq_context_guard_cheatsheet.md` | `plugins/eoq_hook.py` + `cache_radar.py` | EOQ 上下文约束系统运维、逃生口令与配置项速查表 |
| `workspace-plugins/eoq-task-relay/` | `plugins/tasks_plugin.py` | 提供 `task-decomposer` 与 `stage-handoff` 技能，对接任务看板一键启动口令与阶段交接单 |
| `workspace-plugins/engineering-core/` | `plugins/version_vault_plugin.py` | 提供 `version-seal` 旧版本 SHA-256 双向校验外迁技能与配套代码工程准则 `AGENTS.md` |
| `global-plugins/acerola-orion/` | `acerola_orion/*` | 全局插件，内置 `acerola-orion`（配额/雷达/账本/审计/冷库/守卫/面板）与 `orion-vault`（对话防删保险箱）双技能 |
| `global-skills/session-recall/` | 冷窗急救与跨会话历史回溯 | 跨窗口靶词检索与局部切片读取脚本 `recall.py`（零第三方依赖纯 `stdlib`） |
| `global-skills/kv-keepalive/` | `cache_radar.py` | 配合 250s KV 缓存余温雷达的 195s 短时限次自动续杯技能 |

## 2. 目标环境安放位置参考

- **生命周期配置与钩子**：
  - `hooks/eoq_guard_config.json` -> `~/.gemini/config/eoq_guard_config.json`
  - `hooks/hooks.template.json` -> 将 `<PYTHON_EXE>` 替换为已安装 `acerola-orion` 的 Python 解释器路径后并入 `~/.gemini/config/hooks.json`
- **全局插件与全局技能**：
  - `global-plugins/acerola-orion/` -> `~/.gemini/config/plugins/acerola-orion/`
  - `global-skills/session-recall/` -> `~/.gemini/config/skills/session-recall/`
  - `global-skills/kv-keepalive/` -> `~/.gemini/config/skills/kv-keepalive/`
- **工程工作区按需插件**：
  - `workspace-plugins/eoq-task-relay/` -> `<project_workspace>/.agents/plugins/eoq-task-relay/`
  - `workspace-plugins/engineering-core/` -> `<project_workspace>/.agents/plugins/engineering-core/`

## 3. 隐私与洁净保证

- 全套模板与脚本均为零网络外联纯本地工具，严格遵循媒体与个人目录物理隔离原则；
- 不包含任何运行时状态缓存、本地凭据或硬编码个人路径（文档均采用 `%USERPROFILE%`、`~` 或 `<WORKSPACE>` 占位符）。
