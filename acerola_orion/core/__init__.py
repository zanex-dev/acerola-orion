# -*- coding: utf-8 -*-
"""
Core microkernel primitives for Acerola-Orion.
"""

from .kernel import (
    DISABLE_PLUGINS_ENV,
    DISABLED_PLUGINS_ENV,
    PLUGIN_ENTRYPOINT_GROUP,
    HookBus,
    HookEvents,
    OrionKernel,
    OrionPlugin,
    disabled_plugin_names,
    get_kernel,
    is_plugin_disabled,
    plugins_disabled,
    reset_kernel_for_tests,
)

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
