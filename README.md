# MC Localizer

Minecraft Mod 向けの `en_us.json` を OpenAI API で日本語化 (`ja_jp.json`) し、差分を尊重しながらリソースパックを構築する Flet 製 GUI アプリです。翻訳時に色コードやプレースホルダーを保護し、既存訳を壊さず不足分のみを補完します。

## 主な機能
- Flet ベースのクロスプラットフォーム GUI（Windows / macOS / Linux）
- Mod JAR から `en_us.json` を抽出し、OpenAI Responses API で `ja_jp.json` を生成、リソースパック (`<Mod名>_ja_resourcepack`) をまとめて出力
- 複数 namespace（modid）を自動検出し、それぞれの `ja_jp.json` を 1 つのパックに集約
- `%s` や `§a`、`{name}` のようなトークンを自動保護し、バッチ翻訳後に原文どおり復元
- API キーを keyring またはローカルストレージに安全保存でき、環境変数でも指定可能
- 実行ログ・進捗バー・停止ボタン・Windows トースト通知（Windows 10+）で処理状況を把握

## 必要条件
- Python 3.10 以上（Flet 0.28.3+ 推奨）
- OpenAI アカウントと API キー
- インターネット接続（OpenAI API 呼び出しに使用）

## セットアップ
1. 仮想環境の作成（任意）
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
2. 依存パッケージのインストール
   ```powershell
   pip install "flet>=0.28.3" "openai>=1.14.0" keyring
   ```
   `keyring` は任意ですが、API キーを OS の資格情報ストアに保存する場合に便利です。

3. OpenAI API キーの設定
   - アプリの「設定」タブから入力し、保存先（keyring / local）を選択できます。
   - もしくは環境変数で指定します。
     ```powershell
     setx OPENAI_API_KEY "sk-..."
     setx OPENAI_MODEL "gpt-4o-mini"
     ```

## 実行方法
```powershell
python main.py
```
Flet がブラウザまたはネイティブウィンドウで UI を起動します。

## 使い方
1. **Mod JAR**：翻訳対象の Mod JAR を指定します。複数 modid を含む場合はログに一覧が表示され、すべての言語ファイルが対象になります。
2. **出力フォルダ**：生成したリソースパックを配置するフォルダを選択します。実行ごとに `<Mod名>_ja_resourcepack` が作成または更新されます。
3. **設定タブ**：OpenAI API キーとモデル (`gpt-4o-mini` / `gpt-4o` / `o4-mini` / `o4`) を選択し、保存ボタンを押します。環境変数があれば自動で既定値になります。
4. **抽出 / ja_jp 生成**：ボタンを押すとバックグラウンドで抽出・翻訳・パック生成を行います。進捗はステータスバーとログに表示され、必要であれば「停止」で現在のバッチ処理後に中断できます。完了すると（Windows の場合は）トースト通知が表示されます。

Tips:
- 翻訳結果は出力フォルダ内のリソースパックに上書きされます。`pack.png` が既に存在する場合は自動的に保持されます。
- 停止すると現在のバッチ終了後に翻訳が中断され、今回の翻訳ではリソースパックを更新しません。再開したい場合は再度実行してください。

## 出力されるもの
- `<出力フォルダ>/<Mod名>_ja_resourcepack/`
  - `pack.mcmeta`：`supported_formats` 付きで最新 pack format に対応
  - `assets/<modid>/lang/ja_jp.json`：OpenAI で翻訳した日本語ローカライズ
  - （存在する場合）`pack.png`：既存のアイコンを自動コピー

## テンプレートについて
- `pack.mcmeta` と `pack.png` だけは特別扱いされ、`pack.mcmeta` はツールが生成したものが優先され、`pack.png` は既存ファイルが無い場合のみテンプレートからコピーされます。

## Windows 向け EXE ビルド
配布用のスタンドアロン実行ファイル (`.exe`) を作成したい場合は Windows 上で PowerShell を開き、リポジトリ直下で次を実行します。

```powershell
Set-Location <リポジトリのパス>
.\scripts\build_windows_exe.ps1
```

スクリプトは `.venv-build/` に仮想環境を作成し、`requirements.txt` とビルドに必要な PyInstaller をインストールしたあと `python -m flet pack` でパッケージングします。完了すると `dist/MC Localizer/MC Localizer.exe` が出力されます。

ヒント:
- 再ビルド時に環境を作り直したい場合は `-Clean` オプションを付けて実行してください。
- 独自のアイコンを設定したい場合は `scripts/build_windows_exe.ps1` 内の `flet pack` コマンドに `--icon <icoファイル>` を追記します。

## トラブルシューティング
- **API キー未設定**：設定タブで保存し直すか、環境変数を確認してください。
- **翻訳が Rate limit / ネットワークエラーで失敗**：OpenAI 側の制限が原因の場合があります。時間をおいて再実行してください。
- **keyring が利用できない**：`pip show keyring` でインストールを確認し、OS ごとのバックエンド設定を行うか、保存先を `local` に切り替えてください。
- **Windows トーストが出ない**：PowerShell 実行ポリシーや通知設定を確認してください。失敗時はログに警告が表示されます。

## ライセンス
未定義です。必要に応じてプロジェクトのポリシーを追記してください。

## 開発メモ
- Flet 0.28.3 以降の PascalCase API（`Icons` / `Colors` / `Alignment`）に合わせています。
- メイン処理はバックグラウンドスレッドで実行し、UI スレッドはログ更新のみ行います。
- OpenAI Responses API（`client.responses.create`）を利用しているため、`python-openai` v1 系が必要です。

