"""查巅峰战绩 —— 微信小程序官方数据（getPvpInfo）。

★ **现状（2026-11-21）：没接进 bot，只是方法留档。**
  没有 `@llm_tool` 注册、没有配置项，bot 根本看不见它。
  为什么不接：接口没有目标号参数（只认 authority，只能查绑的那一个号），
  且 arena_spirit 是「携带」口径，不满足「查别人战绩」的需求。
  要接回去看 `_re/wechat/接入参考.md`；单跑本文件即可验证方法本体。

和 `seer_pvp` 的区别（**别混**）：

    seer_pvp   走游戏协议，给段位/星数/胜场，是自己账号的实时段位
    seer_record 走小程序 HTTP 接口，给**官方的场次统计**：总场次、胜场、
               以及 **arena_spirit 里每只精灵各自的场次/胜场**

接口（2026-11-20 实测，见 `_re/wechat/接口逆向结论.md`）：

    GET https://webevent.61.com/v2/?action[active]=SeerUnionWxMini
        &action[action]=getPvpInfo&gameId=2
    authorization: <authority>

★ 三条硬事实（都实测过，别再试探）：

    1. **没有目标米米号参数**。带 uid= / guid= / seerSUid= 一律被忽略，
       返回的还是 authority 绑的那个号 —— 所以只能查绑定的号。
    2. authority 是 isLogin（wx.login 的 code）换来的 UUID，**只能用微信客户端换**，
       bot 换不了。所以 authority 走配置/环境变量，过期了要人工重抓。
    3. 2026-11-21 实测：抓到的 authority 隔了一天半**仍然有效**。
       有效期未知 → 失败信息里要把「可能要重抓」写在第一条。

★ 数据口径（还没定死的，展示时别下结论）：

    arena_spirit 固定返回 6 个槽位，场次各不相同（261/236/218/201/118），
    相加 1295 ≫ 总场次 295。现有数据**区分不了**两种语义：
        (a) 只记实际出场的那几只
        (b) 记带入阵容的 6 只，但只返回「用得最多的前 6 只」，其余被截掉
    所以措辞是「带 X：n 场 p%」，不写「出场」「最强」「最弱」。

    零值字段是**没被填充**，不是「恰好为零」：
    145 胜却 arena_maxWinStreak=0、295 场却 arena_maxRound=0 → 这条链路上没填。
    maxHarm / minRound / maxRound / maxWinStreak / arena_gameTrend 一律不展示。

★ season 数组**会返回多项**（2026-11-21 实测：当前季 12 + 上一季 11），
  交接文档里「只返回一项」是错的；展示当前季，其余各给一行场次摘要。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API = (
    "https://webevent.61.com/v2/?action%5Bactive%5D=SeerUnionWxMini"
    "&action%5Baction%5D=getPvpInfo&gameId=2"
)

# 原样抄抓包里的 UA / referer：少了 xweb_xhr 和 referer 服务端直接回「系统错误」
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541f38) XWEB/25819"
)
REFERER = "https://servicewechat.com/wx89f93352629f9554/27/page-frame.html"

# 请求头必须齐：缺 xweb_xhr / referer 时服务端返回「系统错误」（200 但是 4 个汉字）
HEADERS = {
    "accept": "application/json",
    "xweb_xhr": "1",
    "content-type": "application/json",
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "referer": REFERER,
    "accept-language": "zh-CN,zh;q=0.9",
}

# 判「模板全 0」用的字段：这些全 0 就当没数据，**不能**当真实战绩展示
_TEMPLATE_ZERO_KEYS = ("fight", "win")


class RecordError(Exception):
    """三种失败必须分开：net（网络/HTTP）/ api（result!=0）/ empty（模板全 0）。"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass
class SpiritStat:
    monster_id: int
    fight: int
    win: int
    name: str = ""

    @property
    def win_rate(self) -> float | None:
        return (self.win / self.fight * 100.0) if self.fight > 0 else None


@dataclass
class SeasonStat:
    season_id: int
    level_id: int = 0
    fight: int = 0
    win: int = 0
    spirits: list[SpiritStat] = field(default_factory=list)
    wild_fight: int = 0
    wild_win: int = 0

    @property
    def win_rate(self) -> float | None:
        return (self.win / self.fight * 100.0) if self.fight > 0 else None


@dataclass
class PvpRecord:
    nick: str = ""
    seasons: list[SeasonStat] = field(default_factory=list)
    fetched_at: float = 0.0
    # 缓存里的旧值被拿来兜底时，这里放旧值的时间戳
    stale_at: float = 0.0
    anomalies: list[str] = field(default_factory=list)

    @property
    def current(self) -> SeasonStat | None:
        return self.seasons[0] if self.seasons else None


# ── 网络 ────────────────────────────────────────────────────────────


