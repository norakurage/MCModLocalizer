from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path

import flet as ft

from processing import ExtractionResult, extract_localizations, translate_localizations

APP_NAME = "MC Localizer"
BASE_DIR = Path(__file__).resolve().parent
RESOURCE_TEMPLATE_DIR = BASE_DIR / "a"

try:
    import keyring  # type: ignore
except Exception:
    keyring = None


class LocalizeApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.stop_event = threading.Event()
        # 保存キー
        self.K_API = "openai_api_key"
        self.K_MODEL = "openai_model"
        self.K_GLOSS = "glossary_path"
        self.K_SAVE_MODE = "save_mode"  # "keyring" or "local"
        self.K_DIR_JAR = "dir_mod_jar"
        self.K_DIR_OUTPUT = "dir_output_pack"
        self.K_DIR_GLOSSARY = "dir_glossary_csv"
        # 既定値
        self.default_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        # -------------- UI 構築 --------------
        page.title = f"{APP_NAME} (Flet)"
        page.padding = 16
        page.window_width = 1000
        page.window_height = 820
        page.theme_mode = "light"
        # ログ & 進捗
        self.log = ft.TextField(label="ログ", multiline=True, read_only=True, expand=True,
                                min_lines=12, max_lines=9999, border=ft.InputBorder.OUTLINE)
        self.progress = ft.ProgressBar(width=420, value=0)
        self.counter = ft.Text("待機中")
        # -------- 抽出タブ UI --------
        self.mod_jar_path = ft.TextField(label="Mod JAR（必須）", dense=True, expand=True)
        self.output_dir = ft.TextField(label="出力フォルダ（リソースパック保存先）", dense=True, expand=True)
        self.glossary_path = ft.TextField(label="用語集 CSV（任意）", dense=True, expand=True)
        self.fp_jar = ft.FilePicker(on_result=self._on_pick_jar)
        self.fp_dir = ft.FilePicker(on_result=self._on_pick_dir)
        self.fp_gloss = ft.FilePicker(on_result=self._on_pick_glossary)
        self.page.overlay.extend([self.fp_jar, self.fp_dir, self.fp_gloss])
        pick_jar_btn = ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="Mod JAR を選択",
                                     on_click=self._open_jar_picker)
        pick_dir_btn = ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="出力フォルダを選択",
                                     on_click=self._open_output_dir_picker)
        pick_gloss_btn = ft.IconButton(icon=ft.Icons.FOLDER_OPEN, tooltip="用語集 CSV を選択",
                                       on_click=self._open_glossary_picker)
        self.btn_extract = ft.ElevatedButton("抽出 / ja_jp 生成", icon=ft.Icons.DOWNLOAD, on_click=self.on_extract)
        self.btn_stop = ft.OutlinedButton("停止", icon=ft.Icons.STOP, on_click=self.on_stop, disabled=True)
        extract_tab = ft.Column(
            controls=[
                ft.Text("ステップ: JAR から en_us.json を抽出し、ja_jp.json まで自動生成します。", weight=ft.FontWeight.BOLD),
                ft.Row([self.mod_jar_path, pick_jar_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.output_dir, pick_dir_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.glossary_path, pick_gloss_btn], alignment=ft.MainAxisAlignment.START),
                ft.Row([self.btn_extract, self.btn_stop, self.progress, self.counter], spacing=16, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                self.log,
            ],
            expand=True,
            spacing=12,
        )
        # -------- 設定タブ UI --------
        saved_model = self._load_value(self.K_MODEL) or self.default_model
        self.api_key_field = ft.TextField(
            label="OpenAI API キー",
            password=True, can_reveal_password=True, dense=True, expand=True,
            hint_text="例: sk-...", value=self._load_api_key() or "",
        )
        self.model_field = ft.Dropdown(
            label="モデル",
            value=saved_model,
            options=[
                ft.dropdown.Option("gpt-4o-mini"),
                ft.dropdown.Option("gpt-4o"),
                ft.dropdown.Option("o4-mini"),
                ft.dropdown.Option("o4"),
            ],
            dense=True, expand=False, width=220,
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
            ],
            expand=True,
            spacing=12,
        )
        # -------- Tabs --------
        self.tabs = ft.Tabs(
            selected_index=0,
            tabs=[
                ft.Tab(text="抽出", icon=ft.Icons.DOWNLOAD, content=extract_tab),
                ft.Tab(text="設定", icon=ft.Icons.SETTINGS, content=settings_tab),
            ],
            expand=True,
        )
        page.add(self.tabs)
        self._append_log("準備完了。JAR と出力フォルダを指定して抽出を実行すると ja_jp.json とリソースパックを自動生成します。")

    # ------------------------------
    # FilePicker launchers
    # ------------------------------
    def _open_jar_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_JAR)
        self.fp_jar.pick_files(initial_directory=init_dir, allowed_extensions=["jar"], allow_multiple=False)

    def _open_output_dir_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_OUTPUT)
        self.fp_dir.get_directory_path(initial_directory=init_dir)

    def _open_glossary_picker(self, e: ft.ControlEvent):
        init_dir = self._get_initial_directory(self.K_DIR_GLOSSARY)
        self.fp_gloss.pick_files(initial_directory=init_dir, allowed_extensions=["csv"], allow_multiple=False)

    # ------------------------------
    # FilePicker handlers
    # ------------------------------
    def _on_pick_jar(self, e: ft.FilePickerResultEvent):
        if e.files:
            selected = Path(e.files[0].path)
            self.mod_jar_path.value = str(selected)
            self.mod_jar_path.update()
            self._remember_dir(self.K_DIR_JAR, selected)

    def _on_pick_dir(self, e: ft.FilePickerResultEvent):
        if e.path:
            selected = Path(e.path)
            self.output_dir.value = str(selected)
            self.output_dir.update()
            self._remember_dir(self.K_DIR_OUTPUT, selected)

    def _on_pick_glossary(self, e: ft.FilePickerResultEvent):
        if e.files:
            selected = Path(e.files[0].path)
            self.glossary_path.value = str(selected)
            self.glossary_path.update()
            self._remember_dir(self.K_DIR_GLOSSARY, selected)

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

    def on_save_settings(self, e: ft.ControlEvent):
        key = self.api_key_field.value.strip()
        model = self.model_field.value.strip()
        mode = self.save_mode_switch.value
        if not key:
            self._append_log("[ERROR] API キーが空です。")
            return
        self._save_api_key(key, mode)
        self._save_value(self.K_MODEL, model)
        self._save_value(self.K_SAVE_MODE, mode)
        self._append_log(f"[OK] 設定を保存しました（保存先: {mode}、モデル: {model}）。")

    # ------------------------------
    # Log & Progress
    # ------------------------------
    def _append_log(self, msg: str):
        self.log.value = (self.log.value + ("\n" if self.log.value else "")) + msg
        self.log.update()

    def _set_progress(self, ratio: float, text: str = ""):
        self.progress.value = max(0.0, min(1.0, ratio))
        self.progress.update()
        if text:
            self.counter.value = text
            self.counter.update()

    # ------------------------------
    # 抽出フロー
    # ------------------------------
    def on_extract(self, e: ft.ControlEvent):
        jar_path_value = self.mod_jar_path.value.strip()
        jar_path = Path(jar_path_value) if jar_path_value else None
        out_dir_value = self.output_dir.value.strip()
        out_dir = Path(out_dir_value) if out_dir_value else None
        gloss_path_value = self.glossary_path.value.strip()
        gloss_path = Path(gloss_path_value) if gloss_path_value else None
        if not jar_path or not jar_path.exists():
            display = jar_path if jar_path else "(未指定)"
            self._append_log(f"[ERROR] Mod JAR が見つかりません: {display}")
            return
        if out_dir is None:
            self._append_log("[ERROR] 出力フォルダを指定してください。")
            return
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                self._append_log(f"[INFO] 出力フォルダを作成しました: {out_dir}")
            except Exception as ex:
                self._append_log(f"[ERROR] 出力フォルダを作成できません: {repr(ex)}")
                return
        self._remember_dir(self.K_DIR_JAR, jar_path)
        self._remember_dir(self.K_DIR_OUTPUT, out_dir)
        if gloss_path:
            self._remember_dir(self.K_DIR_GLOSSARY, gloss_path)
        self.btn_extract.disabled = True
        self.btn_extract.update()
        self._set_progress(0.0, "抽出開始")

        def _work():
            temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
            temp_dir_path: Path | None = None
            try:
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="mc_localizer_")
                temp_dir_path = Path(temp_dir_obj.name)
                self._append_log(f"[INFO] 一時作業フォルダ: {temp_dir_path}")
                self._append_log(f"[RUN] 抽出: {jar_path}")
                result: ExtractionResult = extract_localizations(
                    jar_path,
                    temp_dir_path,
                    log=self._append_log,
                    progress=self._set_progress,
                )
                targets: list[tuple[str, Path]] = []
                for modid in result.mod_maps.keys():
                    en_path = temp_dir_path / modid / "en_us.json"
                    if en_path.exists():
                        targets.append((modid, en_path))
                if targets:
                    self._append_log("[RUN] 抽出が完了したため、翻訳を開始します。")
                    self._translate_targets(targets, gloss_path, jar_path, temp_dir_path, out_dir)
                else:
                    self._set_progress(1.0, "抽出完了")
                    self._append_log("[WARN] 翻訳対象となる en_us.json が見つかりませんでした。")
            except Exception as ex:
                self._append_log("[ERROR] 抽出処理で例外: " + repr(ex))
                self._append_log(traceback.format_exc())
            finally:
                if temp_dir_obj:
                    temp_dir_obj.cleanup()
                    if temp_dir_path:
                        self._append_log(f"[INFO] 一時作業フォルダを削除しました: {temp_dir_path}")
                self.btn_extract.disabled = False
                self.btn_extract.update()

        threading.Thread(target=_work, daemon=True).start()

    def _translate_targets(
        self,
        targets: list[tuple[str, Path]],
        gloss_path: Path | None,
        jar_path: Path,
        temp_dir: Path,
        output_dir: Path,
    ):
        api_key = self._load_api_key()
        if not api_key:
            self._append_log("[ERROR] API キーが未設定です。設定タブで保存してください。")
            self.tabs.selected_index = 1
            self.tabs.update()
            return
        model = (self._load_value(self.K_MODEL) or self.default_model)
        effective_gloss = gloss_path if gloss_path and gloss_path.exists() else None
        if gloss_path and not gloss_path.exists():
            self._append_log(f"[WARN] 用語集が見つかりませんでした: {gloss_path}")
        self._append_log(f"[INFO] リソースパック出力先: {output_dir}")
        self.stop_event.clear()
        self.btn_stop.disabled = False
        self.btn_stop.update()
        total_targets = len(targets)
        produced: list[tuple[str, Path]] = []
        aborted = False
        try:
            for idx, (modid, in_path) in enumerate(targets, start=1):
                if self.stop_event.is_set():
                    aborted = True
                    break
                if not in_path.exists():
                    self._append_log(f"[WARN] en_us.json が見つかりません: {in_path}")
                    continue
                out_path = in_path.parent / "ja_jp.json"
                self._append_log(f"[RUN] 翻訳 {idx}/{total_targets}: {modid} -> {out_path}")
                self._set_progress(0.0, f"翻訳 {idx}/{total_targets}")
                try:
                    result = translate_localizations(
                        api_key=api_key,
                        model=model,
                        in_path=in_path,
                        out_path=out_path,
                        gloss_path=effective_gloss,
                        log=self._append_log,
                        progress=self._set_progress,
                        should_stop=self.stop_event.is_set,
                    )
                    if result.stopped:
                        self._append_log("[INFO] ユーザーによって翻訳が停止されました。")
                        aborted = True
                        break
                    if out_path.exists():
                        produced.append((modid, out_path))
                    self._append_log(f"[OK] ja_jp.json を作成しました: {out_path}")
                except Exception as ex:
                    self._append_log(f"[ERROR] 翻訳処理で例外 ({modid}): {repr(ex)}")
                    self._append_log(traceback.format_exc())
                    aborted = True
                    break
        finally:
            self.stop_event.clear()
            self.btn_stop.disabled = True
            self.btn_stop.update()
        if produced and not aborted:
            try:
                pack_dir = self._generate_resource_pack(jar_path, temp_dir, output_dir, produced)
                if pack_dir:
                    self._append_log(f"[OK] リソースパックを更新しました: {pack_dir}")
                    pack_png = pack_dir / "pack.png"
                    if not pack_png.exists():
                        self._append_log(f"[INFO] pack.png は手動で配置してください: {pack_png}")
            except Exception as ex:
                self._append_log(f"[ERROR] リソースパックの生成に失敗しました: {repr(ex)}")
                self._append_log(traceback.format_exc())
        elif aborted:
            self._append_log("[WARN] 翻訳が完了しなかったため、リソースパックの作成をスキップしました。")
        else:
            self._append_log("[WARN] ja_jp.json が生成されなかったため、リソースパックの作成をスキップしました。")

    def on_stop(self, e: ft.ControlEvent):
        self.stop_event.set()
        self._append_log("[INFO] 停止要求を送信しました。現在のバッチ終了後に停止します。")

    def _generate_resource_pack(
        self,
        jar_path: Path,
        temp_dir: Path,
        output_dir: Path,
        produced: list[tuple[str, Path]],
    ) -> Path | None:
        if not produced:
            return None
        self._append_log(f"[INFO] リソースパック生成: 作業フォルダ {temp_dir} を参照します。")
        pack_name = f"{jar_path.stem}_ja_resourcepack" if jar_path.stem else "ja_resourcepack"
        pack_dir = output_dir / pack_name
        preserved_pack_png: bytes | None = None
        if pack_dir.exists():
            pack_png_path = pack_dir / "pack.png"
            if pack_png_path.exists():
                try:
                    preserved_pack_png = pack_png_path.read_bytes()
                    self._append_log(f"[INFO] 既存の pack.png を退避します: {pack_png_path}")
                except Exception:
                    preserved_pack_png = None
        if pack_dir.exists():
            shutil.rmtree(pack_dir)
        pack_dir.mkdir(parents=True, exist_ok=True)
        self._apply_template_files(pack_dir)
        assets_dir = pack_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for modid, ja_path in produced:
            target_dir = assets_dir / modid / "lang"
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ja_path, target_dir / "ja_jp.json")
        self._write_pack_mcmeta(pack_dir, jar_path)
        if preserved_pack_png is not None:
            (pack_dir / "pack.png").write_bytes(preserved_pack_png)
        return pack_dir

    def _write_pack_mcmeta(self, pack_dir: Path, jar_path: Path):
        latest_pack_format = 34
        description = f"{jar_path.stem} 日本語ローカライズ\n生成日: {datetime.now().strftime('%Y-%m-%d')}"
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


def main(page: ft.Page):
    LocalizeApp(page)
