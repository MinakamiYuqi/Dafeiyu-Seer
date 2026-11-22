# -*- coding: utf-8 -*-
"""兑换码：43267 的协议层（已实测打通，2026-11-22 十三号鱼）。

## 协议（靠抓包 + 实测反推出来的）

**2026-11-22 20:13 在监控号 959319632 上实测成功**：

```
body = 00000016 23 e4b8b4e697b6e58aa0e7a081e4b88de783a7e5bf83
       └─22─┘ └┘ └──────── 21 字节「临时加码不烧心」────────┘
recv 4549   u32 = [0, 0, 0, 0, 1, 40817, 1, 0, 0, 0]      ← 40817 = 青龙刻印
```

和鱼片抓到的 `4549, 0,0,0,0, 1, 40817, 1, 0,0,0` 逐位一致。

```
send 43267  body = [len: u32be][0x23][UTF-8 码]
recv 43267  空 body            → 请求被受理（只代表格式对）
recv 4549   [0, N, 物品ID, 数量, ...]  → **成功**，同步立刻回；这是唯一可靠的成功判据
recv 8015   [0, 提示ID]        → **异步**，2~3 秒后才到
```

★ **判成功只看 `4549`，别看 `8015`**。2026-11-22 两次实测：
- 成功那两次，`4549` 都到了；其中一次**同时还来了一条 `8015[0,300]`**
  （兑换后跟着的别的提示，不是兑换结果）
- 失败那次（无效码），只有 `8015[0,300]`、没有 `4549`

所以 `300` 只是"某个通用提示"，**不能拿它当兑换失败的判据** —— 差点又因此误判一次。

**四个踩过的坑**：

1. **body 不能补 padding**。服务端按 `len` 字段读，后面多出几个字节就**整包静默丢弃**
   （连空的 43267 都不回）。实测：21 字节中文码 + 3 字节 padding → 无回包；
   去掉 padding → 立刻有回包。
2. **`len` 要含 0x23 那一字节**（= 1 + 码字节数）。抓包里 `22 = 1 + 21`，
   且 `602192052 = 0x23E4B8B4`，0x23 后面正是「临」的 UTF-8。
3. **码是明文 UTF-8 传的**，不是哈希也不是本地 ID。
4. **`8015` 是提示通道，但它是异步的**——原来等 3 秒会偶尔抓不到，所以默认给 5 秒。
   曾经误判它是"别的同步包"，2026-11-22 用「真码已用过 vs 无效码」对比才确认：
   **两者都回 8015[0,300]**，所以 300 是通用失败码；成功那次（抓包）是 8015[0,0]。

**★ 验证这件事时踩的最大的坑：读取器丢帧，被我误判成「码已经用过了」。**
2026-11-22：用 bot 服务号 945266209 试「临时加码不烧心」（**该号当时并没换过**），
拿到 `8015[0,300]` 就断定"这号换过了"，鱼片只好手动上号去换 —— 结果他一换，
这个号就真的没有干净样本了，我等于自己把验证环境毁了。
实际原因是 `headless.recv_one` 超时后**丢掉了已收到的字节**（见
`_re/lineup_followup/TCP续研.md`）：`4549` 很可能到了但被丢帧，于是判成失败。
换成 `_recv_frames()`（跨 recv 保留半包）后，同样的包在监控号上一次成功。
**教训：判"失败"之前先确认读取器没丢帧，别拿失败结果去反推业务原因。**

## 账号

`login(account, password)` 走 `headless.py`。**bot 的登录号和监控号是两个号**
（鱼片 2026-11-22 说明），所以账号要由调用方传进来，这个模块不读任何配置。
"""
from __future__ import annotations

import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import headless  # noqa: E402

