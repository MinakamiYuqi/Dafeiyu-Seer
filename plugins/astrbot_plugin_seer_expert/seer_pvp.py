# -*- coding: utf-8 -*-
"""赛尔号游戏内数据查询 —— bot 侧两个独立工具。

拆成两个，让 bot 按语义自己选：
    · query_pvp_async(米米号)     → 只查巅峰战绩（cmdId 40002）
    · query_roster_async(米米号)  → 只查 12 只阵容 + 渲染图（cmdId 41635）

两者各自登录一次，互不依赖。**不要合并调用** —— 用户只问胜率时不该拉阵容图。

协议来源
--------
· 40002 USER_FOREVER_VALUE：黑盒抓包实测（_re/无头客户端协议-20261106.md）
· 41635 GET_USER_PET_INFO_BY_ID：反编译 GameLogic.dll（_re/lineup_followup/）

⚠️ 服务账号
  必须专用小号，同一账号只能在线一个，别和自己玩的号共用。
  凭据从 SEER_ACCOUNT / SEER_PASSWORD 环境变量读，不落盘不打日志。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import struct
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# headless.py（无头客户端）的候选位置，按顺序取第一个真有的。
# 【2026-11-06 踩过】原来只写 _HERE.parent/_re/xm —— 在工作区跑 CLI 没事，
# 因为插件目录旁边就是 _re；但部署到运行版以后，插件在
# ...\core\data\plugins\ 底下，那儿根本没有 _re，import 直接炸
# （报错还被 bot 说成「出图模块崩了」）。
_XM_CANDIDATES = [
    _HERE / "_xm",                        # ① 插件自带（随插件一起复制，运行版走这条）
    _HERE.parent / "_re" / "xm",          # ② 开发工作区
    Path(r"G:\dsh workspace\_re\xm"),     # ③ 工作区绝对路径兜底
]



def _find_astrbot_instance() -> Path:
    """定位 AstrBot 实例根目录。

    优先级：环境变量 ASTRBOT_INSTANCE / ASTRBOT_ROOT → 从本文件位置向上找。
    找不到时明确报错，而不是悄悄用一条只对某台机器有效的绝对路径。
    """
    for var in ("ASTRBOT_INSTANCE", "ASTRBOT_ROOT"):
        env = os.environ.get(var)
        if env and Path(env).is_dir():
            return Path(env)
    cur = Path(__file__).resolve().parent
    for _ in range(6):
        if (cur / "core" / "data" / "plugin_data").is_dir():
            return cur
        cur = cur.parent
    raise RuntimeError(
        "找不到 AstrBot 实例目录：请设置环境变量 ASTRBOT_INSTANCE 指向实例根路径"
    )

def _find_xm() -> Path | None:
    env = os.environ.get("SEER_XM_DIR")
    if env and (Path(env) / "headless.py").is_file():
        return Path(env)
    for p in _XM_CANDIDATES:
        if (p / "headless.py").is_file():
            return p
    return None


_XM = _find_xm()
if _XM and str(_XM) not in sys.path:
    sys.path.insert(0, str(_XM))


def _import_headless():
    """导入无头客户端，找不到时给出人话报错 + 自愈 login_tail.txt。"""
    try:
        import headless as h
    except ImportError as exc:
        tried = "\n  ".join(str(p) for p in _XM_CANDIDATES)
        raise RuntimeError(
            "找不到 headless.py（无头客户端），查过这些位置：\n  "
            + tried
            + "\n修法：把 _re/xm/headless.py 复制到上面第一个位置，"
              "或设环境变量 SEER_XM_DIR 指向它所在目录。"
        ) from exc

    # headless 把 login_tail.txt 硬编码在 Temp 下，Temp 被清就会哑火。
    # 插件目录里备一份，缺了自动补回去。
    tail = Path(getattr(h, "TAIL_PATH", "") or "")
    if tail.name and not tail.is_file() and _XM:
        src = _XM / "login_tail.txt"
        if src.is_file():
            tail.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, tail)
    return h


SEERAPI = _find_astrbot_instance() / "core" / "data" / "plugin_data" / "astrbot_plugin_seer_info" / "seerapi.sqlite"

# ── 常量 ─────────────────────────────────────────────────
PVP_FIELDS = {
    "竞技": {"当前": 124801, "最高": 124802, "胜场": 124804, "总场": 124805},
    "狂野": {"当前": 124791, "最高": 124792, "胜场": 124793, "总场": 124794},
    "专家": {"当前": 129441, "最高": 129443, "胜场": 129446, "总场": 129447},
    "大师": {"当前": 408302, "最高": 408303, "胜场": 408306, "总场": 408307},
}
RANK_NAMES = {0: "学徒", 1: "猛将", 2: "天骄", 3: "王者", 4: "圣皇"}
PACKED_MODES = {"竞技", "狂野", "大师"}

ROSTER_FIELDS = (
    "catchTime", "useFlag", "id", "level", "dv", "nature",
    "hp", "maxHp", "maxhp_adj", "atk", "atk_adj", "sp_atk", "spatk_adj",
    "def", "def_adj", "sp_def", "spdef_adj", "spd", "spd_adj",
)
FLAG_ATTACK = (1, 2)
FLAG_STANDBY = 7


def rank_display(rk: int, star: int) -> str:
    """圣皇满 100 星显示「宇宙圣皇」，纯装饰。"""
    name = RANK_NAMES.get(rk, f"段位{rk}")
    if rk == 4 and star >= 100:
        name = "宇宙圣皇"
    return f"{name} {star}星"


def _creds(account: str | None, password: str | None) -> tuple[str, str]:
    a = account or os.environ.get("SEER_ACCOUNT")
    p = password or os.environ.get("SEER_PASSWORD")
    if not a or not p:
        raise RuntimeError("缺服务账号：设 SEER_ACCOUNT / SEER_PASSWORD")
    return a, p


def _u32s(b: bytes) -> list[int]:
    return [struct.unpack(">I", b[i:i + 4])[0] for i in range(0, len(b) - len(b) % 4, 4)]


class _Reader:
    """跨 timeout 保留半包。"""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()

    def response(self, wanted: int, timeout: float = 8.0) -> bytes:
        end = time.monotonic() + timeout
        while True:
            while len(self.buf) >= 4:
                n = struct.unpack_from(">I", self.buf)[0]
                if not 17 <= n <= 2_000_000:
                    raise ValueError(f"帧长度非法 {n}")
                if len(self.buf) < n:
                    break
                frame = bytes(self.buf[:n])
                del self.buf[:n]
                cmd = struct.unpack_from(">I", frame, 5)[0]
                if cmd == wanted:
                    return frame[17:]
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"等 {wanted} 超时")
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise TimeoutError(f"等 {wanted} 超时")
            if not chunk:
                raise ConnectionError("服务器断开")
            self.buf.extend(chunk)


def _parse_pvp_value(mode: str, kind: str, raw: int) -> dict:
    if kind in ("胜场", "总场"):
        return {"raw": raw, "text": str(raw)}
    if mode == "专家":
        return {"raw": raw, "text": f"{raw}分"}
    rk, star = raw & 0xFFFF, (raw >> 16) & 0xFFFF
    return {"raw": raw, "rank": rk, "star": star, "text": rank_display(rk, star)}


def _parse_roster(body: bytes) -> dict:
    if len(body) < 8:
        raise ValueError("41635 响应缺头")
    count, header2 = struct.unpack_from(">II", body)
    if count > 1024 or len(body) != 8 + 170 * count:
        raise ValueError(f"41635 长度异常: count={count}, bytes={len(body)}")
    pets = []
    for i in range(count):
        off = 8 + i * 170
        p = dict(zip(ROSTER_FIELDS, struct.unpack_from(">19I", body, off)))
        p["evs"] = list(body[off + 76:off + 82])
        sc = struct.unpack_from(">I", body, off + 82)[0]
        if sc > 5:
            raise ValueError(f"技能数异常 {sc}")
        p["skills"] = [
            dict(zip(("id", "pp"), struct.unpack_from(">II", body, off + 86 + j * 8)))
            for j in range(sc)
        ]
        p["activatedSpecialMoves"] = list(struct.unpack_from(">6I", body, off + 126))
        p["mintmarks"] = list(struct.unpack_from(">3I", body, off + 150))
        p["commonSlotActivated"], p["skinId"] = struct.unpack_from(">II", body, off + 162)
        p["isStarter"] = p["useFlag"] == 2
        # 血量：hp 是当前值（含加成），实际上限 = maxHp + maxhp_adj
        hp_max = p["maxHp"] + p["maxhp_adj"]
        p["hpMax"] = hp_max if hp_max > 0 else p["hp"]
        pets.append(p)
    return {
        "count": count,
        "header2": header2,
        "pets": pets,
        "attack": sorted([p for p in pets if p["useFlag"] in FLAG_ATTACK],
                         key=lambda x: (0 if x["useFlag"] == 2 else 1, x["catchTime"])),
        "standby": sorted([p for p in pets if p["useFlag"] == FLAG_STANDBY],
                          key=lambda x: x["catchTime"]),
        "other": [p for p in pets if p["useFlag"] not in (1, 2, 7)],
    }


# ══════════════════════════════════════════════════════════
# 工具 1：查战绩
# ══════════════════════════════════════════════════════════
def query_pvp_sync(target: int, account: str | None = None,
                   password: str | None = None) -> dict:
    """只查巅峰战绩（16 个字段）。

    返回:
        {"target": 米米号, "queriedAt": ts,
         "pvp": {"竞技": {...}, "狂野": {...}, "专家": {...}, "大师": {...}},
         "error": None}
    """
    account, password = _creds(account, password)
    target = int(target)
    if not 0 < target <= 0xFFFFFFFF:
        raise ValueError("目标米米号必须是无符号 32 位")

    h = _import_headless()

    out: dict = {"target": target, "queriedAt": time.time(), "pvp": {}, "error": None}
    sock = None
    try:
        sock, ser, uid = h.login(account, password)
        reader = _Reader(sock)
        for mode, kinds in PVP_FIELDS.items():
            block: dict = {}
            for kind, fid in kinds.items():
                body = struct.pack(">I", target) + struct.pack(">I", fid)
                sock.sendall(h.pack(40002, uid, body, ser))
                try:
                    u = _u32s(reader.response(40002, timeout=6.0))
                    if u:
                        block[kind] = _parse_pvp_value(mode, kind, u[0])
                except (TimeoutError, ConnectionError):
                    continue
            win = block.get("胜场", {}).get("raw")
            total = block.get("总场", {}).get("raw")
            if win is not None and total:
                block["胜率"] = {"win": win, "total": total,
                                 "text": f"{win}/{total}={win / total * 100:.3f}%"}
            elif total == 0:
                block["胜率"] = {"win": 0, "total": 0, "text": "当前赛季未参赛"}
            out["pvp"][mode] = block
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return out


async def query_pvp_async(target: int, account: str | None = None,
                          password: str | None = None) -> dict:
    return await asyncio.to_thread(query_pvp_sync, target, account, password)


def format_pvp(data: dict) -> str:
    """战绩文字（不看图也能读）。"""
    if data.get("error"):
        return f"查询 {data.get('target')} 战绩失败：{data['error']}"
    lines = [f"米米号 {data['target']} 巅峰战绩"]
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("queriedAt") or time.time()))
    lines.append(f"获取时间 {ts}")
    for mode in ("竞技", "狂野", "专家", "大师"):
        b = (data.get("pvp") or {}).get(mode) or {}
        if not b.get("当前") and not b.get("总场"):
            continue
        cur = (b.get("当前") or {}).get("text", "-")
        top = (b.get("最高") or {}).get("text", "-")
        rate = (b.get("胜率") or {}).get("text", "-")
        lines.append(f"{mode}：{cur}｜历史 {top}｜{rate}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 工具 2：查阵容
# ══════════════════════════════════════════════════════════
def query_roster_sync(target: int, account: str | None = None,
                      password: str | None = None, *,
                      want_image: bool = True,
                      image_dir: str | None = None) -> dict:
    """只查 12 只阵容（含渲染图）。

    返回:
        {"target": 米米号, "queriedAt": ts,
         "roster": {"count", "attack": [...], "standby": [...], "pets": [...]},
         "image": 路径 or None, "error": None}
    """
    account, password = _creds(account, password)
    target = int(target)
    if not 0 < target <= 0xFFFFFFFF:
        raise ValueError("目标米米号必须是无符号 32 位")

    h = _import_headless()

    out: dict = {"target": target, "queriedAt": time.time(),
                 "roster": {}, "image": None, "error": None}
    sock = None
    try:
        sock, ser, uid = h.login(account, password)
        reader = _Reader(sock)
        sock.sendall(h.pack(41635, uid, struct.pack(">I", target), ser))
        out["roster"] = _parse_roster(reader.response(41635, timeout=8.0))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    if want_image and out["roster"].get("pets"):
        try:
            out["image"] = _render(target, out["roster"], image_dir)
        except Exception as e:
            out.setdefault("warnings", []).append(f"渲染失败: {e}")
    return out


def _lookup_factory(roster: dict):
    import sqlite3

    seer = sqlite3.connect("file:" + str(SEERAPI).replace("\\", "/") + "?mode=ro", uri=True)
    skin_by_pet = {}
    for p in roster.get("pets") or []:
        sid = int(p.get("skinId") or 0)
        if sid and sid < 100000:
            skin_by_pet[int(p["id"])] = sid

    def lookup(pid: int) -> dict:
        r = seer.execute("select name, resource_id from pet where id=?", (pid,)).fetchone()
        if not r:
            return {}
        out = {"name": r[0], "resource_id": r[1], "skin": None}
        sid = skin_by_pet.get(pid)
        if sid:
            sr = seer.execute(
                "select name, resource_id from pet_skin where id=?", (sid,)
            ).fetchone()
            if sr and sr[1]:
                out["skin"] = {"resource_id": sr[1], "name": sr[0]}
                # 皮肤头像缺失时的备选链（按优先级）：
                #  ① 同名皮肤在别的机体上做过头像
                #     （星光艾欧「星际领航员」1400840 缺，老机体 3437 的 1400812 有）
                #  ② 同名独立精灵
                #     （「阿娅」是精灵 3399、「艾丽娅」是精灵 3403，皮肤资源缺但精灵头像在）
                alts: list[int] = []
                try:
                    for row in seer.execute(
                        "select resource_id from pet_skin "
                        "where name=? and pet_id!=? and resource_id is not null "
                        "order by pet_id",
                        (sr[0], pid),
                    ):
                        if row[0] and int(row[0]) not in alts and int(row[0]) != sr[1]:
                            alts.append(int(row[0]))
                except Exception:
                    pass
                try:
                    ap = seer.execute(
                        "select resource_id from pet where name=?", (sr[0],)
                    ).fetchone()
                    if ap and ap[0] and int(ap[0]) not in alts and int(ap[0]) != sr[1]:
                        alts.append(int(ap[0]))
                except Exception:
                    pass
                if alts:
                    out["skin"]["alt_resource_ids"] = alts
        return out

    return lookup, seer


def _prune_cache(out_dir: str, keep: int = 60) -> None:
    """只留最近 keep 张阵容图。每张 1.5MB，不清理几个月就是几个 G。"""
    try:
        files = sorted(
            Path(out_dir).glob("roster_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:      # 正被发送占用，下一轮再删
                pass
    except Exception:            # noqa: BLE001
        pass


def _render(target: int, roster: dict, image_dir: str | None) -> str:
    from .render_roster_card import render_roster_to_file

    lookup, seer = _lookup_factory(roster)
    try:
        payload = {"target": target, "queriedAt": time.time(), "pets": roster.get("pets") or []}
        out_dir = image_dir or os.path.join(str(_HERE), "cache", "roster")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"roster_{target}_{int(time.time())}.png")
        asyncio.run(render_roster_to_file(payload, lookup=lookup, out_path=out))
        _prune_cache(out_dir)
        return out
    finally:
        seer.close()


async def query_roster_async(target: int, account: str | None = None,
                             password: str | None = None, *,
                             want_image: bool = True,
                             image_dir: str | None = None) -> dict:
    return await asyncio.to_thread(
        query_roster_sync, target, account, password,
        want_image=want_image, image_dir=image_dir,
    )


def format_roster(data: dict) -> str:
    """阵容文字（配图用的简短说明）。"""
    if data.get("error"):
        return f"查询 {data.get('target')} 阵容失败：{data['error']}"
    r = data.get("roster") or {}
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("queriedAt") or time.time()))
    return (f"米米号 {data['target']} 阵容\n"
            f"获取时间 {ts}\n"
            f"共 {r.get('count', 0)} 只（出战 {len(r.get('attack') or [])} / "
            f"待命 {len(r.get('standby') or [])}）")


# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法:\n  python -m astrbot_plugin_seer_expert.seer_pvp pvp 米米号\n"
              "  python -m astrbot_plugin_seer_expert.seer_pvp roster 米米号")
        sys.exit(1)
    act, tgt = sys.argv[1], int(sys.argv[2])
    if act == "pvp":
        print(format_pvp(query_pvp_sync(tgt)))
    elif act == "roster":
        d = query_roster_sync(tgt)
        print(format_roster(d))
        if d.get("image"):
            print(f"\n图: {d['image']}")
    else:
        print(f"未知动作 {act}")
