# -*- coding: utf-8 -*-
"""
爱弥斯素材包生成器（命令行版）

把一个视频（或图片序列）抠成人像，输出成程序可直接导入的素材包：

    <输出目录>/
        clip.json
        frames/000.webp ...
        sounds/           （可选，你手动放 mp3 进去即可）

用法：
    python clipgen.py -i 视频.mp4 -o 输出目录 --name 角色名 [--fps 30] [--quality 88]

依赖：numpy、opencv-python、pillow、onnxruntime，以及同目录下的 rmbg14.onnx 和 ffmpeg.exe
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import cv2
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DEFAULT = os.path.join(HERE, "rmbg14.onnx")
BOOST = True
FFMPEG_DEFAULT = os.path.join(HERE, "ffmpeg.exe")


def log(*a):
    print(*a, flush=True)


def find_bin(name, default):
    if default and os.path.exists(default):
        return default
    from shutil import which
    p = which(name)
    if p:
        return p
    raise SystemExit("找不到 " + name + "，请把它放在本文件同目录，或加入 PATH")


def video_info(ffmpeg, path):
    # 用 ffmpeg -i 的输出解析尺寸/时长/帧率
    p = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    txt = p.stdout.decode("utf-8", "ignore")
    info = {"width": 0, "height": 0, "fps": 0.0, "duration": 0.0}
    for line in txt.splitlines():
        line = line.strip()
        if "Duration:" in line:
            try:
                t = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = t.split(":")
                info["duration"] = int(h) * 3600 + int(m) * 60 + float(s)
            except Exception:
                pass
        if "Video:" in line and "x" in line:
            import re
            m = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if m:
                info["width"] = int(m.group(1))
                info["height"] = int(m.group(2))
            m2 = re.search(r"([\d.]+) fps", line)
            if m2:
                info["fps"] = float(m2.group(1))
    return info


def iter_frames(ffmpeg, path, fps, width, height):
    """按目标 fps 解码成 BGR 帧，逐帧 yield"""
    cmd = [
        ffmpeg, "-v", "error", "-i", path,
        "-vf", "fps=%g" % fps,
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=width * height * 3 * 4)
    frame_bytes = width * height * 3
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, np.uint8).reshape(height, width, 3)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()


def load_session(model_path, threads):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])


def clahe_bgr(bgr):
    """局部对比度增强：把偏暗的画面提亮，救回模型看不出的暗部身体（头发、深色衣服）"""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a_, b_ = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=6.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge([l, a_, b_]), cv2.COLOR_LAB2BGR)


def infer_alpha(sess, bgr):
    """RMBG-1.4：1024x1024，/255 - 0.5，输出已是 0..1 的 sigmoid"""
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    x = cv2.resize(rgb, (1024, 1024), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0 - 0.5
    x = np.transpose(x, (2, 0, 1))[None]
    name = sess.get_inputs()[0].name
    out = sess.run(None, {name: x})[0]
    a = out[0, 0] if out.ndim == 4 else out[0]
    return np.clip(a, 0.0, 1.0)


def guided_filter(guide, src, r=10, eps=8e-4):
    """用原图引导，把低分辨率的 alpha 上采样到全分辨率并对齐边缘"""
    g = guide.astype(np.float32) / 255.0
    s = src.astype(np.float32)
    k = (2 * r + 1, 2 * r + 1)
    mean_g = cv2.blur(g, k)
    mean_s = cv2.blur(s, k)
    corr_g = cv2.blur(g * g, k)
    corr_s = cv2.blur(s * s, k)
    corr_gs = cv2.blur(g * s, k)
    var_g = corr_g - mean_g * mean_g
    cov_gs = corr_gs - mean_g * mean_s
    a = cov_gs / (var_g + eps)
    b = mean_s - a * mean_g
    mean_a = cv2.blur(a, k)
    mean_b = cv2.blur(b, k)
    return np.clip(mean_a * g + mean_b, 0.0, 1.0)


def despill(bgr):
    """去掉边缘/半透明区域残留的绿色（绿幕素材常见），把 G 压到 R/B 的最大值"""
    b, g, r = cv2.split(bgr.astype(np.float32))
    mx = np.maximum(b, r)
    excess = np.clip(g - mx, 0, None)
    g = g - excess * 0.85
    return cv2.merge([b, g, r]).astype(np.uint8)


def refine_alpha(bgr, alpha_small, out_w, out_h):
    small = cv2.resize(alpha_small, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    fine = guided_filter(gray, small)
    a = np.clip(0.55 * fine + 0.45 * small, 0.0, 1.0)
    # 轻微提纯：压掉背景残留的低透明度雾
    return np.clip((a - 0.04) / 0.92, 0.0, 1.0)


def main():
    ap = argparse.ArgumentParser(description="把视频抠成人像素材包")
    ap.add_argument("-i", "--input", required=True, help="输入视频文件")
    ap.add_argument("-o", "--output", required=True, help="输出素材包目录")
    ap.add_argument("--name", default="", help="素材显示名（默认用输出目录名）")
    ap.add_argument("--fps", type=float, default=30, help="抽帧帧率，默认 30")
    ap.add_argument("--quality", type=int, default=88, help="WebP 质量，默认 88")
    ap.add_argument("--threads", type=int, default=4, help="推理线程数")
    ap.add_argument("--smooth", type=float, default=0.15,
                    help="时间平滑强度 0~1，越大越稳但会拖影，默认 0.15")
    ap.add_argument("--temporal", type=int, default=5,
                    help="透明通道时间中值窗口（3 或 5），压掉逐帧闪烁；1=关闭。默认 5")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--ffmpeg", default=FFMPEG_DEFAULT)
    ap.add_argument("--boost", default="on", choices=["on", "off"],
                    help="暗部增强双推理：对偏暗的画面再跑一遍提亮版并取较大值，"
                         "能救回模型漏掉的暗色身体部位（耗时翻倍）。默认 on")
    args = ap.parse_args()
    global BOOST
    BOOST = (args.boost != "off")

    ffmpeg = find_bin("ffmpeg", args.ffmpeg)
    model = args.model if os.path.exists(args.model) else os.path.join(HERE, "rmbg14.onnx")
    if not os.path.exists(model):
        raise SystemExit("找不到抠像模型 rmbg14.onnx（应放在本文件同目录）")

    info = video_info(ffmpeg, args.input)
    if not info["width"]:
        raise SystemExit("读不出视频信息，确认文件是视频且 ffmpeg 可用")
    W, H = info["width"], info["height"]
    out_dir = args.output
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    for f in os.listdir(frames_dir):
        if f.lower().endswith(".webp"):
            os.remove(os.path.join(frames_dir, f))

    total_est = int(round(info["duration"] * args.fps)) if info["duration"] else 0
    log("输入：%s  %dx%d  %.1fs  抽帧 %.1ffps  预计 %d 帧"
        % (os.path.basename(args.input), W, H, info["duration"], args.fps, total_est))
    log("模型：", model)

    t_start = time.time()
    sess = load_session(model, args.threads)
    prev = None
    n = 0
    t_last = time.time()

    # 时间中值窗口：把相邻几帧的 alpha 取中值，消掉逐帧抖动（播放时的一闪一闪）
    WIN = 5 if args.temporal >= 3 else 1
    half = WIN // 2
    buf = []

    def emit(bgr_f, a_small):
        nonlocal prev, n, t_last
        a = refine_alpha(bgr_f, a_small, W, H)
        if prev is not None and args.smooth > 0:
            a = (1.0 - args.smooth) * a + args.smooth * prev
        prev = a
        rgb = cv2.cvtColor(despill(bgr_f), cv2.COLOR_BGR2RGB)
        rgba = np.dstack([rgb, (a * 255).round()]).astype(np.uint8)
        Image.fromarray(rgba, "RGBA").save(
            os.path.join(frames_dir, "%03d.webp" % n), "WEBP", quality=args.quality, method=4)
        n += 1
        if n % 5 == 0 or n == 1:
            now = time.time()
            per = (now - t_last) / (5 if n > 1 else 1)
            t_last = now
            eta = per * max(0, (total_est - n)) if total_est else 0
            log("  %d 帧  每帧 %.2fs  预计还需 %.0f 分" % (n, per, eta / 60.0))

    for bgr in iter_frames(ffmpeg, args.input, args.fps, W, H):
        a = infer_alpha(sess, bgr)
        if BOOST:
            a = np.maximum(a, infer_alpha(sess, clahe_bgr(bgr)))
        if WIN <= 1:
            emit(bgr, a)
            continue
        buf.append((bgr, a))
        if len(buf) >= WIN:
            mid = buf[half]
            med = np.median(np.stack([b[1] for b in buf]), axis=0).astype(np.float32)
            emit(mid[0], med)
            buf.pop(0)

    while buf:
        mid = buf[len(buf) // 2]
        med = np.median(np.stack([b[1] for b in buf]), axis=0).astype(np.float32)
        emit(mid[0], med)
        buf.pop(0)

    if n == 0:
        raise SystemExit("没有解出任何帧")

    meta = {
        "name": args.name or os.path.basename(os.path.abspath(out_dir)),
        "count": n,
        "fps": args.fps,
        "width": W,
        "height": H,
        "sounds": []
    }
    with open(os.path.join(out_dir, "clip.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.join(out_dir, "sounds"), exist_ok=True)

    secs = n / args.fps
    if secs > 5:
        log("")
        log("提示：这段素材 %.1f 秒，超过 5 秒。导入后播放期间热键会被占住，" % secs)
        log("      播完才能再触发一次。")
    log("")
    log("完成：%d 帧  %.1f 秒  用时 %.1f 分" % (n, secs, (time.time() - t_start) / 60.0))
    log("素材包：", os.path.abspath(out_dir))
    log("下一步：打开程序 → 右键托盘 → 设置热键… → 导入素材 → 选这个文件夹")


if __name__ == "__main__":
    main()
