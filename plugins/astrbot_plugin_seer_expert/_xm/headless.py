"""赛尔号无头客户端 —— 自己建 TCP 连接，不碰游戏进程

协议来源（已实测验证）：
  · oldml/saixiaoxi  backend/src/core/auth/login.py
  · oldml/saixiaoxi  backend/src/network/packet/send.py
  · oldml/saixiaoxi  backend/src/core/crypto/algorithms.py

★ 帧格式（关键，之前理解错过）：
  [4B length][1B version][4B cmd_id][4B user_id][4B result][body]
  · 登录包 version = 0x31
  · user_id = 自己的米米号（不是会话号）
  · result 是累积校验值，种子来自登录回包的 result

★ result 算法：
  crc8 = body 所有字节 XOR   (cmdId > 1000 时)
  result(n) = result(n-1) + crc8 + int(result(n-1)/-3)
              + len(body)%17 + cmdId%23 + 120

★ 登录：
  1. HTTP account-co.61.com/index.php 换 session（密码 md5 单次）
  2. TCP 连 175.24.235.221:1225
  3. 发 cmdId=1001，body = session(16B) + 492B 固定尾巴
  4. 回包 result=25/37/57/... 都算成功，body 前 4 字节是米米号
  5. 用回包的 result 作为后续累积种子

★ 已验证的查询：
  40002 USER_FOREVER_VALUE   任意米米号的巅峰战绩（16 字段）
  41298 GET_IDCARD_USERINFO  模式1=基础信息 模式5=主页展示4只

用法：
  python headless.py login
  python headless.py pvp 258806935
  python headless.py lineup 258806935
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

GAME_HOST = "175.24.235.221"
GAME_PORT = 1225
AUTH_URL = "https://account-co.61.com/index.php"
DEBUG = False

TAIL_PATH = Path(os.environ.get("TEMP", "/tmp")) / "login_tail.txt"

# 段位编号 → 名字（玩家 2026-11-06 确认的完整顺序）
RANK_NAMES = {0: "学徒", 1: "猛将", 2: "天骄", 3: "王者", 4: "圣皇"}


def rank_display(rk: int, star: int) -> str:
    """段位显示。圣皇满 100 星后叫「宇宙圣皇」，纯装饰，数值不变。"""
    name = RANK_NAMES.get(rk, f"段位{rk}")
    if rk == 4 and star >= 100:
        name = "宇宙圣皇"
    return f"{name} {star}星"

FIELDS = {
    124801: ("竞技", "当前"), 124802: ("竞技", "最高"),
    124804: ("竞技", "胜场"), 124805: ("竞技", "总场"),
    124791: ("狂野", "当前"), 124792: ("狂野", "最高"),
    124793: ("狂野", "胜场"), 124794: ("狂野", "总场"),
    129441: ("专家", "当前"), 129443: ("专家", "最高"),
    129446: ("专家", "胜场"), 129447: ("专家", "总场"),
    408302: ("大师", "当前"), 408303: ("大师", "最高"),
    408306: ("大师", "胜场"), 408307: ("大师", "总场"),
}
PACKED_MODES = {"竞技", "狂野", "大师"}      # 段位/星数打包；专家直接是分数


# ══════════════════════════════════════════════════════════
class Serial:
    """result 累积校验"""

    def __init__(self, seed: int = 0) -> None:
        self.result = seed

    def next(self, cmd_id: int, body: bytes) -> int:
        crc8 = 0
        if cmd_id > 1000:
            for b in body:
                crc8 ^= b
        a = self.result
        self.result = a + crc8 + int(a / -3) + (len(body) % 17) + (cmd_id % 23) + 120
        return self.result


def pack(cmd_id: int, user_id: int, body: bytes, ser: Serial, version: int = 0x31) -> bytes:
    result = ser.next(cmd_id, body)
    head = (
        struct.pack(">B", version)
        + struct.pack(">I", cmd_id)
        + struct.pack(">I", user_id)
        + struct.pack(">I", result)
    )
    total = 4 + len(head) + len(body)
    return struct.pack(">I", total) + head + body


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已断开")
        buf += chunk
    return buf


def recv_one(sock: socket.socket):
    try:
        length = struct.unpack(">I", recv_exact(sock, 4))[0]
        if length < 17 or length > 2_000_000:
            return None
        rest = recv_exact(sock, length - 4)
    except (ConnectionError, socket.timeout):
        return None
    version = rest[0]
    cmd_id = struct.unpack(">I", rest[1:5])[0]
    user_id = struct.unpack(">I", rest[5:9])[0]
    result = struct.unpack(">I", rest[9:13])[0]
    return cmd_id, result, rest[13:]


# ══════════════════════════════════════════════════════════
def fetch_session(account: str, password: str) -> bytes:
    import urllib.parse
    import urllib.request

    pwd_md5 = hashlib.md5(password.encode()).hexdigest()
    ts = str(int(time.time() * 1000))
    cb = f"jQuery19008830978978300397_{ts}"
    params = {
        "r": "userIdentity/authenticate", "callback": cb, "account": account,
        "rememberAcc": "false", "passwd": pwd_md5, "rememberPwd": "true",
        "vericode": "", "game": "02", "tad": "none", "_": ts,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore").strip()
    i, j = text.find("("), text.rfind(")")
    payload = json.loads(text[i + 1:j])
    if payload.get("result") != 0:
        raise RuntimeError(f"登录失败: {payload.get('err_desc') or payload}")
    sess = (payload.get("data") or {}).get("session")
    if not sess:
        raise RuntimeError("响应里没有 session")
    return bytes.fromhex(sess)


def login(account: str, password: str, timeout: float = 15.0):
    uid = int(account)
    session = fetch_session(account, password)
    print(f"[1] session 拿到（{len(session)} 字节）")

    sock = socket.create_connection((GAME_HOST, GAME_PORT), timeout=timeout)
    sock.settimeout(timeout)
    print(f"[2] 已连 {GAME_HOST}:{GAME_PORT}")

    tail = open(TAIL_PATH).read().strip()
    body = session + bytes.fromhex(tail)
    sock.sendall(pack(1001, uid, body, Serial(), version=0x31))
    print(f"[3] 发 LOGIN_IN  {len(pack(1001, uid, body, Serial(), 0x31))}B（重算麻烦，仅显示）")

    login_reply = None
    for _ in range(40):
        got = recv_one(sock)
        if not got:
            break
        cmd, result, rbody = got
        if DEBUG:
            print(f"    收到 cmd={cmd} result={result} body={len(rbody)}B")
        if cmd == 1001:
            login_reply = (cmd, result, rbody)
            break
    if not login_reply:
        raise RuntimeError("登录没拿到 1001 回包")
    _cmd, seed, rbody = login_reply
    if len(rbody) < 4:
        raise RuntimeError("登录回包 body 太短")
    echoed = struct.unpack(">I", rbody[0:4])[0]
    if echoed != uid:
        raise RuntimeError(f"回包米米号不对: {echoed} != {uid}")
    # ★ result 种子来自登录回包（saixiaoxi receive.py:67-68）
    print(f"[4] 登录成功  result种子={seed}  回包{len(rbody)}B  米米号={echoed}")
    return sock, Serial(seed), uid


def query(sock, ser, uid, cmd_id, body: bytes, wait: float = 1.5, drains: int = 40, version: int = 0x31):
    sock.sendall(pack(cmd_id, uid, body, ser, version))
    out = []
    sock.settimeout(wait)
    for _ in range(drains):
        got = recv_one(sock)
        if not got:
            break
        out.append(got)
        if got[0] == cmd_id:
            break
    return out


def u32s(b: bytes):
    return [struct.unpack(">I", b[j:j + 4])[0] for j in range(0, len(b) - len(b) % 4, 4)]


# ══════════════════════════════════════════════════════════
def cmd_login(account, password):
    sock, ser, uid = login(account, password)
    print("[✓] 登录可用")
    # 心跳自检
    r = query(sock, ser, uid, 1002, struct.pack(">I", 512), wait=2.0)
    print(f"    心跳 1002: {[(c, len(b)) for c, _, b in r]}")
    sock.close()


def cmd_pvp(account, password, target: int):
    sock, ser, uid = login(account, password)
    print(f"[✓] 开始查 {target}\n")
    res = {}
    for fid in FIELDS:
        body = struct.pack(">I", target) + struct.pack(">I", fid)
        got = query(sock, ser, uid, 40002, body, wait=1.2)
        hit = [g for g in got if g[0] == 40002]
        if not hit:
            print(f"  {FIELDS[fid][0]}·{FIELDS[fid][1]:4s}  ✗ 无回包")
            continue
        b = hit[0][2]
        if DEBUG:
            print(f"    [DEBUG] fid={fid} {len(b)}B {b.hex()}")
        u = u32s(b)
        val = u[0] if u else None
        res[fid] = val
        mode, kind = FIELDS[fid]
        if val is None:
            print(f"  {mode}·{kind:4s}  ✗ 空 body")
        elif kind in ("当前", "最高") and mode in PACKED_MODES:
            rk, star = val & 0xFFFF, (val >> 16) & 0xFFFF
            print(f"  {mode}·{kind:4s} = {val:>10}  → {rank_display(rk, star)}")
        else:
            print(f"  {mode}·{kind:4s} = {val}")

    # 胜率汇总
    print()
    modes = {}
    for fid, val in res.items():
        mode, kind = FIELDS[fid]
        modes.setdefault(mode, {})[kind] = val
    for m in ("竞技", "狂野", "专家", "大师"):
        d = modes.get(m, {})
        win, total = d.get("胜场"), d.get("总场")
        if win is None or total is None:
            continue
        if total == 0:
            print(f"  {m:4s} 胜率 = 当前赛季未参赛")
        else:
            print(f"  {m:4s} 胜率 = {win}/{total} = {win / total * 100:.2f}%")
    sock.close()


def cmd_lineup(account, password, target: int):
    sock, ser, uid = login(account, password)
    print(f"[✓] 探测 {target} 的阵容协议\n")

    print("── 41298 GET_IDCARD_USERINFO 全模式扫描 ──")
    for mode in range(1, 9):
        body = struct.pack(">I", mode) + struct.pack(">I", target) + struct.pack(">II", 0, 0)
        got = query(sock, ser, uid, 41298, body, wait=1.2)
        hit = [g for g in got if g[0] == 41298]
        if not hit:
            print(f"  模式{mode}  → 无回包")
            continue
        b = hit[0][2]
        if not b:
            print(f"  模式{mode}  → 空包")
            continue
        u = u32s(b)
        print(f"  模式{mode}  → {len(b):>5}B  u32前10={u[:10]}")
        pets = [u[k] for k in range(1, len(u) - 3)
                if 1000 <= u[k] <= 5400 and u[k + 1] == 0 and u[k + 2] == 0 and u[k + 3] == 0]
        if pets:
            print(f"      ★ 疑似精灵 {len(pets)} 只: {pets}")

    print("\n── 41298 模式5 分页探测（第3/4参数从0改成1..3）──")
    for p3 in (0, 1, 2, 3):
        for p4 in (0, 1, 2, 3):
            if p3 == 0 and p4 == 0:
                continue
            body = struct.pack(">I", 5) + struct.pack(">I", target) + struct.pack(">II", p3, p4)
            got = query(sock, ser, uid, 41298, body, wait=1.0)
            hit = [g for g in got if g[0] == 41298]
            if hit and hit[0][2]:
                b = hit[0][2]
                u = u32s(b)
                pets = [u[k] for k in range(1, len(u) - 3)
                        if 1000 <= u[k] <= 5400 and u[k + 1] == 0 and u[k + 2] == 0 and u[k + 3] == 0]
                print(f"  [5,target,{p3},{p4}] → {len(b):>5}B  精灵{pets}")
            else:
                print(f"  [5,target,{p3},{p4}] → 空/无回包")

    print("\n── 43706 GET_PET_INFO_BY_ONCE 带目标 ID ──")
    for desc, body in (
        ("[target]", struct.pack(">I", target)),
        ("[0,target]", struct.pack(">II", 0, target)),
        ("[target,0]", struct.pack(">II", target, 0)),
        ("[1,target]", struct.pack(">II", 1, target)),
    ):
        got = query(sock, ser, uid, 43706, body, wait=2.0)
        hit = [g for g in got if g[0] == 43706]
        if hit and hit[0][2]:
            b = hit[0][2]
            u = u32s(b)
            print(f"  {desc:14s} → {len(b):>5}B  u32前6={u[:6]}")
            pets = [u[k] for k in range(1, len(u) - 3)
                    if 1 <= u[k] <= 5400 and u[k + 1] == 0 and u[k + 2] == 0 and u[k + 3] == 0]
            if pets:
                print(f"      ★ 精灵 {len(pets)} 只: {pets[:20]}")
        else:
            print(f"  {desc:14s} → 无回包/空")

    print("\n── 2361 GET_LOVE_PET_LIST 带目标 ID ──")
    for desc, body in (
        ("[target]", struct.pack(">I", target)),
        ("[0,target]", struct.pack(">II", 0, target)),
        ("[1,target]", struct.pack(">II", 1, target)),
    ):
        got = query(sock, ser, uid, 2361, body, wait=2.0)
        hit = [g for g in got if g[0] == 2361]
        if hit and hit[0][2]:
            b = hit[0][2]
            u = u32s(b)
            print(f"  {desc:14s} → {len(b):>5}B  u32前6={u[:6]}")
        else:
            print(f"  {desc:14s} → 无回包/空")

    print("\n── 41921 / 41922 阵容配置（之前抓到是 12 套配置）──")
    for cmd, desc, mk in (
        (41921, "[target]", lambda t: struct.pack(">I", t)),
        (41921, "[0,target]", lambda t: struct.pack(">II", 0, t)),
        (41922, "[2,target]", lambda t: struct.pack(">II", 2, t)),
        (41922, "[target,2]", lambda t: struct.pack(">II", t, 2)),
    ):
        body = mk(target)
        got = query(sock, ser, uid, cmd, body, wait=1.5)
        hit = [g for g in got if g[0] == cmd]
        if hit and hit[0][2]:
            b = hit[0][2]
            u = u32s(b)
            print(f"  {cmd} {desc:12s} → {len(b):>5}B  u32前12={u[:12]}")
            pets = [u[k] for k in range(len(u)) if 1 <= u[k] <= 5400]
            if pets:
                print(f"      候选精灵ID: {pets[:20]}")
        else:
            print(f"  {cmd} {desc:12s} → 无回包/空")

    print("\n── 2303 GET_PET_LIST ——")
    for desc, body in (("[target]", struct.pack(">I", target)),
                       ("[0,target]", struct.pack(">II", 0, target)),
                       ("[target,0]", struct.pack(">II", target, 0))):
        got = query(sock, ser, uid, 2303, body, wait=1.5)
        hit = [g for g in got if g[0] == 2303]
        print(f"  {desc:14s} → {'%dB' % len(hit[0][2]) if hit else '无回包'}")

    print("\n── 2051 / 2052 ——")
    for cmd, nm in ((2051, "GET_SIM_USERINFO"), (2052, "GET_MORE_USERINFO")):
        body = struct.pack(">I", target)
        got = query(sock, ser, uid, cmd, body, wait=2.0)
        hit = [g for g in got if g[0] == cmd]
        if hit:
            b = hit[0][2]
            cn = "".join(c for c in b.decode("utf-8", "ignore") if "\u4e00" <= c <= "\u9fff")
            print(f"  {cmd} {nm:20s} → {len(b)}B 「{cn[:20]}」")
        else:
            print(f"  {cmd} {nm:20s} → 无回包")

    sock.close()


def main():
    global DEBUG
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    # 服务账号：从环境变量读，别把号写进代码
    #   Windows: set SEER_ACCOUNT=你的米米号 & set SEER_PASSWORD=密码
    #   Linux:   export SEER_ACCOUNT=... SEER_PASSWORD=...
    account = os.environ.get("SEER_ACCOUNT", "")
    password = os.environ.get("SEER_PASSWORD", "")
    if not account or not password:
        print("缺服务账号：请先设 SEER_ACCOUNT / SEER_PASSWORD 环境变量")
        print("（查战绩/阵容需要一个专用的赛尔号小号，同一账号只能一个在线）")
        return 1
    action = sys.argv[1]

    if action == "login":
        cmd_login(account, password)
    elif action == "pvp":
        cmd_pvp(account, password, int(sys.argv[2]) if len(sys.argv) > 2 else 258806935)
    elif action == "lineup":
        cmd_lineup(account, password, int(sys.argv[2]) if len(sys.argv) > 2 else 258806935)
    else:
        print(f"未知动作: {action}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
