"""查米米号信息 —— 在线状态 + 基础资料。

三个命令配合，一次登录全拿到（2026-11-19 打通并逐项对过截图）：

    2051 GET_USER_INFO    168 字节  → 昵称/VIP/NONO/登录时间/最后离线/地图
    2052 GET_MORE_INFO    642 字节  → 注册时间（offset 20 的 u32）
    2157 SEE_ONLINE       length=0 → 离线；≥1 → 在线 + server_id

★ 时间戳都是**标准 Unix 秒**（实测三个都对上截图）：
    login_time         最后登录
    last_offline_time  最后离线（协议里字段名是 ollast）
    reg_time           注册时间

★ body 规则（三条命令都不一样，别混）：
    2051: struct.pack(">I",  target)          ← 4 字节
    2052: struct.pack(">I",  target)          ← 4 字节
    2157: struct.pack(">II", 1, target)       ← 8 字节，**开头的 1 不能少**
    自己的 uid 一律**不进 body**（走 13 字节包头）——多塞会直接断连
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime

# ── 2052 里 reg_time 的位置（实测 offset 20）──
REG_TIME_OFFSET = 20


@dataclass
class UserProfile:
    """一个米米号的全部可见信息。"""
    user_id: int = 0
    online: bool = False
    server_id: int = 0

    # 2051 给的
    nick: str = ""
    vip: int = 0
    vip_level: int = 0
    is_extreme_nono: bool = False
    head_id: int = 0            # 头像框 ID（截图里的「薇的注视(235)」）
    head_frame_id: int = 0      # 头像 ID（截图里的「S9赛季圣皇(206)」）
    map_type: int = 0
    map_id: int = 0
    team_id: int = 0
    login_time: int = 0
    last_offline_time: int = 0

    # 2052 给的
    reg_time: int = 0

    errors: list[str] = field(default_factory=list)

    @staticmethod
    def _fmt(ts: int) -> str:
        if not ts:
            return "-"
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError):
            return "-"


def _parse_2051(data: bytes, prof: UserProfile) -> int:
    """按 IronsBot packets/user.py 的 UserInfo 字段顺序解析，返回消耗字节数。"""
    pos = 0

    def u32() -> int:
        nonlocal pos
        v = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        return v

    def u8() -> int:
        nonlocal pos
        v = data[pos]
        pos += 1
        return v

    prof.user_id = u32() or prof.user_id
    raw_nick = data[pos:pos + 16]
    pos += 16
    prof.nick = raw_nick.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    u32()                       # color
    u32()                       # texture
    prof.vip = u32()
    vip_flags = u8()
    prof.is_extreme_nono = bool((vip_flags >> 1) & 1) and prof.vip in (1, 3)
    u32()                       # status
    prof.map_type = u32()
    prof.map_id = u32()
    u32()                       # is_can_be_teacher
    u32()                       # teacher_id
    u32()                       # student_id
    u32()                       # graduation_count
    prof.vip_level = u32()
    prof.team_id = u32()
    u32()                       # team_is_show
    n_clothes = u32()
    pos += 4 * n_clothes            # clothes
    pos += 4 * n_clothes            # clothes_level
    u32()                       # fight_arena_point
    u8()                        # fire_buff
    prof.login_time = u32()
    prof.last_offline_time = u32()
    u8()                        # is_friend
    u8()                        # is_black
    prof.head_id = u32()
    prof.head_frame_id = u32()
    u32()                       # nick_bg
    # 剩下的 decorate_list 不要了
    return pos


def _query_all(target: int, account: str | None = None,
               password: str | None = None) -> UserProfile:
    """一次登录，跑完三条命令。"""
    from .seer_pvp import _import_headless, _creds, _Reader

    prof = UserProfile(user_id=target)
    acc, pwd = _creds(account, password)
    h = _import_headless()
    sock, ser, uid = h.login(acc, pwd)
    reader = _Reader(sock)
    try:
        # ── 2051 基础资料 ──
        try:
            sock.sendall(h.pack(2051, uid, struct.pack(">I", target), ser))
            raw = reader.response(2051, timeout=6.0)
            if raw:
                _parse_2051(raw, prof)
            else:
                prof.errors.append("2051 空回包")
        except Exception as e:      # noqa: BLE001
            prof.errors.append(f"2051 {type(e).__name__}")

        # ── 2052 注册时间 ──
        try:
            sock.sendall(h.pack(2052, uid, struct.pack(">I", target), ser))
            raw2 = reader.response(2052, timeout=6.0)
            if raw2 and len(raw2) >= REG_TIME_OFFSET + 4:
                # 先验一下前 4 字节是不是目标号，确认包没错位
                head_id = struct.unpack_from(">I", raw2, 0)[0]
                if head_id == target:
                    prof.reg_time = struct.unpack_from(">I", raw2, REG_TIME_OFFSET)[0]
                else:
                    prof.errors.append("2052 首字段不是目标号，跳过 reg_time")
            else:
                prof.errors.append("2052 空回包")
        except Exception as e:      # noqa: BLE001
            prof.errors.append(f"2052 {type(e).__name__}")

        # ── 2157 在线状态 ──
        try:
            sock.sendall(h.pack(2157, uid, struct.pack(">II", 1, target), ser))
            raw3 = reader.response(2157, timeout=6.0)
            if len(raw3) >= 4:
                n = struct.unpack_from(">I", raw3, 0)[0]
                if n >= 1 and len(raw3) >= 4 + 16:
                    _, sid, mtype, mid = struct.unpack_from(">IIII", raw3, 4)
                    prof.online = True
                    prof.server_id = sid
                    # 2157 也能给地图，2051 给的是 0 时用它补
                    if not prof.map_type and mtype:
                        prof.map_type = mtype
                    if not prof.map_id and mid:
                        prof.map_id = mid
        except Exception as e:      # noqa: BLE001
            prof.errors.append(f"2157 {type(e).__name__}")
    finally:
        try:
            sock.close()
        except Exception:      # noqa: BLE001
            pass
    return prof


def query_online(target: int, account: str | None = None,
                 password: str | None = None) -> UserProfile:
    return _query_all(target, account, password)


async def query_online_async(target: int, account: str | None = None,
                             password: str | None = None) -> UserProfile:
    import asyncio
    return await asyncio.to_thread(_query_all, target, account, password)


def format_online(p: UserProfile) -> str:
    """给 bot 看的文字。★ 拿不到的项**不写**，别留「地图没查到」这种话给它编。"""
    lines = [f"**{p.user_id}**" + (f"（{p.nick}）" if p.nick else "")]

    # 在线状态 —— 这个最重要，放最前
    if p.online:
        line = "在线"
        if p.server_id:
            line += f"，服务器 {p.server_id}"
        # 地图：**只报非 0 的字段**，两个都是 0 就一个字不提。
        # 不写「地图 0/1」这种 —— bot 会顺着 0 编出「地图没查到」。
        parts = []
        if p.map_type:
            parts.append(f"地图类型 {p.map_type}")
        if p.map_id:
            parts.append(f"地图 {p.map_id}")
        if parts:
            line += "，" + "，".join(parts)
        lines.append(f"· 状态：**{line}**")

    else:
        lines.append("· 状态：**离线**")

    if p.vip or p.vip_level:
        vip_txt = "VIP" if p.vip else "非VIP"
        if p.vip_level:
            vip_txt += f"（等级 {p.vip_level}）"
        if p.is_extreme_nono:
            vip_txt += "｜至尊NONO"
        lines.append(f"· {vip_txt}")

    if p.reg_time:
        lines.append(f"· 注册：{p._fmt(p.reg_time)}")
    if p.login_time:
        lines.append(f"· 最后登录：{p._fmt(p.login_time)}")
    if p.last_offline_time:
        lines.append(f"· 最后离线：{p._fmt(p.last_offline_time)}")

    if p.errors:
        lines.append(f"（部分字段没取到：{'、'.join(p.errors)}）")
    return "\n".join(lines)


if __name__ == "__main__":
    import os
    import sys
    from pathlib import Path

    # 单独跑要先把插件目录加进 sys.path（包内运行时走相对导入，不需要）
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if len(sys.argv) < 2:
        print("用法: python seer_online.py <米米号>")
        raise SystemExit(1)
    t = int(sys.argv[1])
    print(format_online(query_online(t)))
