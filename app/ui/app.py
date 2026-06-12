from __future__ import annotations

import json
import sys
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path

import flet as ft


from ..core.usage import UsageStats, estimate_cost
from ..services import (
    ExtractionResult,
    ResourcePackBuilder,
    TranslationSummary,
    extract_localizations,
    parse_fraction,
    run_translation_jobs,
)

APP_NAME = "MCModLocalizer"
BASE_DIR = Path(__file__).resolve().parent.parent
RESOURCE_TEMPLATE_DIR = BASE_DIR / "a"

try:
    import keyring  # type: ignore
except Exception:
    keyring = None


class LocalizeApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.stop_event = threading.Event()
        self._log_lines: list[str] = []
        self._max_log_lines = 500
        # 保存キー
        self.K_MODEL = "openai_model"
        self.K_DIR_MODS = "dir_mods_root"
        self.K_DIR_OUTPUT = "dir_output_pack"
        self.K_LAST_MODS_PATH = "last_mods_dir_path"
        self.K_LAST_OUTPUT_PATH = "last_output_dir_path"
        self.K_USAGE_HISTORY = "token_usage_history"
        self.K_USAGE_TOTAL_COST = "token_usage_total_cost"
        self.K_USAGE_TOTAL_STATS = "token_usage_total_stats"
        # API Keys
        self.K_KEY_GEMINI = "GEMINI_API_KEY"
        # 既定値
        self.model_pricing = {}
        self.available_models = []
        self.pricing_version = "-"
        self._load_model_pricing()
        # Default model
        self.default_model = "gemini-2.5-flash-lite"
        # -------------- UI 構築 --------------
        page.title = f"{APP_NAME} (Flet)"
        page.padding = 16
        page.window_width = 1000
        page.window_height = 820
        page.theme_mode = "light"
        # ログ & 進捗
        self.log_view = ft.ListView(
            expand=True,
            spacing=2,
            auto_scroll=True,
        )
        self.log_container = ft.Container(
            content=self.log_view,
            expand=True,
            border=ft.border.all(1, ft.Colors.OUTLINE),
            border_radius=4,
            padding=5,
        )
        self.chk_auto_scroll = ft.Checkbox(
            label="自動スクロール",
            value=True,
            on_change=self._on_change_auto_scroll,
        )
        self.btn_copy_log = ft.IconButton(
            icon=ft.Icons.COPY,
            tooltip="ログをクリップボードにコピー",
            on_click=self._on_click_copy_log
        )
        self.progress = ft.ProgressBar(width=420, value=0)
        self.counter = ft.Text("待機中")
        self.detail_progress = ft.ProgressBar(width=420, value=0)
        self.detail_counter = ft.Text("")
        # -------- 抽出タブ UI --------
        self.mods_dir_path = ft.TextField(
            label="Mods フォルダ（必須）",
            dense=True,
            expand=True,
            read_only=True,
            value=self._load_value(self.K_LAST_MODS_PATH) or "",
        )
        self.output_dir = ft.TextField(
            label="出力フォルダ（リソースパック保存先）",
            dense=True,
            expand=True,
            read_only=True,
            value=self._load_value(self.K_LAST_OUTPUT_PATH) or "",
        )
        self.fp_mods = ft.FilePicker(on_result=self._on_pick_mods_dir)
        self.fp_dir = ft.FilePicker(on_result=self._on_pick_dir)
        self.page.overlay.extend([self.fp_mods, self.fp_dir])
        pick_mods_btn = ft.ElevatedButton(
            "modsフォルダを選択",
            icon=ft.Icons.FOLDER_OPEN,
            tooltip="Mods フォルダを選択",
            on_click=self._open_mods_picker,
        )
        pick_dir_btn = ft.ElevatedButton(
            "リソースパックフォルダを選択",
            icon=ft.Icons.FOLDER_OPEN,
            tooltip="出力フォルダを選択",
            on_click=self._open_output_dir_picker,
        )
        self.btn_extract = ft.ElevatedButton("抽出 / リソースパック生成", icon=ft.Icons.DOWNLOAD, on_click=self.on_extract)
        self.btn_stop = ft.OutlinedButton("停止", icon=ft.Icons.STOP, on_click=self.on_stop, disabled=True)
        self.progress_panel = ft.Column(
            visible=False,
            controls=[
                ft.Row(
                    [self.progress, self.counter],
                    spacing=12,
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [self.detail_progress, self.detail_counter],
                    spacing=12,
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=8,
        )
        extract_tab = ft.Column(
            controls=[
                ft.Text("ステップ: JAR から en_us.json を抽出し、ja_jp.json まで自動生成します。", weight=ft.FontWeight.BOLD),
                ft.Row([self.mods_dir_path, pick_mods_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.output_dir, pick_dir_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row(
                    [self.btn_extract, self.btn_stop],
                    spacing=16,
                    alignment=ft.MainAxisAlignment.START,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.progress_panel,
                ft.Row(
                    [ft.Text("ログ"), ft.Container(expand=True), self.chk_auto_scroll, self.btn_copy_log],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.log_container,
            ],
            expand=True,
            spacing=12,
        )
        # -------- 設定タブ UI --------
        saved_model = self._load_value(self.K_MODEL) or self.default_model
        if saved_model not in self.available_models:
            saved_model = self.default_model
        
        self.btn_config_api_key = ft.ElevatedButton("APIキー再設定", icon=ft.Icons.KEY, on_click=self._open_api_key_dialog)
        
        self.model_pricing_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Model")),
                ft.DataColumn(ft.Text("Input ($/1M tokens)")),
                ft.DataColumn(ft.Text("Cached input ($/1M tokens)")),
                ft.DataColumn(ft.Text("Output ($/1M tokens)")),
            ],
            rows=[],
        )
        self._refresh_pricing_table_ui()

        self.model_field = ft.Dropdown(
            label="モデル",
            value=saved_model,
            options=[ft.dropdown.Option(m) for m in self.available_models],
            dense=True, expand=False, width=220,
            on_change=self._on_model_change,
        )

        settings_tab = ft.Column(
            controls=[
                ft.Text("API 設定", weight=ft.FontWeight.BOLD),
                ft.Row([self.model_field, self.btn_config_api_key], spacing=12),
                ft.Text("※APIキーは keyring を使用してシステムに安全に保存されます。"),
                ft.Row([
                    ft.Text("料金テーブル (USD, 1M トークンあたり)", weight=ft.FontWeight.BOLD),
                    ft.Text(f"最終更新: {self.pricing_version}")
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.END),
                self.model_pricing_table,
                ft.Divider(),
                ft.Text("デバッグ・メンテナンス", weight=ft.FontWeight.BOLD),
                ft.ElevatedButton("アプリを初期化する（設定リセット）", color=ft.Colors.ERROR, on_click=self._on_click_debug_reset),
            ],
            expand=True,
            spacing=12,
        )

        self.usage_history: list[dict[str, object]] = self._load_usage_history()
        self.total_usage: UsageStats = self._load_total_stats()
        self.token_usage_summary = ft.Text("まだ翻訳の実行履歴がありません。")
        self.token_usage_prompt_text = ft.Text(f"入力トークン: {self.total_usage.prompt_tokens}")
        self.token_usage_completion_text = ft.Text(f"出力トークン: {self.total_usage.completion_tokens}")
        self.token_usage_total_text = ft.Text(f"合計トークン: {self.total_usage.total_tokens}")
        self.total_cost = self._load_total_cost()
        self.token_usage_cost_text = ft.Text(f"概算コスト累計: ${self.total_cost:.3f}")
        self.token_usage_updated_text = ft.Text("更新時刻: -")
        history_rows = self._generate_history_rows()
        self.token_usage_history_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("日時")),
                ft.DataColumn(ft.Text("モデル")),
                ft.DataColumn(ft.Text("入力トークン")),
                ft.DataColumn(ft.Text("出力トークン")),
                ft.DataColumn(ft.Text("概算コスト")),
            ],
            rows=history_rows,
            width=None,
        )
        token_tab = ft.Column(
            controls=[
                ft.Text("API のトークン使用量を確認します。", weight=ft.FontWeight.BOLD),
                self.token_usage_summary,
                self.token_usage_prompt_text,
                self.token_usage_completion_text,
                self.token_usage_total_text,
                self.token_usage_cost_text,
                self.token_usage_updated_text,
                ft.Text("API 利用履歴", weight=ft.FontWeight.BOLD),
                ft.Container(
                    content=ft.Column(
                        controls=[self.token_usage_history_table],
                        tight=True,
                        scroll=ft.ScrollMode.AUTO,
                    ),
                    expand=True,
                    width=float("inf"),
                    height=240,
                    border=ft.border.all(1, ft.Colors.TRANSPARENT),
                    clip_behavior=ft.ClipBehavior.HARD_EDGE,
                ),
            ],
            expand=True,
            spacing=12,
        )
        # -------- Tabs --------
        self.tabs = ft.Tabs(
            selected_index=0,
            tabs=[
                ft.Tab(text="抽出", icon=ft.Icons.DOWNLOAD, content=extract_tab),
                ft.Tab(text="トークン", icon=ft.Icons.ASSESSMENT, content=token_tab),
                ft.Tab(text="設定", icon=ft.Icons.SETTINGS, content=settings_tab),
            ],
            expand=True,
        )
        self.pack_builder = ResourcePackBuilder(
            template_dir=RESOURCE_TEMPLATE_DIR,
            icon_path=self._get_bundled_asset_path("icon.png"),
            log=self._append_log,
        )
        page.add(self.tabs)
        self._append_log("準備完了。Mods フォルダと出力フォルダを指定して抽出を実行するとリソースパックを自動生成します。")

        if not self._load_api_key():
            self._open_api_key_dialog()

    def _load_model_pricing(self):
        defaults = {
            "gemini-2.5-flash": {"input": 0.30, "cached_input": 0.03, "output": 2.50},
            "gemini-2.5-flash-lite": {"input": 0.10, "cached_input": 0.01, "output": 0.40},
        }
        try:
            path = self._get_bundled_asset_path("pricing.json")
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "models" in data:
                        self.model_pricing = data["models"]
                        self.pricing_version = data.get("version", "-")
                    else:
                         self.model_pricing = defaults
            else:
                self.model_pricing = defaults
        except Exception as e:
            print(f"Failed to load pricing.json: {e}")
            self.model_pricing = defaults
            
        self.available_models = list(self.model_pricing.keys())

    def _refresh_pricing_table_ui(self):
        self.model_pricing_table.rows = [
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(model)),
                    ft.DataCell(ft.Text(f"${rates['input']:.3f}")),
                    ft.DataCell(ft.Text(f"${rates['cached_input']:.3f}")),
                    ft.DataCell(ft.Text(f"${rates['output']:.3f}")),
                ]
            )
            for model, rates in self.model_pricing.items()
        ]
        if self.model_pricing_table.page:
            self.model_pricing_table.update()

    # ------------------------------
    # FilePicker launchers
    # ------------------------------
    def _open_mods_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_MODS)
        self.fp_mods.get_directory_path(initial_directory=init_dir)

    def _open_output_dir_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_OUTPUT)
        self.fp_dir.get_directory_path(initial_directory=init_dir)

    # ------------------------------
    # FilePicker handlers
    # ------------------------------
    def _on_pick_mods_dir(self, e: ft.FilePickerResultEvent):
        if e.path:
            selected = Path(e.path)
            self.mods_dir_path.value = str(selected)
            self.mods_dir_path.update()
            self._save_value(self.K_LAST_MODS_PATH, str(selected))
            self._remember_dir(self.K_DIR_MODS, selected)
            self._auto_set_output_dir(selected)

    def _on_pick_dir(self, e: ft.FilePickerResultEvent):
        if e.path:
            selected = Path(e.path)
            self.output_dir.value = str(selected)
            self.output_dir.update()
            self._save_value(self.K_LAST_OUTPUT_PATH, str(selected))
            self._remember_dir(self.K_DIR_OUTPUT, selected)

    # ------------------------------
    # Settings
    # ------------------------------
    def _load_value(self, key: str) -> str | None:
        return self.page.client_storage.get(key)

    def _save_value(self, key: str, value: str):
        self.page.client_storage.set(key, value)

    def _remember_dir(self, key: str, path: Path | None):
        if not path:
            return
        dir_path = path if path.is_dir() else path.parent
        if not str(dir_path):
            return
        try:
            dir_path = dir_path.resolve()
        except Exception:
            dir_path = dir_path.absolute()
        self._save_value(key, str(dir_path))

    def _auto_set_output_dir(self, source_path: Path):
        try:
            source_path = source_path.resolve()
        except Exception:
            source_path = source_path.absolute()
        target_root: Path | None = None
        if source_path.is_dir() and source_path.name.lower() == "mods":
            target_root = source_path.parent
        else:
            for parent in source_path.parents:
                if parent.name.lower() == "mods":
                    target_root = parent.parent
                    break
        if not target_root:
            return
        candidate_dir: Path | None = None
        candidate_names = [
            "resourcepacks",
            "resource_packs",
            "resourcepack",
            "resource",
        ]
        for name in candidate_names:
            candidate = target_root / name
            if candidate.exists() and candidate.is_dir():
                candidate_dir = candidate
                break
        if candidate_dir is None:
            candidate_dir = target_root / "resourcepacks"
        self.output_dir.value = str(candidate_dir)
        self.output_dir.update()
        self._save_value(self.K_LAST_OUTPUT_PATH, str(candidate_dir))
        self._remember_dir(self.K_DIR_OUTPUT, candidate_dir)
        self._append_log(
            f"[INFO] リソースパックフォルダを自動設定しました: {candidate_dir}"
        )

    def _get_initial_directory(self, key: str) -> str | None:
        stored = self._load_value(key)
        if not stored:
            return None
        p = Path(stored)
        while True:
            if p.exists():
                return str(p)
            if p.parent == p:
                break
            p = p.parent
        return None

    def _load_api_key(self) -> str | None:
        key_name = self.K_KEY_GEMINI
        if keyring:
            try:
                v = keyring.get_password(APP_NAME, key_name)
                if v:
                    return v
            except Exception:
                pass
        return None

    def _save_api_key(self, value: str):
        key_name = self.K_KEY_GEMINI
        if keyring:
            try:
                keyring.set_password(APP_NAME, key_name, value)
                return
            except Exception as e:
                self._append_log(f"[ERROR] keyring への保存に失敗しました: {e}")
        else:
             self._append_log("[ERROR] keyring モジュールが利用できないため、APIキーを保存できません。")

    def _on_model_change(self, e: ft.ControlEvent):
        value = (self.model_field.value or "").strip()
        if value not in self.available_models:
            return
        self._save_value(self.K_MODEL, value)
        self._append_log(f"[INFO] モデル選択を更新しました: {value}")

    def _load_usage_history(self) -> list[dict[str, object]]:
        raw = self._load_value(self.K_USAGE_HISTORY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            self._append_log("[WARN] トークン使用履歴の読み込みに失敗しました。データを破棄します。")
            return []
        if not isinstance(data, list):
            return []
        history: list[dict[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("timestamp", ""))
            model = str(item.get("model", ""))
            prompt = item.get("prompt", 0)
            completion = item.get("completion", 0)
            total = item.get("total", 0)
            cost = item.get("cost", 0.0)
            try:
                prompt_i = int(prompt)
            except Exception:
                prompt_i = 0
            try:
                completion_i = int(completion)
            except Exception:
                completion_i = 0
            try:
                total_i = int(total) if total else prompt_i + completion_i
            except Exception:
                total_i = prompt_i + completion_i
            try:
                cost_f = float(cost)
            except Exception:
                cost_f = 0.0
            history.append(
                {
                    "timestamp": ts,
                    "model": model,
                    "prompt": prompt_i,
                    "completion": completion_i,
                    "total": total_i,
                    "cost": cost_f,
                }
            )
        return history

    def _load_total_cost(self) -> float:
        raw = self._load_value(self.K_USAGE_TOTAL_COST)
        if not raw:
            return 0.0
        try:
            return float(raw)
        except Exception:
            return 0.0

    def _load_total_stats(self) -> UsageStats:
        raw = self._load_value(self.K_USAGE_TOTAL_STATS)
        if raw:
            try:
                data = json.loads(raw)
                return UsageStats(
                    prompt_tokens=int(data.get("prompt_tokens", 0)),
                    completion_tokens=int(data.get("completion_tokens", 0)),
                    total_tokens=int(data.get("total_tokens", 0)),
                )
            except Exception:
                pass
        
        # Fallback: calculate from history if no saved stats found
        stats = UsageStats()
        for record in self.usage_history:
            stats.prompt_tokens += int(record.get("prompt", 0))
            stats.completion_tokens += int(record.get("completion", 0))
            stats.total_tokens += int(record.get("total", 0))
        return stats

    def _persist_usage_history(self) -> None:
        try:
            payload = json.dumps(self.usage_history, ensure_ascii=False)
            self._save_value(self.K_USAGE_HISTORY, payload)
        except Exception as ex:
            self._append_log(f"[WARN] トークン使用履歴の保存に失敗しました: {repr(ex)}")

    def _persist_total_cost(self) -> None:
        try:
            self._save_value(self.K_USAGE_TOTAL_COST, f"{self.total_cost:.6f}")
        except Exception as ex:
            self._append_log(f"[WARN] トークン累計コストの保存に失敗しました: {repr(ex)}")

    def _persist_total_stats(self) -> None:
        try:
            data = {
                "prompt_tokens": self.total_usage.prompt_tokens,
                "completion_tokens": self.total_usage.completion_tokens,
                "total_tokens": self.total_usage.total_tokens,
            }
            self._save_value(self.K_USAGE_TOTAL_STATS, json.dumps(data))
        except Exception as ex:
            self._append_log(f"[WARN] トークン累計使用量の保存に失敗しました: {repr(ex)}")

    def _generate_history_rows(self) -> list[ft.DataRow]:
        grouped: dict[tuple[str, str], dict] = {}
        for record in self.usage_history:
            ts = str(record.get("timestamp", "-"))
            model = str(record.get("model", "-"))
            key = (ts, model)
            
            p = int(record.get("prompt", 0))
            c = int(record.get("completion", 0))
            cost = float(record.get("cost", 0.0))
            
            if key not in grouped:
                grouped[key] = {
                    "timestamp": ts,
                    "model": model,
                    "prompt": 0,
                    "completion": 0,
                    "cost": 0.0,
                }
            
            grouped[key]["prompt"] += p
            grouped[key]["completion"] += c
            grouped[key]["cost"] += cost
            
        return [
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(data["timestamp"])),
                    ft.DataCell(ft.Text(data["model"])),
                    ft.DataCell(ft.Text(str(data["prompt"]))),
                    ft.DataCell(ft.Text(str(data["completion"]))),
                    ft.DataCell(ft.Text(f"${data['cost']:.3f}")),
                ]
            )
            for data in grouped.values()
        ]

    def _refresh_usage_history_table(self) -> None:
        self.token_usage_history_table.rows = self._generate_history_rows()
        self.token_usage_history_table.update()

    def _open_dialog(self, dlg: ft.AlertDialog) -> None:
        if hasattr(self.page, "open"):
            self.page.open(dlg)
        else:
            self.page.dialog = dlg
            dlg.open = True
            self.page.update()

    def _open_api_key_dialog(self, e=None):
        try:

            
            def close_dlg(e):
                dlg.open = False
                self.page.update()

            def save_dlg(e):
                val_gemini = key_field_gemini.value.strip()

                if val_gemini:
                    self._save_api_key(val_gemini)
                
                self._append_log("[OK] 設定された API Key を keyring に保存しました。")
                dlg.open = False
                self.page.update()

            # 現在設定されているかどうかだけ確認（セキュリティのため値は表示しない）
            has_gemini = bool(self._load_api_key())
            
            label_text = "Gemini API Key (設定済み)" if has_gemini else "Gemini API Key"

            key_field_gemini = ft.TextField(
                label=label_text,
                password=True,
                can_reveal_password=True,
                value="",
                hint_text="設定済み (変更しない場合は空欄)" if has_gemini else "未設定",
                expand=True,
            )

            dlg = ft.AlertDialog(
                title=ft.Text("API Key 設定"),
                content=ft.Column([
                    ft.Text("使用するモデルに対応する API Key を設定してください。"),
                    ft.Markdown(
                        "APIキーは [Google AI Studio](https://aistudio.google.com/app/apikey) から取得できます。",
                        on_tap_link=lambda e: self.page.launch_url(e.data),
                    ),
                    key_field_gemini,
                    ft.Text("※ keyring は OS の資格情報マネージャーを使用します。", size=12, color=ft.Colors.GREY),
                ], tight=True, width=500),
                actions=[
                    ft.TextButton("キャンセル", on_click=close_dlg),
                    ft.ElevatedButton("保存", on_click=save_dlg),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            
            self._open_dialog(dlg)

        except Exception as ex:
            self._append_log(f"[ERROR] ダイアログの表示に失敗しました: {repr(ex)}")
            import traceback
            traceback.print_exc()

    def _on_click_debug_reset(self, e: ft.ControlEvent):
        def reset_confirmed(e):
            # API Key 削除
            if keyring:
                try:
                    keyring.delete_password(APP_NAME, self.K_KEY_GEMINI)
                    self._append_log("[INFO] API Key を削除しました。")
                except Exception:
                    pass
            
            # Client Storage クリア
            try:
                self.page.client_storage.clear()
                self._append_log("[INFO] アプリ設定(client_storage)をクリアしました。")
            except Exception as ex:
                self._append_log(f"[ERROR] 設定クリア失敗: {ex}")
            
            # ダイアログを閉じる
            dlg.open = False
            self.page.update()
            
            # 完了通知
            self._show_completion_toast("初期化が完了しました。アプリを再起動してください。")


        dlg = ft.AlertDialog(
            title=ft.Text("初期化の確認"),
            content=ft.Text("すべての設定と履歴を削除します。よろしいですか？\n(APIキー、フォルダ履歴、トークン使用履歴などが消去されます)"),
            actions=[
                ft.TextButton("キャンセル", on_click=lambda e: setattr(dlg, 'open', False) or self.page.update()),
                ft.TextButton("初期化する", on_click=reset_confirmed, style=ft.ButtonStyle(color=ft.Colors.ERROR)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._open_dialog(dlg)

    # ------------------------------
    # Log & Progress
    # ------------------------------
    def _on_change_auto_scroll(self, e: ft.ControlEvent):
        self.log_view.auto_scroll = e.control.value
        self.log_view.update()

    def _on_click_copy_log(self, e: ft.ControlEvent):
        if not self._log_lines:
            return
        text = "\n".join(self._log_lines)
        self.page.set_clipboard(text)
        self._show_completion_toast("ログをクリップボードにコピーしました")

    def _append_log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        raw_lines = msg.splitlines() or [msg]
        indent = " " * (len(timestamp) + 3)
        
        new_controls = []
        for idx, raw in enumerate(raw_lines):
            content = raw.strip() if raw.strip() else raw
            line = f"[{timestamp}] {content}" if idx == 0 else f"{indent}{content}"
            self._log_lines.append(line)
            new_controls.append(ft.Text(line, selectable=True, font_family="Consolas,monospace"))

        if len(self._log_lines) > self._max_log_lines:
            excess = len(self._log_lines) - self._max_log_lines
            self._log_lines = self._log_lines[-self._max_log_lines :]
            for _ in range(excess):
                if self.log_view.controls:
                    self.log_view.controls.pop(0)

        self.log_view.controls.extend(new_controls)
        self.log_view.update()

    def _set_progress(self, ratio: float, text: str = ""):
        self.progress.value = max(0.0, min(1.0, ratio))
        self.progress.update()
        self.counter.value = text or ""
        self.counter.update()

    def _set_detail_progress(self, ratio: float, text: str = ""):
        self.detail_progress.value = max(0.0, min(1.0, ratio))
        self.detail_progress.update()
        self.detail_counter.value = text or ""
        self.detail_counter.update()

    def _update_extraction_progress(self, ratio: float, text: str):
        parsed = parse_fraction(text)
        if parsed:
            done, total = parsed
            label = f"抽出中: {done}件完了（全{total}件）"
        else:
            percent = int(max(0.0, min(1.0, ratio)) * 100)
            label = f"抽出中: {percent}%"
        self._set_progress(ratio, label)

    def _update_token_usage_ui(self, summary: TranslationSummary):
        pricing = self.model_pricing.get(summary.model)
        if summary.usage_records:
            for prompt, completion, total in summary.usage_records:
                cost = estimate_cost(pricing, prompt, completion)
                self.total_cost += cost
                record = {
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "model": summary.model or "(不明)",
                    "prompt": prompt,
                    "completion": completion,
                    "total": total,
                    "cost": cost,
                }
                self.usage_history.append(record)
            # keep latest 200 entries to avoid unbounded growth
            if len(self.usage_history) > 200:
                self.usage_history = self.usage_history[-200:]
            self._persist_usage_history()
            self._persist_total_cost()

            # Update total usage stats
            self.total_usage.prompt_tokens += summary.prompt_tokens
            self.total_usage.completion_tokens += summary.completion_tokens
            self.total_usage.total_tokens += summary.total_tokens
            self._persist_total_stats()

        if summary.total_tokens > 0:
            calls = len(summary.usage_records)
            base_msg = (
                f"直近の翻訳で {summary.total_tokens} トークンを使用しました "
                f"(入力 {summary.prompt_tokens} / 出力 {summary.completion_tokens})"
            )
            if summary.model:
                base_msg += f"。モデル: {summary.model}"
            if calls:
                base_msg += f"、API コール {calls} 回"
            base_msg += "。"
            self.token_usage_summary.value = base_msg
        elif summary.translated_mods > 0:
            note = "翻訳は完了しましたが、新たに使用されたトークンは報告されませんでした。"
            if summary.aborted:
                note = "翻訳が途中で停止したため、トークン使用量は 0 として集計しています。"
            self.token_usage_summary.value = note
        else:
            self.token_usage_summary.value = "まだ翻訳の実行履歴がありません。"

        self.token_usage_prompt_text.value = f"入力トークン: {self.total_usage.prompt_tokens}"
        self.token_usage_completion_text.value = f"出力トークン: {self.total_usage.completion_tokens}"
        self.token_usage_total_text.value = f"合計トークン: {self.total_usage.total_tokens}"
        self.token_usage_cost_text.value = f"概算コスト累計: ${self.total_cost:.3f}"
        self.token_usage_updated_text.value = f"更新時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        self._refresh_usage_history_table()

        self.token_usage_summary.update()
        self.token_usage_prompt_text.update()
        self.token_usage_completion_text.update()
        self.token_usage_total_text.update()
        self.token_usage_cost_text.update()
        self.token_usage_updated_text.update()

    def _show_completion_toast(self, message: str, *, is_error: bool = False):
        color = ft.Colors.ERROR if is_error else None
        snack = ft.SnackBar(ft.Text(message), bgcolor=color)
        if hasattr(self.page, "open"):
            self.page.open(snack)
        else:
            snack.open = True
            self.page.snack_bar = snack
            self.page.update()


    # ------------------------------
    # 抽出フロー
    # ------------------------------
    def on_extract(self, e: ft.ControlEvent):
        mods_dir_value = self.mods_dir_path.value.strip()
        mods_dir = Path(mods_dir_value) if mods_dir_value else None
        out_dir_value = self.output_dir.value.strip()
        out_dir = Path(out_dir_value) if out_dir_value else None
        if not mods_dir or not mods_dir.exists() or not mods_dir.is_dir():
            self._append_log("[ERROR] Mods フォルダが見つかりません。")
            return
        if out_dir is None:
            self._append_log("[ERROR] 出力フォルダを指定してください。")
            return
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                self._append_log("[INFO] 出力フォルダを作成しました。")
            except Exception as ex:
                self._append_log(f"[ERROR] 出力フォルダを作成できません: {repr(ex)}")
                return
        self._remember_dir(self.K_DIR_MODS, mods_dir)
        self._remember_dir(self.K_DIR_OUTPUT, out_dir)
        self._save_value(self.K_LAST_MODS_PATH, str(mods_dir))
        self._save_value(self.K_LAST_OUTPUT_PATH, str(out_dir))
        self.btn_extract.disabled = True
        self.progress_panel.visible = True
        self.btn_extract.update()
        self.progress_panel.update()
        self._set_progress(0.0, "抽出準備中")

        def _work():
            temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
            temp_dir_path: Path | None = None
            toast_message = "処理が完了しました。"
            toast_is_error = False
            self._set_detail_progress(0.0, "")
            try:
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="mc_localizer_")
                temp_dir_path = Path(temp_dir_obj.name)
                self._append_log("[INFO] 一時作業フォルダを準備しました。")
                self._append_log("[RUN] Mods フォルダから抽出を開始します。")
                result: ExtractionResult = extract_localizations(
                    mods_dir,
                    temp_dir_path,
                    log=self._append_log,
                    progress=self._update_extraction_progress,
                )
                targets: list[tuple[str, Path, dict[str, str]]] = []
                for modid in result.mod_maps.keys():
                    en_path = temp_dir_path / modid / "en_us.json"
                    if en_path.exists():
                        existing_ja = result.existing_ja_maps.get(modid, {})
                        targets.append((modid, en_path, existing_ja))
                if targets:
                    self._append_log("[RUN] 抽出が完了したため、翻訳を開始します。")
                    summary = self._translate_targets(targets, out_dir)
                    self._update_token_usage_ui(summary)
                    if summary.aborted:
                        toast_message = "翻訳が停止されました。"
                        toast_is_error = False
                    elif summary.had_error:
                        toast_message = "翻訳処理でエラーが発生しました。ログを確認してください。"
                        toast_is_error = True
                    elif summary.translated_mods == 0:
                        toast_message = "翻訳対象の ja_jp.json は生成されませんでした。"
                    else:
                        mod_part = f"{summary.total_mods} Mod 中 {summary.translated_mods} Mod"
                        if summary.total_entries:
                            entry_part = f"、{summary.total_entries} 件中 {summary.translated_entries} 件"
                        else:
                            entry_part = ""
                        toast_message = f"翻訳が完了しました ({mod_part}{entry_part})。"
                else:
                    self._set_progress(1.0, "抽出完了")
                    self._append_log("[WARN] 翻訳対象となる en_us.json が見つかりませんでした。")
                    toast_message = "抽出完了 (翻訳対象なし)。"
            except Exception as ex:
                self._append_log("[ERROR] 抽出処理で例外: " + repr(ex))
                self._append_log(traceback.format_exc())
                toast_message = "処理中にエラーが発生しました。ログを確認してください。"
                toast_is_error = True
            finally:
                if temp_dir_obj:
                    temp_dir_obj.cleanup()
                    if temp_dir_path:
                        self._append_log("[INFO] 一時作業フォルダを削除しました。")
                self.btn_extract.disabled = False
                self.progress_panel.visible = False
                self.btn_extract.update()
                self.progress_panel.update()
                self._show_completion_toast(toast_message, is_error=toast_is_error)

        threading.Thread(target=_work, daemon=True).start()

    def _translate_targets(
        self,
        targets: list[tuple[str, Path, dict[str, str]]],
        output_dir: Path,
    ) -> TranslationSummary:
        model = self.model_field.value or self._load_value(self.K_MODEL) or self.default_model
        model = model.strip()
        if model not in self.available_models:
            model = self.available_models[0]

        api_key = self._load_api_key()
        if not api_key:
            self._append_log("[ERROR] API キーが未設定です。設定タブで保存してください。")
            self.tabs.selected_index = 1
            self.tabs.update()
            return TranslationSummary(
                translated_mods=0,
                total_mods=len(targets),
                translated_entries=0,
                total_entries=0,
                aborted=False,
                had_error=True,
                pack_dir=None,
            )

        self._append_log("[INFO] リソースパック出力先を確認しました。")
        self.stop_event.clear()
        self.btn_stop.disabled = False
        self.btn_stop.update()
        try:
            return run_translation_jobs(
                targets,
                api_key=api_key,
                model=model,
                output_dir=output_dir,
                build_pack=lambda produced: self.pack_builder.build(output_dir, produced),
                log=self._append_log,
                set_overall=self._set_progress,
                set_detail=self._set_detail_progress,
                should_stop=self.stop_event.is_set,
            )
        finally:
            self.stop_event.clear()
            self.btn_stop.disabled = True
            self.btn_stop.update()

    def on_stop(self, e: ft.ControlEvent):
        self.stop_event.set()
        self._append_log("[INFO] 停止要求を送信しました。現在のバッチ終了後に停止します。")

    def _get_bundled_asset_path(self, filename: str) -> Path:
        if not hasattr(sys, "_MEIPASS"):
            return BASE_DIR / "assets" / filename

        base = Path(sys._MEIPASS)
        return base / "assets" / filename


def main(page: ft.Page):
    LocalizeApp(page)