#: 兑换码命令号（实测确认）
CMD_REDEEM = 43267
#: 服务端成功时推「获得物品」用的命令号
CMD_GAIN = 4549
#: 服务端的通用提示通道（在 XM 的 `cmdid` 白名单里也有它）。
#: body 是 `[0, 提示ID]`：**0 = 成功**，**300 = 失败**（不区分"无效/已用过/已过期"）。
CMD_TIP = 8015
#: 失败提示 ID（2026-11-22 实测：真码已用过、纯无效码，两者都回这个）
TIP_FAIL = 300


def pack_redeem_body(code: str, with_prefix: bool = True) -> bytes:
    """构造 43267 的包体：`[len:u32be][0x23][UTF-8 码]`，**不补 padding**。

    Args:
        code: 兑换码原文（中文也行，按 UTF-8 编码）。
        with_prefix: 是否带那个 0x23。**默认 True —— 这才是客户端真实发的形状**
            （抓包 `22, 602192052, ...` 里 22 = 1+21，且 602192052 = 0x23E4B8B4）。
            置 False 只用于对比实验，别在生产里用。

    Returns:
        包体字节。

    ★ 两个坑一起记：
      ① `len` 必须等于「1 + 码字节数」（含 0x23 那一字节），不是码长本身
      ② **padding 一个字节都不能加** —— 服务端按 len 读，多出来就整包静默丢弃
    """
    cb = str(code).strip().encode("utf-8")
    head = b"\x23" if with_prefix else b""
    n = len(cb) + len(head)
    return struct.pack(">I", n) + head + cb


def _recv_frames(sock, timeout: float = 6.0) -> list[tuple[int, bytes]]:
    """在 timeout 内收**所有**完整帧，返回 [(cmd, body), ...]。

    ★ 为什么不用 `headless.recv_one`：它超时后会把已收到的字节丢掉，直接返回 None
      （`_re/lineup_followup/TCP续研.md` 记过这条）。`4549` 是成功通知，一旦被丢
      就会误判成失败 —— 2026-11-22 就是这么把「码没用过」误判成「码用过了」的。
    ★ 这里跨 recv 保留半包（同 `seer_pvp._Reader` 的做法，但那个只等指定命令、
      会把别的帧消费掉，我要的是全部帧，所以不能直接用它）。

    Args:
        sock: 已登录的连接。
        timeout: 总等待秒数（从调用时刻算）。

    Returns:
        `[(cmd, body), ...]`，按到达顺序。
    """
    buf = bytearray()
    out: list[tuple[int, bytes]] = []
    end = time.monotonic() + timeout
    while True:
        while len(buf) >= 4:
            n = struct.unpack_from(">I", buf)[0]
            if not 17 <= n <= 2_000_000:
                return out                    # 帧长非法：连接脏了，能拿多少算多少
            if len(buf) < n:
                break
            frame = bytes(buf[:n])
            del buf[:n]
            out.append((struct.unpack_from(">I", frame, 5)[0], frame[17:]))
        left = end - time.monotonic()
        if left <= 0:
            return out
        sock.settimeout(left)
        try:
            chunk = sock.recv(65536)
        except (socket.timeout, OSError):
            return out
        if not chunk:
            return out                         # 服务端断开
        buf.extend(chunk)


def _pick_items(u: list[int]) -> list[tuple[int, int]]:
    """从 4549 的 u32 里抠出 (物品ID, 数量) 对。

    实测这个 body 有两种形态，所以用扫描法而不是固定偏移：

    ```
    两个物品：[0, 2, 300066, 1, 300917, 1, 0, 0, 0, 0, 0, 0]
    一个物品：[0, 0, 0, 0, 1, 40817, 1, 0, 0, 0]          ← 前面多几个 0，看不出规律
    ```

    两种都能靠「ID 够大 + 数量是个小数」扫出来，且不会把中间的 0 当物品。
    2026-11-22 实测：`300066`/`300917` = 「通用刻印激活水晶」/「全能抗性转化晶石-橙」，
    和一个物品那次抠出的 `40817` 都对得上背包里的东西。

    Args:
        u: `headless.u32s(4549 的 body)` 的结果。

    Returns:
        `[(id, qty), ...]`；扫不出就是空列表（宁缺勿错）。
    """
    out: list[tuple[int, int]] = []
    i = 0
    while i + 1 < len(u):
        idv, qty = u[i], u[i + 1]
        if 1000 <= idv <= 10_000_000 and 1 <= qty <= 999:
            out.append((idv, qty))
            i += 2
        else:
            i += 1
    return out


