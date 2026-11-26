# 更新日志 (Changelog)

[English](./CHANGELOG.md) | **简体中文**

**`acerola-orion`** 的所有重要版本变更均记录于此文件中。

日志格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本 2.0.0 (SemVer)](https://semver.org/lang/zh-CN/)。

## [2.4.5] - 2026-11-26 — 旅途的终点（随仓白帽工作流套件、可逆装配器与洁净室门禁）

> **「这是我们的最后一次更新，真的，这次绝不骗你（Re-Logic 语气）。可惜大版本已经上 2 回不去 1.4.5 了，2.4.5 精神续作凑合看吧 lol。」**

### 新增功能
- **内置 200K 上下文 EOQ 水位守卫 Hook（`acerola_orion/plugins/eoq_hook.py`）**：
  - 将纯标准库零依赖的会话水位 Hook（`python -m acerola_orion.plugins.eoq_hook`）收编入主包：支持 `95K / 175K` 热窗分级收口提示、`180K` `PreToolUse` 强提醒与 `PostToolUse` 自动解锁、冷窗拦截与救援草稿落盘、`/compact` 水位重置，以及与主控台 `eoq_guard` 单窗免限开关的双向联动（异常默认 fail-open 放行，关闭插件即自动短路）。
- **随仓官方白帽 Antigravity 伴侣套件（`acerola_orion/integrations/antigravity/`，共 18 个纯明文资产）**：
  - **全局伴侣插件与技能**：内置 `acerola-orion` 全局插件（含 `acerola-orion` 星盘速查技能与 `orion-vault` 对话保险箱技能），以及 `session-recall`（跨会话靶词检索与 `recall.py` 切片调阅，免整窗通读爆上下文）与 `kv-keepalive`（短时限次 Prompt KV 缓存余温护航，用户发言自动提前取消）。
  - **工作区工程插件**：内置 `eoq-task-relay`（`task-decomposer` 先只读探索拆解 `PLAN.md` + `≤1.5K` 原子卡再轻载施工、`stage-handoff` 阶段交接单，直接驱动主控台 `tasks_board` 看板）与 `engineering-core`（`version-seal` 联动 `archive` 双向 SHA-256 校验外迁旧版入冷库，附可选工程准则模板 `rules/AGENTS.md`）。
  - **Hooks 配置模板与速查手册**：随附 `hooks.template.json`、`eoq_guard_config.json` 默认水位配置与 `eoq_context_guard_cheatsheet.md`。
- **一键可逆伴侣装配器 CLI（`acerola_orion/companion.py`，`acerola-orion companion`）**：
  - 默认绝不静默改写 `~/.gemini/config/`；新增 `companion status | install | uninstall` 子命令（支持 `--workspace`、`--include-rules`、`--no-hooks`、`--no-global-skills`、`--force`、`--python-exe`、`--remove-global`、`--remove-workspace`、`--json`）。
  - 安装时对 `hooks.json` 执行非破坏性合并（完整保留用户已有自定义 Hooks，未传 `--force` 不覆盖已有文件）；卸载时仅精准摘除 Orion 托管项，绝不误删用户自有技能与配置。
- **架构路标与 Agent 寻路地图（`AGENTS.md`）**：
  - 新增仓库级模块边界、大文件符号行号锚点与单测映射导航表，降低跨模块定位与上下文开销。

### 安全自审与测试加固
- **洁净室静态安全门禁扩容（`tests/test_public_suite.py`）**：`test_01_static_security_zero_egress_and_clean_room_audit` 现自动递归审计 `acerola_orion/integrations/` 下全部 `.py` / `.md` / `.json` / `.svg` 资产与 `AGENTS.md`，强制校验零外联依赖、零凭据读取及零本机用户绝对路径硬编码泄漏。
- 新增 `tests/test_eoq_hook.py` 与 `tests/test_companion_installer.py`，公开仓零外联净室单测扩展至 **`85/85` 项全绿**（完整覆盖默认态与 `ACEROLA_ORION_DISABLE_PLUGINS=1` 纯净裸核态）。

## [2.1.0] - 2026-11-25 — 官方内置白帽可选插件生态、每日日志归档与按需自选开关

### 新增功能
- **3 大官方内置白帽可选插件（`acerola_orion/plugins/`，保持 `0` 第三方依赖，默认随包注册且可随时按需关闭）**：
  - `version_vault`：提供不可删工作区版本冷库（`~/.gemini/acerola_orion/version_vault/`）与完整 `acerola-orion archive` CLI（`status` / `scan` / `seal` / `list` / `restore` / `export` / `push`）及主控台冷库页签，防止版本迭代误删旧稿。
  - `tasks_board`：提供只读 `.agents/tasks/` 任务拆解包与阶段交接单可视化看板页签，实时跟踪多卡片状态与进度。
  - `eoq_guard`：提供 200K 上下文 EOQ 水位收口提示与单窗免限切换控件。
- **裸核默认与逐个插件自选开关**：
  - 支持通过 `ACEROLA_ORION_DISABLED_PLUGINS=eoq_guard`（逗号分隔可组合）按需关闭任意内置或外部插件，或通过 `ACEROLA_ORION_DISABLE_PLUGINS=1` 一键回退至 0 插件纯净裸核模式。
- **每日日志无覆盖归档、午夜计数归零与插件槽位扩展（`guillotine.py`、`telemetry.py`、`panel.py`、`ui/plugin_host.py`、`ui/hud.py`）**：
  - 新增守卫日志按日/512KB 无覆盖分卷归档、宿主日志只读跨日快照与过期 hub 清理；补齐午夜计数自动归零、超时异常去重及雷达/HUD 插件扩展槽位。

### 许可、文档与测试
- 开源许可证切换为 [PolyForm Noncommercial License 1.0.0](./LICENSE)（个人学习与非营利研究免费开放，禁止商业用途与二次售卖）。
- 同步更新中英文 `README` 与 [docs/PLUGIN_ARCHITECTURE.md](./docs/PLUGIN_ARCHITECTURE.md) 官方白帽插件入口点清单与禁用示例。
- 零外联净室单元测试扩展至 **`74/74` 项全绿**（完整验证裸核模式与插件启用双态）。

## [2.0.0] - 2026-11-21 — 纯标准库微内核与 Slot 插槽驱动模块化 UI 架构重构

> **「问我为什么重构？因为我是他妈的开源 GOAT。」**

### 架构重构
- **纯标准库微内核底座（`acerola_orion/core/kernel.py`、`core/__init__.py`）**：
  - 全程保持 **`0` 第三方依赖**（`dependencies = []`），抽离独立微内核与插件发现、依赖声明、生命周期及 Hook 机制，彻底物理隔离公共底座与外部扩展包。
  - 后续新增功能可直接以插件包形式编写并声明插件依赖，不再硬耦合主仓库内部实现。
- **Slot 驱动模块化 UI（`acerola_orion/ui/**`、`panel.py`）**：
  - 将原 3,400+ 行单体 `panel.py` 彻底拆解为职责单一的独立模块：`astrolabe.py`、`charts.py`、`hud.py`、`plugin_host.py`、`radar_controller.py`、`window_chrome.py`、`theme.py`、`widgets.py`、`builtin_tabs.py` 与可插拔页签 `ui/tabs/`（`common.py`、`hub_tab.py`、`ledger_tab.py`、`vault_tab.py`、`diag_tab.py`、`zen_view.py`）。
- **插件架构文档与测试翻倍加固（`docs/PLUGIN_ARCHITECTURE.md`、`tests/test_kernel.py`、`tests/test_core_hooks.py`、`tests/test_panel_ui_slots.py`）**：
  - 新增插件架构规范文档 [docs/PLUGIN_ARCHITECTURE.md](./docs/PLUGIN_ARCHITECTURE.md)，明确 Hook 扩展点与 UI Slot 槽位契约。
  - 新增 1,400+ 行内核、Hook 与 UI Slot 自动化单元测试，零外联净室测试套件扩展至 **`42/42` 项全绿**。

## [1.4.0] - 2026-11-21 — Orion-Vault 对话时光保险箱、Gemini 4 Neon、200K 双倍预警与安全审计加固

### 新增功能
- **Orion-Vault 只增不删对话自动增量备份与误删恢复（`acerola_orion/vault.py`、`vault_hook.py`、`guillotine.py`、`panel.py`、`cli_render.py`、`__main__.py`）**：
  - 新增纯本地只增不删备份引擎：通过 SQLite Online Backup + gzip 热备 `conversations/<uuid>.db`（`conversation.db.gz`），完整留存 `conversation_summaries.db`（含 `raw_summary`）、`annotations/<uuid>.pbtxt` 与 `brain/<uuid>/` 纯文本/SVG Artifacts；拦截客户端删除操作并自动标记为 `deleted`，支持一键原位恢复（`vault restore`）与 Markdown 导出（`vault export`）。
  - 新增可控 `vault_auto_backup` 开关（跨进程文件锁 + 原子写入 `config.json`），打通 CLI 全套子命令（`vault status|sync|on|off|toggle|list|search|show|restore|export|hook-stop`）、主控台第 4 页签「🗄️ 对话防删保险箱」、系统托盘开关与守卫后台增量巡检线程，并在 `install-launchers` 新增 5 个 Vault 启动脚本（共 11 个）。
  - 严格执行媒体隐私隔离（跳过 `.user_uploaded`、`browser_sandbox`、`tempmediaStorage`、`.allow_media` 与全部位图/音视频文件）并加装 `_is_reparse_or_link` 阻止符号链接与 NTFS 重解析点穿越。
- **Gemini 4 Neon (`4N`) 支持与 200K 双倍计价（2x）预警（`constants.py`、`ledger.py`、`cache_radar.py`、`panel.py`）**：
  - 新增 `Gemini 4 Neon (4N)` 费率目录分类及旧账本缓存自动失效重算机制。
  - 在 CLI `radar`、星盘主面板、悬浮胶囊 HUD 与托盘右键菜单同步加入 `180K` 临近预警、`≥200K` 双倍计价（2x）告警及 `/compact` 一键复制到剪贴板。

### 安全审计与稳定性加固（A01–A18 公开版基线）
- **归属句柄精确进程清理与非破坏性生命周期（`rpc.py`、`guillotine.py`、`__main__.py`）**：
  - 引入 `open_owned_language_server` / `terminate_owned_handle`，通过 `advapi32` 同用户 SID fail-closed 校验、安装目录、UI 祖先与自有 hub 归属确认后再终止受控进程，并在 `finally` 严格释放句柄（A10）。
  - `retire-legacy` 改为非破坏性只读提示，`stop_guard_daemon` 移除未验证 PID 盲杀，`uninstall_guard_autostart` 与 `guard` CLI 返回真实状态码（A18）。
- **代理环境变量凭证剥离（`telemetry.py`）**：
  - `_inspect_proxy_mode()` 升级为 `urllib.parse.urlsplit` 解析，彻底剥离 `userinfo`、query 与 fragment，仅保留合法 `scheme://host[:port]`（A13）。
- **账本与缓存雷达数据完整性（`ledger.py`、`cache_radar.py`）**：
  - 兼容 `cache_read_input_tokens` 统计（A01）；移除缓存雷达缺失文件负缓存以即时发现晚创建日志（A02）；账本增量解析加入跨进程文件锁、SHA-256 增量前缀校验、坏游标回退重建与同 step 模型切换替换计票（A04）。
  - 新增独立 `p3_5h_drop_pct` 字段（A05）、偶数样本 `statistics.median` 均值中位数（A06）、数值 `last_epoch` 排序与旧库迁移（A07）、Top-N `EXISTS` 预筛选（A08）以及无注释标题回退 `conv_id[:8]` + 旧库标题清洗防 Prompt 泄漏（A15）。
- **UI 定时器防泄漏与诚实准确文案对齐（`panel.py`、`win_tray.py`、`cli_render.py`、`README*`）**：
  - 隐藏至托盘或关闭窗口时显式取消 `_auto_refresh_id`，杜绝反复隐藏/唤醒导致的定时器堆叠（A03）。
  - 全仓代码与文档统一将 `1.5x` 明确标注为本地预设倍率估算，并移除无实测依据的 `0.00% CPU` / `<1ms` 承诺文案（A16 / A17）。
- **测试与静态清白自审扩充（`tests/test_public_suite.py`、`.github/workflows/ci.yml`）**：
  - 自动化安全与回归测试套件扩充至 **`20/20` 项全绿**，覆盖 A01–A18 回归、Gemini 4 Neon、200K 2x 预警与 Orion-Vault 全链路，并将 `test_01` 静态零外联/白帽清白审计覆盖至 `vault.py`、`vault_hook.py` 及全部发布文档。

## [1.3.0] - 2026-11-17 — 被动式 Prompt KV 缓存余温雷达、配额计时消歧与高分屏 UI 防截断加固

### 新增与优化
- **纯本地被动「Prompt KV 缓存 250s 余温雷达」与桌面悬浮胶囊（`acerola_orion/cache_radar.py`、`rpc.py`、`panel.py`、`__main__.py`）**：
  - 新增 `read_shared_tail_bytes()` Win32 无锁末尾 `16KB` 增量读取器与 `CacheRadarSampler` 被动观测引擎（`0` 网络发包、`0` 端口介入、严格跳过媒体附件目录与符号链接越界），实时计算活跃上下文体积、缓存命中率与 **250s（4分10秒）缓存余温倒计时**。
  - 支持 `smart_guard`（智能守 `≥100K` 未冷大窗防抢焦）、`latest`（纯跟最新）与 `pinned`（手动锁定窗口）三种策略，集成主星盘内嵌雷达条、`acerola-orion radar` CLI 子命令、极简模式 `📡 缓存余温` 专注视图以及可拖拽置顶迷你胶囊 HUD（`smart` / `always` / `off`）。
- **配额满额待命占位符消歧与日志 429 降噪（`acerola_orion/telemetry.py` & `cli_render.py`）**：
  - 新增 `describe_quota_timer()`，精准区分 `100.000%` 未动用 `5h` 池的滑动占位符（提示 `满额待命 · 未开始计时`）与真实激活后的重置倒计时。
  - 将不影响主对话的旁路 `429`（自动起标题、旁路审查器限流、搜索摘要）拆分为独立 `aux_429_count`，新增旁路一审/二审失败统计与 `glog` 假报错头剥离。
- **高分屏 UI 边界与托盘唤醒防截断加固（`acerola_orion/panel.py`）**：
  - 为全部账单 `Treeview` 补齐暗色横向滚动条 `Orion.Horizontal.TScrollbar`；星盘、统计图与进度条顶栏改为右侧读数/徽章优先固定防挤压；为隐藏态/跨页签画布加装 `_deferred_redraw_pending` 单次 `after_idle` 补画门闩与托盘唤醒重校准，彻底消除隐藏态 `1px` 缩条与自旋死锁（`15/15` 自动化安全与功能测试全绿）。

## [1.2.0] - 2026-11-16 — 猎户座星盘计时仪 UI 焕新与极简专注模式

### 新增与优化
- **猎户座双星轨精密计时仪桌面 UI（`acerola_orion/panel.py`）**：
  - 全新矢量自绘 **猎户座星盘计时仪（Orion Astrolabe & Chronograph）**：60 格腕表刻度圈 + 外圈 `5h 滚动池` 高亮弧 + 内圈 `Weekly 周总池` 星轨弧 + **金针时间流逝基准**（实时对比窗口剩余时间与剩余配额，直观标识盈余巡航与超速透支），配合原生 Win32 `DwmSetWindowAttribute` 沉浸暗黑标题栏与全暗色滚动条。
  - 新增支持鼠标悬停十字准星读数的矢量统计图组件（`OrionChartWidget`），覆盖 24 小时配额水位轨迹、近 14 天等效美元消耗柱状图与旁路风控附加耗时示波器。
- **可选「极简专注模式」（`acerola-orion panel --zen`）**：
  - 支持右上角一键切换、托盘右键切换或命令行 `--zen / --full / --reload` 启动（自动记忆至 `ui_prefs.json`），同屏严格只展示 **1 个核心指标表盘 + 1 张对应统计趋势图**，兼顾极客全量审计与清爽直观浏览。
- **遥测超时去噪与多池曲线隔离（`acerola_orion/telemetry.py` & `acerola_orion/ledger.py`）**：
  - 剥离客户端后台 `play.googleapis.com/log` / `firebaselogging` 埋点上报被代理拦截引发的超时误计，并在 `get_recent_quota_series()` 中引入周重置指纹同池过滤，消除多池交替采样的曲线跳刺（`14/14` 自动化安全与功能测试全绿）。

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
