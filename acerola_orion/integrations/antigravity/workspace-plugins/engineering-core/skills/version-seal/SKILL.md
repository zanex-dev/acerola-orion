---
name: version-seal
description: 每次完成新版本创建（新 `_vN` 替代旧版、发版打包、跨版重构验收、或任务包全 `[x]` 结项）后自动激活；也可由“封版 / 外迁旧版 / 清理冷库 / archive seal”显式触发。负责扫描工作区旧版本候选，呈报确认后调用 `acerola-orion archive seal` 双向校验外迁至 `~/.gemini/acerola_orion/version_vault/` 外部不可删版本冷库。
---

# Version Seal（新版本结项与旧版外部冷库外迁协议）

用**可追溯、双向 SHA-256 校验的外部冷库外迁**替代直接删除，在保持工作区干净的同时守住「资产零覆盖、历史永不误删」底线。

## 1. 触发条件
满足以下任一场景时自动激活：
- 创建了新的 `_vN` 版本文件（源码、配置、文档、视觉/SVG 资产等）替代旧版；
- 完成发版打包或跨版重构验收；
- `task-decomposer` 任务包全部子卡均已验收打勾 `[x]` 结项；
- 用户显式要求封版、扫描或外迁工作区旧版本。

## 2. 标准四步执行流

### Step 1：只读扫描旧版候选（绝不改磁盘）
优先运行只读扫描获取结构化待迁出清单与受保护项：
```powershell
python -m acerola_orion archive scan --json
```
- 可选过滤：`--project <项目名>`、`--category <类别>`、`--workspace <路径>`。
- 若需排查静默失活项目目录，须显式附加 `--include-stale-projects`（默认不包含）。
- 重点核对输出中的**待迁出候选**（数量、类别、体积）与**受保护项**（当前最新版、活跃文件、`pinned_paths` 豁免项）。

### Step 2：呈报清单并确认迭代
- 向用户简明呈报扫描结果（待外迁清单摘要、总大小、受保护项计数），请用户确认当前迭代已稳定可外迁旧版。
- **自动放行特例**：仅当冷库策略 `version_vault_mode` 为 `auto_after_settle` 且候选全部属于版本层（**不含 `stale_project`**）时，可按策略交由后台守护满宽限期自动静默外迁，无需拦截打断。

### Step 3：异步校验外迁入冷库
用户确认后，调用封存命令异步入队并拉起后台 worker，完成 `~/.gemini/acerola_orion/version_vault/` 写入、SHA-256 双向校验及源路径移出：
```powershell
python -m acerola_orion archive seal --confirm --reason "<迭代/结项说明>" --json
```
- 局部精准封存时附加 `--project <项目名>`、`--category <类别>` 或显式传入 `paths...`；
- 仅归档留底不移出源文件时附加 `--keep-source`；
- 若本机已配置自定义桌面穿透协议（可选示例），也可提供快捷入口：`[封存外迁旧版](agyrun://orion-archive-seal?silent)`。

### Step 4：状态核验与云端同步提示
- 查看入库队列、快照数与冷库健康状态：
  ```powershell
  python -m acerola_orion archive status --json
  ```
- 如需同步外部 Git 远端冷备份（永不 `--force`），提示用户执行：
  - 命令：`python -m acerola_orion archive push --json`
  - 可选桌面穿透示例：`[推送冷库](agyrun://orion-archive-push?silent)`

## 3. 常用命令与可选 `agyrun://` 桌面穿透示例

> 注：下表右侧 `agyrun://` 链接为本机已注册自定义桌面启动协议时的可选示例；未配置该协议时直接使用左侧 CLI 命令即可。

| 用途 | CLI 命令 | 可选桌面穿透示例 |
|---|---|---|
| 只读扫描旧版 | `python -m acerola_orion archive scan --json` | `[扫描旧版候选](agyrun://orion-archive-scan?silent)` |
| 确认后异步封存外迁 | `python -m acerola_orion archive seal --confirm --json` | `[封存外迁旧版](agyrun://orion-archive-seal?silent)` |
| 推送冷库到远端 Git | `python -m acerola_orion archive push --json` | `[推送冷库](agyrun://orion-archive-push?silent)` |
| 查看冷库状态 | `python -m acerola_orion archive status --json` | `[查看冷库状态](agyrun://orion-archive-status?silent)` |
| 查看策略/队列/历史 | `archive policy --json` / `archive jobs --json` / `archive list --json` | — |
| 原样校验还原快照 | `python -m acerola_orion archive restore <snapshot_id> --json` | — |

## 4. 安全红线
- **严禁直接删除旧版**：不得用 `rm` / `Remove-Item` 清理 `_versions/`、`_vN` 历史副本或旧草稿，一律经冷库外迁。
- **确认前禁落盘移出**：未获用户明确确认前，严禁擅自对真实工作区执行 `archive seal --confirm`（策略明确为 `auto_after_settle` 的非 `stale_project` 守护流转除外）。
- **整项目失活单独审批**：`stale_project` 候选任何时候均须显式 `--include-stale-projects` 并经用户单独确认。
- **冷库只增不删**：严禁手工删改 `~/.gemini/acerola_orion/version_vault/` 下的归档对象，严禁绕过媒体隐私隔离规则。
