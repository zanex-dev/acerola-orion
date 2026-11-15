# 更新日志 (Changelog)

[English](./CHANGELOG.md) | **简体中文**

**`acerola-orion`** 的所有重要版本变更均记录于此文件中。

日志格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本 2.0.0 (SemVer)](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-11-15 — 原生系统托盘常驻、多尺寸高清图标与开始菜单集成

### 新增
- **零依赖原生 Win32 系统托盘驻留与休眠引擎（`acerola_orion/win_tray.py` & `acerola_orion/panel.py`）**：
  - 基于纯标准库 `ctypes` 实现独立消息泵线程 `Shell_NotifyIconW` 托盘控制器（支持资源管理器重启 `TaskbarCreated` 自动重挂、悬停实时预览 `5h / 周` 配额百分比及右键快捷菜单）。
  - 点击主控台右上角 `×` 自动转入右下角托盘静默休眠并暂停定时采样（待机 `0% CPU`），支持 `acerola-orion panel --minimized` 静默启动及跨进程单实例前置唤醒。
- **多尺寸高清应用图标与开始菜单/任务栏独立身份（`assets/app.ico` & `acerola-orion shortcut`）**：
  - 内置 9 档尺寸（`16px`–`256px`）高清透明 `.ico` 图标，绑定显式 `AppUserModelID`（`Trinity.AcerolaOrion.Panel`），脱离默认 Python 图标分组。
  - 基于纯 `ctypes` COM 接口（`IShellLinkW` + `IPropertyStore` `PKEY_AppUserModel_ID`）新增 `acerola-orion shortcut [--remove]` 命令，一键注册无黑框 `pythonw.exe` 开始菜单与任务栏可固定应用入口，全程维持 `0` 第三方依赖（`13/13` 自动化测试全绿）。

## [1.0.0] - 2026-11-15 — 代号：`Acerola-Orion (雅赛劳拉莉昂)` · 正式版星象台发布

### 正式版晋升与边界锁定
- **晋升 Production/Stable 正式版（`pyproject.toml` & `acerola_orion`）**：
  - 在完成静态零外联源码审计、Win32 内核句柄生命周期校验与 Windows CI 事件同步加固（`12/12` 自动化测试全绿）后，包成熟度分类正式由 `4 - Beta` 晋升为 `5 - Production/Stable`。
- **零信任只读安全边界定型（Zero-Trust Boundary Freeze）**：
  - 正式锁定 `1.x` 周期内的本地物理边界契约：`0` 第三方依赖（`dependencies = []`）、全仓唯一 `HTTPConnection` 硬编码断言 `127.0.0.1` 纯环回只读查询、零凭据/零 Cookie 触碰、零对话正文存储、阻断 NTFS 软链接/目录联接越界，以及 CLI/JSON 输出账号邮箱自动脱敏。
- **三大核心子系统与 CLI 契约稳定（适配基线 Google Antigravity `v2.32+`）**：
  - 稳定锁定 `status`、`ledger`、`guard`、`panel` 命令契约与高分屏黑金绯红 `tkinter` 桌面控制台，涵盖 **Orion-Telemetry**（7 位小数配额水位与无锁 `(file_id, offset)` 增量日志游标）、**Orion-Ledger**（隐私隔离 SQLite 明细验钞账本与离线幽灵掉电审计）及 **Acerola-Guillotine**（`0.00%` CPU 占用的 Win32 `WaitForMultipleObjects` 孤儿进程退出断头台）。
- **文档与三部曲规范对齐**：
  - 新增中英双语 `CHANGELOG.md` / `CHANGELOG_ZH.md` 并将更新日志纳入自动化静态安全自检（`tests/test_public_suite.py`），正式完成 **Kiss-Shot · Acerola-Orion · Heart-Under-Blade** 本地主权与安全三部曲的全员 `1.x` 收官合拢。

## [0.2.2] - 2026-11-15 — 初始公开发布与 Windows CI 同步加固

### 新增与加固
- **Orion-Telemetry 配额与健康度遥测（`acerola_orion/telemetry.py` & `acerola_orion/rpc.py`）**：
  - 基于亚毫秒级 Win32 `CreateToolhelp32Snapshot` 进程树发现与 `127.0.0.1` 本地只读状态查询（`GetUserStatus`、`GetLoadCodeAssist`、`RetrieveUserQuotaSummary`），精准透视 `gemini-5h`、`gemini-weekly`、`3p-5h`、`3p-weekly` 四大池 7 位小数剩余配额与重置倒计时。
  - 采用 `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` 无锁增量游标统计 `HTTP 429/503` 限流频次与内置风控旁路延迟。
- **Orion-Ledger 隐私账本与幽灵掉电审计（`acerola_orion/ledger.py`）**：
  - 严格隐私隔离的 SQLite 增量索引器，输出按日、按模型家族与按会话维度的 `1.0x` 官方基准价、`0.5x` 优惠承诺价与 `1.5x` 内部倍率对比，并关联 7 位精度配额水位差定位零聊天离线暗扣（Phantom Drain）。
- **Acerola-Guillotine 零轮询内核退出守卫（`acerola_orion/guillotine.py`）**：
  - 基于 Win32 内核 `SYNCHRONIZE` 句柄阻塞等待主 UI 进程真实退出，辅以同句柄可执行映像路径二次核验、UTF-16LE BOM 宽字符静默启动器及事件同步化 Windows CI 单元测试。
- **原生高分屏桌面控制台（`acerola_orion/panel.py`）**：
  - 内置 `tkinter` 实时配额仪表盘、账本透视表、健康度诊断与守卫一键启停控制。
