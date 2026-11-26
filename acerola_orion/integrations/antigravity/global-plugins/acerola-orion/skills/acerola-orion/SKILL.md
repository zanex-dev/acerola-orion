---
name: acerola-orion
description: Google Antigravity 7位精度实时配额遥测 (Orion)、被动 KV 缓存余温与 200K 2x 计价墙雷达、本地隐私隔离 Token 账单与倍率审计、Orion-Vault 对话保险箱、Version-Vault 版本冷库、配套技能装配器 (companion) 及零轮询内核进程守卫 (Acerola)。当用户询问配额剩余百分比、缓存余温、Token 消耗账单、版本封存、进程守卫或装配配套技能时激活。
---

# Acerola-Orion (雅赛劳拉莉昂) 使用指南

`acerola-orion` 是面向 Google Antigravity 的纯本地标准库（零网络外联）可观测性与工程护航微内核，提供 CLI 与桌面星盘主控台。

## 1. 常用只读诊断与管理命令（可在 `run_command` 直接执行）

> **纯单行直调原则**：在 `run_command` 中执行 `acerola_orion` 命令时，请保持**纯单行直调**，避免拼接管道 `|`、分号 `;`、重定向 `>` 或多行脚本块。

```powershell
# 1. 实时 7 位精度配额水位、订阅层级、内核守卫与对话保险箱状态
python -m acerola_orion status

# 2. 实时观测本地活跃会话上下文体积、200K 2x 预警与 Prompt KV 缓存 250s 余温倒计时
python -m acerola_orion radar

# 3. 本地隐私隔离 Token 明细账本（每日汇总、模型家族分布、活跃会话与倍率对照）
python -m acerola_orion ledger --days 14

# 4. 单独输出高精度配额消耗斜率与未匹配步骤变化审计
python -m acerola_orion audit

# 5. Orion-Vault 对话自动增量备份、可控开关与误删对话恢复/导出
python -m acerola_orion vault status
python -m acerola_orion vault on
python -m acerola_orion vault off
python -m acerola_orion vault list --deleted
python -m acerola_orion vault search 关键词
python -m acerola_orion vault restore <UUID前8位>
python -m acerola_orion vault export <UUID前8位>

# 6. Version-Vault 外部不可删版本冷库扫描、双向 SHA-256 校验封存与状态查看
python -m acerola_orion archive status --json
python -m acerola_orion archive scan --json
python -m acerola_orion archive seal --confirm --reason "迭代封版" --json

# 7. Acerola 零轮询 Windows 内核句柄进程守卫状态与启停管理
python -m acerola_orion guard status
python -m acerola_orion guard start
python -m acerola_orion guard stop
python -m acerola_orion guard install
python -m acerola_orion guard uninstall

# 8. Antigravity 配套 Skills、工作区插件与 EOQ/Vault Hooks 一键装配器
python -m acerola_orion companion status
python -m acerola_orion companion install --workspace "<工作区路径>"
```

## 2. 桌面 GUI 主控台与可选快捷方式

- **`panel` 为桌面 GUI 窗口，请勿在隐藏桌面的 `run_command` 中直接阻塞调用**：
  - 提示用户在系统终端运行：`pythonw -m acerola_orion panel`（或 `python -m acerola_orion panel`）。
  - 若用户已运行 `python -m acerola_orion install-launchers` 并配置了可选的 `agyrun://` 自定义桌面协议，可提供快捷入口示例：
    - 桌面主控台：`[点击打开：Acerola-Orion 桌面主控台](agyrun://orion-panel)`
    - 启用内核守卫：`[点击启用：内核进程守卫](agyrun://acerola-guard-on?silent)`
