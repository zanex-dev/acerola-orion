# Acerola-Orion (雅赛劳拉莉昂) Global Plugin

`Kiss-Shot · Acerola-Orion · Heart-Under-Blade` 体系公开白帽微内核全局插件封装。

- **所属 Python 包**：`acerola-orion`（纯 `stdlib` 零网络外联微内核）
- **提供技能**：
  - `acerola-orion`（7 位精度实时配额遥测、被动 KV 缓存 TTL 与 200K 2x 计价墙雷达、本地隐私隔离 Token 账单与倍率审计、Version-Vault 版本冷库、零轮询 Windows 内核句柄进程守卫、配套组件一键装配器与桌面星盘）
  - `orion-vault`（Orion-Vault 对话自动增量备份、随时可控总开关、已删对话全文检索、一键无损恢复与 Markdown 导出）
- **生命周期钩子联动**：配合 `hooks.json` 中的 `Stop` 事件（`python -m acerola_orion.vault_hook`）增量归档会话，以及 `PreInvocation` / `PreToolUse` / `PostToolUse` 事件（`python -m acerola_orion.plugins.eoq_hook`）守护上下文水位。
