# AGENTS.md — Acerola-Orion 公开白帽微内核架构导览

本文件面向人类开发者与 AI 代理，定义 `acerola-orion-public`（包名 `acerola_orion`）的排他性架构不变量、分层结构、大文件行号路标、单测映射与洁净室门禁纪律。任何代码、文档或测试改动前，均须先核对本页边界。

---

## 1. 排他性架构不变量（绝对红线）

1. **零外部依赖与纯本地回环**
   - 运行时严格限定为 **Python 标准库 + Win32 `ctypes`**（零第三方 Python 包依赖）。
   - `acerola_orion/rpc.py` 中的 `ConnectRpcClient` 硬锁定 `127.0.0.1` 回环通信。
   - 严禁发起外部网络请求，严禁读取或解密本地凭据库与账号敏感令牌。

2. **五核心子系统严格正交**
   五大底座子系统职责互斥，禁止跨层反向耦合、禁止越权读写彼此底层存储：

   | 子系统 | 模块路径 | 单一职责与不可越边界 |
   |---|---|---|
   | 遥测采集 | `acerola_orion/telemetry.py` | 本地配额与游标遥测采样；只读采集，不负责账本落盘 |
   | 缓存雷达 | `acerola_orion/cache_radar.py` | KV 缓存命中状态与 200K 上下文水位采样评估 |
   | 统计账本 | `acerola_orion/ledger.py` | SQLite 轻量账本与验钞校验；**仅存结构化统计与元数据，严禁落盘对话正文** |
   | 增量快照 | `acerola_orion/vault.py` | 增量文本与 `.svg` 矢量备份；**物理跳过一切二进制图片与音视频媒体文件** |
   | 孤儿守卫 | `acerola_orion/guillotine.py` | Win32 孤儿进程守卫与进程生命周期安全收敛 |

3. **薄壳 UI 与微内核插件槽位**
   - `acerola_orion/panel.py` 与 `acerola_orion/ui/tabs/*` 为**薄壳视图层**，仅做状态呈现与事件转发，不得堆砌业务规则或绕过核层状态机。
   - **内置插件**收口于 `acerola_orion/plugins/`，包括 `version_vault`（含版本扫描与外部冷库）、`tasks_board`（任务包看板）与 `eoq_guard`（EOQ 水位护航）。
   - **外部扩展**统一经 `acerola_orion/core/kernel.py` + `acerola_orion/ui/plugin_host.py` 的 `importlib.metadata.entry_points(group="acerola_orion.plugins")` 动态发现并挂载。

4. **双态隔离环境变量**

   | 环境变量 | 取值 / 说明 | 行为约束 |
   |---|---|---|
   | `ACEROLA_ORION_DISABLE_PLUGINS` | `1` / `true` / `yes` / `on` | 跳过外部插件发现，100% 退回纯净裸核模式（公开单测默认启用） |
   | `ACEROLA_ORION_HOME` | 目录路径 | 隔离运行期配置、状态与账本等本地数据目录，禁止污染用户真实主目录 |

---

## 2. 仓库分层速查

```text
acerola-orion-public/
├── acerola_orion/
│   ├── __main__.py              # CLI 入口、参数解析与子命令路由
│   ├── rpc.py                   # 127.0.0.1 回环 ConnectRpcClient
│   ├── telemetry.py             # 配额 / 游标遥测
│   ├── cache_radar.py           # KV 缓存 / 200K 上下文采样
│   ├── ledger.py                # SQLite 账本与验钞（不存正文）
│   ├── vault.py                 # 增量文本 / SVG 备份（物理跳媒体）
│   ├── guillotine.py            # Win32 孤儿进程守卫
│   ├── panel.py                 # Tk 主面板薄壳与槽位装配
│   ├── core/kernel.py           # 微内核生命周期与插件发现
│   ├── ui/                      # plugin_host.py 与 tabs/* 薄壳标签页
│   └── plugins/                 # 内置插件（version_vault / tasks_board / eoq_guard）
├── docs/PLUGIN_ARCHITECTURE.md  # 插件槽位与扩展接入规范
└── tests/                       # 模块单测与公开洁净室综合套件
```

---

## 3. 大文件行号路标（定点切片读，禁止整文件通读）

以下模块体量较大，阅读或修改前须先按路标行号切片定位，禁止全仓盲搜或整文件通读：