def fetch_raw(authority: str, timeout: float = 15.0) -> dict:
    """打一次接口，返回整包 JSON。失败抛 RecordError。"""
    authority = (authority or "").strip()
    if not authority:
        raise RecordError("api", "authority 为空")
    req = urllib.request.Request(API, method="GET")
    req.add_header("authorization", authority)
    req.add_header("user-agent", UA)
    for k, v in HEADERS.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RecordError("net", f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001  URLError / timeout / ssl 都算网络类
        raise RecordError("net", f"{type(e).__name__}: {e}") from e

    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        # 明文「系统错误」也走这条：多半是请求头不全或 authority 失效
        raise RecordError("api", f"非 JSON：{body[:40]}") from None
    if not isinstance(obj, dict):
        raise RecordError("api", "返回不是对象")
    if obj.get("result") != 0:
        raise RecordError("api", f"result={obj.get('result')}")
    return obj


def parse(obj: dict) -> PvpRecord:
    """把整包 JSON 拆成 PvpRecord。模板全 0 → RecordError('empty')。"""
    data = obj.get("data")
    pvp = data.get("pvpInfo") if isinstance(data, dict) else None
    if not isinstance(pvp, dict):
        raise RecordError("empty", "没有 pvpInfo")

    rec = PvpRecord(nick=str(pvp.get("nick") or ""), fetched_at=time.time())
    for s in pvp.get("season") or []:
        if not isinstance(s, dict):
            continue
        season = SeasonStat(
            season_id=int(s.get("season_id") or 0),
            level_id=int(s.get("arena_levelId") or 0),
            fight=int(s.get("arena_fight") or 0),
            win=int(s.get("arena_win") or 0),
            wild_fight=int(s.get("wild_fight") or 0),
            wild_win=int(s.get("wild_win") or 0),
        )
        for sp in s.get("arena_spirit") or []:
            if not isinstance(sp, dict):
                continue
            mid = int(sp.get("monster_id") or 0)
            if mid <= 0:
                continue
            season.spirits.append(
                SpiritStat(monster_id=mid, fight=int(sp.get("fight") or 0),
                           win=int(sp.get("win") or 0))
            )
        rec.seasons.append(season)

    if not rec.seasons:
        raise RecordError("empty", "season 为空")
    # 模板全 0：整季场次 0 且所有精灵场次 0 → 没数据，不是「战绩是 0」
    cur = rec.seasons[0]
    if all(getattr(cur, k) == 0 for k in _TEMPLATE_ZERO_KEYS) and not any(
        s.fight for s in cur.spirits
    ):
        raise RecordError("empty", "本赛季全 0（模板）")
    return rec


# ── 缓存（存原始 JSON，派生值读取时再算）────────────────────────────


def cache_key(authority: str) -> str:
    return hashlib.sha1((authority or "").strip().encode("utf-8")).hexdigest()[:12]


def _cache_path(cache_dir: str, authority: str) -> str:
    return os.path.join(cache_dir, f"wechat_pvp_{cache_key(authority)}.json")


def save_raw(cache_dir: str, authority: str, obj: dict) -> None:
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = _cache_path(cache_dir, authority) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "raw": obj}, f, ensure_ascii=False)
        os.replace(tmp, _cache_path(cache_dir, authority))
    except OSError:
        pass        # 缓存写不了不影响主流程


def load_raw(cache_dir: str, authority: str) -> tuple[dict, float] | None:
    path = _cache_path(cache_dir, authority)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    raw = blob.get("raw")
    if not isinstance(raw, dict):
        return None
    return raw, float(blob.get("ts") or 0.0)


def check_monotonic(new: PvpRecord, old: PvpRecord) -> list[str]:
    """同一 season_id 下 arena_fight 只增不减；变小就是异常。"""
    old_by_id = {s.season_id: s for s in old.seasons}
    out: list[str] = []
    for s in new.seasons:
        o = old_by_id.get(s.season_id)
        if o and s.fight < o.fight:
            out.append(f"第 {s.season_id} 季场次 {o.fight} → {s.fight}（变小了）")
    return out


# ── 展示 ────────────────────────────────────────────────────────────


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"{rate:.1f}%"


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def format_record(rec: PvpRecord, names: dict[int, str] | None = None) -> str:
    """给 bot 看的文字。

    规则（来自接口结论 §四之二，别改）：
      · 每只精灵写「带 X：n 场 p%」，不排名、不加「最强/最弱」
      · 零值字段不展示
      · 只展示一个口径的胜率（这里用自算的，和场次同源）
    """
    names = names or {}
    cur = rec.current
    lines: list[str] = []

    head = f"**{rec.nick}** 巅峰战绩（小程序官方）" if rec.nick else "巅峰战绩（小程序官方）"
    lines.append(head)
    if cur is None:
        return "\n".join(lines + ["· 没拿到赛季数据"])

    lines.append(
        f"· 本赛季（第 {cur.season_id} 季）：**{cur.fight} 场 · {cur.win} 胜 · "
        f"{_pct(cur.win_rate)}**"
    )
    if cur.fight:
        lines.append(f"· 负 {cur.fight - cur.win} 场")

    spirits = [s for s in cur.spirits if s.fight > 0]
    if spirits:
        lines.append("· 精灵场次（带 X：n 场 p%）")
        for s in spirits:
            name = names.get(s.monster_id) or f"ID {s.monster_id}"
            lines.append(f"   · {name}：{s.fight} 场 {s.win} 胜 {_pct(s.win_rate)}")

    if not cur.wild_fight:
        lines.append("· 野外：未参与")

    for s in rec.seasons[1:]:
        lines.append(f"· 第 {s.season_id} 季：{s.fight} 场 · {s.win} 胜 · {_pct(s.win_rate)}")

    stamp = rec.stale_at or rec.fetched_at
    if stamp:
        lines.append(f"（数据时间 {_fmt_ts(stamp)}）")
    if rec.stale_at:
        lines.append("（本次没拉到新数据，上面是缓存；auth 可能要重抓）")
    for a in rec.anomalies:
        lines.append(f"⚠ {a}（缓存没被覆盖）")
    return "\n".join(lines)


def resolve_names(ids, lookup) -> dict[int, str]:
    """ids → {id: 名字}；lookup(pid) -> name|None。查不到就缺项，别编。"""
    out: dict[int, str] = {}
    for pid in ids:
        try:
            name = lookup(int(pid))
        except Exception:  # noqa: BLE001
            name = None
        if name:
            out[int(pid)] = str(name)
    return out


if __name__ == "__main__":
    import sys

    auth = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SEER_WECHAT_AUTHORITY", "")
    obj = fetch_raw(auth)
    print(format_record(parse(obj)))
