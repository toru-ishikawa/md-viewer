#!/usr/bin/env python3
"""md-viewer サーバモード — index.html をローカル HTTP で配信し、WSL / Linux 側の md を
「パス指定で開く・相対パス画像つきで表示・一覧と全文検索・編集して保存」できるようにする。
標準ライブラリのみ。ブラウザは Windows 側でも WSL 側でもよい(localhost で届く)。

  python3 serve.py open <file.md> [--root DIR]      未起動なら常駐起動してから既定ブラウザで開く
                                                    (yazi などの opener 用。同じサーバを使い回す)
  python3 serve.py [serve] [--port N] [--root DIR ...] [--open]
                                                    フォアグラウンドで起動(Ctrl+C で終了)
  python3 serve.py status | stop                    常駐サーバの状態表示 / 終了

  URL: http://localhost:8788/?path=<絶対パス>[&root=<絶対パス>]
       path … 開く md(許可ルート配下)。root … 一覧・全文検索の範囲(省略時は path から
       最も近い git リポジトリのルート、それも無ければ md のあるディレクトリ)

設定ファイル ~/.config/md-viewer/serve.json(すべて省略可):
  { "roots": ["/mnt/d/Dropbox"],        許可ルートに追加(コード既定は ~/repos のみ)
    "port": 8788,
    "browser": null,                     ブラウザ実行ファイル(既定: WSL は rundll32 経由で既定ブラウザ, Linux は xdg-open)
    "editor": ["code", "-r"],            「VS Code で開く」が実行するコマンド
    "backupKeep": 20 }                   保存前バックアップの世代数(~/.cache/md-viewer/backup/)

セキュリティ: 127.0.0.1 バインド + Host ヘッダ検査 + Sec-Fetch-Site 検査(他サイトからの読み出し拒否)
+ realpath 後の許可ルート検査 + 拡張子 allowlist(md/markdown/txt/画像/pdf のみ読める、書けるのは
md/markdown/txt のみ)。CORS ヘッダは出さない。
"""

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

VERSION = "2026-08-29.1"
DEFAULT_PORT = 8788
DEFAULT_ROOTS = ["~/repos"]
# 環境変数 MDV_CONFIG / MDV_STATE_DIR で差し替え可(テスト用)
CONFIG_PATH = Path(os.environ.get("MDV_CONFIG") or "~/.config/md-viewer/serve.json").expanduser()
STATE_DIR = Path(os.environ.get("MDV_STATE_DIR") or "~/.cache/md-viewer").expanduser()
PID_FILE = STATE_DIR / "serve.pid"
LOG_FILE = STATE_DIR / "serve.log"
BACKUP_KEEP = 20

# index.html の IGNORE_DIRS と同じ(一覧から除外)
IGNORE_DIRS = {".git", "node_modules", ".venv", ".venv-marker", "__pycache__", ".embeddings"}
MD_EXT = {".md", ".markdown"}
TEXT_EXT = MD_EXT | {".txt"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".avif"}
READ_EXT = TEXT_EXT | IMAGE_EXT | {".pdf"}
LIST_EXT = TEXT_EXT | IMAGE_EXT
LIST_LIMIT = 5000              # /api/list の最大件数(超えたら truncated)
LIST_DEPTH = 8                 # /api/list の最大深さ
INLINE_TEXT_BYTES = 24 << 20   # /api/list?texts=1 で同梱する md 本文の合計上限
MAX_FILE_BYTES = 256 << 20     # /api/file の上限
MAX_SAVE_BYTES = 16 << 20      # /api/save の上限


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


# ---------------------------------------------------------------- config

def load_config_file():
    if not CONFIG_PATH.is_file():
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError) as e:
        sys.exit("設定ファイルを読めません: %s (%s)" % (CONFIG_PATH, e))
    if not isinstance(cfg, dict):
        sys.exit("設定ファイルはオブジェクトである必要があります: %s" % CONFIG_PATH)
    return cfg


