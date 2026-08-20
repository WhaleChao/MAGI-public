"""Lazy V2 HTTP compatibility surfaces for V3 production roles.

Importing this package only defines adapters.  It does not import either
legacy Flask application, open sockets, or start background processes.
"""

from .gateway import (
    CompatibilityLoadError,
    CompatibilitySurfaceError,
    LazyCompatibilityApp,
    RouteInventory,
    create_app,
    create_main_app,
    create_tools_app,
    inventory_report,
)


def create_admin_server(**kwargs):
    """Resolve the stdlib admin compatibility server only when control starts."""

    from .admin import create_admin_server as factory

    return factory(**kwargs)

__all__ = [
    "CompatibilityLoadError",
    "CompatibilitySurfaceError",
    "LazyCompatibilityApp",
    "RouteInventory",
    "create_app",
    "create_main_app",
    "create_tools_app",
    "create_admin_server",
    "inventory_report",
]
