# -*- coding: utf-8 -*-
"""兑换码代换 —— 私聊绑定、群里本人触发、登录他自己的号去换。

## 流程（鱼片 2026-11-22 定）

```
私聊 /绑定 米米号 密码      → 一个 QQ 可以绑多个号，第一个自动成为默认
群里 /兑换 码               → 默认用默认号；只有绑定本人的 sender_id 能触发
群里 /兑换 码 945266209     → 指定用哪个号（鱼片：必须他明确说了才换号）
执行前先查在线（2157）      → 在线就回「先退游戏」，否则等于把他自己挤下线
登录他的号 → 43267 → 群里直接报物品
失败直说，不重试、不记幂等
```

## 三条设计口径（都是鱼片定的，别改）

1. **结果群里直接发** —— 重要的码不会往群里丢，群里走的都是限量全民码，公开没坏处。
2. **不做幂等表** —— 码是一次性的，失败就直说「换不出来」，重试纯浪费。
3. **不做限流** —— 监控号每天重登几百次都没事，实证过。

## 密码存哪

密文进 `<data_dir>/redeem.db`，**密钥单独放 `~/.dsh/.redeem_key`**。
分开是为了：备份/同步 plugin_data 时不会把密钥一起带走，光拿到库解不开。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

#: 一个 QQ 可以绑多个米米号；`is_default=1` 的那个是 `/兑换` 不指定时用的
_SCHEMA = """
create table if not exists bind(
  qq text not null,
  mmid integer not null,
  pwd_enc blob not null,
  ts integer not null,
  is_default integer not null default 0,
  primary key(qq, mmid));
