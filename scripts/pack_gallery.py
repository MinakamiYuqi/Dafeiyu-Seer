#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把群友积累的配置图 / 奶龙图打成「图库包」，供新装的人直接有内容。

用法（在仓库根目录跑）：

    python scripts/pack_gallery.py               # 打两个包
    python scripts/pack_gallery.py configs       # 只打配置图
    python scripts/pack_gallery.py milk          # 只打奶龙图
    python scripts/pack_gallery.py --src <dir>   # 指定源目录（默认读本机实例）

⚠️ 脱敏规则（**别改**）
--------------------------------
配置图和奶龙图的元数据里有个 `uploader` 字段，是**群友QQ号的md5哈希**。
哈希本身不可逆，但它能在群成员之间做关联（同一个人传的图能串起来），
所以对外发布时一律删掉。

`author`（群友昵称）**保留** —— 赛尔号圈子里「这张配置图是谁配的」是荣誉，
而且只有昵称、没有号码。

`uploaders.json`（141 条 **明文 QQ 号 + 群昵称**）**永远不进包**，
这个文件只在本机自己用。

包结构（解压到 plugin_data/astrbot_plugin_seer_info/ 即可）：
    seer_pet_configs.zip
      └── pet_configs/<精灵id>/<n>.jpg + <n>.json
    seer_milk_dragon.zip
      └── milk_dragon/*.png + index.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(REPO_ROOT, "dist")

# 默认源：本机 AstrBot 实例的 plugin_data
DEFAULT_SRC = (
    r"G:\temp\instances\eb74352a-db17-4359-aade-ba98505d2e47"
    r"\core\data\plugin_data\astrbot_plugin_seer_info"
)

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# 这些名字的东西不打包
SKIP_NAMES = {"uploaders.json", ".cache"}
SKIP_DIRS = {".cache", "__pycache__"}


def strip_uploader(obj):
    """递归删掉 uploader 字段（保 author）。"""
    if isinstance(obj, dict):
        return {k: strip_uploader(v) for k, v in obj.items() if k != "uploader"}
    if isinstance(obj, list):
        return [strip_uploader(x) for x in obj]
    return obj


def pack_pet_configs(src: str, stage: str) -> tuple[int, int]:
    """配置图：72 个精灵目录 / 97 张图。返回 (文件数, 字节数)。"""
    root = os.path.join(src, "pet_configs")
    dst_root = os.path.join(stage, "pet_configs")
    n = total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_NAMES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            out = os.path.join(dst_root, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if fn.lower().endswith(".json"):
                try:
                    with open(full, encoding="utf-8") as f:
                        data = json.load(f)
                    data = strip_uploader(data)
                    with open(out, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    total += os.path.getsize(out)
                except Exception:
                    shutil.copy2(full, out)
                    total += os.path.getsize(out)
            elif fn.lower().endswith(IMG_EXT):
                shutil.copy2(full, out)
                total += os.path.getsize(out)
            else:
                continue
            n += 1
    return n, total


def pack_milk_dragon(src: str, stage: str) -> tuple[int, int]:
    """奶龙图：index.json 里的 uploader 也要删。"""
    root = os.path.join(src, "milk_dragon")
    dst_root = os.path.join(stage, "milk_dragon")
    os.makedirs(dst_root, exist_ok=True)
    n = total = 0
    for fn in sorted(os.listdir(root)):
        full = os.path.join(root, fn)
        if not os.path.isfile(full) or fn in SKIP_NAMES:
            continue
        out = os.path.join(dst_root, fn)
        if fn == "index.json":
            with open(full, encoding="utf-8") as f:
                data = json.load(f)
            data = strip_uploader(data)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        elif fn.lower().endswith(IMG_EXT):
            shutil.copy2(full, out)
        else:
            continue
        total += os.path.getsize(out)
        n += 1
    return n, total


def zip_dir(stage: str, out_path: str) -> float:
    """把 stage 目录打成 zip，路径正斜杠。"""
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, dirnames, filenames in os.walk(stage):
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, stage).replace(os.sep, "/")
                z.write(full, rel)
    return os.path.getsize(out_path) / 1048576


def build(kind: str, src: str) -> tuple[str, int, float]:
    os.makedirs(DIST_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gallery-") as stage:
        if kind == "configs":
            n, total = pack_pet_configs(src, stage)
            name = "seer_pet_configs"
        else:
            n, total = pack_milk_dragon(src, stage)
            name = "seer_milk_dragon"
        if n == 0:
            raise SystemExit(f"⚠ {kind} 没找到任何文件（源目录对吗：{src}）")
        # 脱敏自检：绝不能出现 uploaders.json，也不能有明文 QQ 号
        leak = []
        for dirpath, _dirs, files in os.walk(stage):
            for fn in files:
                if fn == "uploaders.json":
                    leak.append(os.path.join(dirpath, fn))
        if leak:
            raise SystemExit(f"❌ 脱敏失败，包里混进了 uploaders.json: {leak}")
        zpath = os.path.join(DIST_DIR, f"{name}.zip")
        mb = zip_dir(stage, zpath)
    return zpath, n, mb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kinds", nargs="*", default=None,
                    help="configs / milk，留空=两个都打")
    ap.add_argument("--src", default=DEFAULT_SRC, help="源 plugin_data 目录")
    args = ap.parse_args()

    kinds = args.kinds or ["configs", "milk"]
    if not os.path.isdir(args.src):
        print(f"❌ 源目录不存在: {args.src}")
        print("   用 --src 指定本机 plugin_data/astrbot_plugin_seer_info 的路径")
        return 1

    print(f"源: {args.src}\n")
    for kind in kinds:
        if kind not in ("configs", "milk"):
            print(f"⚠ 跳过未知类型: {kind}")
            continue
        zpath, n, mb = build(kind, args.src)
        print(f"  ✅ {os.path.basename(zpath)}  {mb:.1f} MB  {n} 个文件")

    print(f"\n输出目录: {DIST_DIR}")
    print("脱敏已做：删掉 uploader 哈希、保留 author 昵称、uploaders.json 不进包。")
    print("下一步：上传到 GitHub Releases（README 里当「可选图库包」）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
