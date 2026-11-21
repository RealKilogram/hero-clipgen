#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""英雄登场 · 抠像工具（本地网页界面）

用法：双击「抠像工具.bat」，浏览器会自动打开操作界面。
不需要额外安装 GUI 库，只用标准库 + clipgen.py 依赖的那几个包。
"""
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # 项目根目录


def _app_root(base):
    """兼容两种布局：开发目录（app/...）和分发包（resources/app/...）。"""
    packed = os.path.join(base, "resources", "app")
    if os.path.isdir(os.path.join(packed, "assets")):
        return packed
    return base


APP_ROOT = _app_root(ROOT)
CLIPS_DIR = os.path.join(APP_ROOT, "app", "assets", "clips") if APP_ROOT == ROOT \
    else os.path.join(APP_ROOT, "assets", "clips")
# 单独下载本工具（旁边没有播放器）时，素材库不存在，就导出到工具目录下的 out/
if not os.path.isdir(os.path.dirname(CLIPS_DIR)):
    CLIPS_DIR = os.path.join(HERE, "out")
CACHE = os.path.join(HERE, "_cache")
PORT = int(os.environ.get("CLIPGEN_PORT", "17331"))
STARTED = time.strftime("%H:%M:%S")

STATE = {
    "phase": "idle",      # idle | running | done | error
    "pct": 0,
    "msg": "",
    "logs": [],
    "out": "",
    "in_library": False,
    "error": "",
    "frames": 0,
    "total": 0,
}
LOCK = threading.Lock()


def resolve_out_dir(raw, safe_name):
    """把用户填的导出目录规范化。空 = 程序素材库。返回 None 表示无效。"""
    raw = (raw or "").strip().strip('"')
    if not raw:
        return os.path.join(CLIPS_DIR, safe_name)
    # 允许 %USERPROFILE% 之类的环境变量
    raw = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(raw):
        raw = os.path.join(ROOT, raw)
    p = os.path.normpath(raw)
    drive, tail = os.path.splitdrive(p)
    # 拒绝磁盘根目录（例如直接选到 D:\），避免把一堆帧倒进根目录
    if not tail or tail == os.sep:
        return None
    # 如果用户选的是父目录还想再套一层素材名，这里不强行加，尊重用户填的完整路径
    return p


def is_in_library(p):
    try:
        lib = os.path.normcase(os.path.abspath(CLIPS_DIR))
        tgt = os.path.normcase(os.path.abspath(p))
        return tgt == lib or tgt.startswith(lib + os.sep)
    except Exception:
        return False


def list_dir(url_path):
    """网页用的目录浏览：返回当前目录、上级目录和子目录列表。"""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url_path).query)
    raw = (q.get("path", [""])[0] or "").strip()

    if not raw:
        drives = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            d = letter + ":\\"
            if os.path.isdir(d):
                drives.append({"name": d, "path": d})
        return {"path": "", "parent": None, "dirs": drives, "drives": True}

    p = os.path.normpath(raw)
    if not os.path.isdir(p):
        return {"path": p, "parent": None, "dirs": [], "error": "目录不存在"}
    parent = os.path.dirname(p.rstrip("\\/"))
    if parent and parent != p and len(parent) >= 3:
        parent = parent
    else:
        parent = ""
    dirs = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir():
                        dirs.append({"name": e.name, "path": os.path.join(p, e.name)})
                except OSError:
                    continue
    except OSError as e:
        return {"path": p, "parent": parent, "dirs": [], "error": str(e)}
    dirs.sort(key=lambda x: x["name"].lower())
    return {"path": p, "parent": parent, "dirs": dirs, "drives": False}


def pick_folder():
    """调用 Windows 原生文件夹选择框。取消返回 None。

    主路线：起一个 -STA 的 PowerShell 用 WinForms FolderBrowserDialog。
    （ctypes 直接调 SHBrowseForFolderW 在多线程 HTTP 服务里经常弹不出来，
    因为调用线程的 COM 单元状态不可控；独立进程最稳。）
    失败再退回 ctypes。
    """
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description = '选择导出目录';"
        "$d.ShowNewFolderButton = $true;"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {"
        "  [Console]::Out.Write($d.SelectedPath) }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        out = (r.stdout or "").strip()
        if out:
            return out
    except Exception as e:
        sys.stderr.write("powershell picker failed: %r\n" % (e,))
    return _pick_folder_ctypes()


def _pick_folder_ctypes():
    import ctypes
    from ctypes import wintypes

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040
    BIF_EDITBOX = 0x0010

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]

    # 浏览器选择框依赖 COM，服务跑在多线程里，调用线程必须先初始化 STA，
    # 否则 SHBrowseForFolderW 直接返回 0（表现为"点了没反应"）
    hr = 0
    try:
        hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    except Exception:
        hr = -1

    try:
        fore = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        fore = 0

    try:
        for flags in (BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX,
                      BIF_RETURNONLYFSDIRS):
            buf = ctypes.create_unicode_buffer(260)
            bi = BROWSEINFO()
            bi.hwndOwner = fore  # 挂到前台窗口，免得对话框被浏览器挡在后面
            bi.pidlRoot = None
            bi.pszDisplayName = buf
            bi.lpszTitle = "选择导出目录"
            bi.ulFlags = flags
            pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
            if not pidl:
                continue  # 这组参数没弹出来，换旧式对话框再试一次
            try:
                path = ctypes.create_unicode_buffer(520)
                if shell32.SHGetPathFromIDListW(pidl, path):
                    return path.value or None
                return None
            finally:
                try:
                    ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
                except Exception:
                    pass
        return None
    finally:
        if hr in (0, 1):  # S_OK / S_FALSE
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


import shutil


def cleanup_out(out):
    """删除导出目录里本次生成的产物（frames/*.webp、clip.json、空的 sounds/），
    目录空了再删目录。只碰这几个位置，不会误删别人的文件。"""
    if not out or not os.path.isdir(out):
        return 0, False, "目录不存在"
    removed = 0
    frames = os.path.join(out, "frames")
    if os.path.isdir(frames):
        for fn in os.listdir(frames):
            if fn.lower().endswith(".webp"):
                try:
                    os.remove(os.path.join(frames, fn))
                    removed += 1
                except Exception:  # noqa: BLE001
                    pass
        try:
            if not os.listdir(frames):
                os.rmdir(frames)
        except Exception:  # noqa: BLE001
            pass
    cj = os.path.join(out, "clip.json")
    if os.path.isfile(cj):
        try:
            os.remove(cj)
        except Exception:  # noqa: BLE001
            pass
    snd = os.path.join(out, "sounds")
    if os.path.isdir(snd):
        try:
            if not os.listdir(snd):
                os.rmdir(snd)
        except Exception:  # noqa: BLE001
            pass
    try:
        if not os.listdir(out):
            os.rmdir(out)
    except Exception:  # noqa: BLE001
        pass
    return removed, True, ""


def push(line):
    with LOCK:
        STATE["logs"].append(line)
        if len(STATE["logs"]) > 400:
            STATE["logs"] = STATE["logs"][-400:]
        line = line.strip()
        # 解析 "预计 N 帧" 和 "  N 帧  每帧"
        if "预计" in line and "帧" in line:
            try:
                STATE["total"] = int(line.split("预计")[1].split("帧")[0].strip())
            except Exception:
                pass
        if line.startswith("完成"):
            STATE["phase"] = "done"
            STATE["pct"] = 100
        if line and line[0].isdigit() and "帧" in line:
            try:
                n = int(line.split("帧")[0].strip())
                STATE["frames"] = n
                if STATE["total"]:
                    STATE["pct"] = min(99, int(n * 100 / STATE["total"]))
            except Exception:
                pass


CURRENT = {"proc": None, "out": "", "stopped": False}


def run_job(video, out_dir, name, fps, quality):
    in_lib = is_in_library(out_dir)
    CURRENT["stopped"] = False
    CURRENT["out"] = os.path.abspath(out_dir)
    with LOCK:
        STATE.update(phase="running", pct=0, logs=[], out="", error="",
                     frames=0, total=0, in_library=in_lib)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = ""
        if getattr(e, "winerror", None) == 5 or "拒绝访问" in msg or "denied" in msg.lower():
            hint = ("\n常见原因：直接把目录建在盘符根下（如 D:\\）时，Windows 默认只允许"
                    "管理员写入。换成带一级子目录的路径就好，例如 D:\\素材\\角色。")
        with LOCK:
            STATE["phase"] = "error"
            STATE["error"] = "创建导出目录失败：%s%s" % (msg, hint)
        push("创建导出目录失败：" + msg + hint)
        return

    # 可写性预检：失败只警告，不阻断（有些环境禁止建临时文件但能建正式文件）
    try:
        probe = os.path.join(out_dir, ".aemeath_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:  # noqa: BLE001
        push("警告：这个目录可能不可写（%s），继续尝试…" % e)
    push("导出目录：" + os.path.abspath(out_dir))
    push("（进素材库：%s）" % ("是，完成后程序里就能看到" if in_lib else "否，需要手动导入"))
    cmd = [
        sys.executable, os.path.join(HERE, "clipgen.py"),
        "-i", video, "-o", out_dir,
        "--name", name or os.path.splitext(os.path.basename(out_dir))[0],
        "--fps", str(fps), "--quality", str(quality),
    ]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    try:
        proc = subprocess.Popen(
            cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True, encoding="utf-8",
            errors="replace", env=env,
        )
        CURRENT["proc"] = proc
        for line in proc.stdout:
            push(line.rstrip())
            if line.startswith("完成"):
                with LOCK:
                    STATE["out"] = os.path.abspath(out_dir)
        proc.wait()
        with LOCK:
            if CURRENT["stopped"]:
                STATE["phase"] = "stopped"
                STATE["error"] = ""
            elif STATE["phase"] != "done":
                STATE["phase"] = "error" if proc.returncode else "done"
                if proc.returncode:
                    STATE["error"] = "处理失败，返回码 %d（看下面的日志）" % proc.returncode
            if not STATE["out"] and STATE["phase"] == "done":
                STATE["out"] = os.path.abspath(out_dir)
    except Exception as e:  # noqa: BLE001
        with LOCK:
            STATE["phase"] = "error"
            STATE["error"] = str(e)
        push("异常：" + str(e))


def probe():
    """检查运行环境，每项返回 [名称, 是否就绪, 说明, 补救办法]"""
    items = []
    pip_cmd = "pip install numpy opencv-python-headless pillow onnxruntime"
    try:
        import numpy  # noqa: F401
        import cv2  # noqa: F401
        import PIL  # noqa: F401
        import onnxruntime  # noqa: F401
        items.append(["Python 依赖", True, "numpy / opencv / pillow / onnxruntime 已安装", ""])
    except Exception as e:  # noqa: BLE001
        items.append(["Python 依赖", False, "缺少：%s" % e, pip_cmd])
    model = os.path.join(HERE, "rmbg14.onnx")
    has_model = os.path.exists(model)
    items.append([
        "抠像模型", has_model,
        "tools/rmbg14.onnx" if has_model else "缺少 tools/rmbg14.onnx（约 168MB）",
        "" if has_model else
        "https://hf-mirror.com/briaai/RMBG-1.4/resolve/main/onnx/model.onnx  下载后存为 tools/rmbg14.onnx",
    ])
    ff = os.path.join(HERE, "ffmpeg.exe")
    has_ff = os.path.exists(ff) or bool(shutil.which("ffmpeg"))
    items.append([
        "ffmpeg", has_ff,
        "tools/ffmpeg.exe" if os.path.exists(ff) else ("PATH 里的 ffmpeg" if has_ff else "缺少 ffmpeg"),
        "" if has_ff else
        "把 ffmpeg.exe 放进 tools\\ 目录，或装好 ffmpeg 并加入 PATH",
    ])
    return items


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "gui.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/status":
            with LOCK:
                return self._send(200, json.dumps(STATE, ensure_ascii=False))
        if path == "/probe":
            data = list(probe())
            data.append(["本次启动", True, STARTED])
            return self._send(200, json.dumps(data, ensure_ascii=False))

        if path == "/ls":
            return self._send(200, json.dumps(list_dir(self.path), ensure_ascii=False))

        if path == "/favicon.ico":
            return self._send(204, b"")
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        if path == "/upload":
            os.makedirs(CACHE, exist_ok=True)
            fn = self.headers.get("X-Filename", "input.mp4")
            ext = os.path.splitext(fn)[1] or ".mp4"
            dst = os.path.join(CACHE, "upload" + ext)
            left = n
            with open(dst, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            return self._send(200, json.dumps({"path": dst, "size": os.path.getsize(dst)}))
        if path == "/start":
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            missing = [it for it in probe() if it[0] != "本次启动" and not it[1]]
            if missing:
                detail = "；".join("%s（%s）" % (it[0], it[3] or it[2]) for it in missing)
                return self._send(400, json.dumps(
                    {"error": "环境未就绪：%s" % detail}, ensure_ascii=False))
            video = body.get("video") or ""
            if not os.path.exists(video):
                return self._send(400, json.dumps({"error": "视频文件不存在"}))
            name = (body.get("name") or "").strip()
            safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "clip"
            out_dir = resolve_out_dir(body.get("outdir") or "", safe)
            if not out_dir:
                return self._send(400, json.dumps(
                    {"error": "导出目录无效（不能是磁盘根目录）"}, ensure_ascii=False))
            in_lib = is_in_library(out_dir)
            if os.path.isdir(out_dir) and os.listdir(out_dir) and not body.get("force"):
                return self._send(200, json.dumps(
                    {"exists": True, "out": out_dir, "name": safe, "in_library": in_lib},
                    ensure_ascii=False))
            fps = float(body.get("fps") or 30)
            quality = int(body.get("quality") or 88)
            threading.Thread(target=run_job, args=(video, out_dir, name, fps, quality),
                             daemon=True).start()
            return self._send(200, json.dumps(
                {"ok": True, "out": out_dir, "in_library": in_lib}, ensure_ascii=False))
        if path == "/stop":
            try:
                if n:
                    self.rfile.read(n)
            except Exception:  # noqa: BLE001
                pass
            proc = CURRENT.get("proc")
            killed = False
            if proc is not None and proc.poll() is None:
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   capture_output=True, creationflags=0x08000000)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                killed = True
            CURRENT["stopped"] = True
            with LOCK:
                STATE["phase"] = "stopped"
                STATE["error"] = ""
            push("—— 已手动终止 ——")
            return self._send(200, json.dumps({
                "ok": True, "killed": killed,
                "out": CURRENT.get("out") or STATE.get("out") or "",
                "frames": STATE.get("frames", 0),
            }, ensure_ascii=False))
        if path == "/cleanup":
            body = {}
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                pass
            out = body.get("out") or CURRENT.get("out") or STATE.get("out") or ""
            removed, ok, msg = cleanup_out(out)
            push("—— 已清理 %d 张图片 ——" % removed if ok else "—— 清理失败：%s ——" % msg)
            return self._send(200 if ok else 400, json.dumps(
                {"ok": ok, "removed": removed, "out": out, "error": "" if ok else msg},
                ensure_ascii=False))
        if path == "/pickdir":
            try:
                p = pick_folder()
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
            if not p:
                return self._send(200, json.dumps({"cancel": True}))
            return self._send(200, json.dumps({"path": p}, ensure_ascii=False))
        if path == "/open":
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            p = body.get("path") or ""
            if os.path.isdir(p):
                subprocess.Popen(["explorer", os.path.normpath(p)])
            return self._send(200, json.dumps({"ok": True}))
        return self._send(404, json.dumps({"error": "not found"}))


def open_browser(url):
    """尽量把界面推到用户面前，失败也不影响使用（地址已打印在黑窗口里）"""
    try:
        if os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(url)
            return True
    except Exception as e:
        print("自动打开浏览器失败：", e)
    try:
        import webbrowser
        if webbrowser.open(url):
            return True
    except Exception as e:
        print("自动打开浏览器失败：", e)
    try:
        subprocess.Popen('start "" "%s"' % url, shell=True)
        return True
    except Exception:
        return False


def main():
    os.makedirs(CACHE, exist_ok=True)

    srv = None
    port = PORT
    for cand in range(PORT, PORT + 12):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            port = cand
            break
        except OSError:
            continue
    if srv is None:
        print("端口 %d~%d 都被占用了，关掉其他程序再试。" % (PORT, PORT + 11))
        input("按回车退出…")
        return

    url = "http://127.0.0.1:%d/" % port
    print("=" * 52)
    print(" 英雄登场 · 抠像工具")
    print("=" * 52)
    print(" 界面地址：%s" % url)
    print(" 浏览器没自动弹出来的话，手打上面这行地址。")
    print(" 关闭这个窗口即可退出。")
    print()
    for item in probe():
        name, ok, detail = item[0], item[1], item[2]
        fix = item[3] if len(item) > 3 else ""
        print("  [%s] %s  %s" % ("OK" if ok else "!!", name, detail))
        if not ok and fix:
            print("       -> " + fix)
    print()
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(0.9, lambda: open_browser(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