class Config:
    def __init__(self, args):
        cfg = load_config_file()
        self.port = int(getattr(args, "port", None) or cfg.get("port") or DEFAULT_PORT)
        roots = list(DEFAULT_ROOTS) + list(cfg.get("roots") or []) + list(getattr(args, "root", None) or [])
        self.roots = []
        for r in roots:
            if not isinstance(r, str):
                sys.exit("roots は文字列の配列で指定してください: %r" % (r,))
            p = os.path.realpath(os.path.expanduser(r))
            if os.path.isdir(p) and p not in self.roots:
                self.roots.append(p)
        if not self.roots:
            sys.exit("許可ルートが 1 つもありません(既定 ~/repos が無い場合は --root か serve.json の roots で指定)")
        self.browser = cfg.get("browser") or None
        editor = cfg.get("editor") or ["code", "-r"]
        if not (isinstance(editor, list) and editor and all(isinstance(x, str) for x in editor)):
            sys.exit("editor は文字列の配列で指定してください")
        self.editor = editor
        self.backup_keep = int(cfg.get("backupKeep", BACKUP_KEEP))
        self.backup_dir = Path(os.path.expanduser(cfg.get("backupDir") or str(STATE_DIR / "backup")))
        # --root で追加した分は open → serve の子プロセスにも引き継ぐ
        self.cli_roots = [os.path.realpath(os.path.expanduser(r)) for r in (getattr(args, "root", None) or [])]


# ---------------------------------------------------------------- path helpers

def under(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def allowed_root_of(cfg, path):
    for r in cfg.roots:
        if under(path, r):
            return r
    return None


def resolve_file(cfg, raw, exts):
    """クエリ/JSON で受けたパス → realpath。許可ルート配下の通常ファイルで拡張子が exts のものだけ通す。"""
    if not raw or not isinstance(raw, str):
        raise ApiError(400, "path がありません")
    if not os.path.isabs(raw):
        raise ApiError(400, "path は絶対パスで指定してください: %s" % raw)
    p = os.path.realpath(raw)
    if allowed_root_of(cfg, p) is None:
        raise ApiError(403, "許可ルートの外です: %s (許可: %s)" % (raw, ", ".join(cfg.roots)))
    ext = os.path.splitext(p)[1].lower()
    if ext not in exts:
        raise ApiError(403, "この種類のファイルは扱えません: %s" % raw)
    if not os.path.isfile(p):
        raise ApiError(404, "ファイルがありません: %s" % raw)
    return p


def resolve_dir(cfg, raw):
    if not raw or not isinstance(raw, str):
        raise ApiError(400, "root がありません")
    if not os.path.isabs(raw):
        raise ApiError(400, "root は絶対パスで指定してください: %s" % raw)
    p = os.path.realpath(raw)
    if allowed_root_of(cfg, p) is None:
        raise ApiError(403, "許可ルートの外です: %s (許可: %s)" % (raw, ", ".join(cfg.roots)))
    if not os.path.isdir(p):
        raise ApiError(404, "ディレクトリがありません: %s" % raw)
    return p


def find_git_root(cfg, path):
    """path から上へ、許可ルートの中で最初に .git を持つディレクトリ。無ければ None。"""
    top = allowed_root_of(cfg, path)
    d = os.path.dirname(path)
    while top and under(d, top):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def list_tree(cfg, root):
    """root 配下の一覧 [(rel, full, stat)]。IGNORE_DIRS を刈り、深さ・件数で打ち切る。
    許可ルートの外を指すシンボリックリンクは載せない(/api/file で 403 になるだけなので)。"""
    out = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS) if depth < LIST_DEPTH else []
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in LIST_EXT:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            if os.path.islink(full) and allowed_root_of(cfg, os.path.realpath(full)) is None:
                continue
            rel = name if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/" + name
            out.append((rel, full, st))
            if len(out) >= LIST_LIMIT:
                truncated = True
                return out, truncated
    return out, truncated


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def decode_text(data):
    return data.decode("utf-8-sig", errors="replace")


def backup_file(cfg, path, data):
    """保存前の内容を ~/.cache/md-viewer/backup/<key>/<timestamp>.md に退避し、古い世代を捨てる。"""
    if cfg.backup_keep <= 0:
        return None
    key = hashlib.sha1(path.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    d = cfg.backup_dir / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "path.txt").write_text(path + "\n", encoding="utf-8")
    ext = os.path.splitext(path)[1] or ".txt"
    dest = d / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ext)
    dest.write_bytes(data)
    gens = sorted(f for f in d.iterdir() if f.name != "path.txt")
    for f in gens[:-cfg.backup_keep]:
        try:
            f.unlink()
        except OSError:
            pass
    return str(dest)


