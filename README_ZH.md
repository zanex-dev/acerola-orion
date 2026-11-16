<p align="center">
  <img src="assets/hero-banner.png" width="100%" alt="Acerola-Orion — Local Quota Observatory · Token Ledger · Zero-Polling Exit Guard" />
</p>

<h1 align="center">Acerola-Orion (雅赛劳拉莉昂)</h1>

<p align="center">
  <strong>面向 Google Antigravity 的 7 位高精度配额水位看板、本地隐私隔离 Token 明细验钞账本（Orion）与零轮询 Win32 内核孤儿进程退出守卫（Acerola）。</strong>
</p>

<p align="center">
  <a href="https://github.com/zanex-dev/acerola-orion/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/tests-14%2F14%20passing-brightgreen" alt="Tests" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-v1.2.0-be123c" alt="Version v1.2.0" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-e11d48.svg" alt="License: MIT" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B%20(0%20deps)-3776AB.svg" alt="Python 3.10+" /></a>
  <img src="https://img.shields.io/badge/network-127.0.0.1%20loopback%20only-059669" alt="Loopback Only" />
</p>

<p align="center">
  <a href="README.md"><strong>English</strong></a> · <a href="README_ZH.md"><strong>简体中文</strong></a> · <a href="CHANGELOG_ZH.md">更新日志</a>
</p>

---

## 🔒 零信任安全自证（3 秒源码验毒）

针对社区近期对第三方工具窃取登录态或诱导异常授权的担忧，本项目在架构层执行最严苛的本地物理边界，欢迎任何人直接审计明文源码：

1. **零第三方依赖（`dependencies = []`）**：100% 构建于 Python 3.10+ 标准库与原生 Win32 `ctypes`，拒绝引入任何第三方 `pip` 供应链黑盒包。
2. **零外网连接（硬编码锁死 `127.0.0.1`）**：全仓唯一的 `HTTPConnection` 在构造函数层硬性断言 `host == "127.0.0.1"`，仅向本机运行中的官方客户端回环端口发起只读状态查询，**绝不向谷歌或任何外部服务器发送 1 字节网络包**。
3. **零凭证与零隐私触碰**：
   - **绝不**请求任何 OAuth 授权，**绝不**读取 `state.vscdb`、Windows DPAPI 凭据库、浏览器 Cookie 或任何 Token；
   - 本地账本仅只读统计 `transcript.jsonl` 中的 Token 计数字段与会话标题，**不存储对话正文**、自动跳过所有媒体附件并拦截符号链接/目录联接越界；终端与 JSON 输出自动脱敏，不回显个人登录邮箱。
4. **一键源码自查命令（PowerShell 3 秒验真）**：

```powershell
# 一键检索全仓所有网络、凭据与外部进程调用，结果一目了然：
Select-String -Path .\acerola_orion\*.py -Pattern "HTTPConnection|socket|subprocess|vscdb|Cookie|oauth"
```

---

## 核心能力矩阵

### 🔭 1. Orion · 高精度配额透视与本地 Token 验钞账本
1. **7 位浮点精度双通道配额看板 (`Orion-Telemetry`)**
   - 原生 Win32 `CreateToolhelp32Snapshot` 亚毫秒级进程树识别 + 本机 `127.0.0.1` 只读 Connect-RPC 查询（`GetUserStatus`、`GetLoadCodeAssist`、`RetrieveUserQuotaSummary`）；
   - 绕过前端 UI 模糊进度条，精确直读 `gemini-5h`、`gemini-weekly`、`3p-5h`、`3p-weekly` 剩余百分比与重置倒计时；
   - 采用 Win32 `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` 无锁增量游标解析本地日志，统计 `HTTP 429/503` 限流次数与旁路风控模型（`sir_flash_lite` / `severe_internal_risks_agy`）附加延迟耗时。
2. **GPT 同款本地 Token 明细账本 & `1.5x` 电子秤验钞机 (`Orion-Ledger`)**
   - 按**日期、模型家族（4C / 4A / 4H / Flash / Claude / GPT-OSS）、单次会话**精确统计 `Input`、`Cache-Read`、`Output` Token，并列折算 **1.0x 官方 API 原价**、**0.5x 首发促销半价** 与 **1.5x 估算倍率权重**；
   - **幽灵掉点对账**：比对历史配额水位快照与真实对话步数，自动识别零对话区间的**离线幽灵掉点（Phantom Drain）**，并实测推算等效总额度池容量。

### ⚔️ 2. Acerola · 零轮询内核句柄孤儿进程断头台
- **三大核心防坑价值（关窗即断，拒绝后台小动作）**：
  1. **防止后台自动压缩上下文**：官方客户端在关闭主窗口后，残留的 `language_server*.exe` 孤儿进程往往不会立即退出，而是继续在后台静默触发上下文自动压缩（Auto-Compaction），把你原本保留完整的长会话上下文擅自裁切；
  2. **防止污染本地工程记忆**：阻止孤儿进程在你关窗收工后静默执行后台记忆蒸馏（Knowledge / Memory Distillation），避免将未完结的调试废稿、报错状态或幻觉写入跨会话工程记忆库；
  3. **防止偷跑顶级模型额度**：孤儿进程会直接继承你关窗前挂载的昂贵主力模型（如 `Gemini 4 Carbon / Argon` 或 `Claude Opus`）在后台暗扣配额，是“下线后配额莫名掉点”的头号元凶。
