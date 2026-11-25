# Acerola-Orion 微内核与插件开发架构指南

本文档描述公开白帽底座 `acerola-orion-public` 的微内核扩展体系，涵盖插件自动发现、环境变量隔离、中立事件总线（`HookBus` / `HookEvents`）、桌面端 UI 插槽（`PanelPluginHost`）与通用原子配置读写规范。

---

## 1. 架构概览

`acerola-orion-public` 采用 **纯标准库微内核（Core）+ 可插拔扩展（Plugins）** 的正向叠加设计：

- **零外部强依赖**：微内核（`acerola_orion/core/kernel.py`）仅使用 Python >= 3.10 标准库，无网络客户端依赖，可在无插件环境下独立完整运行。
- **单向依赖边界**：底座永远不硬编码导入任何外部插件包；插件通过标准 Python `importlib.metadata.entry_points` 向底座单向注册能力。
- **严格故障隔离**：任意插件在加载、钩子执行、CLI 分发、启动器生成或 UI 插槽注册期间抛出的异常，均由内核捕获并记录告警日志，绝不阻塞主流程或其他插件。

---

## 2. 插件发现与环境隔离开关

### 2.1 Entry Point 约定

第三方或本地扩展包在各自的 `pyproject.toml` 中声明如下入口点组（对应常量 `PLUGIN_ENTRYPOINT_GROUP = "acerola_orion.plugins"`）：

```toml
[project.entry-points."acerola_orion.plugins"]
my_extension = "my_extension.plugin:MyExtensionPlugin"
```

内核 `OrionKernel.discover_plugins()` 支持以下三种入口点目标形式，并按 `(entry_point.name, entry_point.value)` 自动去重：

1. 插件类（无参实例化）；
2. 工厂函数（无参调用返回插件对象，返回 `None` 时静默忽略）；
3. 已构造好的插件实例（实现至少一个 `OrionPlugin` 方法）。

### 2.2 双层环境变量开关

| 环境变量 | 对应常量 / 辅助函数 | 作用 |
| --- | --- | --- |
| `ACEROLA_ORION_DISABLE_PLUGINS=1\|true\|yes\|on` | `DISABLE_PLUGINS_ENV` / `plugins_disabled()` | 全局跳过 `entry_points` 扫描，进入纯净裸核模式 |
| `ACEROLA_ORION_DISABLED_PLUGINS=name_a,name_b` | `DISABLED_PLUGINS_ENV` / `disabled_plugin_names()` / `is_plugin_disabled(name)` | 逗号分隔、大小写不敏感地禁用指定插件（匹配入口点名、插件 `.name` 属性或类名）；命中入口点名时跳过 `ep.load()`，不触发其导入副作用 |

### 2.3 官方内置白帽可选插件入口点（随包注册，可逐个自选关闭）

`acerola-orion` 裸核保持纯标准库 **0 第三方依赖**，同时在 `pyproject.toml` 的 `[project.entry-points."acerola_orion.plugins"]` 下内置注册以下 3 个官方白帽可选插件：

| Entry Point 名称 | 能力摘要 |
| --- | --- |
| `version_vault` | 不可删工作区版本冷库（`~/.gemini/acerola_orion/version_vault/`）+ `acerola-orion archive` CLI 与主控台冷库页签 |
| `tasks_board` | 只读 `.agents/tasks/` 任务拆解包与阶段交接单看板页签 |
| `eoq_guard` | 200K 上下文 EOQ 水位提示与单窗免限切换控件 |

按需禁用示例（PowerShell）：

```powershell
# 仅关闭指定插件（逗号分隔，可自由组合，例如关闭 eoq_guard 或 tasks_board,eoq_guard）
$env:ACEROLA_ORION_DISABLED_PLUGINS="eoq_guard"
acerola-orion panel

# 一键进入 0 插件纯净裸核模式
$env:ACEROLA_ORION_DISABLE_PLUGINS="1"
acerola-orion status
```

---

## 3. 插件协议契约（`OrionPlugin`）

`acerola_orion.core.OrionPlugin` 是 `@runtime_checkable` 的结构化协议（`typing.Protocol`），插件可按需实现以下任意子集：

```python
from pathlib import Path
from typing import Any, Dict, Optional
from acerola_orion.core import HookBus, HookEvents


class SamplePlugin:
    name = "sample_plugin"

    def register_cli(self, subparsers: Any) -> None:
        """向主 CLI 挂载自定义子命令。"""
        subparsers.add_parser("sample-status", help="输出示例扩展状态")

    def handle_cli(self, cmd: str, args: Any) -> Optional[int]:
        """处理对应子命令；返回 int 退出码表示已消费，返回 None 交由后续插件。"""
        if cmd == "sample-status":
            print("sample_plugin ready")
            return 0
        return None

    def extra_launchers(
        self, py_exe: str, pyw_exe: str, pkg_root: Path | str
    ) -> Dict[str, str]:
        """返回需额外生成的启动器脚本映射 {文件名: 脚本内容}。"""
        return {}

    def register_hooks(self, bus: HookBus) -> None:
        """订阅中立生命周期与遥测事件。"""
        bus.on(HookEvents.RENDER_STATUS_LINES, self._render_lines, priority=10)

    def register_ui(self, ui_host: Any) -> None:
        """向桌面星盘 PanelPluginHost 注册自定义标签页、动作槽、托盘项或雷达刻度线。"""
        pass

    def _render_lines(self, report: Any) -> list[str]:
        return ["SamplePlugin: active"]
```

