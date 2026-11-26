---
name: orion-vault
description: Orion-Vault 对话自动增量备份与防误删时光保险箱（内置于 acerola-orion 插件）。支持随时开启/关闭对话自动备份可控开关、检索已删除的历史对话、切片预览、一键无损恢复误删对话回 Antigravity 客户端，或导出为 Markdown 归档。当用户询问备份对话、开关对话备份、找回/恢复误删对话或导出聊天记录时激活。
---

# Orion-Vault (对话防误删时光保险箱) 使用指南

`Orion-Vault` 内置于 `acerola-orion` 核心引擎与插件体系，采用**只增不删（Append-Only）**增量热备机制，严格遵循媒体隐私隔离规范（零触碰 `.user_uploaded` 及位图/音视频文件）。

> **纯单行直调原则**：在 `run_command` 中执行 `acerola_orion` 命令时，请保持**纯单行直调**，避免拼接管道 `|`、分号 `;` 或重定向 `>`。

## 1. 随时可控总开关（即时生效 · 无需重启）

```powershell
# 查看保险箱总状态、自动备份开关、已保护会话数与已删可恢复会话数
python -m acerola_orion vault status

# 开启自动备份（默认开启：每轮 Stop Hook + 守护进程增量巡检）
python -m acerola_orion vault on

# 关闭自动备份（立即短路放行，零磁盘读写，已有历史备份完整保留）
python -m acerola_orion vault off

# 一键切换开关状态
python -m acerola_orion vault toggle
```

可选桌面快捷穿透示例（若用户本机已通过 `install-launchers` 配置 `agyrun://` 自定义协议，可提供一键点击）：
- 开启自动备份：`[点击开启：对话自动备份](agyrun://orion-vault-on?silent)`
- 关闭自动备份：`[点击关闭：对话自动备份](agyrun://orion-vault-off?silent)`
- 切换备份开关：`[点击切换：对话自动备份开关](agyrun://orion-vault-toggle?silent)`
- 手动全量同步：`[点击执行：立即备份全量对话](agyrun://orion-vault-sync?silent)`
- 打开可视化面板（含「🗄️ 对话防删保险箱」标签页与托盘开关）：`[点击打开：Acerola-Orion 桌面主控台](agyrun://orion-panel)`

## 2. 检索、预览与找回已删除对话

```powershell
# 仅列出客户端中已删除但保险箱仍保留的对话
python -m acerola_orion vault list --deleted

# 全文搜索保险箱（含已删除对话），支持多关键词共现
python -m acerola_orion vault search 关键词1 关键词2

# 仅在已删除对话中检索
python -m acerola_orion vault search 关键词 --deleted

# 切片预览某段备份对话（自动限制 <= 40 行防撑爆上下文）
python -m acerola_orion vault show <UUID前8位> --tail 15
python -m acerola_orion vault show <UUID前8位> --around-line 20
```

## 3. 一键无损恢复回客户端 / 导出 Markdown

```powershell
# 将已删除对话（含 .db 数据库、brain 上下文与 conversation_summaries 列表索引）无损恢复回 Antigravity
python -m acerola_orion vault restore <UUID前8位>

# 将任意备份对话（在线或已删）导出为独立可读的递增编号 Markdown 文件 (_v1.md, _v2.md)
python -m acerola_orion vault export <UUID前8位>

# 手动立即执行一次全量增量备份（即使自动开关处于关闭态也可手动触发）
python -m acerola_orion vault sync
```
