# C 档 EOQ 上下文约束系统 · 运维与开关速查手册

> **核心原则**：冷窗禁重吞（`>250s` 且 `≥30K` 在调模型前 0-Token 拦截并自动生成急救草稿）、热窗按 EOQ 甜点区（`95K~120K`）主动落盘 `≤1.5K` 交接单切窗、逼近 `200K` 双倍计价墙前通过 `PreToolUse` 锁定增肥工具防暴走（用户在弹窗点允许后经 `PostToolUse` 自动解锁本窗）。

---

## 1. 组件拓扑与关键文件

| 组件 | 路径 / 模块 | 职责 |
|---|---|---|
| 统一阈值真源 | `%USERPROFILE%\.gemini\config\eoq_guard_config.json` | 全部阈值、总开关、豁免词、锁控工具列表 |
| Hook 挂载配置 | `%USERPROFILE%\.gemini\config\hooks.json` | 挂载 `eoq-context-guard`（`PreInvocation` + 7 工具 `PreToolUse` / `PostToolUse`） |
| 生命周期引擎 | `python -m acerola_orion.plugins.eoq_hook` | 纯 stdlib，冷窗熔断、单次里程表、180K 增肥锁、弹窗放行自动解锁、0-Token 急救草稿 |
| 运行时状态缓存 | `%USERPROFILE%\.gemini\config\.eoq_guard_state.json` | 记录各会话峰值水位与已提醒档位（7 天自动过期清理） |
| 按需交接单技能 | `<WORKSPACE>\.agents\plugins\eoq-task-relay\skills\stage-handoff\SKILL.md` | 仅在收到收口提示或口令时激活，生成 `≤1.5K` 交接单落盘至 `.agents/checkpoints/` |
| 桌面缓存雷达 | `acerola_orion/cache_radar.py` | `30K` 冷窗警戒线、`95K~120K` 热窗 EOQ 建议收口带、`180K/200K` 2x 计价警报 |

### 标准 Hook 命令速查

- 调用前检查：`<PYTHON_EXE> -m acerola_orion.plugins.eoq_hook --event PreInvocation`
- 工具前锁控：`<PYTHON_EXE> -m acerola_orion.plugins.eoq_hook --event PreToolUse`
- 工具后联动：`<PYTHON_EXE> -m acerola_orion.plugins.eoq_hook --event PostToolUse`

---

## 2. 日常交互口令与逃生舱

| 场景 | 发送内容 | 系统行为 |
|---|---|---|
| 冷窗被拦截，但**确需在原冷窗强行继续** | 在消息末尾加 `!go` | 单次放行本次请求（跳过冷窗拦截） |
| **本窗免限·一直做完（不想被 200K/弹窗打扰）** | **任选其一**：① 180K 弹窗直接点**「允许」**；② 点聊天提示条 `[🔓 本窗免限·一直做完]`；③ 点 `Acerola-Orion` 雷达栏/悬浮胶囊右键 **`🔓 本窗免限`**；④ 消息带 `!unlock` / `!一直做` / `!免限` / `!force` / `解锁本窗` | **仅解锁当前窗口**（其他窗口仍受 EOQ 保护）：后续工具不再弹窗、跳过冷窗拦截与水位收口，并自动撤销此前收口指令让 AI 连续做到完成 |
| 恢复当前窗口 EOQ 保护 | 发送 `!lock` / `锁定本窗`，或再次点击面板/提示条恢复按钮 | 立即重新启用本窗冷窗拦截、水位提醒与 180K/200K 悬崖锁 |
| 主动压缩当前长窗 | `/compact` | 自动放行；压缩后水位下降 `≥40K` 时自动重置里程表档位 |
| 给热窗做短时 KV 保活 | `保活` / `续杯` / `取消保活` | 豁免冷窗熔断与热窗提醒 |
| 热窗主动落盘交接单切新窗 | `写交接单` / `落盘交接` / `切窗收口` | 激活 `stage-handoff`，落盘 `.agents/checkpoints/handoff_*.md` 并给新窗启动句 |
| 冷窗被拦截后零 Token 接力 | 直接复制拦截提示里的「新窗粘贴指令」开新窗 | 新窗以 `~18K` 轻载读 `cold_rescue_*.md` 接力（0 重烧旧窗） |
| `≥180K` 触发 `PreToolUse` 弹窗锁定 | 点**允许**直接解锁本窗一路做完；不想跨线点**拒绝**让模型直接落盘交接单 | 写入 `.agents/checkpoints/` 或 `.gemini/antigravity/brain/` 永远自动放行，不会死锁 |

