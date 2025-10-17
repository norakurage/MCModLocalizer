from __future__ import annotations

import flet as ft

try:
    from .ui import main as app_main
except ImportError:  # pragma: no cover - fallback for script execution
    import pathlib
    import sys

    package_root = pathlib.Path(__file__).resolve().parent.parent
    package_path = str(package_root)
    if package_path not in sys.path:
        sys.path.insert(0, package_path)

    from app.ui import main as app_main


if __name__ == "__main__":
    ft.app(target=app_main)
