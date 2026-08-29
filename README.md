# md-viewer

A single-file, offline-capable Markdown viewer with KaTeX math, Mermaid diagrams, and full-text search.

数式（KaTeX）・Mermaid 図・全文検索に対応した、単一 HTML ファイルの Markdown ビューアです。

**▶ https://toru-ishikawa.github.io/md-viewer/**

## 使い方

- **フォルダを開く（画像付き）**: md と画像を含む親フォルダを選択（またはウィンドウにドロップ）。相対パス画像も解決して表示。左に一覧、全文検索、右に目次
- 前回開いたフォルダ／ファイルの場所を記憶（IndexedDB）。ダイアログはその場所から開き、左の一覧の「前回のフォルダを開く」でダイアログなしに開き直せます。同じフォルダなら最後に見ていた md を再表示
- **.md 単体を開く**: 1 ファイルだけをさっと表示
- **ペースト**: ページ上のどこでも Ctrl+V するだけで、クリップボードのテキストを即プレビュー。下書きは自動保存され、「保存」で .md として書き出し
  - **折り返し解除**（既定 ON、貼り付け欄の上で切り替え・ブラウザに保存）: Claude Code のターミナル出力などをコピーした際に端末幅で折り返された行を、元の 1 行に戻して表示（日本語の文字単位折り返しにも対応）。ツール結果（`⎿`）やコードらしい塊はそのまま。生のテキストは textarea に残るので、切ればいつでも元通り
- 「訳文のみ」: 引用ブロック（`>`）を畳んで表示するモード
- 「表示」メニュー: 文字サイズ（既定は「自動」＝ウィンドウ幅に応じて 14〜20px、本文の幅もそれに連動）・行間・本文の幅・フォント（ゴシック/明朝）・テーマ（ライト/ダーク/OS に従う）を切り替え。設定はブラウザに保存
- 本文幅より広い別行数式は自動で縮小して収める（ウィンドウ幅やパネルの開閉に追従）。狭いウィンドウ／タブレットではファイル一覧・目次を自動で畳み、開いたときは本文に重ねて表示
- 図はクリックで拡大（Esc で閉じる）。1 行に画像だけを置いた段落は alt テキストをキャプションとして表示。コードブロックには言語名、幅の広い表は横スクロール
- 「再読み込み」（Ctrl+R / F5）: 開いているファイルをディスクから読み直す。読んでいた位置は保持。フォルダで開いている場合は新規・削除・変更されたファイルも一覧に反映（Chrome で「フォルダを開く」「.md 単体を開く」またはドラッグ＆ドロップで開いた場合。File System Access API のハンドルを保持するため、ファイルが更新されても読み直せる）
- 印刷（Ctrl+P）にも対応（ダークテーマ中でも白地で印刷）

## サーバモード（WSL / Linux のファイルをパス指定で開く・編集する）

`serve.py`（Python 3 標準ライブラリのみ）を添えて `http://localhost:8788/` で開くと、ブラウザのダイアログを経ずにファイルをパスで指定して開けます。WSL 側のファイルを Windows のブラウザで見る、`yazi` などのファイラから Enter で開く、といった用途向けです。

```sh
python3 ~/repos/md-viewer/serve.py open ~/repos/foo/notes/x.md   # 未起動なら常駐起動 → 既定ブラウザで開く
python3 ~/repos/md-viewer/serve.py status                        # 常駐サーバの状態
python3 ~/repos/md-viewer/serve.py stop                          # 終了（明示的に止めるまで常駐）
python3 ~/repos/md-viewer/serve.py serve --port 8788 --open      # フォアグラウンドで起動
```

- URL は `http://localhost:8788/?path=<絶対パス>`（`&root=<絶対パス>` で一覧の範囲を指定可）。ファイラの opener には `serve.py open %s` を登録するだけで済みます
- 開いた md のあるリポジトリ（最寄りの `.git`）を一覧・全文検索の範囲にします。git 管理外なら md のあるフォルダ。相対パス画像は許可ルート内であれば `../figs/x.png` のような参照も表示
- Ctrl+R / F5 はディスクから読み直し、一覧の新規・削除・変更も反映。一覧から別の md を選ぶと URL も追従するので、ブラウザのリロードやブックマークでも同じファイルに戻れます
- **編集**: 「編集」ボタン（Ctrl+E）で左に編集欄、右にライブプレビュー。保存は明示的に **Ctrl+S**（保存ボタン）。編集中の内容はブラウザにも下書き保存され、タブを閉じても次に開いたとき復元できます。保存時にディスク上のファイルが別のプロセス（Claude Code など）に書き換えられていれば競合を検出し、「ディスク版を読み込む（自分の編集は下書きに退避）」か「上書き保存」かを選べます。保存はアトミック（一時ファイル → 置換）で、元ファイルの改行コード（CRLF/LF）と BOM を保ちます。保存前の内容は `~/.cache/md-viewer/backup/` に 20 世代まで退避
- **VS Code で開く**: 表示中のファイルを `code -r` で開きます（コマンドは設定で変更可）
- 設定ファイル `~/.config/md-viewer/serve.json`（省略可）:

  ```json
  { "roots": ["/mnt/d/Dropbox"], "port": 8788, "browser": null, "editor": ["code", "-r"], "backupKeep": 20 }
  ```

  `roots` は許可ルートへの追加（コード既定は `~/repos` のみ）。`browser` はブラウザの実行ファイル（既定は WSL なら `rundll32 url.dll,FileProtocolHandler` で Windows の既定ブラウザ、Linux なら `xdg-open`。`explorer.exe` や `cmd /c start` は URL のクエリを落とすので使いません）

- 安全策: 127.0.0.1 にのみバインド、Host ヘッダと `Sec-Fetch-Site` を検査（他のサイトからの読み出しを拒否）、realpath 後に許可ルート配下かを検査、扱う拡張子を md/markdown/txt/画像/pdf に限定（書き込みは md/markdown/txt のみ）。`.env` などはサーバから読めません
- 通常の `file://` や GitHub Pages で開いたときの動作は変わりません（サーバモードのコードは `http://localhost` でしか動きません）

## プライバシー

すべての処理はブラウザ内で完結します。開いたファイル・貼り付けた内容が外部に送信されることはありません（GitHub Pages は静的配信のみ）。ペーストの下書きと「表示」の設定はお使いのブラウザの localStorage にのみ保存されます。サーバモードの `serve.py` も自分の PC の 127.0.0.1 でのみ待ち受け、ネットワークには出ません。

## 同梱ライブラリ

単一 HTML に以下を同梱しています（いずれも MIT License）:

- [markdown-it](https://github.com/markdown-it/markdown-it)
- [KaTeX](https://katex.org/)（フォント含む）
- [Mermaid](https://mermaid.js.org/)

## License

MIT — 詳細は [LICENSE](LICENSE) を参照。同梱ライブラリは各プロジェクトのライセンス条件に従います。