- **零轮询内核级秒斩**：直接对真实 Electron UI 根主进程持有 Win32 `SYNCHRONIZE` 内核句柄并阻塞于 `WaitForMultipleObjects`——日常挂机、窗口最小化、托盘隐藏期间 **0.00% CPU 占用**，绝不误杀；一旦主程序真正关闭退出，**1 毫秒内**内核态唤醒，经同句柄映像名二次校验后精准清理残留孤儿 `language_server*.exe` 与陈旧 `hub_*.json`。

---

## ⚔️ 断头台守卫（Acerola Guard）使用与后台常驻说明

> 💡 **核心机制：开启一次，永久无感驻留（与 GUI 仪表盘完全独立！）**
> - **只需开启一次**：无论通过桌面主控台点击 **`🛡️ 自启守卫`**，还是在终端执行一次 `acerola-orion guard install`，断头台就会作为独立的无界面后台守护进程一直留在系统中（`0.00% CPU` 内核睡眠监听）。
> - **开机静默自启，绝不弹仪表盘**：注册自启后，每次电脑开机登录时**仅在后台静默拉起断头台守卫进程，绝不会弹黑框，也绝不会自动唤起 GUI 仪表盘打扰你**。
> - **彻底关掉仪表盘也照常生效**：桌面仪表盘（`panel`）仅用于你想看星盘水位或查账单时按需打开；哪怕你点击“彻底退出”关掉仪表盘，后台断头台依然全天候值守，每次关掉 Antigravity 主窗口都会自动秒斩残留孤儿进程。

| 使用场景 | 图形界面操作 (`acerola-orion panel`) | 命令行操作 (PowerShell / CMD) |
| :--- | :--- | :--- |
| **一键开启并开机静默自启（推荐）** | 点击守卫卡片 **`🛡️ 自启守卫`**（或托盘右键启用） | `acerola-orion guard install` |
| **查看断头台当前是否在后台运行** | 查看守卫卡片是否显示 `🟢 武装就绪 · 0.00% CPU` | `acerola-orion guard status` |
| **仅本次临时启动 / 停止（不写自启）** | — | `acerola-orion guard start` / `stop` |
| **彻底停用并干净移除开机自启** | 点击守卫卡片 **`⏹️ 停用`**（或托盘右键停用） | `acerola-orion guard uninstall` |
| **查看历史斩杀记录日志** | 切换至 **「🩺 旁路风控示波与守护日志」** 页签 | 查看 `~/.gemini/acerola_orion/guillotine.log` |

---

## 快速使用

### 1. 按需运行（无需安装后台自启）

```powershell
git clone https://github.com/zanex-dev/acerola-orion.git
cd acerola-orion
pip install -e . --no-deps

# 实时 7 位精度配额水位、订阅层级与旁路延迟体检
acerola-orion status

# 近 14 天每日 Token 明细账单、模型倍率审计与验钞报告
acerola-orion ledger --days 14

# 单独执行配额水位采样与幽灵掉点审计
acerola-orion audit

# 启动猎户座星盘桌面主控台（支持 DWM 沉浸式暗黑窗框、点 × 自动缩入托盘 0% CPU 休眠）
acerola-orion panel

# 一键以「极简专注模式」启动（同屏仅聚焦 1 个核心指标 + 1 张趋势统计图，亦可在界面右上角随时切换）
acerola-orion panel --zen

# 可选：一键注册 Windows 开始菜单与任务栏独立图标快捷方式（附加 --remove 可移除）
acerola-orion shortcut
```

### 2. 可选：开机自启孤儿进程守卫（完全透明可逆）

```powershell
# 启用并注册当前用户登录自启守卫
acerola-orion guard install

# 一键彻底停止守卫并移除计划任务与启动脚本
acerola-orion guard uninstall
```

> **系统驻留透明度说明**：执行 `guard install` 仅在用户目录 `~/.gemini/acerola_orion/start_guard_silent.vbs` 生成 3 行明文 VBScript（用于在登录时不弹黑框拉起 `pythonw.exe -m acerola_orion guard daemon`），并注册当前用户级计划任务 `AcerolaOrionGuard`（无需管理员提权、不修改注册表或客户端 `app.asar`）。执行 `guard uninstall` 即可一键彻底清除。

### 3. 运行安全自审与单元测试套件

```powershell
python -m unittest discover -s tests -v
```

---

## ⚠️ 非官方声明与免责边界 (Disclaimer)

1. 本项目为独立开发的纯本地成本记账、配额观测与退出后孤儿进程回收工具，**与 Google 无任何隶属、赞助或官方关联**。
2. 本工具全程仅读取本地文本日志与本机 `127.0.0.1` 回环状态接口，**不修改客户端安装包（`app.asar`）、不破坏数字签名、不提取任何账号凭证、不代理或篡改任何云端请求**。
3. 本软件按 MIT 许可证“原样（AS IS）”提供，仅供个人学习与本地可观测性研究，使用者自担风险。若使用者同时或过往使用过第三方 OAuth 登录态提取器、`2API` 逆向反代池、篡改版客户端或非正规共享/转售账号，由此引发的一切账号风控或限制均与本纯本地工具无关。

## 开源许可

基于 [MIT License](LICENSE) 开源发布。
