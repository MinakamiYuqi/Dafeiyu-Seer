# -*- coding: utf-8 -*-
"""巅峰圣战投票查询：游戏协议 cmd 4481 的封装 + 期次自动探测。

协议来源：Murmansk5000/IronsBot 的 headless_seer（逆向，已公开）
    cmd 4481 GET_DAILY_RANK_INFO
    body = DailyRankParam(key, sub_key, start, end)  → 4 个 u32 大端
    key=191 限制池（限2）取前 20
    key=192 准限制池（限3）取前 30
    响应 = u32 count + count × (u32 精灵id + i32 票数 + 16B 占位)

2026-11-14 实测记录（这些是判断「哪一期投了哪个池」的依据）：
    20261030 期 → key=191 有数据（20 条），key=192 空
    20261106 期 → key=192 有数据（30 条），key=191 空
    20261009 期 → key=192 有数据（30 条）
    sub_key=0   → 一律空（服务端不给默认期次）
    → **限2 和限3 是交替投票的**，一期只投一个池。

期次格式：每期 8 位数字（20261030 = 2026-10-30），按周更新。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import struct

logger = logging.getLogger(__name__)

# 池子 → (标签, 取前几条)
VOTE_KEYS: dict[int, tuple[str, int]] = {
    191: ("限制池（限2）", 19),
    192: ("准限制池（限3）", 29),
}

# 探测候选期次的回溯周数（每期一周，回看 12 周够用）
_PROBE_WEEKS = 12


def _candidates(today: _dt.date | None = None) -> list[int]:
    """生成候选期次：从今天往前推，每周一个，周五起算。

    游戏的周更一般在周五，期次号就是那一天。多往前推几周容错。
    """
    base = today or _dt.date.today()
    # 找最近的周五（weekday: 周一=0 … 周五=4）
    offset = (base.weekday() - 4) % 7
    friday = base - _dt.timedelta(days=offset)
    out: list[int] = []
    for i in range(_PROBE_WEEKS):
        d = friday - _dt.timedelta(weeks=i)
        out.append(int(d.strftime("%Y%m%d")))
    return out


_NAME_CACHE: dict[int, str] = {}


def pet_names() -> dict[int, str]:
    """精灵 id → 名字。查 seerapi.pet，回落手册 pets.db。

    进程内缓存一次，数据变了（mtime 变）也不重载 —— 名字表基本不动。
    """
    global _NAME_CACHE
    if _NAME_CACHE:
        return _NAME_CACHE
    import sqlite3
    from pathlib import Path
    here = Path(__file__).resolve()
    # .../core/data/plugins/astrbot_plugin_seer_expert/seer_vote.py
    #   parents[0]=插件目录  [1]=plugins  [2]=data ← 这层
    data_dir = here.parents[2] / "plugin_data"
    for db, sql in (
        (data_dir / "astrbot_plugin_seer_info" / "seerapi.sqlite", "select id, name from pet"),
        (data_dir / "astrbot_plugin_seer_expert" / "manual_db" / "pets.db", "select ID, Name from pets"),
    ):
        if not db.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            for pid, nm in con.execute(sql):
                _NAME_CACHE.setdefault(int(pid), str(nm))
            con.close()
        except sqlite3.Error:
            continue
    return _NAME_CACHE


def _parse_rank_list(body: bytes, names: dict[int, str]) -> list[dict]:
    """解 DailyRankList → [{id, name, score}]。"""
    if len(body) < 4:
        return []
    count = struct.unpack(">I", body[:4])[0]
    off = 4
    out = []
    for _ in range(min(count, 200)):
        if off + 24 > len(body):
            break
        pid = struct.unpack(">I", body[off:off + 4])[0]
        score = struct.unpack(">i", body[off + 4:off + 8])[0]
        out.append({"id": pid, "name": names.get(pid, ""), "score": score})
        off += 24
    return out


def _raw_query(account: str, password: str, key: int, sub_key: int) -> bytes:
    """发一次 4481，返回原始 body（空 bytes = 没回包或空结果）。"""
    from .seer_pvp import _import_headless

    h = _import_headless()
    label, end = VOTE_KEYS.get(key, ("未知池", 19))
    sock = None
    try:
        sock, ser, uid = h.login(account, password)
        body = struct.pack(">IIII", key, sub_key, 0, end)
        sock.sendall(h.pack(4481, uid, body, ser))
        # 收包：headless 的 recv_one 返回 (cmd_id, result, body)
        sock.settimeout(3.0)
        for _ in range(40):
            cmd, _result, rbody = h.recv_one(sock)
            if cmd == 4481:
                return rbody
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return b""


def query_vote_sync(key: int, sub_key: int = 0, *, account: str | None = None,
                    password: str | None = None, names: dict[int, str] | None = None) -> dict:
    """同步查一期投票榜。

    返回 {"key", "label", "sub_key", "rows", "error"}。
    rows 为空且 error 为 None 表示「这一期没有数据」（正常情况，不是错误）。
    """
    from .seer_pvp import _creds

    label = VOTE_KEYS.get(key, (f"key={key}", 19))[0]
    out: dict = {
        "key": key, "label": label, "sub_key": int(sub_key),
        "rows": [], "error": None,
    }
    acct, pwd = _creds(account, password)
    if not acct or not pwd:
        out["error"] = "缺服务账号（seer_account / seer_password 未配置）"
        return out
    try:
        body = _raw_query(acct, pwd, key, int(sub_key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[vote] 查询失败 key=%s sub=%s: %s", key, sub_key, exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["rows"] = _parse_rank_list(body, names if names is not None else pet_names())
    return out


def probe_latest_sync(key: int, *, account: str | None = None,
                      password: str | None = None,
                      names: dict[int, str] | None = None) -> dict:
    """自动找「最近一期这个池子有投票数据」的期次。

    服务端 sub_key=0 不给默认期次，只能挨个试。每试一次要重新登录（约 0.9s），
    所以这里**一次登录把候选期次全试完**，别每期都登录一次。
    """
    from .seer_pvp import _creds, _import_headless

    label = VOTE_KEYS.get(key, (f"key={key}", 19))[0]
    out: dict = {
        "key": key, "label": label, "sub_key": 0,
        "rows": [], "error": None, "tried": [],
        # 当期（最近的周五）有没有投票数据。False = 官方还没开投票，
        # 命中 sub_key 就是「上期」或更早。输出层要据此说明，别让群友误以为是当期。
        "current_open": False,
        "current_period": 0,
    }
    acct, pwd = _creds(account, password)
    if not acct or not pwd:
        out["error"] = "缺服务账号（seer_account / seer_password 未配置）"
        return out

    try:
        h = _import_headless()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"headless 不可用：{exc}"
        return out

    sock = None
    try:
        sock, ser, uid = h.login(acct, pwd)
        sock.settimeout(3.0)
        _cands = _candidates()
        out["current_period"] = _cands[0] if _cands else 0
        for _idx, sk in enumerate(_cands):
            body = struct.pack(">IIII", key, sk, 0, VOTE_KEYS.get(key, ("", 19))[1])
            try:
                sock.sendall(h.pack(4481, uid, body, ser))
            except Exception:
                break
            got = b""
            for _ in range(40):
                try:
                    cmd, _r, rbody = h.recv_one(sock)
                except Exception:
                    break
                if cmd == 4481:
                    got = rbody
                    break
            rows = _parse_rank_list(got, names if names is not None else pet_names())
            out["tried"].append({"sub_key": sk, "count": len(rows)})
            if _idx == 0:
                out["current_open"] = bool(rows)
            if rows:
                out["sub_key"] = sk
                out["rows"] = rows
                return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("[vote] 探测失败 key=%s: %s", key, exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return out


def _fmt_period(sub_key: int) -> str:
    s = str(sub_key or "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s or "未标注"


def format_vote(data: dict, *, limit: int = 20) -> str:
    """投票榜文字版（不看图也能读）。"""
    if data.get("error"):
        return f"查 {data.get('label', '')} 投票失败：{data['error']}"
    rows = data.get("rows") or []
    if not rows:
        tried = data.get("tried") or []
        note = f"（试了 {len(tried)} 个期次）" if tried else ""
        return (
            f"{data.get('label', '')} 投票榜目前没有数据{note}。\n"
            "池子刚更新时投票还没开始，通常要等官方开投票。"
        )
    lines = [
        f"{data['label']} 投票榜 · {_fmt_period(data.get('sub_key'))} 期"
        f"（共 {len(rows)} 项）"
    ]
    # 当期没开投票时，命中的是上期 —— 必须说清楚，否则群友会以为这是当前的
    if not data.get("current_open"):
        cur = data.get("current_period") or 0
        lines.append(
            f"⚠ 当期（{_fmt_period(cur)}）的投票还没开始，"
            f"下面是最近一期有数据的 {_fmt_period(data.get('sub_key'))} 期。"
        )
    for i, r in enumerate(rows[:limit], 1):
        nm = r.get("name") or f"#{r.get('id')}"
        lines.append(f"{i:>2}. {nm} {r.get('score')} 票")
    if len(rows) > limit:
        lines.append(f"…另有 {len(rows) - limit} 项")
    return "\n".join(lines)


async def query_vote(key: int, sub_key: int = 0, **kw) -> dict:
    return await asyncio.to_thread(query_vote_sync, key, sub_key, **kw)


async def probe_latest(key: int, **kw) -> dict:
    return await asyncio.to_thread(probe_latest_sync, key, **kw)
