from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.sax import saxutils

import flet as ft

from processing import ExtractionResult, extract_localizations, translate_localizations

APP_NAME = "MC Localizer"
BASE_DIR = Path(__file__).resolve().parent
RESOURCE_TEMPLATE_DIR = BASE_DIR / "a"

try:
    import keyring  # type: ignore
except Exception:
    keyring = None


@dataclass
class TranslationSummary:
    translated_mods: int
    total_mods: int
    translated_entries: int
    total_entries: int
    aborted: bool
    had_error: bool
    pack_dir: Path | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    usage_records: list[tuple[int, int, int]] = field(default_factory=list)


class LocalizeApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.stop_event = threading.Event()
        self._log_lines: list[str] = []
        self._max_log_lines = 500
        # 保存キー
        self.K_API = "openai_api_key"
        self.K_MODEL = "openai_model"
        self.K_SAVE_MODE = "save_mode"  # "keyring" or "local"
        self.K_DIR_MODS = "dir_mods_root"
        self.K_DIR_OUTPUT = "dir_output_pack"
        self.K_LAST_MODS_PATH = "last_mods_dir_path"
        self.K_LAST_OUTPUT_PATH = "last_output_dir_path"
        self.K_USAGE_HISTORY = "token_usage_history"
        self.K_USAGE_TOTAL_COST = "token_usage_total_cost"
        # 既定値
        self.model_pricing = {
            "gpt-5": {"input": 1.25, "cached_input": 0.13, "output": 10.00},
            "gpt-5-mini": {"input": 0.25, "cached_input": 0.03, "output": 2.00},
            "gpt-5-nano": {"input": 0.05, "cached_input": 0.01, "output": 0.40},
            "gpt-4.1-mini": {"input": 0.40, "cached_input": 0.10, "output": 1.60},
            "gpt-4.1-nano": {"input": 0.10, "cached_input": 0.03, "output": 0.40},
            "gpt-4o-mini": {"input": 0.15, "cached_input": 0.08, "output": 0.60},
        }
        self.available_models = list(self.model_pricing.keys())
        default_model_env = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.default_model = default_model_env if default_model_env in self.available_models else self.available_models[0]
        # -------------- UI 構築 --------------
        page.title = f"{APP_NAME} (Flet)"
        page.padding = 16
        page.window_width = 1000
        page.window_height = 820
        page.theme_mode = "light"
        # ログ & 進捗
        self.log = ft.TextField(
            label="ログ",
            multiline=True,
            read_only=True,
            expand=True,
            min_lines=12,
            max_lines=9999,
            border=ft.InputBorder.OUTLINE,
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
        progress_panel = ft.Column(
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
                progress_panel,
                self.log,
            ],
            expand=True,
            spacing=12,
        )
        # -------- 設定タブ UI --------
        saved_model = self._load_value(self.K_MODEL) or self.default_model
        if saved_model not in self.available_models:
            saved_model = self.default_model
        self.api_key_field = ft.TextField(
            label="OpenAI API キー",
            password=True, can_reveal_password=True, dense=True, expand=True,
            hint_text="例: sk-...", value=self._load_api_key() or "",
        )
        pricing_rows = [
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(model)),
                    ft.DataCell(ft.Text(f"${rates['input']:.2f}")),
                    ft.DataCell(ft.Text(f"${rates['cached_input']:.2f}")),
                    ft.DataCell(ft.Text(f"${rates['output']:.2f}")),
                ]
            )
            for model, rates in self.model_pricing.items()
        ]
        self.model_pricing_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Model")),
                ft.DataColumn(ft.Text("Input ($/1M tokens)")),
                ft.DataColumn(ft.Text("Cached input ($/1M tokens)")),
                ft.DataColumn(ft.Text("Output ($/1M tokens)")),
            ],
            rows=pricing_rows,
        )

        self.model_field = ft.Dropdown(
            label="モデル",
            value=saved_model,
            options=[ft.dropdown.Option(m) for m in self.available_models],
            dense=True, expand=False, width=220,
            on_change=self._on_model_change,
        )
        save_mode = (self._load_value(self.K_SAVE_MODE) or ("keyring" if keyring else "local"))
        self.save_mode_switch = ft.Dropdown(
            label="APIキーの保存先", value=save_mode,
            options=[ft.dropdown.Option("keyring"), ft.dropdown.Option("local")],
            helper_text="keyring: OS の資格情報（推奨） / local: この端末のユーザーストレージ",
            width=220,
        )
        self.btn_save_settings = ft.ElevatedButton("保存", icon=ft.Icons.SAVE, on_click=self.on_save_settings)
        settings_tab = ft.Column(
            controls=[
                ft.Text("OpenAI 設定", weight=ft.FontWeight.BOLD),
                ft.Row([self.api_key_field, self.model_field, self.save_mode_switch, self.btn_save_settings], spacing=12),
                ft.Text("ヒント：環境変数 OPENAI_MODEL / OPENAI_API_KEY を設定している場合は、それらも自動的に参照します。"),
                ft.Text("料金テーブル (USD, 1M トークンあたり)", weight=ft.FontWeight.BOLD),
                self.model_pricing_table,
            ],
            expand=True,
            spacing=12,
        )

        self.usage_history: list[dict[str, object]] = self._load_usage_history()
        self.token_usage_summary = ft.Text("まだ翻訳の実行履歴がありません。")
        self.token_usage_prompt_text = ft.Text("入力トークン: 0")
        self.token_usage_completion_text = ft.Text("出力トークン: 0")
        self.token_usage_total_text = ft.Text("合計トークン: 0")
        self.total_cost = self._load_total_cost()
        self.token_usage_cost_text = ft.Text("概算コスト（今回）: $0.00")
        self.token_usage_cost_total_text = ft.Text(f"概算コスト累計: ${self.total_cost:.2f}")
        self.token_usage_updated_text = ft.Text("更新時刻: -")
        history_rows = [
            ft.DataRow(
                cells=[
                    ft.DataCell(ft.Text(str(record.get("timestamp", "-")))),
                    ft.DataCell(ft.Text(str(record.get("model", "-")))),
                    ft.DataCell(ft.Text(str(record.get("prompt", 0)))),
                    ft.DataCell(ft.Text(str(record.get("completion", 0)))),
                    ft.DataCell(ft.Text(str(record.get("total", 0)))),
                    ft.DataCell(ft.Text(f"${record.get('cost', 0.0):.2f}")),
                ]
            )
            for record in self.usage_history
        ]
        self.token_usage_history_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("日時")),
                ft.DataColumn(ft.Text("モデル")),
                ft.DataColumn(ft.Text("入力トークン")),
                ft.DataColumn(ft.Text("出力トークン")),
                ft.DataColumn(ft.Text("合計")),
                ft.DataColumn(ft.Text("概算コスト")),
            ],
            rows=history_rows,
            width=None,
        )
        token_tab = ft.Column(
            controls=[
                ft.Text("OpenAI API のトークン使用量を確認します。", weight=ft.FontWeight.BOLD),
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
                ft.Row(
                    controls=[self.token_usage_cost_total_text],
                    alignment=ft.MainAxisAlignment.END,
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
        page.add(self.tabs)
        self._append_log("準備完了。Mods フォルダと出力フォルダを指定して抽出を実行するとリソースパックを自動生成します。")

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
        if keyring:
            try:
                v = keyring.get_password(APP_NAME, "OPENAI_API_KEY")
                if v:
                    return v
            except Exception:
                pass
        v = self._load_value(self.K_API)
        if v:
            return v
        return os.getenv("OPENAI_API_KEY")

    def _save_api_key(self, value: str, mode: str):
        if mode == "keyring" and keyring:
            try:
                keyring.set_password(APP_NAME, "OPENAI_API_KEY", value)
                self.page.client_storage.remove(self.K_API)
                return
            except Exception:
                self._append_log("[WARN] keyring への保存に失敗。ローカル保存にフォールバックします。")
        self._save_value(self.K_API, value)

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

    def _refresh_usage_history_table(self) -> None:
        rows: list[ft.DataRow] = []
        total_cost = 0.0
        for record in self.usage_history:
            cost_value = float(record.get("cost", 0.0))
            total_cost += cost_value
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(str(record.get("timestamp", "-")))),
                        ft.DataCell(ft.Text(str(record.get("model", "-")))),
                        ft.DataCell(ft.Text(str(record.get("prompt", 0)))),
                        ft.DataCell(ft.Text(str(record.get("completion", 0)))),
                        ft.DataCell(ft.Text(str(record.get("total", 0)))),
                        ft.DataCell(ft.Text(f"${cost_value:.2f}")),
                    ]
                )
            )
        self.token_usage_history_table.rows = rows
        self.token_usage_history_table.update()

    def on_save_settings(self, e: ft.ControlEvent):
        key = self.api_key_field.value.strip()
        model = self.model_field.value.strip()
        mode = self.save_mode_switch.value
        if not key:
            self._append_log("[ERROR] API キーが空です。")
            return
        if model not in self.available_models:
            model = self.available_models[0]
            self.model_field.value = model
            self.model_field.update()
        self._save_api_key(key, mode)
        self._save_value(self.K_MODEL, model)
        self._save_value(self.K_SAVE_MODE, mode)
        self._append_log(f"[OK] 設定を保存しました（保存先: {mode}、モデル: {model}）。")

    # ------------------------------
    # Log & Progress
    # ------------------------------
    def _append_log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        raw_lines = msg.splitlines() or [msg]
        indent = " " * (len(timestamp) + 3)
        for idx, raw in enumerate(raw_lines):
            content = raw.strip() if raw.strip() else raw
            line = f"[{timestamp}] {content}" if idx == 0 else f"{indent}{content}"
            self._log_lines.append(line)
        if len(self._log_lines) > self._max_log_lines:
            self._log_lines = self._log_lines[-self._max_log_lines :]
        self.log.value = "\n".join(self._log_lines)
        try:
            self.log.scroll_to(offset=1.0, duration=0)
        except Exception:
            pass
        self.log.update()

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

    @staticmethod
    def _parse_fraction(text: str) -> tuple[int, int] | None:
        if "/" not in text:
            return None
        left, right = text.split("/", 1)
        try:
            return int(left.strip()), int(right.strip())
        except ValueError:
            return None

    def _update_extraction_progress(self, ratio: float, text: str):
        parsed = self._parse_fraction(text)
        if parsed:
            done, total = parsed
            label = f"抽出中: {done}件完了（全{total}件）"
        else:
            percent = int(max(0.0, min(1.0, ratio)) * 100)
            label = f"抽出中: {percent}%"
        self._set_progress(ratio, label)

    def _update_token_usage_ui(self, summary: TranslationSummary):
        pricing = self.model_pricing.get(summary.model)
        last_cost_total = 0.0
        if summary.usage_records:
            for prompt, completion, total in summary.usage_records:
                cost = 0.0
                if pricing:
                    cost += pricing["input"] * prompt / 1_000_000
                    cost += pricing["output"] * completion / 1_000_000
                last_cost_total += cost
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

        self.token_usage_prompt_text.value = f"入力トークン: {summary.prompt_tokens}"
        self.token_usage_completion_text.value = f"出力トークン: {summary.completion_tokens}"
        self.token_usage_total_text.value = f"合計トークン: {summary.total_tokens}"
        if summary.usage_records and pricing:
            self.token_usage_cost_text.value = f"概算コスト（今回）: ${last_cost_total:.2f}"
        elif summary.usage_records:
            self.token_usage_cost_text.value = "概算コスト（今回）: 不明 (料金表に無いモデル)"
        else:
            self.token_usage_cost_text.value = "概算コスト（今回）: $0.00"
        self.token_usage_cost_total_text.value = f"概算コスト累計: ${self.total_cost:.2f}"
        self.token_usage_updated_text.value = f"更新時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        self._refresh_usage_history_table()

        self.token_usage_summary.update()
        self.token_usage_prompt_text.update()
        self.token_usage_completion_text.update()
        self.token_usage_total_text.update()
        self.token_usage_cost_text.update()
        self.token_usage_updated_text.update()

    def _show_completion_toast(self, message: str, *, is_error: bool = False):
        if sys.platform != "win32":
            return
        try:
            self._show_windows_toast(APP_NAME, message, is_error=is_error)
        except Exception as ex:
            self._append_log(f"[WARN] Windowsトーストの表示に失敗しました: {repr(ex)}")

    def _show_windows_toast(self, title: str, message: str, *, is_error: bool = False):
        if sys.platform != "win32":
            return
        body = saxutils.escape(message.replace("\r\n", "\n").replace("\r", "\n")).replace("\n", "&#10;")
        header_text = f"{title} - エラー" if is_error else title
        header = saxutils.escape(header_text)
        visual = f"<toast><visual><binding template='ToastGeneric'><text>{header}</text><text>{body}</text></binding></visual></toast>"
        script = f"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@"
{visual}
"@)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.ExpirationTime = [DateTimeOffset]::Now.AddMinutes(1)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_NAME}')
$notifier.Show($toast)
"""
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        flags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags |= subprocess.DETACHED_PROCESS
        subprocess.Popen(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded],
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

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
        self.btn_extract.update()
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
                    summary = self._translate_targets(targets, mods_dir, temp_dir_path, out_dir)
                    self._update_token_usage_ui(summary)
                    if summary.aborted:
                        toast_message = "翻訳が停止されました。"
                        toast_is_error = True
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
                self.btn_extract.update()
                self._show_completion_toast(toast_message, is_error=toast_is_error)

        threading.Thread(target=_work, daemon=True).start()

    def _translate_targets(
        self,
        targets: list[tuple[str, Path, dict[str, str]]],
        source_path: Path,
        temp_dir: Path,
        output_dir: Path,
    ) -> TranslationSummary:
        total_targets = len(targets)
        api_key = self._load_api_key()
        if not api_key:
            self._append_log("[ERROR] API キーが未設定です。設定タブで保存してください。")
            self.tabs.selected_index = 1
            self.tabs.update()
            return TranslationSummary(
                translated_mods=0,
                total_mods=total_targets,
                translated_entries=0,
                total_entries=0,
                aborted=False,
                had_error=True,
                pack_dir=None,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                model="",
                usage_records=[],
            )
        model = self.model_field.value or self._load_value(self.K_MODEL) or self.default_model
        model = model.strip()
        if model not in self.available_models:
            model = self.available_models[0]
        self._append_log("[INFO] リソースパック出力先を確認しました。")
        self.stop_event.clear()
        self.btn_stop.disabled = False
        self.btn_stop.update()
        produced: list[tuple[str, Path]] = []
        aborted = False
        had_error = False
        total_entries = 0
        translated_entries = 0
        pack_dir_path: Path | None = None
        pack_generated_once = False
        self._set_progress(0.0, "翻訳準備中")
        self._set_detail_progress(0.0, "")
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_token_count = 0
        usage_records: list[tuple[int, int, int]] = []
        existing_pack_translations = self._collect_pack_translations(output_dir)
        resume_root = output_dir / ".resume"
        skipped_existing = 0

        def _register_pack_contents(pack_root: Path | None):
            if not pack_root:
                return
            for mod_name, lang_path in self._collect_pack_translations(pack_root).items():
                existing_pack_translations[mod_name] = lang_path

        try:
            for idx, (modid, in_path, existing_ja) in enumerate(targets, start=1):
                if self.stop_event.is_set():
                    aborted = True
                    break
                if not in_path.exists():
                    self._append_log(f"[WARN] en_us.json が見つかりません: {modid}")
                    continue
                out_path = in_path.parent / "ja_jp.json"
                completed_mods = idx - 1
                overall_ratio = completed_mods / total_targets if total_targets else 0.0
                self._set_progress(
                    overall_ratio,
                    f"翻訳中: {completed_mods}件完了（全{total_targets}件）",
                )
                self._append_log(f"[RUN] 翻訳を開始します: {modid}")
                self._set_detail_progress(0.0, f"{modid}: 0%")

                def _progress_wrapper(ratio: float, text: str, *, _modid: str = modid):
                    parsed = self._parse_fraction(text.strip())
                    if parsed:
                        done, total = parsed
                        label = f"{_modid}: {done}件完了（全{total}件）"
                    else:
                        percent = int(max(0.0, min(1.0, ratio)) * 100)
                        label = f"{_modid}: {percent}%"
                    self._set_detail_progress(ratio, label)

                pack_lang_path = existing_pack_translations.get(modid)
                resume_path = resume_root / modid / "ja_jp.json"
                resume_exists = resume_path.exists()
                if resume_exists:
                    self._append_log(
                        f"[INFO] 中断済みの翻訳ファイルを検出しました（{modid}）。未訳を引き継ぎます。"
                    )
                if (
                    pack_lang_path
                    and pack_lang_path.exists()
                    and resume_root not in pack_lang_path.parents
                ):
                    skipped_existing += 1
                    self._append_log(
                        f"[INFO] mods_ja_resource に既存の翻訳が見つかったためスキップします（{modid}）。"
                    )
                    overall_ratio = idx / total_targets if total_targets else 1.0
                    self._set_progress(
                        overall_ratio,
                        f"翻訳中: {idx}件完了（全{total_targets}件）",
                    )
                    self._set_detail_progress(1.0, f"{modid}: 既存訳を使用")
                    continue

                try:
                    result = translate_localizations(
                        api_key=api_key,
                        model=model,
                        in_path=in_path,
                        out_path=out_path,
                        existing_translations=existing_ja,
                        log=self._append_log,
                        progress=_progress_wrapper,
                        should_stop=self.stop_event.is_set,
                        resume_path=resume_path,
                    )
                    total_entries += result.total
                    translated_entries += result.created
                    total_prompt_tokens += result.prompt_tokens
                    total_completion_tokens += result.completion_tokens
                    total_token_count += result.total_tokens
                    for usage in result.usages:
                        prompt = usage.prompt_tokens
                        completion = usage.completion_tokens
                        total_tok = usage.total_tokens or (prompt + completion)
                        usage_records.append((prompt, completion, total_tok))
                    remaining = max(0, result.total - result.created)
                    if result.stopped:
                        if remaining:
                            self._append_log(
                                f"[INFO] 未翻訳 {remaining} 件の進捗を保存しました。再開時は自動的に続きから処理します。"
                            )
                        elif resume_exists:
                            self._append_log(
                                "[INFO] 停止時点の翻訳は保存済みです。再開時に利用されます。"
                            )
                        self._append_log("[INFO] ユーザーによって翻訳が停止されました。")
                        aborted = True
                        break
                    if out_path.exists():
                        produced.append((modid, out_path))
                    self._append_log(f"[OK] ja_jp.json を作成しました（{modid}）。")
                    pack_dir = self._generate_resource_pack(source_path, temp_dir, output_dir, produced)
                    if pack_dir:
                        pack_dir_path = pack_dir
                        pack_generated_once = True
                        self._append_log(f"[OK] リソースパックを更新しました（{modid}）。")
                        _register_pack_contents(pack_dir)
                    if not aborted:
                        overall_ratio = idx / total_targets if total_targets else 1.0
                        self._set_progress(
                            overall_ratio,
                            f"翻訳中: {idx}件完了（全{total_targets}件）",
                        )
                except Exception as ex:
                    had_error = True
                    self._append_log(f"[ERROR] 翻訳処理で例外 ({modid}): {repr(ex)}")
                    self._append_log(traceback.format_exc())
                    aborted = True
                    break
        finally:
            self.stop_event.clear()
            self.btn_stop.disabled = True
            self.btn_stop.update()
        if produced and not aborted and not pack_generated_once:
            try:
                pack_dir = self._generate_resource_pack(source_path, temp_dir, output_dir, produced)
                if pack_dir:
                    pack_dir_path = pack_dir
                    self._append_log(f"[OK] リソースパックを更新しました（{pack_dir.name}）。")
                    pack_png = pack_dir / "pack.png"
                    if not pack_png.exists():
                        self._append_log(f"[INFO] pack.png は手動で配置してください（{pack_dir.name}）。")
                    _register_pack_contents(pack_dir)
            except Exception as ex:
                had_error = True
                self._append_log(f"[ERROR] リソースパックの生成に失敗しました: {repr(ex)}")
                self._append_log(traceback.format_exc())
        elif aborted:
            self._append_log("[WARN] 翻訳が完了しなかったため、リソースパックの作成をスキップしました。")
        elif skipped_existing:
            self._append_log(
                f"[INFO] {skipped_existing} 件の Mod は mods_ja_resource に既存の翻訳があったため処理をスキップしました。"
            )
        else:
            self._append_log("[WARN] ja_jp.json が生成されなかったため、リソースパックの作成をスキップしました。")
        return TranslationSummary(
            translated_mods=len(produced),
            total_mods=total_targets,
            translated_entries=translated_entries,
            total_entries=total_entries,
            aborted=aborted,
            had_error=had_error,
            pack_dir=pack_dir_path,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_token_count,
            model=model,
            usage_records=usage_records,
        )

    def on_stop(self, e: ft.ControlEvent):
        self.stop_event.set()
        self._append_log("[INFO] 停止要求を送信しました。現在のバッチ終了後に停止します。")

    def _generate_resource_pack(
        self,
        source_path: Path,
        temp_dir: Path,
        output_dir: Path,
        produced: list[tuple[str, Path]],
    ) -> Path | None:
        if not produced:
            return None
        self._append_log("[INFO] リソースパック生成を開始します。")
        base_name = self._determine_pack_base_name(source_path)
        if not base_name and produced and produced[0][0]:
            base_name = produced[0][0]
        base_name = base_name or "ja_resource"
        pack_name = f"{base_name}_ja_resource"
        pack_dir = output_dir / pack_name
        pack_dir.mkdir(parents=True, exist_ok=True)
        self._apply_template_files(pack_dir)
        assets_dir = pack_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for modid, ja_path in produced:
            target_dir = assets_dir / modid / "lang"
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ja_path, target_dir / "ja_jp.json")
        self._write_pack_mcmeta(pack_dir, base_name)
        return pack_dir

    def _determine_pack_base_name(self, source_path: Path) -> str:
        candidate = ""
        try:
            if source_path.is_dir():
                candidate = source_path.name
            else:
                candidate = source_path.stem
        except Exception:
            candidate = ""
        return candidate.strip()

    def _write_pack_mcmeta(self, pack_dir: Path, mod_name: str):
        latest_pack_format = 34
        description = f"{mod_name} 日本語ローカライズ\n生成日: {datetime.now().strftime('%Y-%m-%d')}"
        pack_meta = {
            "pack": {
                "pack_format": latest_pack_format,
                "supported_formats": {
                    "edition": "java",
                    "min_inclusive": 1,
                    "max_inclusive": latest_pack_format,
                },
                "description": description,
            }
        }
        pack_dir.mkdir(parents=True, exist_ok=True)
        mcmeta_path = pack_dir / "pack.mcmeta"
        mcmeta_path.write_text(json.dumps(pack_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _apply_template_files(self, pack_dir: Path):
        if not RESOURCE_TEMPLATE_DIR.exists():
            return
        for item in RESOURCE_TEMPLATE_DIR.iterdir():
            if item.name in {"pack.mcmeta", "pack.png"}:
                continue
            target = pack_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif item.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)

    def _collect_pack_translations(self, base_dir: Path) -> dict[str, Path]:
        translations: dict[str, Path] = {}
        if not base_dir.exists() or not base_dir.is_dir():
            return translations

        try:
            for candidate in base_dir.rglob("ja_jp.json"):
                if not candidate.is_file():
                    continue
                parent = candidate.parent
                if parent.name == "lang" and parent.parent != parent:
                    translations[parent.parent.name] = candidate
                else:
                    translations[parent.name] = candidate
        except Exception:
            return translations

        return translations


def main(page: ft.Page):
    LocalizeApp(page)
