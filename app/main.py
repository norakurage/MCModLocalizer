from __future__ import annotations

import flet as ft

from .ui import main as app_main


def run() -> None:
    """Flet アプリを起動します。"""

    ft.app(target=app_main)


if __name__ == "__main__":
    run()
