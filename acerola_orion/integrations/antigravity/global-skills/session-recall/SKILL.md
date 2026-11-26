---
name: session-recall
description: 跨窗口靶词检索与切片调阅本地历史聊天记录（全区通用）。当提及“之前聊过…”、“上个窗口”或需跨会话找回历史结论时激活。
---

# Session Recall（本地历史会话打靶检索与切片调档）

本技能通过零依赖纯文本索引脚本 [recall.py](scripts/recall.py) 对 `~/.gemini/antigravity/annotations/*.pbtxt`（会话标题）与 `brain/<uuid>/.system_generated/logs/transcript.jsonl`（对话轨迹）执行毫秒级检索，自动剥离系统注入噪声，严格遵循 `agent-blindfold` 媒体隐私隔离规范（绝不触碰 `.user_uploaded` 或任何二进制媒体文件）。

## 核心检索路由铁律（严禁整窗倾倒）

1. **有指向必先提炼靶词检索（含附图/隐性线索）**：
   只要当前消息带有具体指向——无论是文字概念，还是**附图中的文字/模型名/用户名/UI词条**、报错日志、文件名——哪怕用户口头只说“详见上个窗口/刚才聊的”，**必须先从中提炼 2～4 个高区分度靶词跑 `search` 锁定精确行号**，严禁当成“无关键词”盲目拉取历史正文。
2. **已知目标窗口 + 有话题指向 = 窗口内定向打靶**：
   当用户明确指向“上个窗口/昨天的窗口”且带有话题线索时：先用 `recent --limit 3` 确认目标 `UUID`，若该会话 `> 30` 行，立即用 `search <靶词> --session <目标UUID前8位>` 在该窗口内精准打出行号，再用 `show <UUID> --around-line <行号>` 切片读取。
3. **单次切片跨度红线（硬上限 ≤ 40 行）**：
   **严禁执行 `--lines 1:N` 全量倾倒或直接用 `read_file` 通读整份 `transcript.jsonl`**（脚本已内置 40 行防撑爆物理限位，非用户显式要求导出全量记录严禁传 `--allow-wide`）。即使遇到纯开放式提问（如完全无图无词纯问“上个窗口干了啥”），长会话也只准抽读**开头起因（默认裸调前 8 轮或 `--lines 1:12`）+ 结尾结论（`--tail 15`）**。
4. **防旁路审查延迟铁律**：
   在 `run_command` 中调用 `recall.py` 时，**必须保持纯单行直调**，严禁拼接管道 `|`、分号 `;` 或重定向 `>`。

---

## 标准工作流命令速查

### 第一步：打靶定位 `UUID` 与精确 `Line` 行号

```powershell
# 场景 A：有具体指向（从用户文字或附图中提取 2~4 个短靶词，排除当前窗口）
python "%USERPROFILE%\.gemini\config\skills\session-recall\scripts\recall.py" search Orchestrator Codex 缓存 --exclude <当前会话前8位>

# 场景 B：明确指定“上个窗口”且有指向 -> 先看最近列表拿 UUID，再加 --session 窗口内精准打靶
python "%USERPROFILE%\.gemini\config\skills\session-recall\scripts\recall.py" recent --limit 5
python "%USERPROFILE%\.gemini\config\skills\session-recall\scripts\recall.py" search Orchestrator Codex --session 4a736f12 --snippets 6
```

* 可选参数：`--session <前缀>`（仅搜指定窗口）、`--days 7`（仅搜最近 7 天）、`--all`（强制全词共现）、`--regex`（正则匹配）、`--thinking`（连同内部思考链检索）。

### 第二步：围绕命中行号切片精读（免撑爆当前窗口）

```powershell
# 1. 首选：围绕命中行号前后小范围展开（默认前后 5 行）
python "%USERPROFILE%\.gemini\config\skills\session-recall\scripts\recall.py" show 4a736f12 --around-line 19 --context 4

# 2. 纯开放式无关键词回顾：仅抽看末尾收官结论（最后 15 行）
python "%USERPROFILE%\.gemini\config\skills\session-recall\scripts\recall.py" show 4a736f12 --tail 15

# 3. 若命中行提示 [原字段有折叠] 且需看完整长文，仅对该窄区间加 --full 读取无损底片
python "%USERPROFILE%\.gemini\config\skills\session-recall\scripts\recall.py" show 4a736f12 --lines 18:20 --full
```