| 文件路径 | 约略行数 | 关键符号与锚点 | 职责摘要 |
|---|---:|---|---|
| `acerola_orion/__main__.py` | ~586L | CLI 入口与子命令分发 | 命令行参数解析、裸核/插件子命令路由 |
| `acerola_orion/rpc.py` | ~1390L | `ConnectRpcClient` (`L850+`) | `127.0.0.1` 回环 RPC 客户端封装 |
| `acerola_orion/panel.py` | ~1319L | `AcerolaOrionApp` (`L183+`) | Tkinter 主面板薄壳、托盘与 UI 槽位装配 |
| `acerola_orion/vault.py` | ~1572L | `OrionVault` (`L306+`) | 增量文本/SVG 快照管理与媒体物理过滤 |
| `acerola_orion/ledger.py` | ~1593L | `OrionLedger` (`L378+`) | SQLite 账本读写、统计聚合与验钞核对 |
| `acerola_orion/plugins/version_scanner.py` | ~1851L | 扫描引擎与候选分类 | 工作区历史版本候选扫描与保留判定 |
| `acerola_orion/plugins/version_vault.py` | ~1891L | `WorkspaceVersionVault` (`L386+`) | 外部不可删版本冷库双校验外迁与索引 |

---

## 4. 单测映射与定向验证

日常迭代优先运行受影响模块的独立单测；`tests/test_public_suite.py` 约 1710 行，改动局部逻辑时请使用 `-k` 精准过滤，避免每次盲跑全量套件。

| 测试文件 | 覆盖范围 | 推荐定向命令 |
|---|---|---|
| `tests/test_kernel.py` | 微内核生命周期、插件发现与裸核开关 | `python -m unittest tests/test_kernel.py -v` |
| `tests/test_core_hooks.py` | 核心钩子注册、派发顺序与容错隔离 | `python -m unittest tests/test_core_hooks.py -v` |
| `tests/test_panel_ui_slots.py` | 面板薄壳与 UI 插槽渲染契约 | `python -m unittest tests/test_panel_ui_slots.py -v` |
| `tests/test_plugin_eoq_guard.py` | 内置 `eoq_guard` 水位采样与护航 | `python -m unittest tests/test_plugin_eoq_guard.py -v` |
| `tests/test_plugin_tasks_board.py` | 内置 `tasks_board` 任务包解析与看板 | `python -m unittest tests/test_plugin_tasks_board.py -v` |
| `tests/test_plugin_version_vault.py` | 内置 `version_vault` 扫描与冷库外迁 | `python -m unittest tests/test_plugin_version_vault.py -v` |
| `tests/test_daily_log_archive.py` | 日常日志归档、轮转与留存策略 | `python -m unittest tests/test_daily_log_archive.py -v` |
| `tests/test_public_suite.py` (~1710L) | 公开态综合回归与洁净室静态门禁 | `python -m unittest discover -s tests -p "test_public_suite.py" -k "clean_room" -v` |

常用校验命令（在 `acerola-orion-public` 根目录执行）：

```powershell
# 1. 洁净室与零外联门禁（改文档或源码必跑）
python -m unittest discover -s tests -p "test_public_suite.py" -k "clean_room" -v

# 2. 纯净裸核模式冒烟（屏蔽外部插件发现）
$env:ACEROLA_ORION_DISABLE_PLUGINS="1"; python -m acerola_orion --help; Remove-Item Env:ACEROLA_ORION_DISABLE_PLUGINS

# 3. 公开仓全量单测回归
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## 5. 洁净室门禁与修改纪律

1. **洁净室扫描覆盖本文件**：`tests/test_public_suite.py` 的 `clean_room` 用例自动扫描全部 `.py` 源码、`pyproject.toml`、`assets/logo.svg`、`AGENTS.md`、`README*.md` 与 `CHANGELOG*.md`。严禁在受检文件中出现：
   - 任何外部私有扩展包名、专有网关切流/路由控制符号、第三方 HTTP 客户端导入；
   - 本地凭据库路径、系统解密 API 名称或个人机器绝对路径。
2. **最小改动与非破坏性留底**：
   - 严格聚焦目标行段，禁止顺手重排或格式化无关大文件；严禁将业务状态机下沉进 `panel.py` 或 `ui/tabs/*` 薄壳。
   - 跨版重构或整文件重写前先留底至 `_versions/`；迭代验收后经版本冷库外迁归档，严禁直接删除历史版本。
3. **AI 代理施工速查**：
   - 修改前先核对第 1 节正交边界与第 3 节大文件行号锚点，优先按行段切片读取，避免整文件通读。
   - 涉及内置插件（`version_vault`、`tasks_board`、`eoq_guard`）的需求均在公开仓 `acerola_orion/plugins/` 内完成，不把公开能力拆分到外部扩展。
   - 禁止在公开仓引入任何跨子系统隐式耦合（如让 `ledger.py` 保存正文，或让 `vault.py` 扫描/复制二进制媒体文件）。
   - 改动源码、架构文档或门禁清单后，至少通过受影响模块单测以及 `test_public_suite.py -k "clean_room"` 双重校验。
   - 如需查询外部插件挂载协议与 UI 槽位契约细节，按需阅读 `docs/PLUGIN_ARCHITECTURE.md`，禁止凭记忆猜测钩子签名。