"""


def _key_path() -> Path:
    """密钥位置：用户目录下，和 plugin_data 分开放。"""
    return Path.home() / ".dsh" / ".redeem_key"


def _fernet():
    """取 Fernet 实例；没有密钥就生成一个（0600）。"""
    from cryptography.fernet import Fernet

    p = _key_path()
    if p.is_file():
        return Fernet(p.read_bytes().strip())
    p.parent.mkdir(parents=True, exist_ok=True)
    k = Fernet.generate_key()
    p.write_bytes(k)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass          # Windows 上不生效，无害
    return Fernet(k)


def _con(data_dir: str | Path) -> sqlite3.Connection:
    db = Path(data_dir) / "redeem.db"
    con = sqlite3.connect(str(db), timeout=10)
    con.executescript(_SCHEMA)
    return con


def bind(data_dir, qq: str, mmid: int, pwd: str, make_default: bool = False) -> bool:
    """绑定一个米米号，返回它是否成了默认号。

    规则：**这个 QQ 的第一个号自动成为默认**；之后绑的不动默认，
    除非调用方显式传 `make_default=True`。

    Args:
        data_dir: plugin_data 目录。
        qq: 发指令那个人的 QQ 号。
        mmid: 要绑的米米号。
        pwd: 明文密码（只在这里出现一次，落盘前就加密）。
        make_default: 是否把这个号设为默认。
    """
    enc = _fernet().encrypt(str(pwd).encode("utf-8"))
    con = _con(data_dir)
    try:
        had = con.execute("select count(*) from bind where qq=?", (str(qq),)).fetchone()[0]
        con.execute(
            "insert or replace into bind(qq, mmid, pwd_enc, ts, is_default)"
            " values(?,?,?,?,"
            "   coalesce((select is_default from bind where qq=? and mmid=?), 0))",
            (str(qq), int(mmid), enc, int(time.time()), str(qq), int(mmid)))
        first = (had == 0)
        if first or make_default:
            con.execute("update bind set is_default=0 where qq=?", (str(qq),))
            con.execute("update bind set is_default=1 where qq=? and mmid=?",
                        (str(qq), int(mmid)))
        con.commit()
        return bool(first or make_default)
    finally:
        con.close()


def unbind(data_dir, qq: str, mmid: int | None = None) -> int:
    """解绑；`mmid=None` 表示全删。返回删掉几条。

    全删之后再绑，第一个又会自动成为默认。
    """
    con = _con(data_dir)
    try:
        if mmid is None:
            n = con.execute("delete from bind where qq=?", (str(qq),)).rowcount
        else:
            n = con.execute("delete from bind where qq=? and mmid=?",
                            (str(qq), int(mmid))).rowcount
        # 默认号被删了就把最早绑的那个提上来
        if n and con.execute("select count(*) from bind where qq=? and is_default=1",
                             (str(qq),)).fetchone()[0] == 0:
            row = con.execute("select mmid from bind where qq=? order by ts limit 1",
                              (str(qq),)).fetchone()
            if row:
                con.execute("update bind set is_default=1 where qq=? and mmid=?",
                            (str(qq), row[0]))
        con.commit()
        return n
    finally:
        con.close()


def list_binds(data_dir, qq: str) -> list[tuple[int, bool]]:
    """这个 QQ 绑了哪些号 —— `[(米米号, 是否默认), ...]`，按下标顺序稳定。"""
    con = _con(data_dir)
    try:
        rows = con.execute(
            "select mmid, is_default from bind where qq=? order by is_default desc, ts",
            (str(qq),)).fetchall()
    finally:
        con.close()
    return [(int(a), bool(b)) for a, b in rows]


def get_bind(data_dir, qq: str, mmid: int | None = None) -> tuple[int, str] | None:
    """取 `(米米号, 明文密码)`。

    `mmid=None` 时用默认号；显式给了就必须正好绑过那个号（不会退而求其次）。

    Args:
        data_dir: plugin_data 目录。
        qq: QQ 号。
        mmid: 指定米米号；None = 默认号。
    """
    con = _con(data_dir)
    try:
        if mmid is None:
            row = con.execute(
                "select mmid, pwd_enc from bind where qq=? and is_default=1",
                (str(qq),)).fetchone()
            if not row:
                row = con.execute(
                    "select mmid, pwd_enc from bind where qq=? order by ts limit 1",
                    (str(qq),)).fetchone()
        else:
            row = con.execute(
                "select mmid, pwd_enc from bind where qq=? and mmid=?",
                (str(qq), int(mmid))).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        pwd = _fernet().decrypt(row[1]).decode("utf-8")
    except Exception:      # noqa: BLE001  密钥换了/密文坏了
        return None
    return int(row[0]), pwd


def describe_binds(data_dir, qq: str) -> str:
    """给用户看自己绑了哪些号 —— 只报米米号，绝不回显密码。"""
    rows = list_binds(data_dir, qq)
    if not rows:
        return "还没绑定。私聊我发：/绑定 米米号 密码（可以绑多个，第一个是默认）"
    lines = []
    for mmid, is_def in rows:
        lines.append(f"· {mmid}" + ("（默认）" if is_def else ""))
    return "你绑定的米米号：\n" + "\n".join(lines) + "\n要换绑定的号：/默认 米米号"


def set_default(data_dir, qq: str, mmid: int) -> bool:
    """把某个已绑的号设为默认；没绑过返回 False。"""
    con = _con(data_dir)
    try:
        if con.execute("select count(*) from bind where qq=? and mmid=?",
                       (str(qq), int(mmid))).fetchone()[0] == 0:
            return False
        con.execute("update bind set is_default=0 where qq=?", (str(qq),))
        con.execute("update bind set is_default=1 where qq=? and mmid=?", (str(qq), int(mmid)))
        con.commit()
        return True
    finally:
        con.close()


def _xm_redeem():
    """拿到 `_xm/redeem.py` 这个模块。

    ★ 不能直接 `from ._xm import redeem` —— `_xm` 目录**没有 `__init__.py`**，
      不是正规包。两条路：
        ① 插件正常运行时：`seer_pvp` 已经把 `_xm/` 注入 `sys.path`，`import redeem` 就行
        ② 拿不到（独立加载 / 包上下文缺失）：**按文件路径直接 exec**，不依赖任何 path
      2026-11-22 只写了 ① 的写法，结果 bot 回话里物品名全是「未知物品（id=…）」。
    """
    import importlib
    import importlib.util
    import sys

    try:
        from .seer_pvp import _XM      # noqa: PLC0415

        if _XM and str(_XM) not in sys.path:
            sys.path.insert(0, str(_XM))
        return importlib.import_module("redeem")
    except Exception:      # noqa: BLE001
        pass
    here = Path(__file__).resolve().parent / "_xm" / "redeem.py"
    spec = importlib.util.spec_from_file_location("_seer_redeem_proto", here)
    if spec is None or spec.loader is None:
        raise ImportError(f"找不到协议模块：{here}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seerapi_db(data_dir=None):
    """定位 seerapi 库（物品名要从这里查）。

    ★ 别用 `Path(__file__).parents[N]` 推 —— 在实例里 `__file__` 在
      `core/data/plugins/…` 下，往上数是 plugins，不是 `core/data`，必然推错。
      用 `data_dir`（= `plugin_data/astrbot_plugin_seer_expert`）的**兄弟目录**最稳。
    """
    cands = []
    if data_dir:
        cands.append(Path(data_dir).parent / "astrbot_plugin_seer_info" / "seerapi.sqlite")
    inst = os.environ.get("ASTRBOT_INSTANCE")
    if inst:
        cands.append(Path(inst) / "core/data/plugin_data"
                     / "astrbot_plugin_seer_info" / "seerapi.sqlite")
    cands.append(Path(__file__).resolve().parents[2] / "dist" / "data" / "seerapi.sqlite")
    return next((p for p in cands if p.is_file()), None)


def _fmt_items(item_ids, data_dir=None) -> list[str]:
    """把 (id, 数量) 翻成「名字 ×数量」，翻不出来就照实报 ID。"""
    if not item_ids:
        return []
    try:
        _R = _xm_redeem()
        names = _R.resolve_names([i for i, _ in item_ids], _seerapi_db(data_dir))
    except Exception:      # noqa: BLE001
        names = {}
    out = []
    for i, qty in item_ids:
        info = names.get(i)
        if info:
            desc = f"（{info['desc']}）" if info.get("desc") else ""
            out.append(f"{info['name']} ×{qty}{desc}")
        else:
            out.append(f"未知物品（id={i}）×{qty}")
    return out


async def do_redeem(data_dir, qq: str, code: str, mmid: int | None = None) -> str:
    """真正干活：查在线 → 登录他的号 → 兑换 → 拼回话。

    Args:
        data_dir: 插件的 plugin_data 目录（`redeem.db` 在这）。
        qq: **发指令那个人的 QQ 号** —— 调用方必须已经确认他就是绑定本人。
        code: 兑换码原文。
        mmid: 指定用哪个号；None = 默认号。

    Returns:
        直接发到群里的文本。
    """
    got = get_bind(data_dir, qq, mmid)
    if not got:
        if mmid is None:
            return ("你还没绑定账号。私聊我发：`/绑定 米米号 密码`，"
                    "绑完在群里喊 `/兑换 兑换码` 就行。")
        return (f"你名下没有绑 {mmid} 这个号。"
                f"已绑的：/我的号 看一下；要加就私聊 `/绑定 {mmid} 密码`。")
    use_mmid, pwd = got

    # ── ① 先查在线：在线就让他先退游戏，不然一登录就把他挤掉 ──
    try:
        from .seer_online import query_online_async
    except Exception:      # noqa: BLE001
        query_online_async = None      # type: ignore[assignment]
    if query_online_async is not None:
        try:
            prof = await query_online_async(use_mmid)
            if getattr(prof, "online", False):
                return (f"你的号 {use_mmid} **现在在线** —— 先在游戏里退出来，"
                        f"不然我一登录就把你挤下线了。退了再喊我。")
        except Exception:      # noqa: BLE001
            # 查不到在线状态不阻断，但要说一句（可能是查询号自己出问题）
            pass

    # ── ② 登录他的号去换（同步 socket，丢线程里跑）──
    try:
        _R = _xm_redeem()
    except Exception as e:      # noqa: BLE001
        return f"兑换模块加载失败：{type(e).__name__}: {e}"
    try:
        r = await asyncio.to_thread(_R.redeem, str(use_mmid), pwd, code, 8.0)
    except Exception as e:      # noqa: BLE001
        # 登录挂了（密码错 / 网络 / 服务器）—— 这时**码根本没发出去**，没损失
        msg = str(e)
        low = msg.lower()
        if "密码" in msg or "password" in low:
            return (f"❌ {use_mmid} 登录失败：**用户名或密码错误**。\n"
                    f"密码填错了就私聊重新发 `/绑定 {use_mmid} 密码`。码没动。")
        return (f"❌ {use_mmid} 登录失败：{type(e).__name__}: {msg}\n"
                f"（码没动，可以隔一会儿再试）")

    if r.get("ok"):
        items = _fmt_items(r.get("item_ids") or [], data_dir)
        body = "\n".join(f"· {x}" for x in items) if items else "（服务端没给物品清单）"
        return f"✅ {use_mmid} 兑换成功\n{body}"
    if r.get("tip") is not None:
        return (f"❌ {use_mmid} 兑换失败：码无效 / 已用过 / 已过期 —— "
                f"服务端对这三种不区分，没法再细分。")
    return f"❌ {use_mmid} 兑换没成功：服务端没给回应（码可能没被受理）。"