def atomic_write(path, data):
    """同じディレクトリの一時ファイルに書いて fsync → os.replace(途中で落ちても元ファイルは無傷)。"""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".mdv-tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # DrvFS などで未対応でも続行
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- HTTP handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "md-viewer/" + VERSION
    cfg = None           # Config(起動時にセット)
    allowed_hosts = ()   # Host ヘッダ許可リスト
    index_path = Path(os.environ.get("MDV_INDEX") or (Path(__file__).resolve().parent / "index.html"))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (datetime.now().strftime("%m-%d %H:%M:%S"), fmt % args))
        sys.stderr.flush()

    # -- response helpers --
    def _send(self, status, body, ctype, headers=None):
        h = {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
             "X-Content-Type-Options": "nosniff"}
        h.update(headers or {})
        self.send_response(status)
        for k, v in h.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, e):
        if isinstance(e, ApiError):
            body = {"error": str(e)}
            body.update(e.extra)
            self._json(e.status, body)
        else:
            traceback.print_exc()
            self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})

    # -- guards --
    def _guard(self, method):
        host = self.headers.get("Host") or ""
        if host not in self.allowed_hosts:
            raise ApiError(403, "Host ヘッダが不正: %s" % host)
        sfs = self.headers.get("Sec-Fetch-Site")
        if method == "GET":
            # 同一オリジン / アドレスバー入力 / ヘッダを送らないクライアント(curl 等)のみ。他サイトの <img> などは拒否
            if sfs not in (None, "same-origin", "none"):
                raise ApiError(403, "他サイトからのアクセスは拒否します (Sec-Fetch-Site: %s)" % sfs)
        else:
            if sfs != "same-origin":
                raise ApiError(403, "書き込み API は同一オリジンからのみ (Sec-Fetch-Site: %s)" % sfs)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise ApiError(415, "Content-Type は application/json で")

    # -- routing --
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._guard("GET")
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            if u.path in ("/", "/index.html"):
                return self._serve_index()
            if u.path == "/api/status":
                return self._json(200, {"mdv": True, "version": VERSION, "port": self.cfg.port,
                                        "roots": self.cfg.roots, "pid": os.getpid()})
            if u.path == "/api/resolve":
                return self._api_resolve(q)
            if u.path == "/api/list":
                return self._api_list(q)
            if u.path == "/api/file":
                return self._api_file(q)
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            raise ApiError(404, "not found: %s" % u.path)
        except Exception as e:  # noqa: BLE001
            self._error(e)

    def do_POST(self):
        try:
            self._guard("POST")
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_SAVE_BYTES + 4096:
                raise ApiError(413, "リクエストが大きすぎます")
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError as e:
                raise ApiError(400, "JSON が不正: %s" % e)
            if not isinstance(body, dict):
                raise ApiError(400, "JSON オブジェクトで送ってください")
            if u.path == "/api/save":
                return self._api_save(body)
            if u.path == "/api/open-editor":
                return self._api_open_editor(body)
            raise ApiError(404, "not found: %s" % u.path)
        except Exception as e:  # noqa: BLE001
            self._error(e)

    # -- static --
    def _serve_index(self):
        try:
            st = self.index_path.stat()
            data = self.index_path.read_bytes()
        except OSError:
            raise ApiError(404, "index.html が見つかりません: %s" % self.index_path)
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # no-cache(毎回 ETag で確認)。index.html を更新したら次のリロードで反映される
        self._send(200, data, "text/html; charset=utf-8", {"ETag": etag, "Cache-Control": "no-cache"})

    # -- API --
    def _api_resolve(self, q):
        p = resolve_file(self.cfg, q.get("path"), MD_EXT)
        if q.get("root"):
            root = resolve_dir(self.cfg, q.get("root"))
            if not under(p, root):
                raise ApiError(400, "path が root の外です")
        else:
            root = find_git_root(self.cfg, p) or os.path.dirname(p)
        st = os.stat(p)
        self._json(200, {"path": p, "root": root, "rel": os.path.relpath(p, root).replace(os.sep, "/"),
                         "mtime_ns": str(st.st_mtime_ns)})

    def _api_list(self, q):
        root = resolve_dir(self.cfg, q.get("root"))
        want_texts = q.get("texts") == "1"
        files, truncated = list_tree(self.cfg, root)
        out = []
        inline = 0
        for rel, full, st in files:
            item = {"rel": rel, "mtime_ns": str(st.st_mtime_ns), "size": st.st_size}
            if want_texts and os.path.splitext(rel)[1].lower() in MD_EXT and inline + st.st_size <= INLINE_TEXT_BYTES:
                try:
                    with open(full, "rb") as f:
                        item["text"] = decode_text(f.read())
                    inline += st.st_size
                except OSError:
                    pass
            out.append(item)
        self._json(200, {"root": root, "files": out, "truncated": truncated, "limit": LIST_LIMIT})

    def _api_file(self, q):
        p = resolve_file(self.cfg, q.get("path"), READ_EXT)
        st = os.stat(p)
        if st.st_size > MAX_FILE_BYTES:
            raise ApiError(413, "ファイルが大きすぎます: %s" % p)
        with open(p, "rb") as f:
            data = f.read()
        ext = os.path.splitext(p)[1].lower()
        if ext in MD_EXT:
            ctype = "text/markdown; charset=utf-8"
        elif ext == ".txt":
            ctype = "text/plain; charset=utf-8"
        else:
            ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        self._send(200, data, ctype, {
            "X-Mtime-Ns": str(st.st_mtime_ns),
            "X-Sha256": sha256_hex(data),
            # 直接ナビゲートされても SVG 内スクリプト等を動かさない
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    def _api_save(self, body):
        text = body.get("text")
        if not isinstance(text, str):
            raise ApiError(400, "text がありません")
        p = resolve_file(self.cfg, body.get("path"), TEXT_EXT)
        with open(p, "rb") as f:
            old = f.read()
        cur_sha = sha256_hex(old)
        st = os.stat(p)
        if not body.get("force") and body.get("base_sha256") != cur_sha:
            raise ApiError(409, "ディスク上のファイルが変更されています", sha256=cur_sha,
                           mtime_ns=str(st.st_mtime_ns), text=decode_text(old))
        # 元ファイルの BOM / 改行コードを保つ(textarea は CRLF を LF に正規化して返す)
        bom = old.startswith(b"\xef\xbb\xbf")
        crlf = b"\r\n" in old and b"\n" not in old.replace(b"\r\n", b"")
        norm = text.replace("\r\n", "\n")
        if crlf:
            norm = norm.replace("\n", "\r\n")
        data = norm.encode("utf-8")
        if bom:
            data = b"\xef\xbb\xbf" + data
        if len(data) > MAX_SAVE_BYTES:
            raise ApiError(413, "保存内容が大きすぎます")
        backup = None
        if data != old:
            try:
                backup = backup_file(self.cfg, p, old)
            except OSError as e:
                self.log_message("backup failed for %s: %s", p, e)
            atomic_write(p, data)
        st = os.stat(p)
        self._json(200, {"sha256": sha256_hex(data), "mtime_ns": str(st.st_mtime_ns), "bytes": len(data),
                         "changed": data != old, "backup": backup})

    def _api_open_editor(self, body):
        p = resolve_file(self.cfg, body.get("path"), TEXT_EXT)
        cmd = list(self.cfg.editor)
        exe = shutil.which(cmd[0])
        if not exe:
            raise ApiError(500, "エディタが見つかりません: %s (serve.json の editor で指定)" % cmd[0])
        subprocess.Popen([exe] + cmd[1:] + [p], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        self._json(200, {"ok": True, "cmd": cmd + [p]})


# ---------------------------------------------------------------- process control

def probe(port, timeout=0.5):
    """起動済みの md-viewer サーバなら /api/status の dict、それ以外は None。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
            return j if isinstance(j, dict) and j.get("mdv") else None
    except Exception:  # noqa: BLE001
        return None


def port_in_use(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def read_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            if b"serve.py" not in f.read():
                return None
    except OSError:
        return None
    return pid


def stop_server(cfg, quiet=False):
    pid = read_pid()
    st = probe(cfg.port)
    if pid is None and st and st.get("pid"):
        pid = int(st["pid"])
    if pid is None:
        if not quiet:
            print("常駐サーバは動いていません")
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print("停止できません (pid %d): %s" % (pid, e))
        return False
    for _ in range(30):
        if not port_in_use(cfg.port):
            break
        time.sleep(0.1)
    if not quiet:
        print("停止しました (pid %d)" % pid)
    return True


def spawn_server(cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab")
    cmd = [sys.executable, os.path.abspath(__file__), "serve", "--port", str(cfg.port)]
    for r in cfg.cli_roots:
        cmd += ["--root", r]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True, cwd=str(Path.home()))


def ensure_server(cfg):
    """常駐サーバを起動済みにする。版が違えば再起動。戻り値は /api/status。"""
    st = probe(cfg.port)
    if st and st.get("version") != VERSION:
        print("serve.py が更新されているので常駐サーバを再起動します (%s → %s)" % (st.get("version"), VERSION))
        stop_server(cfg, quiet=True)
        st = None
    if st:
        return st
    if port_in_use(cfg.port):
        sys.exit("ポート %d は別のプロセスが使用中です(--port か serve.json の port で変更)" % cfg.port)
    spawn_server(cfg)
    for _ in range(50):
        time.sleep(0.1)
        st = probe(cfg.port)
        if st:
            return st
    sys.exit("サーバを起動できませんでした。ログ: %s" % LOG_FILE)


def open_browser(cfg, url):
    """既定ブラウザで url を開く。WSL では rundll32 (URL のクエリを無傷で渡す。explorer.exe は
    ?path=… を落とし、cmd /c start は & 以降を切る) → powershell → xdg-open の順に探す。"""
    if cfg.browser:
        cmd = [cfg.browser, url]
    elif shutil.which("rundll32.exe"):
        cmd = [shutil.which("rundll32.exe"), "url.dll,FileProtocolHandler", url]
    elif shutil.which("powershell.exe"):
        cmd = [shutil.which("powershell.exe"), "-NoProfile", "-Command", "Start-Process '%s'" % url.replace("'", "''")]
    elif shutil.which("xdg-open"):
        cmd = [shutil.which("xdg-open"), url]
    else:
        print("ブラウザを開けません。次の URL を開いてください: %s" % url)
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError as e:
        print("ブラウザを起動できません (%s): %s" % (cmd[0], e))
        return False


def file_url(cfg, path, root=None):
    url = "http://localhost:%d/?path=%s" % (cfg.port, quote(path, safe="/"))
    if root:
        url += "&root=" + quote(root, safe="/")
    return url


# ---------------------------------------------------------------- commands

def cmd_serve(cfg, args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    Handler.cfg = cfg
    Handler.allowed_hosts = ("localhost:%d" % cfg.port, "127.0.0.1:%d" % cfg.port)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", cfg.port), Handler)
    except OSError as e:
        sys.exit("ポート %d で起動できません: %s" % (cfg.port, e))
    httpd.daemon_threads = True
    PID_FILE.write_text(str(os.getpid()))

    def on_term(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    url = "http://localhost:%d/" % cfg.port
    print("md-viewer %s: %s  (index: %s)" % (VERSION, url, Handler.index_path))
    print("許可ルート: %s" % ", ".join(cfg.roots))
    sys.stdout.flush()
    if getattr(args, "open", False):
        open_browser(cfg, url)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        try:
            if PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass


def cmd_open(cfg, args):
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        sys.exit("ファイルがありません: %s" % path)
    root = os.path.abspath(args.root[0]) if args.root else None
    ensure_server(cfg)
    url = file_url(cfg, path, root)
    # 許可ルート外などはブラウザでも表示されるが、端末にも出しておく
    try:
        q = "http://127.0.0.1:%d/api/resolve?path=%s" % (cfg.port, quote(path, safe=""))
        if root:
            q += "&root=" + quote(root, safe="")
        with urllib.request.urlopen(q, timeout=2) as r:
            r.read()
    except urllib.error.HTTPError as e:
        try:
            print("注意: %s" % json.loads(e.read().decode("utf-8")).get("error"), file=sys.stderr)
        except Exception:  # noqa: BLE001
            print("注意: %s" % e, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    open_browser(cfg, url)
    print(url)


def cmd_status(cfg, args):
    st = probe(cfg.port)
    if not st:
        print("停止中 (port %d)" % cfg.port)
        return 1
    print("起動中: http://localhost:%d/  version %s  pid %s" % (cfg.port, st.get("version"), st.get("pid")))
    print("許可ルート: %s" % ", ".join(st.get("roots") or []))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, help="ポート (既定 %d、serve.json の port でも指定可)" % DEFAULT_PORT)
    common.add_argument("--root", action="append", metavar="DIR",
                        help="許可ルートを追加 (複数可)。open では一覧の範囲 (&root=) にもなる")
    ap = argparse.ArgumentParser(description="md-viewer local server (see the docstring of serve.py)")
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("serve", parents=[common], help="フォアグラウンドで起動 (既定)")
    sp.add_argument("--open", action="store_true", help="起動後にブラウザで開く")
    op = sub.add_parser("open", parents=[common], help="常駐サーバを確保してファイルをブラウザで開く")
    op.add_argument("file")
    sub.add_parser("status", parents=[common], help="常駐サーバの状態")
    sub.add_parser("stop", parents=[common], help="常駐サーバを終了")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (["-h"], ["--help"]):
        print(__doc__)
        ap.print_help()
        return 0
    if not argv or argv[0].startswith("-"):
        argv = ["serve"] + argv  # bare `serve.py [--port N]` = serve
    args = ap.parse_args(argv)
    cfg = Config(args)
    if args.cmd in (None, "serve"):
        return cmd_serve(cfg, args)
    if args.cmd == "open":
        return cmd_open(cfg, args)
    if args.cmd == "status":
        return cmd_status(cfg, args)
    if args.cmd == "stop":
        return 0 if stop_server(cfg) else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