---

## 3. 配置项速查（`eoq_guard_config.json`）

| 键名 | 默认值 | 作用说明 |
|---|---|---|
| `enabled` | `true` | **一键总开关**：改为 `false` 立即全量放行（无需重启 IDE） |
| `cold_ttl_sec` | `250` | 冷窗判定阈值（秒，对齐实测 4 分 10 秒安全线与 `CacheRadar`） |
| `cold_block_tokens` | `30000` | 冷窗拦截水位线（`prompt_tokens = in + cache`，免疫思考输出虚高） |
| `cold_auto_rescue_draft` | `true` | 冷窗拦截时是否自动落盘 0-Token 本地急救草稿 `cold_rescue_*.md` |
| `hot_nudge_tokens` | `95000` | 热窗 EOQ 甜点区收口提醒线（每会话单次触发，`/compact` 后重置） |
| `cliff_warn_tokens` | `175000` | 逼近 200K 双倍计价墙的紧急收口提醒线（每会话单次触发） |
| `cliff_tool_lock_tokens` | `180000` | `PreToolUse` 增肥工具弹窗锁定水位线（对齐 `CacheRadar` `near_2x`） |
| `cliff_allow_unlocks_window` | `true` | 180K 弹窗点允许放行后是否经 `PostToolUse` 自动将本窗标为免限 |
| `compact_reset_drop_tokens` | `40000` | 水位较峰值回落超过此值时自动清空已提醒标记（识别 `/compact`） |
| `chat_workspace_mode` | `"off"` | 聊天区（`exempt_workspace_names`）策略：`warn_only`（仅顶部提示不熔断）/ `full`（同工程区）/ `off`（全关） |
| `bypass_tokens` | `["!go","!force","/compact"]` | 强制逃生词列表 |
| `exempt_input_prefixes` | `["保活","续杯","取消保活","停止保活","[KV-KeepAlive"]` | 豁免前缀列表 |
| `cliff_locked_tools` | 7 个增肥工具 | `read_file`、`run_command`、`search_web`、`call_mcp_tool`、`invoke_subagent`、`run_workflow`、`read_resource` |
| `cliff_allow_write_dir_markers` | `[".agents/checkpoints",".gemini/antigravity/brain"]` | 防死锁写入放行白名单目录 |
| `handoff_dir_rel` | `".agents/checkpoints"` | 工程区交接单与急救草稿默认落盘目录 |
| `handoff_max_chars` | `2400` | 急救草稿硬字符上限（约 `≤1.5K` Token） |

---

## 4. 紧急停用 / 降级操作

1. **软关闭（推荐，秒生效）**：打开 `%USERPROFILE%\.gemini\config\eoq_guard_config.json`，将 `"enabled": true` 改为 `false`。
2. **仅关闭 180K 工具弹窗锁**：将 `cliff_locked_tools` 改为空数组 `[]`（或把 `cliff_tool_lock_tokens` 调高至 `999999`）。
3. **停用 Hook 挂载**：在 `%USERPROFILE%\.gemini\config\hooks.json` 中将 `eoq-context-guard.enabled` 改为 `false`。
4. **重置单会话提醒状态**：删除 `%USERPROFILE%\.gemini\config\.eoq_guard_state.json`（下次调用自动重建）。
