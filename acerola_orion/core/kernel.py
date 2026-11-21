# -*- coding: utf-8 -*-
"""
Microkernel plugin registry & fault-isolated hook bus for Acerola-Orion.

Pure standard library (Python >= 3.10). Discovers optional extensions via
`importlib.metadata.entry_points` under the `acerola_orion.plugins` group
unless disabled with `ACEROLA_ORION_DISABLE_PLUGINS=1|true`.
"""

from __future__ import annotations

import importlib.metadata
import itertools
import logging
import os
from pathlib import Path
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

logger = logging.getLogger("acerola_orion.core.kernel")

PLUGIN_ENTRYPOINT_GROUP = "acerola_orion.plugins"
DISABLE_PLUGINS_ENV = "ACEROLA_ORION_DISABLE_PLUGINS"
DISABLED_PLUGINS_ENV = "ACEROLA_ORION_DISABLED_PLUGINS"


class HookEvents:
    """Canonical neutral hook event names emitted by the Acerola-Orion core."""

    PROBE_GUARD: str = "probe_guard"
    ENRICH_TELEMETRY: str = "enrich_telemetry"
    RENDER_STATUS_LINES: str = "render_status_lines"
    ENRICH_RADAR: str = "enrich_radar"
    GUARD_START: str = "guard_start"
    GUARD_LS_SEEN: str = "guard_ls_seen"
    GUARD_TICK: str = "guard_tick"
    GUARD_STOP: str = "guard_stop"

    ALL: Tuple[str, ...] = (
        PROBE_GUARD,
        ENRICH_TELEMETRY,
        RENDER_STATUS_LINES,
        ENRICH_RADAR,
        GUARD_START,
        GUARD_LS_SEEN,
        GUARD_TICK,
        GUARD_STOP,
    )


_PLUGIN_METHOD_NAMES: Tuple[str, ...] = (
    "register_cli",
    "handle_cli",
    "extra_launchers",
    "register_hooks",
    "register_ui",
)


