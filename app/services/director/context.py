"""Shared namespace through which the director modules reach each other."""
from __future__ import annotations

from typing import Any, Callable


class DirectorContext:
    """Attribute bag holding the store, dependencies and installed closures.

    Modules are installed in dependency order; each reads what it needs from
    the context once and registers its own closures for later modules.
    """

    def __init__(self, **values: Any):
        self.__dict__.update(values)

    def register(self, *functions: Callable[..., Any]) -> None:
        for function in functions:
            setattr(self, function.__name__, function)
