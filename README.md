# md-viewer

A single-file, offline-capable Markdown viewer with KaTeX math, Mermaid diagrams, and full-text search.

数式（KaTeX）・Mermaid 図・全文検索に対応した、単一 HTML ファイルの Markdown ビューアです。

**▶ https://toru-ishikawa.github.io/md-viewer/**

## 使い方

- **フォルダを開く（画像付き）**: md と画像を含む親フォルダを選択（またはウィンドウにドロップ）。相対パス画像も解決して表示。左に一覧、全文検索、右に目次
- **.md 単体を開く**: 1 ファイルだけをさっと表示
- **ペースト**: ページ上のどこでも Ctrl+V するだけで、クリップボードの markdown を即プレビュー。下書きは自動保存され、「保存」で .md として書き出し
- 「訳文のみ」: 引用ブロック（`>`）を畳んで表示するモード
- 印刷（Ctrl+P）にも対応

## プライバシー

すべての処理はブラウザ内で完結します。開いたファイル・貼り付けた内容が外部に送信されることはありません（GitHub Pages は静的配信のみ）。ペーストの下書きはお使いのブラウザの localStorage にのみ保存されます。

## 同梱ライブラリ

単一 HTML に以下を同梱しています（いずれも MIT License）:

- [markdown-it](https://github.com/markdown-it/markdown-it)
- [KaTeX](https://katex.org/)（フォント含む）
- [Mermaid](https://mermaid.js.org/)

## License

MIT — 詳細は [LICENSE](LICENSE) を参照。同梱ライブラリは各プロジェクトのライセンス条件に従います。