def plugins_disabled() -> bool:
    """Return True when external plugin discovery is disabled via environment variable."""
    raw = os.environ.get(DISABLE_PLUGINS_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def disabled_plugin_names() -> frozenset[str]:
    """Return lower-cased plugin or entry-point names disabled via `ACEROLA_ORION_DISABLED_PLUGINS`."""
    raw = os.environ.get(DISABLED_PLUGINS_ENV, "")
    if not raw:
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_plugin_disabled(name: str) -> bool:
    """Return True when `name` is disabled globally or listed in `ACEROLA_ORION_DISABLED_PLUGINS`."""
    if plugins_disabled():
        return True
    norm = str(name or "").strip().lower()
    if not norm:
        return False
    return norm in disabled_plugin_names()


@runtime_checkable
class OrionPlugin(Protocol):
    """
    Neutral extension contract for Acerola-Orion plugins.

    Plugins may implement any subset of the optional methods declared below;
    the kernel inspects each hook dynamically and isolates failures per plugin.
    """

    def register_cli(self, subparsers: Any) -> None: ...

    def handle_cli(self, cmd: str, args: Any) -> Optional[int]: ...

    def extra_launchers(
        self, py_exe: str, pyw_exe: str, pkg_root: Path | str
    ) -> Dict[str, str]: ...

    def register_hooks(self, bus: "HookBus") -> None: ...

    def register_ui(self, ui_host: Any) -> None: ...


class HookBus:
    """
    Thread-safe, fault-isolated publish/subscribe event bus.

    A failing listener is logged and skipped without blocking subsequent
    listeners or propagating exceptions into the core host flow.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._handlers: Dict[str, List[Tuple[int, int, Callable[..., Any]]]] = {}

    def on(
        self,
        event: str,
        fn: Callable[..., Any],
        *,
        priority: int = 0,
    ) -> Callable[[], None]:
        """Subscribe `fn` to `event` and return an idempotent unsubscribe callback."""
        if not isinstance(event, str) or not event.strip():
            raise ValueError("Hook event name must be a non-empty string")
        if not callable(fn):
            raise TypeError("Hook handler must be callable")
        seq = next(self._seq)
        entry = (int(priority), seq, fn)
        with self._lock:
            bucket = self._handlers.setdefault(event, [])
            bucket.append(entry)
            bucket.sort(key=lambda item: (item[0], item[1]))

        def _unsubscribe() -> None:
            with self._lock:
                items = self._handlers.get(event)
                if not items:
                    return
                remaining = [item for item in items if item[1] != seq]
                if remaining:
                    self._handlers[event] = remaining
                else:
                    self._handlers.pop(event, None)

        return _unsubscribe

    def off(self, event: str, fn: Callable[..., Any]) -> bool:
        """Remove all registrations of `fn` for `event`. Returns True if any removed."""
        with self._lock:
            items = self._handlers.get(event)
            if not items:
                return False
            remaining = [item for item in items if item[2] is not fn and item[2] != fn]
            removed = len(remaining) != len(items)
            if remaining:
                self._handlers[event] = remaining
            else:
                self._handlers.pop(event, None)
            return removed

    def has_listeners(self, event: str) -> bool:
        """Return True if at least one handler is registered for `event`."""
        with self._lock:
            return bool(self._handlers.get(event))

    def listener_count(self, event: str) -> int:
        """Return the number of handlers currently registered for `event`."""
        with self._lock:
            return len(self._handlers.get(event, ()))

    def clear(self, event: Optional[str] = None) -> None:
        """Clear handlers for a specific event, or all events when `event` is None."""
        with self._lock:
            if event is None:
                self._handlers.clear()
            else:
                self._handlers.pop(event, None)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> List[Any]:
        """
        Invoke all handlers for `event` in `(priority, registration_order)` order.

        Returns a list of return values from handlers that completed without raising.
        Any exception raised by an individual handler is logged and isolated.
        """
        with self._lock:
            snapshot = list(self._handlers.get(event, ()))
        results: List[Any] = []
        for _prio, _seq, fn in snapshot:
            try:
                results.append(fn(*args, **kwargs))
            except Exception as exc:
                logger.warning(
                    "HookBus handler %r failed on event %r: %s",
                    fn,
                    event,
                    exc,
                    exc_info=True,
                )
        return results


def _looks_like_plugin_instance(obj: Any) -> bool:
    return any(callable(getattr(obj, m, None)) for m in _PLUGIN_METHOD_NAMES)


def _plugin_label(plugin: Any) -> str:
    name = getattr(plugin, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return type(plugin).__name__


def _iter_entry_points(group: str) -> List[Any]:
    try:
        eps = importlib.metadata.entry_points(group=group)
    except TypeError:
        raw_eps = importlib.metadata.entry_points()
        if hasattr(raw_eps, "select"):
            eps = raw_eps.select(group=group)
        elif isinstance(raw_eps, dict):
            eps = raw_eps.get(group, ())
        else:
            eps = [ep for ep in raw_eps if getattr(ep, "group", None) == group]
    else:
        if isinstance(eps, dict):
            eps = eps.get(group, ())
    return sorted(
        list(eps),
        key=lambda ep: (str(getattr(ep, "name", "")), str(getattr(ep, "value", ""))),
    )


class OrionKernel:
    """
    Central microkernel registry coordinating plugin discovery, hook subscriptions,
    CLI subcommand extensions, launcher generation, and UI host slots.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.bus = HookBus()
        self._plugins: List[Any] = []
        self._plugin_ids: set[int] = set()
        self._loaded_ep_keys: set[Tuple[str, str]] = set()
        self._load_errors: List[Tuple[str, str]] = []
        self._discovered = False

    @property
    def plugins(self) -> Tuple[Any, ...]:
        """Immutable snapshot of registered plugins in load order."""
        with self._lock:
            return tuple(self._plugins)

    @property
    def load_errors(self) -> Tuple[Tuple[str, str], ...]:
        """Immutable snapshot of `(plugin_or_ep_name, error_repr)` encountered."""
        with self._lock:
            return tuple(self._load_errors)

    @property
    def discovered(self) -> bool:
        """Whether `discover_plugins()` has run on this kernel instance."""
        with self._lock:
            return self._discovered

    def has_plugin(self, name: str) -> bool:
        """Return True if a registered plugin matches `name` (by `.name` or class name)."""
        with self._lock:
            for plugin in self._plugins:
                if getattr(plugin, "name", None) == name or type(plugin).__name__ == name:
                    return True
            return False

    def register(self, plugin: Any, *, source: str = "manual") -> bool:
        """
        Register a plugin object and bind its `register_hooks(bus)` if defined.
        Returns True if newly registered, False if ignored (None or already registered).
        """
        if plugin is None:
            return False
        pid = id(plugin)
        with self._lock:
            if pid in self._plugin_ids:
                return False
            self._plugin_ids.add(pid)
            self._plugins.append(plugin)

        register_hooks = getattr(plugin, "register_hooks", None)
        if callable(register_hooks):
            try:
                register_hooks(self.bus)
            except Exception as exc:
                label = _plugin_label(plugin)
                logger.warning(
                    "Plugin %s (%s) failed in register_hooks: %s",
                    label,
                    source,
                    exc,
                    exc_info=True,
                )
                with self._lock:
                    self._load_errors.append((label, f"register_hooks: {exc!r}"))
        return True

    def discover_plugins(
        self,
        group: str = PLUGIN_ENTRYPOINT_GROUP,
        *,
        force: bool = False,
    ) -> List[Any]:
        """
        Discover and load plugins advertised under `entry_points(group=group)`.
        Skips discovery completely when `ACEROLA_ORION_DISABLE_PLUGINS` is truthy.
        """
        with self._lock:
            if self._discovered and not force:
                return list(self._plugins)
            self._discovered = True
            if plugins_disabled():
                logger.debug(
                    "Plugin discovery skipped because %s is enabled",
                    DISABLE_PLUGINS_ENV,
                )
                return list(self._plugins)

        try:
            eps = _iter_entry_points(group)
        except Exception as exc:
            logger.warning(
                "Failed to enumerate entry_points for group %r: %s",
                group,
                exc,
                exc_info=True,
            )
            with self._lock:
                self._load_errors.append((group, f"entry_points: {exc!r}"))
                return list(self._plugins)

        disabled_names = disabled_plugin_names()
        newly_loaded: List[Any] = []
        for ep in eps:
            ep_name = str(getattr(ep, "name", "<unknown>"))
            ep_value = str(getattr(ep, "value", ""))
            if ep_name.strip().lower() in disabled_names:
                logger.debug(
                    "Plugin entry point %r skipped because it is listed in %s",
                    ep_name,
                    DISABLED_PLUGINS_ENV,
                )
                continue
            ep_key = (ep_name, ep_value)
            with self._lock:
                if ep_key in self._loaded_ep_keys:
                    continue
                self._loaded_ep_keys.add(ep_key)
            try:
                loaded = ep.load()
                if isinstance(loaded, type):
                    plugin = loaded()
                elif callable(loaded) and not _looks_like_plugin_instance(loaded):
                    plugin = loaded()
                else:
                    plugin = loaded
                if plugin is None:
                    continue
                p_name = str(getattr(plugin, "name", "") or "").strip().lower()
                p_cls = type(plugin).__name__.strip().lower()
                if (p_name and p_name in disabled_names) or (p_cls in disabled_names):
                    logger.debug(
                        "Plugin %r (entry point %r) skipped because it is listed in %s",
                        _plugin_label(plugin),
                        ep_name,
                        DISABLED_PLUGINS_ENV,
                    )
                    with self._lock:
                        self._loaded_ep_keys.discard(ep_key)
                    continue
                if self.register(plugin, source=f"entry_point:{ep_name}"):
                    newly_loaded.append(plugin)
            except Exception as exc:
                logger.warning(
                    "Failed to load plugin entry point %r (%s): %s",
                    ep_name,
                    ep_value,
                    exc,
                    exc_info=True,
                )
                with self._lock:
                    self._load_errors.append((ep_name, f"load: {exc!r}"))
        return newly_loaded

    def register_cli(self, subparsers: Any) -> None:
        """Let every plugin attach optional CLI subcommands to `subparsers`."""
        for plugin in self.plugins:
            fn = getattr(plugin, "register_cli", None)
            if not callable(fn):
                continue
            try:
                fn(subparsers)
            except Exception as exc:
                logger.warning(
                    "Plugin %s failed in register_cli: %s",
                    _plugin_label(plugin),
                    exc,
                    exc_info=True,
                )

    def handle_cli(self, cmd: str, args: Any) -> Optional[int]:
        """Dispatch CLI command `cmd` to plugins; returns first non-None exit code."""
        for plugin in self.plugins:
            fn = getattr(plugin, "handle_cli", None)
            if not callable(fn):
                continue
            try:
                res = fn(cmd, args)
            except Exception as exc:
                logger.warning(
                    "Plugin %s failed in handle_cli(%r): %s",
                    _plugin_label(plugin),
                    cmd,
                    exc,
                    exc_info=True,
                )
                continue
            if res is not None:
                return int(res)
        return None

    def collect_extra_launchers(
        self, py_exe: str, pyw_exe: str, pkg_root: Path | str
    ) -> Dict[str, str]:
        """Collect extra launcher definitions from all plugins as `{filename: content}`."""
        merged: Dict[str, str] = {}
        for plugin in self.plugins:
            fn = getattr(plugin, "extra_launchers", None)
            if not callable(fn):
                continue
            try:
                extra = fn(py_exe, pyw_exe, pkg_root)
            except Exception as exc:
                logger.warning(
                    "Plugin %s failed in extra_launchers: %s",
                    _plugin_label(plugin),
                    exc,
                    exc_info=True,
                )
                continue
            if not isinstance(extra, dict):
                logger.warning(
                    "Plugin %s extra_launchers returned non-dict %r; ignored",
                    _plugin_label(plugin),
                    type(extra).__name__,
                )
                continue
            for k, v in extra.items():
                if isinstance(k, str) and isinstance(v, str):
                    merged[k] = v
        return merged

    def register_ui(self, ui_host: Any) -> None:
        """Let every plugin attach optional tabs, toolbar actions, or tray slots."""
        for plugin in self.plugins:
            fn = getattr(plugin, "register_ui", None)
            if not callable(fn):
                continue
            try:
                fn(ui_host)
            except Exception as exc:
                logger.warning(
                    "Plugin %s failed in register_ui: %s",
                    _plugin_label(plugin),
                    exc,
                    exc_info=True,
                )

    def emit(self, event: str, *args: Any, **kwargs: Any) -> List[Any]:
        """Convenience wrapper around `self.bus.emit(event, *args, **kwargs)`."""
        return self.bus.emit(event, *args, **kwargs)


_KERNEL_LOCK = threading.RLock()
_KERNEL_INSTANCE: Optional[OrionKernel] = None


def get_kernel(*, auto_discover: bool = True) -> OrionKernel:
    """Return the process-wide `OrionKernel` singleton, discovering plugins once by default."""
    global _KERNEL_INSTANCE
    with _KERNEL_LOCK:
        if _KERNEL_INSTANCE is None:
            _KERNEL_INSTANCE = OrionKernel()
        kernel = _KERNEL_INSTANCE
    if auto_discover and not kernel.discovered:
        kernel.discover_plugins()
    return kernel


def reset_kernel_for_tests() -> None:
    """Reset the global kernel singleton and clear hook subscriptions (test helper)."""
    global _KERNEL_INSTANCE
    with _KERNEL_LOCK:
        if _KERNEL_INSTANCE is not None:
            _KERNEL_INSTANCE.bus.clear()
        _KERNEL_INSTANCE = None


__all__ = [
    "PLUGIN_ENTRYPOINT_GROUP",
    "DISABLE_PLUGINS_ENV",
    "DISABLED_PLUGINS_ENV",
    "HookEvents",
    "OrionPlugin",
    "HookBus",
    "OrionKernel",
    "plugins_disabled",
    "disabled_plugin_names",
    "is_plugin_disabled",
    "get_kernel",
    "reset_kernel_for_tests",
]
