# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Module discovery and lifecycle.

Stage 0 implementation: a simple dynamic-import registry over
``cloudy.modules``. Each module package exposes a module-level ``MODULE``
class implementing :class:`~cloudy.core.interfaces.ServiceModule`. This is
designed to migrate to libpeas-2 later without changing the interfaces; see
docs/MODULES.md.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Dict, List

from .interfaces import ServiceModule


class PluginEngine:
    def __init__(self, settings):
        self._settings = settings
        self._modules: Dict[str, ServiceModule] = {}

    # -- discovery --------------------------------------------------------
    def discover(self) -> None:
        from .. import modules as modules_pkg

        for info in pkgutil.iter_modules(modules_pkg.__path__):
            if info.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(
                    f"{modules_pkg.__name__}.{info.name}"
                )
            except Exception as exc:  # noqa: BLE001 - never let one module break startup
                print(f"[plugin] failed to import {info.name}: {exc}")
                continue

            module_cls = getattr(mod, "MODULE", None)
            if module_cls is None or not issubclass(module_cls, ServiceModule):
                continue
            instance = module_cls()
            self._modules[instance.id] = instance

    # -- access -----------------------------------------------------------
    def modules(self) -> List[ServiceModule]:
        return list(self._modules.values())

    def get(self, module_id: str) -> ServiceModule | None:
        return self._modules.get(module_id)

    # -- enable/disable state (persisted in GSettings) --------------------
    def is_enabled(self, module_id: str) -> bool:
        return module_id in self._settings.get_strv("enabled-modules")

    def set_enabled(self, module_id: str, enabled: bool) -> None:
        current = list(self._settings.get_strv("enabled-modules"))
        if enabled and module_id not in current:
            current.append(module_id)
        elif not enabled and module_id in current:
            current.remove(module_id)
        self._settings.set_strv("enabled-modules", current)