def resolve_names(ids, db: "str | Path | None" = None) -> dict:
    """把 4549 里的物品 ID 翻成人看的名字 —— **两张表都得查**。

    ★ 为什么必须查两张：
      · 普通道具在 `item` 表（300066 = 通用刻印激活水晶）
      · **刻印在 `mintmark` 表**（40817 = 苍魂之魄）——
        拿 item 表查刻印会查空，我一开始就误以为它是"背包实例 ID"。

    Args:
        ids: 物品 ID 列表 / 单个 ID。
        db: seerapi 库路径；不给就按 `ASTRBOT_INSTANCE` 推，再退回仓库里的 `dist/data`。

    Returns:
        `{id: {"name": str, "from": "item"|"mintmark", "desc": str}}`；查不到的 id 不在结果里
        （宁缺勿错 —— 别给它编个名字）。
    """
    import os
    import sqlite3

    if isinstance(ids, int):
        ids = [ids]
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    if db is None:
        cand = []
        inst = os.environ.get("ASTRBOT_INSTANCE")
        if inst:
            cand.append(Path(inst) / "core/data/plugin_data"
                        / "astrbot_plugin_seer_info/seerapi.sqlite")
        cand.append(Path(__file__).resolve().parents[2] / "dist" / "data" / "seerapi.sqlite")
        db = next((p for p in cand if p.is_file()), None)
    if db is None or not Path(db).is_file():
        return {}

    out: dict = {}
    con = sqlite3.connect(str(db), timeout=10)
    try:
        q = ",".join("?" * len(ids))
        for tid, name in con.execute(f"select id, name from item where id in ({q})", ids):
            out[tid] = {"name": name, "from": "item", "desc": ""}
        for tid, name, desc in con.execute(
                f"select id, name, coalesce(desc,'') from mintmark where id in ({q})", ids):
            out[tid] = {"name": name, "from": "mintmark", "desc": desc}
    finally:
        con.close()
    return out


def redeem_on(sock, ser, uid: int, code: str, wait: float = 6.0) -> dict:
    """在**已经登录好的连接**上兑换 —— `redeem()` 的登录版调它。

    单独拆出来是为了能「先在这条连接上跑个只读包确认线路，再兑换」——
    兑换码只能用一次，不能拿它去试线路。

    Args:
        sock: 已登录的 socket。
        ser: 该连接的 `headless.Serial`（result 累积值，必须一路沿用）。
        uid: 自己的米米号。
        code: 兑换码。
        wait: 收包总时长。

    Returns:
        同 :func:`redeem`。
    """
    out = {"ok": False, "items": [], "item_ids": [], "codes": [], "tip": None, "note": ""}
    body = pack_redeem_body(code)
    sock.sendall(headless.pack(CMD_REDEEM, uid, body, ser, 0x31))
    seen = []
    for cmd, payload in _recv_frames(sock, timeout=wait):
        seen.append(cmd)
        if cmd == CMD_GAIN:
            u = headless.u32s(payload)
            out["items"] = u
            out["item_ids"] = _pick_items(u)
            out["ok"] = True
        elif cmd == CMD_TIP:
            u = headless.u32s(payload)
            if len(u) >= 2:
                out["tip"] = u[1]
    out["codes"] = seen
    if out["ok"]:
        out["note"] = "兑换成功"
    elif out["tip"] == TIP_FAIL:
        out["note"] = ("兑换失败（提示码 300）—— 码无效 / 已用过 / 已过期，"
                       "服务端对这三种不区分")
    elif out["tip"] is None:
        out["note"] = "没等到提示（8015）—— 码没被受理，或超时太短"
    else:
        out["note"] = f"兑换失败，提示码 {out['tip']}（含义未知）"
    return out


