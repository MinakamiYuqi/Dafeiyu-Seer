#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 plugins/ 下的两个插件打成可安装的 zip，输出到 dist/。

用法（在仓库根目录跑）：

    python scripts/pack_plugins.py              # 打两个
    python scripts/pack_plugins.py expert       # 只打 seer_expert
    python scripts/pack_plugins.py --list       # 看会打哪些

为什么要这个脚本
----------------
AstrBot 装插件要求 **zip 根目录就是插件内容**（metadata.yaml / main.py 在最外层），
而本仓库是「一仓两插件 + README + CLI」，插件在 `plugins/` 子目录。
直接把仓库压成 zip 会被 AstrBot 拒掉（"未在仓库根目录找到 metadata.yaml"）。

所以这里逐插件打包，并做三件事：
  1. 把插件目录**内容**放到 zip 根（不带 `plugins/插件名/` 前缀）
  2. 路径一律 **正斜杠**（Linux 解压安全；Windows 的 `Compress-Archive` 会出错）
  3. 排除 `__pycache__` / `tests` / `*.bak-*` / `*.pyc`

打完的 zip 上传到 GitHub Releases 就行，不用进 git（`dist/` 已在 .gitignore）。
"""
from __future__ import annotations

import os
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugins")
DIST_DIR = os.path.join(REPO_ROOT, "dist")

# 不进 zip 的目录 / 后缀
SKIP_DIRS = {"__pycache__", "tests", ".pytest_cache", "cache", "_unused"}
SKIP_SUFFIX = (".pyc", ".pyo", ".log", ".tmp")
SKIP_INFIX = (".bak-", ".bak", ".orig", ".rej")

# 别名 → 目录名（方便命令行只打一个）
ALIASES = {
    "expert": "astrbot_plugin_seer_expert",
    "info": "astrbot_plugin_seer_info",
}


def discover() -> list[str]:
    """列出 plugins/ 下所有「看起来像插件」的目录。"""
    if not os.path.isdir(PLUGIN_DIR):
        return []
    out = []
    for name in sorted(os.listdir(PLUGIN_DIR)):
        p = os.path.join(PLUGIN_DIR, name)
        if not os.path.isdir(p):
            continue
        # 认插件的最低标准：有 metadata.yaml 或有 main.py
        if os.path.isfile(os.path.join(p, "metadata.yaml")) or os.path.isfile(
            os.path.join(p, "main.py")
        ):
            out.append(name)
    return out


def skip_file(name: str) -> bool:
    if name.endswith(SKIP_SUFFIX):
        return True
    return any(x in name for x in SKIP_INFIX)


def build(pname: str) -> tuple[str, int, float]:
    """打一个插件，返回 (zip 路径, 文件数, MB)。"""
    src = os.path.join(PLUGIN_DIR, pname)
    if not os.path.isdir(src):
        raise SystemExit(f"插件目录不存在: {src}")
    if not os.path.isfile(os.path.join(src, "metadata.yaml")):
        print(f"  ⚠ {pname} 没有 metadata.yaml —— AstrBot 装不上，还是打进去了")

    os.makedirs(DIST_DIR, exist_ok=True)
    zpath = os.path.join(DIST_DIR, f"{pname}.zip")
    if os.path.exists(zpath):
        os.remove(zpath)

    count = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in sorted(files):
                if skip_file(f):
                    continue
                full = os.path.join(root, f)
                # 关键：rel 一定是正斜杠，且不带顶层目录名
                rel = os.path.relpath(full, src).replace(os.sep, "/")
                z.write(full, rel)
                count += 1
    return zpath, count, os.path.getsize(zpath) / 1048576


def verify(zpath: str, pname: str) -> bool:
    """确认 zip 结构符合 AstrBot 要求。"""
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    ok_meta = "metadata.yaml" in names
    ok_main = "main.py" in names
    ok_slash = all("\\" not in n for n in names)
    ok_flat = not any(n.startswith(f"{pname}/") for n in names)
    marks = "".join(
        "✅" if x else "❌" for x in (ok_meta, ok_main, ok_slash, ok_flat)
    )
    print(f"    结构检查 {marks}  (metadata / main / 正斜杠 / 无顶层目录)")
    return all((ok_meta, ok_main, ok_slash, ok_flat))


def main(argv: list[str]) -> int:
    if "--list" in argv or "-l" in argv:
        print("plugins/ 下发现的插件：")
        for name in discover():
            print(f"  {name}")
        return 0

    wanted: list[str] = []
    for a in argv:
        if a.startswith("-"):
            continue
        target = ALIASES.get(a.lower(), a)
        if not os.path.isdir(os.path.join(PLUGIN_DIR, target)):
            print(f"⚠ 跳过未知插件: {a}")
            continue
        wanted.append(target)
    if not wanted:
        wanted = discover()
    if not wanted:
        print("没有找到任何插件（plugins/ 是空的？）")
        return 1

    print(f"=== 打包 ({len(wanted)} 个) ===")
    results = []
    for pname in wanted:
        zpath, count, mb = build(pname)
        results.append((zpath, pname, count, mb))
        print(f"  ✅ {os.path.basename(zpath)}  {mb:.2f} MB  {count} 项")

    print("\n=== 验证 ===")
    all_ok = True
    for zpath, pname, count, mb in results:
        print(f"  {os.path.basename(zpath)}")
        if not verify(zpath, pname):
            all_ok = False

    print(f"\n输出目录: {DIST_DIR}")
    print("下一步：把 dist/ 里的 zip 上传到 GitHub Releases。")
    if not all_ok:
        print("⚠ 有 zip 结构不对，别急着发。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
