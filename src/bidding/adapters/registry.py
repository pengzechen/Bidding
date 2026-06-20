from __future__ import annotations

import importlib
import pkgutil
from typing import Type

from bidding.adapters.base import SiteAdapter

_REGISTRY: dict[str, Type[SiteAdapter]] = {}


def register(cls: Type[SiteAdapter]) -> Type[SiteAdapter]:
    _REGISTRY[cls.meta.name] = cls
    return cls


def get_adapter(name: str) -> Type[SiteAdapter]:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown adapter: {name}. Available: {list(_REGISTRY)}")
    return _REGISTRY[name]


def list_adapters() -> dict[str, Type[SiteAdapter]]:
    return dict(_REGISTRY)


def auto_discover():
    import bidding.adapters as pkg

    for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
        if module_name not in ("base", "registry"):
            importlib.import_module(f"bidding.adapters.{module_name}")
