#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对已有素材包做「透明通道时间中值滤波」，压掉逐帧闪烁。

用法：
  python denoise_clip.py <素材包目录> [--size 5] [--quality 95]

原理：抠像模型每帧独立推理，输出会有轻微跳动，表现为播放时边缘/身体
忽明忽暗。对 alpha 通道做时间轴上的中值滤波，能消掉这些单帧跳变，
代价是极快的动作会稍微变软。
"""
import argparse
import os
import shutil
import sys

import numpy as np
from PIL import Image


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip", help="素材包目录（含 frames/）")
    ap.add_argument("--size", type=int, default=5, help="时间窗帧数，3 或 5，默认 5")
    ap.add_argument("--quality", type=int, default=95, help="重写 webp 的质量，默认 95")
    ap.add_argument("--keep-backup", action="store_true", default=True)
    args = ap.parse_args()

    d = os.path.join(args.clip, "frames")
    if not os.path.isdir(d):
        raise SystemExit("找不到 " + d)

    fs = sorted(f for f in os.listdir(d) if f.lower().endswith(".webp"))
    n = len(fs)
    if n < 3:
        raise SystemExit("帧数太少")

    print("读取 %d 帧 ..." % n)
    alphas = []
    for f in fs:
        im = Image.open(os.path.join(d, f)).convert("RGBA")
        alphas.append(np.asarray(im.getchannel("A")))
    A = np.stack(alphas)
    print("  尺寸", A.shape)

    def pair_diff(X):
        dd = np.abs(X[1:].astype(np.int16) - X[:-1].astype(np.int16)).mean(axis=(1, 2)) / 255.0
        return np.median(dd), dd.max()

    print("滤波前  帧间中位差 %.3f  最大 %.3f" % pair_diff(A))

    half = args.size // 2
    out = A.copy()
    for i in range(half, n - half):
        window = A[i - half:i + half + 1]
        out[i] = np.median(window, axis=0).astype(np.uint8)
    print("滤波后  帧间中位差 %.3f  最大 %.3f" % pair_diff(out))

    if args.keep_backup:
        bak = os.path.join(args.clip, "frames_backup")
        if not os.path.exists(bak):
            print("备份到", bak)
            shutil.copytree(d, bak)

    print("写回 %d 帧 ..." % n)
    for idx, f in enumerate(fs):
        path = os.path.join(d, f)
        im = Image.open(path).convert("RGBA")
        r, g, b, _ = im.split()
        Image.merge("RGBA", (r, g, b, Image.fromarray(out[idx]))).save(
            path, "WEBP", quality=args.quality, method=4)
        if (idx + 1) % 20 == 0:
            print("  %d/%d" % (idx + 1, n))

    print("完成：", args.clip)


if __name__ == "__main__":
    main()