---

## 4. 中立事件总线（`HookBus`）与标准钩子（`HookEvents`）

`HookBus` 提供线程安全、支持优先级排序的发布/订阅模型：

- `bus.on(event, fn, priority=0)`：按 `(priority, registration_order)` 升序触发，返回幂等取消订阅闭包。
- `bus.off(event, fn)` / `bus.clear(event=None)`：按处理器或事件名解除订阅。
- `bus.emit(event, *args, **kwargs)`：依次调用处理器并收集未抛异常的返回值列表。

内核在 `acerola_orion.core.HookEvents` 中集中声明 8 个中立钩子常量（也可通过 `HookEvents.ALL` 枚举）：

| 常量 | 事件名 | 触发时机与典型用途 |
| --- | --- | --- |
| `HookEvents.PROBE_GUARD` | `probe_guard` | 探测本地守护进程状态时补充辅助诊断信息 |
| `HookEvents.ENRICH_TELEMETRY` | `enrich_telemetry` | 聚合遥测报告后附加只读扩展指标字段 |
| `HookEvents.RENDER_STATUS_LINES` | `render_status_lines` | CLI 状态摘要渲染时追加自定义展示行 |
| `HookEvents.ENRICH_RADAR` | `enrich_radar` | KV 缓存雷达采样后追加扩展上下文元数据 |
| `HookEvents.GUARD_START` | `guard_start` | 守护进程启动初始化完成时通知插件 |
| `HookEvents.GUARD_LS_SEEN` | `guard_ls_seen` | 守护进程观测到目标本地语言服务进程时触发 |
| `HookEvents.GUARD_TICK` | `guard_tick` | 守护进程主循环周期心跳回调 |
| `HookEvents.GUARD_STOP` | `guard_stop` | 守护进程优雅退出前清理扩展资源 |

---

## 5. 桌面端 UI 扩展插槽（`PanelPluginHost`）

桌面星盘（`acerola_orion/panel.py`）通过 `acerola_orion.ui.plugin_host.PanelPluginHost` 向 `OrionPlugin.register_ui(ui_host)` 暴露受控 UI 扩展点，所有插件槽位均与内置 4 个标签页隔离：

- **动态标签页（Tab）**：注册独立 `ttk.Frame` 构建器与数据刷新回调，不可覆盖内置保留 Tab 键。
- **Hub 动作槽**：在控制台总览页注入自定义只读/工具动作按钮。
- **系统托盘菜单槽**：在 Win32 托盘右键菜单追加自定义命令项。
- **KV 缓存雷达自定义刻度线**：通过 `collect_radar_markers(state)` 在雷达进度条上叠加自定义颜色、虚线与标签刻度。
- **顶栏状态徽章**：在窗口顶部状态栏展示简短只读扩展徽章。

窗体基础能力（DPI 感知、Win32 DWM 暗色标题栏、UI 偏好持久化与单实例切换）已收敛至 `acerola_orion.ui.window_chrome`，KV 缓存雷达与悬浮胶囊状态机已收敛至纯函数模块 `acerola_orion.ui.radar_controller`。

---

## 6. 通用原子配置持久化

插件可在不引入独立配置文件的前提下，复用底座的跨进程安全 JSON 读写接口（`acerola_orion.constants`）：

- `load_app_config()`：安全读取主配置 JSON，自动兼容尾逗号并在损坏时回退默认字典。
- `update_app_config(mutator)`：在跨进程文件锁保护下执行原地修改或返回新字典的原子写入，严格保留其他模块或插件写入的未知扩展键；遇到损坏文件自动生成带时间戳的 `.corrupt-*.bak` 备份后再恢复写入。

建议插件将自身配置收纳于独立命名空间键（例如 `cfg["my_extension"] = {...}`），避免与核心键名冲突。

---

## 7. 测试与验收建议

1. **单测隔离**：在 `setUp()` 与 `tearDown()` 中调用 `acerola_orion.core.reset_kernel_for_tests()`，确保全局内核单例与事件总线互不污染。
2. **双态验证**：
   - 裸核态：设置 `ACEROLA_ORION_DISABLE_PLUGINS=1` 运行底座测试，确认无插件依赖时全绿；
   - 组合态：启用插件（或通过 `ACEROLA_ORION_DISABLED_PLUGINS` 选择性屏蔽单个插件）运行集成测试，验证钩子与 UI 插槽均按契约工作。
3. **洁净室边界**：公开底座仓的源码与文档不得引入任何外部私有实现细节或网络客户端依赖，所有扩展应以独立插件包形式发布与安装。
