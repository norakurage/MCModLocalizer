from __future__ import annotations

import flet as ft

try:
    from .ui import main as app_main
except ImportError:  # pragma: no cover - fallback for script execution
    from app.ui import main as app_main


if __name__ == "__main__":
    ft.app(target=app_main)