def redeem(account: str, password: str, code: str,
           wait: float = 6.0, drains: int = 60) -> dict:
    """登录 → 兑换 → 关连接。account/password 由调用方给（本模块不读任何配置）。

    Args:
        account: 米米号。
        password: 密码（调用方负责不要落日志/落盘）。
        code: 兑换码。
        wait: 收包总时长；`8015` 提示是异步的，别低于 5 秒。
        drains: 保留参数（现在由 `_recv_frames` 自己控制，不再用）。

    Returns:
        `{"ok": bool, "items": [u32...], "item_ids": [(id, qty)...],
          "codes": [收到的命令号...], "tip": 提示码或 None, "note": str}`
        **`ok` 只看有没有收到 4549** —— 那是唯一的成功信号。
    """
    sock = ser = uid = None
    try:
        sock, ser, uid = headless.login(account, password)
        return redeem_on(sock, ser, uid, code, wait=wait)
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:      # noqa: BLE001
            pass
        try:
            if sock is not None:
                sock.close()
        except Exception:      # noqa: BLE001
            pass
    return out


def selftest() -> int:
    """包体构造的自检 —— 不联网。重点钉住「不补 padding」这条。"""
    cases = [
        ("AAAAAAAA", 13),                     # 4 + 1(0x23) + 8
        ("临时加码不烧心", 26),               # 4 + 1 + 21（不是 28，也不是 25）
        ("  abc  ", 8),                       # strip 后 3 字节 → 4 + 1 + 3
    ]
    bad = 0
    for code, want_len in cases:
        b = pack_redeem_body(code)
        ok_len = len(b) == want_len
        # 长度字段必须等于实际码字节数，且 body 总长正好 4+len（无 padding）
        n = struct.unpack(">I", b[:4])[0]
        ok_lf = n == len(b) - 4
        mark = "✔" if (ok_len and ok_lf) else "✘"
        if not (ok_len and ok_lf):
            bad += 1
        print(f"  {mark} {code!r} → {len(b)}B（期望 {want_len}）长度字段={n} "
              f"{'一致' if ok_lf else '不一致'}")
    # padding 反例：多补一个字节必须能被识别为「不对」
    b = pack_redeem_body("临时加码不烧心") + b"\x00"
    n = struct.unpack(">I", b[:4])[0]
    ok = n != len(b) - 4
    mark = "✔" if ok else "✘"
    if not ok:
        bad += 1
    print(f"  {mark} 反例：补 1 字节 padding 后，长度字段 {n} ≠ 实际 {len(b) - 4}"
          f"（服务端会丢弃这种包）")
    # 4549 物品解析：两种实测形态都必须抠对
    item_cases = [
        # 2026-11-22 实测：码「2026立于光中」→ 水晶 + 晶石
        ([0, 2, 300066, 1, 300917, 1, 0, 0, 0, 0, 0, 0],
         [(300066, 1), (300917, 1)]),
        # 2026-11-22 实测：密码「临时加码不烧心」→ 青龙刻印（单物品，前面多几个 0）
        ([0, 0, 0, 0, 1, 40817, 1, 0, 0, 0], [(40817, 1)]),
        ([0, 0, 0], []),
    ]
    for u, want in item_cases:
        got = _pick_items(u)
        mark = "✔" if got == want else "✘"
        if got != want:
            bad += 1
        print(f"  {mark} 物品解析 {u[:6]}… → {got}  期望 {want}")
    print(f"\nselftest {'PASS' if bad == 0 else f'FAIL（{bad} 条不对）'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(__doc__)
