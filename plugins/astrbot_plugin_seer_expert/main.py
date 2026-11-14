"""赛尔知识库插件：把 SeerAPI 本地库变成可调用的工具，查不到就让人补。"""
from __future__ import annotations

import hashlib
import re
import json
import os
import asyncio
import os
import shutil
import time
from datetime import datetime

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image as ImageComp
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .db import SeerKnowledge
from .data_sync import sync_all as sync_data
from .manual_sync import fetch_icons, sync as sync_manual
from .focus import clear_focus, is_active as focus_is_active, set_focus
from .formula import estimate, explain, explain_settle, settle, combo_damage, explain_combo

PLUGIN_NAME = "astrbot_plugin_seer_expert"


@register(
    PLUGIN_NAME,
    "大肥鱼",
    "赛尔号专家：精灵/技能/魂印/异常状态/属性克制/伤害估算，查不到就追问并记住答案。",
    "0.1.0",
)


class SeerExpertPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.cfg = config or {}
        data_root = get_astrbot_plugin_data_path()
        self.data_dir = os.path.join(data_root, PLUGIN_NAME)
        here = os.path.dirname(os.path.abspath(__file__))
        # 候补路径：工作目录不同时 get_astrbot_plugin_data_path() 可能指错地方
        candidates = [
            str(self._c("seer_db_path", "") or ""),
            os.path.join(data_root, "astrbot_plugin_seer_info", "seerapi.sqlite"),
            os.path.join(self.data_dir, "..", "astrbot_plugin_seer_info", "seerapi.sqlite"),
            os.path.join(here, "..", "..", "plugin_data", "astrbot_plugin_seer_info", "seerapi.sqlite"),
        ]
        seer_path = next(
            (c for c in candidates if c and os.path.isfile(os.path.abspath(c))),
            candidates[1],
        )
        self.data_dir = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
        if not os.path.isdir(self.data_dir):
            os.makedirs(self.data_dir, exist_ok=True)
        self._bg_tasks: set = set()
        # 配置图缓存：会话 key → 上一次生成用的全部参数（供 seer_config_tweak 增量修改）
        self._config_cache: dict[str, dict] = {}
        self.kb = SeerKnowledge(str(seer_path), os.path.join(self.data_dir, "kb.sqlite"))
        self._data_root = get_astrbot_plugin_data_path()
        if not self.kb.available():
            logger.warning(
                f"[赛尔知识库] 没找到 SeerAPI 本地库，尝试过：{candidates}"
            )
        else:
            logger.info(f"[赛尔知识库] 使用数据库：{seer_path}")
        # 代理守护：与 AstrBot 同生共死（不随开机自启）
        try:
            self._proxy_guard_task = asyncio.create_task(self._proxy_guard_loop())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[代理守护] 启动失败: {exc}")
        try:
            self._platform_guard_task = asyncio.create_task(self._platform_guard_loop())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[平台守护] 启动失败: {exc}")
        try:
            self._autologin_future = asyncio.create_task(self._autologin_task())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[自动登录] 启动失败: {exc}")
        # 游戏内实时查询（seer_pvp / seer_roster）
        # 服务账号同一时刻只能一个在线，两次查询撞一起会互相踢下线 —— 必须串行
        self._seer_io_lock = asyncio.Lock()
        # 米米号 → (时间戳, 结果)；60 秒内重复查询直接复用，少登一次是一次
        self._seer_cache: dict[str, tuple[float, dict]] = {}
        self._SEER_CACHE_TTL = 60.0
        # 手册库定时同步（manual_db 原先只由 bot 命令触发，没人发命令就永远不更新）
        try:
            self._manual_sync_task = asyncio.create_task(self._manual_sync_loop())
            self._data_sync_task = asyncio.create_task(self._data_sync_loop())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[手册同步] 定时任务启动失败: {exc}")

    # ---------------- 手册库定时同步 ----------------
    # 背景（2026-11-06）：manual_db 由 manual_sync.sync() 从「火の辞典」拉取，
    # 但原先唯一触发点是 bot 命令 cmd_sync_manual —— 没人发命令就永远不动。
    # 实测停在 11-03，而上游 11-05 的包里已经有「神魂之器」「神途之熠」，
    # 导致 seer_glossary 一连几天查不到新词条。
    # 这里补一个后台循环：启动后等 10 分钟跑第一轮（别跟启动抢资源），之后每 N 小时一轮。
    _MANUAL_SYNC_FIRST_DELAY = 600.0

    async def _manual_sync_loop(self):
        if not bool(self._c("manual_sync_enabled", True)):
            logger.info("[手册同步] 已禁用（manual_sync_enabled=false）")
            return
        try:
            hours = float(self._c("manual_sync_hours", 24) or 24)
        except (TypeError, ValueError):
            hours = 24.0
        interval = max(hours, 0.5) * 3600.0
        await asyncio.sleep(self._MANUAL_SYNC_FIRST_DELAY)
        while True:
            try:
                db_dir = os.path.join(self.data_dir, "manual_db")
                res = await sync_manual(db_dir)
                bad = [
                    f"{k}={v}" for k, v in res.items()
                    if any(x in v for x in ("失败", "丢弃", "HTTP"))
                ]
                if bad:
                    logger.warning(
                        f"[手册同步] {len(res) - len(bad)}/{len(res)} 成功，失败：{'｜'.join(bad)}"
                    )
                else:
                    logger.info(f"[手册同步] 完成 {len(res)}/{len(res)} 个库")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # 断网/上游挂了都是正常路径，打一条警告等下一轮，不要打死循环
                logger.warning(f"[手册同步] 本轮失败：{type(exc).__name__}: {exc}")
            await asyncio.sleep(interval)


    # ---------------- seerapi / aliases 定时同步 ----------------
    # 这两个库以前由第三方插件 astrbot_plugin_seer_info 维护，现在自己管，
    # 不再依赖它。上游地址和验货规则见 data_sync.py 头部注释。
    _DATA_SYNC_FIRST_DELAY = 180.0

    async def _data_sync_loop(self):
        """每 60 分钟比对一次上游 .sha256，有变化才下载。

        验货闸门不通过时整包丢弃、保留旧库 —— 上游曾漏发 34 张表
        （含 pet_special_effect），无校验直接覆盖会让专属名词查询静默失效。
        """
        await asyncio.sleep(self._DATA_SYNC_FIRST_DELAY)
        while True:
            try:
                # 下到「实际被读取的那个目录」。seerapi 可能在
                # plugin_data/astrbot_plugin_seer_info/ 下（见 __init__ 的 candidates），
                # 用 self.data_dir 会下到 expert 目录，下完也没人读。
                seer_dir = os.path.dirname(os.path.abspath(self.kb.seer_path))
                res = await sync_data(seer_dir)
                for k, v in res.items():
                    if any(x in v for x in ("failed", "失", "未通过", "丢弃", "跳过")):
                        logger.warning("[data_sync] %s -> %s", k, v)
                    else:
                        logger.info("[data_sync] %s -> %s", k, v)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[data_sync] round failed: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(60 * 60)
    # ---------------- 代理守护（直连不通时按需拉起 Clash Party） ----------------
    _GUARD_HOST = "oss-us.dimenoss.com"
    _GUARD_PORT = 443
    _PROXY_PORT = 7890
    _CLASH_LNK = (
        r"C:\Users\19552\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Clash Party.lnk"
    )

    async def _tcp_ok(self, host: str, port: int, timeout: float = 3.0) -> bool:
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _proxy_running(self) -> bool:
        return await self._tcp_ok("127.0.0.1", self._PROXY_PORT, timeout=2.0)

    def _start_clash(self) -> bool:
        try:
            if not os.path.isfile(self._CLASH_LNK):
                return False
            os.startfile(self._CLASH_LNK)  # noqa: S606
            return True
        except Exception:  # noqa: BLE001
            return False

    def _stop_clash(self) -> bool:
        try:
            import subprocess

            done = False
            for name in ("Clash Party.exe", "mihomo.exe"):
                r = subprocess.run(
                    ["taskkill", "/IM", name, "/F"],
                    capture_output=True,
                    timeout=10,
                )
                done = done or (r.returncode == 0)
            return done
        except Exception:  # noqa: BLE001
            return False



    # ---------------- 出站消息清洗：去掉模型漏出来的“代码样”标记 ----------------
    @staticmethod
    def _clean_outgoing(text: str) -> str:
        """出站文本清洗：把一切“代码样”残留清掉。

        meme_manager 的标记 &&分类&& 由它自己在更早阶段消费（换成图片并删掉标记），
        走到这里的标记说明没被消费，留着只会变成一串字符，所以一律删除。
        """
        if not text:
            return text
        import re as _re

        out = text
        out = _re.sub(r"&&[^&\r\n]{1,64}&&", "", out)      # &&sleep&&
        out = _re.sub(r"[&]{1,2}[A-Za-z_]{2,20}[&]{1,2}", "", out)  # &sleep& / &&x&
        out = _re.sub(r"\[CQ:[^\]]*\]", "", out)
        out = _re.sub(r"\[ComponentType\.[A-Za-z_]+\]", "", out)
        out = _re.sub(r"\[Component\.[A-Za-z_]+\]", "", out)
        out = _re.sub(r"<\|?[a-zA-Z_]{2,20}\|?>", "", out)
        out = _re.sub(r"[ \t]+\n", "\n", out)
        out = _re.sub(r"\n{3,}", "\n\n", out)
        out = _re.sub(r" {2,}", " ", out)
        return out.strip()

    @filter.on_decorating_result()
    async def clean_outgoing_markers(self, event: AstrMessageEvent):
        """发送前清洗文本，避免群友看到 &sleep& / CQ 码这种东西。"""
        try:
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return
            changed = False
            for comp in result.chain:
                text = getattr(comp, "text", None)
                if isinstance(text, str) and text:
                    new = self._clean_outgoing(text)
                    if new != text:
                        comp.text = new if new else "\u200b"
                        changed = True
            if changed:
                logger.info("[出站清洗] 已移除残留标记（&sleep& / CQ 码 / 组件标签）")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[出站清洗] 跳过: {exc}")


    # ---------------- 启动时自动登录 NapCat（账号密码） ----------------
    # 个人部署配置，不进仓库。需要时用环境变量给：
    #   NAPCAT_UIN           QQ 号
    #   NAPCAT_PWD_MD5_FILE  密码 md5 缓存文件路径
    #   NAPCAT_WEBUI_CFG     WebUI 配置文件路径
    # 未设置时自动登录不可用（autologin_enable 本来就默认 false）
    _AUTO_UIN = os.environ.get("NAPCAT_UIN", "")
    _PWD_MD5_FILE = os.environ.get("NAPCAT_PWD_MD5_FILE", "")
    _WEBUI_CFG = os.environ.get("NAPCAT_WEBUI_CFG", "")

    async def _port_open(self, port: int, timeout: float = 1.5) -> bool:
        try:
            fut = asyncio.open_connection("127.0.0.1", port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    def _read_webui_token(self) -> str:
        try:
            import json as _json

            d = _json.load(open(self._WEBUI_CFG, encoding="utf-8"))
            return str(d.get("token") or "")
        except Exception:
            return ""

    def _pwd_md5(self) -> str:
        try:
            return open(self._PWD_MD5_FILE, encoding="utf-8").read().strip()
        except Exception:
            return ""

    @staticmethod
    def _post_json(url: str, body: dict, token: str = "", timeout: int = 60) -> dict:
        import json as _json
        import urllib.request

        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            url, data=_json.dumps(body).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())

    def _napcat_login_blocking(self, port: int = 6099) -> tuple[bool, str]:
        """调 NapCat WebUI 接口做账号密码登录。返回 (成功?, 说明)。"""
        import hashlib

        token = self._read_webui_token()
        md5 = self._pwd_md5()
        if not token:
            return False, "读不到 WebUI token"
        if not md5:
            return False, "读不到密码 md5（G:/bot相关/napcat/.pwd_md5）"
        base = f"http://127.0.0.1:{port}/api"
        try:
            h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
            cred = self._post_json(f"{base}/auth/login", {"hash": h}).get("data", {}).get("Credential", "")
            if not cred:
                return False, "WebUI 凭证获取失败"
            st = self._post_json(f"{base}/QQLogin/CheckLoginStatus", {}, cred).get("data", {})
            if st.get("isLogin"):
                return True, "已处于登录状态"
            r = self._post_json(
                f"{base}/QQLogin/PasswordLogin",
                {"uin": self._AUTO_UIN, "passwordMd5": md5},
                cred,
            )
            if r.get("code") != 0:
                return False, f"密码登录失败：{r.get('message')}"
            data = r.get("data") or {}
            if data.get("needCaptcha"):
                return False, f"需要人工过验证码：{data.get('proofWaterUrl', '')}"
            return True, "密码登录成功"
        except Exception as exc:  # noqa: BLE001
            return False, f"调用异常：{exc}"

    async def _autologin_task(self):
        """AstrBot 启动后：若 OneBot 未连接，自动起 NapCat 并用账号密码登录。

        默认关闭（autologin_enable=false），避免和启动器/NapCat 面板抢着拉进程。
        """
        await asyncio.sleep(25)
        if not self._c("autologin_enable", False):
            logger.info("[自动登录] 未启用（autologin_enable=false），不拉进程")
            return
        try:
            if await self._ws_connected():
                logger.info("[自动登录] OneBot 已连接，跳过")
                return
            n = await self._napcat_count()
            if n == 0:
                logger.info("[自动登录] NapCat 未运行 → 拉起")
                self._start_napcat()
            else:
                logger.info(f"[自动登录] NapCat 已在运行（{n} 个实例），直接尝试登录")

            for _ in range(30):
                if await self._port_open(6099):
                    break
                await asyncio.sleep(2)
            else:
                logger.warning("[自动登录] WebUI 6099 未起来，放弃")
                return

            ok, msg = await asyncio.to_thread(self._napcat_login_blocking, 6099)
            if ok:
                logger.info(f"[自动登录] {msg}")
            else:
                logger.warning(f"[自动登录] 失败：{msg}")
                qr = self._export_qr()
                if qr:
                    logger.warning(f"[自动登录] 可扫码登录，二维码：{qr}")
            for _ in range(10):
                await asyncio.sleep(3)
                if await self._ws_connected():
                    logger.info("[自动登录] OneBot 已连接 ✅")
                    return
            logger.warning("[自动登录] 仍未连上 OneBot，等人工处理")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[自动登录] 异常：{exc}")

    # ---------------- 平台守护：NapCat 掉线自动重登 ----------------
    _NAPCAT_DIR = r"G:\bot相关\napcat"
    _NAPCAT_LOGDIR = r"G:\bot相关\logs"
    _ONEBOT_PORT = 6199

    async def _ws_connected(self) -> bool:
        """6199 上是否有 NapCat 连进来。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command",
                "(Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | "
                f"Where-Object {{ $_.LocalPort -eq {self._ONEBOT_PORT} -or $_.RemotePort -eq {self._ONEBOT_PORT} }} | "
                "Measure-Object).Count",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return int((out or b"0").strip() or 0) > 0
        except Exception:
            return True  # 查询失败时不轻举妄动

    async def _napcat_count(self) -> int:
        """当前有几个 napcat 进程（多开会互踢，必须保持 1 个）。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                "Where-Object { $_.CommandLine -like '*napcat*' } | Measure-Object).Count",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return int((out or b"0").strip() or 0)
        except Exception:
            return -1

    async def _napcat_alive(self) -> bool:
        n = await self._napcat_count()
        return True if n < 0 else n > 0

    async def _kill_napcat(self) -> None:
        try:
            import subprocess
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                "Where-Object { $_.CommandLine -like '*napcat*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=25)
        except Exception:
            pass

    def _start_napcat(self, uin: str = "2") -> None:
        """用快速登录拉起 NapCat（免扫码）；ticket 失效时它会退化成出二维码。"""
        try:
            os.makedirs(self._NAPCAT_LOGDIR, exist_ok=True)
            log = os.path.join(self._NAPCAT_LOGDIR, "napcat.log")
            import subprocess
            # with 保证异常路径也会关闭父进程这边的句柄：
            # 子进程已继承 fd，Popen 返回后父进程关闭自己的副本不影响它。
            with open(log, "ab") as log_fh:
                subprocess.Popen(
                    [os.path.join(self._NAPCAT_DIR, "node.exe"), "index.js", "-q", uin],
                    cwd=self._NAPCAT_DIR,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            logger.info("[平台守护] 已尝试拉起 NapCat（-q 快速登录）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[平台守护] 拉起 NapCat 失败: {exc}")

    def _export_qr(self) -> str:
        """把二维码复制到固定位置，方便人扫。"""
        try:
            src = os.path.join(self._NAPCAT_DIR, "napcat", "cache", "qrcode.png")
            if not os.path.isfile(src):
                return ""
            if time.time() - os.path.getmtime(src) > 300:
                return ""  # 超过 5 分钟的旧码不提醒
            dst = os.path.join(self._NAPCAT_LOGDIR, "napcat_qr.png")
            shutil.copy2(src, dst)
            return dst
        except Exception:
            return ""

    async def _platform_guard_loop(self):
        """平台守护（默认只观察不自动重启，避免多实例互踢）。

        要在配置里打开 platform_guard_enable=true 才会自动拉起/重启 NapCat。
        """
        await asyncio.sleep(45)
        if not self._c("platform_guard_enable", False):
            logger.info("[平台守护] 只观察模式（不自动重启）；需要自动重登请在插件配置开启 platform_guard_enable")
            while True:
                try:
                    ok = await self._ws_connected()
                    if not ok:
                        n = await self._napcat_count()
                        logger.warning(f"[平台守护] OneBot 未连接，NapCat 实例数={n}（需人工处理）")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(120)
        logger.info("[平台守护] 已启动（掉线自动重登）")
        fail_rounds = 0
        while True:
            try:
                ok = await self._ws_connected()
                if ok:
                    fail_rounds = 0
                else:
                    fail_rounds += 1
                    n = await self._napcat_count()
                    if n > 1:
                        logger.warning(f"[平台守护] 发现 {n} 个 NapCat 实例（会互踢）→ 清理后只留一个")
                        await self._kill_napcat()
                        await asyncio.sleep(4)
                        self._start_napcat()
                        await asyncio.sleep(25)
                    elif n == 0:
                        logger.warning("[平台守护] 检测到 OneBot 未连接且 NapCat 未运行 → 启动")
                        self._start_napcat()
                        await asyncio.sleep(25)
                    elif fail_rounds == 3:
                        # 进程活着但连不上（通常需要扫码）→ 只重启一次，避免反复互踢
                        logger.warning("[平台守护] OneBot 长时间未连接（进程在）→ 重启一次 NapCat")
                        # 只杀 napcat 相关进程，绝不 taskkill 全部 node.exe（会误杀 DSH 本体）
                        try:
                            import subprocess
                            ps = (
                                "$ids = Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                                "Where-Object { $_.CommandLine -like '*bot\\u76f8\\u5173\\napcat*' } | "
                                "Select-Object -ExpandProperty ProcessId; "
                                "foreach ($i in $ids) { Stop-Process -Id $i -Force -ErrorAction SilentlyContinue }"
                            )
                            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                           capture_output=True, timeout=20)
                        except Exception:
                            pass
                        await asyncio.sleep(3)
                        self._start_napcat()
                        await asyncio.sleep(25)
                    qr = self._export_qr()
                    if qr:
                        logger.warning(
                            f"[平台守护] 需要扫码登录，二维码已导出：{qr}（扫完即可恢复）"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[平台守护] 循环异常: {exc}")
            await asyncio.sleep(45)

    async def _proxy_guard_loop(self):
        """每 60 秒一次：直连不通→拉起代理；直连恢复→关掉自己拉起的代理。"""
        started_by_me = False
        direct_ok_streak = 0
        await asyncio.sleep(20)  # 等 AstrBot 启动完成
        logger.info("[代理守护] 已启动（随 AstrBot 运行）")
        while True:
            try:
                direct = await self._tcp_ok(self._GUARD_HOST, self._GUARD_PORT)
                if direct:
                    direct_ok_streak += 1
                    if started_by_me and direct_ok_streak >= 3 and await self._proxy_running():
                        if self._stop_clash():
                            started_by_me = False
                            logger.info("[代理守护] 直连已恢复，已关闭自己拉起的代理")
                else:
                    direct_ok_streak = 0
                    if not await self._proxy_running():
                        if self._start_clash():
                            started_by_me = True
                            logger.info("[代理守护] 直连不通 → 已拉起 Clash Party")
                            await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[代理守护] 循环异常: {exc}")
            await asyncio.sleep(60)

    def _db_missing(self) -> str:
        """本地库读不到时给一句明确的话，避免模型说成“连不上数据库”。"""
        if self.kb.available():
            return ""
        return (
            "本地 SeerAPI 库当前读不到（文件被占用、正在被覆盖或路径变更）。"
            "这不是网络问题，稍后再试；如果持续出现，请让管理员检查插件数据目录。"
        )

    def _c(self, key: str, default):
        value = self.cfg.get(key, default) if hasattr(self.cfg, "get") else default
        return default if value is None else value

    # ---------------- 专注模式 ----------------

    def _umo(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _focus_active(self, event: AstrMessageEvent) -> bool:
        return focus_is_active(self._data_root, self._umo(event))

    def _strip_patterns(self) -> list[str]:
        raw = str(self._c("focus_strip_patterns", "") or "")
        return [x.strip() for x in raw.splitlines() if x.strip()]

    SEER_HINTS = ("赛尔", "精灵", "魂印", "技能", "刻印", "宝石", "套装", "部件", "称号",
                  "属性", "克制", "配招", "伤害", "第五", "巅峰", "进化", "皮肤",
                  "表哥", "表姐", "帝皇之戎", "帝皇之御", "帝皇之钺", "空元", "失力", "抗性",
                  "暴击", "减伤", "增伤", "粉伤", "真伤", "固伤", "红伤", "时点", "先手")

    def _looks_seer(self, text: str) -> bool:
        t = text or ""
        return any(h in t for h in self.SEER_HINTS)

    @filter.on_llm_request()
    async def enforce_local_source(self, event: AstrMessageEvent, req=None):
        """赛尔号话题：禁止联网搜索/凭记忆，必须走本地库工具。"""
        if req is None or not self._c("strict_source", True):
            return
        msg = (getattr(event, "message_str", "") or "")[:300]
        try:
            chat_facts = self.kb.facts_for_chat(msg, 5)
        except Exception:
            chat_facts = []
        # 只要命中玩家自己教过的规则，就当作赛尔话题处理
        if not (self._looks_seer(msg) or self._focus_active(event) or chat_facts):
            logger.info(f"[赛尔注入] 跳过（非赛尔话题）msg={msg[:30]!r}")
            return
        logger.info(
            f"[赛尔注入] 进入 msg={msg[:36]!r} seer={self._looks_seer(msg)} "
            f"focus={self._focus_active(event)} facts={len(chat_facts)}"
        )
        try:
            from astrbot.core.agent.message import TextPart
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                logger.warning("[赛尔注入] extra_user_content_parts 为 None，注入失败")
                return
            pool_kw = [w for w in ("限二", "限三", "限零", "限2", "限3", "禁用池", "池子", "专家池", "点池") if w in msg]
            if pool_kw:
                pools = self.kb.pool_list()
                brief = "｜".join(f"{p['label']} {len(p['pets'])} 只" for p in pools)
                _pver = ""
                try:
                    _pver = self.kb.pool_data_version()
                except Exception:
                    _pver = ""
                _vline = f"（数据版本 {_pver}）" if _pver else ""
                pieces = [
                    f"【本条涉及巅峰池·概览】{brief}{_vline}",
                    "要具体名单就调 seer_pool(pool=…)；数据来自手册 pvp.db。"
                    "★ 档期/版本只能引用上面这个时间，库里没写的日期不许自己补；"
                    "pvp.db 的 ban_* 档期字段本来就是空的，说「未标注」即可。",
                ]
                for w in pool_kw:
                    hit = self.kb.pool_match(w)
                    if hit:
                        pieces.append(f"{hit['label']}名单（{len(hit['pets'])} 只）：" + "、".join(hit["pets"]))
                parts.append(TextPart(text="\n".join(pieces)))
            # 克制问句自动计算：「A打B几倍」直接算好塞进来。
            # 目的：模型对双属性克制极易凭记忆答错（最典型：自然/圣灵打电 答成 ×2，
            # 实际是双2特例 ×4）。工具虽然存在（seer_type_chart），但模型经常不调。
            try:
                _tq = self.kb.type_query_answer(msg)
            except Exception:
                _tq = ""
            if _tq:
                parts.append(TextPart(text=_tq))
                logger.info(f"[赛尔注入] 克制自动计算已注入 msg={msg[:30]!r}")
            # 特性默认值：玩家说「精神/强袭/坚硬」但没给星级时，按满配默认值算，
            # 免得模型自己瞎猜一个百分比。
            try:
                _trait_hits = [w for w in ("精神", "强袭", "坚硬") if w in msg]
            except Exception:
                _trait_hits = []
            if _trait_hits:
                _lines = ["【本条涉及特性·玩家没给星级时按下表默认值，别自己猜】"]
                if "精神" in _trait_hits:
                    _lines.append("· **精神**（特殊攻击伤害增加）：默认按**满星 8%** 计（星0~5 = 3/4/5/6/7/8%）。"
                                  "只对**特殊攻击**生效。")
                if "强袭" in _trait_hits:
                    _lines.append("· **强袭**（物理攻击伤害增加）：默认按**满星 8%** 计（星0~5 = 3/4/5/6/7/8%）。"
                                  "只对**物理攻击**生效。")
                if "坚硬" in _trait_hits:
                    _lines.append("· **坚硬**（受到任何伤害减少）：默认按 **10%** 计。"
                                  "**有 bug：星3 就封顶 10%，星4 反而更低、星5 也是 10%**；"
                                  "库内文案写的 12%/14% **不可采信**。")
                _lines.append(
                    "口径：**精神/强袭是「额外增伤」乘区**（作用在连击总伤之后、与通用增伤相乘，"
                    "不是相加进通用那一档）；**坚硬是「额外减伤」**，"
                    "与晨曦这类通用减伤**连乘**：总减伤 = 1−(1−通用)×(1−额外)。"
                    "例：晨曦10%(通用)+坚硬10%(额外) = 19%，不是 20%。"
                )
                parts.append(TextPart(text="\n".join(_lines)))
                logger.info(f"[赛尔注入] 特性默认值已注入 {_trait_hits} msg={msg[:26]!r}")
            # 「强能」是宝石、「强袭」是特性——只差一个字，模型极易混用。
            # 两者乘区也不同（通用 vs 额外），混了会算出完全不同的数。
            try:
                _q_name = "强能" in msg
                _x_name = "强袭" in msg or "精神" in msg
                _is_qn = _q_name or _x_name
            except Exception:
                _is_qn = False
            if _is_qn:
                _l = ["【先分清名字·「强能」和「强袭」不是一个东西】"]
                if _q_name:
                    _l.append("· **强能＝宝石**：自身处于能力提升状态时，攻击伤害 +5/10/15/20/25%（Ⅰ~Ⅴ，满级 **25%**）。"
                              "属**通用增伤**——与晨曦、爆伤等**相加**进同一乘区。需**真实**强化才触发（虚拟等级不算）。")
                if "强袭" in msg:
                    _l.append("· **强袭＝特性**：物理攻击伤害 +3/4/5/6/7/8%（星0~5，满星 **8%**）。"
                              "属**额外增伤**——与通用增伤**相乘**（独立乘区），且**只对物理攻击**生效。")
                if "精神" in msg:
                    _l.append("· **精神＝特性**：特殊攻击伤害 +3~8%，同样是**额外增伤**，只对**特殊攻击**生效。")
                _l.append("→ 口诀：**强能进通用（相加），强袭/精神进额外（相乘）**；一个管“伤害提升”，一个管“物理/特殊”限定。")
                parts.append(TextPart(text="\n".join(_l)))
                logger.info(f"[赛尔注入] 强能/强袭辨析已注入 msg={msg[:26]!r}")
            # 冰王（外号 48）「强化接先手」= 连击技：听到这个组合就按连击口径提示，
            # 免得模型把先手当成一次普通攻击（它有 90 次、翻倍后 180 次）。
            try:
                # 技能名本身就是唯一标识：凝寒止喧/傲视千川只有冰王有
                _cold = any(w in msg for w in ("冰王", "48", "冰魄", "柯尔德",
                                               "凝寒止喧", "傲视千川", "傲视"))
                _first = any(w in msg for w in ("先手", "凝寒止喧", "傲视", "强化接"))
                _is_cold_open = _cold and _first
            except Exception:
                _is_cold_open = False
            if _is_cold_open:
                parts.append(TextPart(text=(
                    "【冰王（外号 48）的「强化接先手」是连击技·必须按连击公式】\n"
                    "先手＝**凝寒止喧 #37809**：威力 **1**、物理、冰系、先制+3，"
                    "**连续攻击 90 次**，**自身处于真实能力提升状态时效果翻倍 → 180 次**。\n"
                    "强化＝**傲视千川 #26781**：全属性 **+1**（对手冰封时翻倍→+2）、"
                    "下 2 回合攻击伤害 **额外 +100%**（独立乘区 ×2）、下 2 回合全技能先制+2。\n"
                    "所以「傲视接先手」= 攻击先按等级换算（如 700×1.5=1050）→ 进 base（威力用 **1**）"
                    "→ ×本系1.5 → ×克制 → ×浮动 → ×**180 连击** → ×通用增伤（强能等）→ ×傲视 **×2**。\n"
                    "四处最易错：① 威力当 90 算（90 是**次数**不是威力）② 连击漏翻倍（用 90）"
                    "③ 攻击不按等级换算（用 700 而非 1050，少约 33%）"
                    "④ 傲视 100% 当通用增伤（它是**额外增伤独立乘区**，与强能**相乘**不是相加）。"
                )))
                logger.info(f"[赛尔注入] 冰王强化接先手（连击）已注入 msg={msg[:26]!r}")
            # 「视为 / 虚拟等级」自动注入：这类判定极易想当然。
            # 典型错法：看到「视为能力提升」就以为能吃强能宝石「处于能力提升状态」——
            # 实际虚拟等级不参与「处于…」判定（库里「虚拟等级的作用边界」条早有结论）。
            try:
                _is_xuni = any(w in msg for w in
                               ("视为", "当作", "算作", "视弱为强", "视强为弱",
                                "虚拟", "处于能力提升", "处于强化", "无视强化"))
            except Exception:
                _is_xuni = False
            if _is_xuni:
                parts.append(TextPart(text=(
                    "【本条涉及「视为/虚拟等级」·边界规则，别想当然】\n"
                    "虚拟能力等级（逆命之威、弥沦之镜、攻击时视弱为强 等）**只改数值计算**，"
                    "**不改状态判定、不改按级数结算**。\n"
                    "· 反例1（吃不到）：古龙逆命之威、冰王凝寒止喧的「视为强化」"
                    "**都不满足**强能宝石「自身处于能力提升状态」的判定 → 吃不到那 25%。\n"
                    "· 反例2（拿不走）：虚拟强化**也不会**被对手的消强 / 无视强化吃掉。\n"
                    "· 反例3（不按级算）：衔烛「每有1级能力下降吸45」只认**真实**弱化级数。\n"
                    "判据：写「视为/当作」≠ 真的处于该状态；要触发「处于…」类增益，必须是真的实际等级。"
                )))
                logger.info(f"[赛尔注入] 虚拟等级边界已注入 msg={msg[:30]!r}")
            # 连击问句自动注入：遇到「连击 / 几连 / X连击 / 连段」时，
            # 强行把新版公式与取整规则摆给模型，并禁止「威力×次数」这种错法。
            # 目的：模型口算连击几乎必错（实测会把威力×次数当单次高威力，低威力场景能差 60%）。
            try:
                _is_combo = any(w in msg for w in
                                ("连击", "几连", "连段", "二连", "三连", "四连", "五连",
                                 "2连", "3连", "4连", "5连", "多段", "连打"))
            except Exception:
                _is_combo = False
            if _is_combo:
                parts.append(TextPart(text=(
                    "【本条涉及连击·必须按新版连击公式，禁止口算近似】\n"
                    "公式：[(等级×0.4+2)×**面板威力**×攻击÷防御÷50+2]×本系×克制×浮动(217~255)/255"
                    "×暴击抗性×暴击系数×**连击次数**\n"
                    "取整：全部向下取整；方括号是整体不可拆；**浮动算完要取整，之后每步都 floor**。\n"
                    "三条硬纪律：\n"
                    "① **绝不能把「威力×次数」当一次高威力技能**——base 里的 +2 只加一次，"
                    "这样会系统性偏低（威力1×90段差 −60%，威力90×4段差 −1.5%）。\n"
                    "② **浮动只掷一次**，连击次数是最后乘的系数，不是每段各算各的再相加。\n"
                    "③ 玩家给了攻击/防御就**必须调 seer_damage(hit_count=N, attack_type=…, defend_type=…)** 实算，"
                    "不要自己凑数；没给参数就显式写「按 攻击X/防御Y 假设」。\n"
                    "连击次数口径：一般是「1 + 精灵先制等级」，上限看技能描述；"
                    "**别把先制数当连击数**（先制3 → 4 连）。"
                )))
                logger.info(f"[赛尔注入] 连击纪律已注入 msg={msg[:30]!r}")
            # 术语自动识别：用户话里出现官方词条名 → 自动附官方定义。
            # 目的：防止模型对「最优系别 / 未触发 / 之后 / 时点」这类词凭印象发挥。
            # 数据来自手册 glossary_entry（595 条），跳过单字与「待补」占位。
            try:
                _terms = self.kb.glossary_terms_in(msg, max_n=5)
            except Exception:
                _terms = []
            if _terms:
                _lines = ["【本条涉及的官方词条定义·一律以此为准，别凭印象解释】"]
                for _t in _terms:
                    _lines.append(
                        f"- {_t['name']}（{_t['kind_label'][:30]}）：{_t['desc'][:200]}"
                    )
                _lines.append(
                    "规则：① 上述定义与你记忆冲突时，**以词条为准**；"
                    "② 标了「专属名词·归一化描述」的（kind1）是**概要**，"
                    "判某只精灵的实际效果时**必须再去 seer_soulmark 读魂印原文**，"
                    "词条可能放大主体范围（例：宁界之律词条写「己方在场精灵」，"
                    "实际 <5 层只作用自身）；"
                    "③ 若玩家问的词条这里没出现，先调 seer_glossary 查一次再回答，"
                    "**不要说「库里没有」**——表名是 glossary_entry，别查成 glossary。"
                )
                parts.append(TextPart(text="\n".join(_lines)))
                logger.info(
                    f"[赛尔注入] 术语命中 {len(_terms)} 条："
                    + "｜".join(f"{x['name']}(k{x['kind']})" for x in _terms)
                )
            # 玩家之前教过的规则：按原话关键词 + 别名（表哥/表姐…）召回，跨群/私聊通用
            if chat_facts:
                pieces = [
                    "【玩家已确认的规则·本条相关（优先于你记忆与原文推断）】"
                ]
                # 注入预算制：facts 实测最长 1447 字、中位仅 120 字，
                # 所以按「总预算 + 单条上限」给，绝大多数条目可以完整注入。
                # 旧写法 `700 if idx < 2 else 260` 会把第 3 条起砍到 260 字，
                # 关键数值（如「对手最大体力50%」）常被拦腰截断，模型只能靠猜——
                # 实测出现过「追加【大招威力一半】的百分比伤害」这种编造。
                # 预算 8000：实测 6 条召回最坏合计约 6668 字，8000 可保证 0 截断；
                # 若用 6000 则有半数问题会把第 6 条砍到只剩百余字。
                _BUDGET = 8000
                _SINGLE_MAX = 1500
                used = 0
                truncated = False
                for idx, (t, c) in enumerate(chat_facts):
                    room = _BUDGET - used
                    if room <= 120:
                        truncated = True
                        break
                    cap = min(len(c), _SINGLE_MAX, room)
                    if len(c) > cap:
                        truncated = True
                        pieces.append(
                            f"- {t}：{c[:cap]}…【本条已截断】"
                        )
                    else:
                        pieces.append(f"- {t}：{c}")
                    used += cap
                if truncated:
                    pieces.append(
                        "注意：上面有规则被标了【本条已截断】或未完全展开。"
                        "**被截断的条目必须调 seer_recall 读全文再答，不准凭残句推测数值**；"
                        "尤其「百分比伤害」「最大体力X%」「消耗/吸取」「斩杀线」这类数字，"
                        "截断后极易被误读成别的意思。"
                    )
                pieces.append(
                    "用法：玩家说的「表哥/表姐/表亲」等代称，按上面规则直接答；"
                    "「表哥」=帝皇之钺(#3404)、「表姐」=帝皇之御(#3512)，这两个俗称已入 pet_alias，"
                    "可以正常去 seer_pet 查；要查帝皇之戎团体系另用 帝皇之戎(#4950)。"
                    "俗称查不到时按规则里点出的精灵名再查一次，不要说「库里翻不到」。"
                )
                pieces.append(
                    "【务必遵守·别把算例当前提】上面规则里的具体数字（血量、攻击、防御、"
                    "「受到 1000 固伤」「H=1200」等）绝大多数是**某组假设下的算例**，不是通用前提。"
                    "玩家没给这些参数时：①不要默认套用那组数字往下算；"
                    "②优先给公式与口径（如 实扣=量−floor(量×抗性)、月亮按实际恢复量、连击=单段×次数）；"
                    "③确实要给数就显式写出「按 X/Y/Z 假设」。"
                    "反过来，玩家给了具体数值就**用玩家的**，不要拿规则里的示例值覆盖。"
                )
                pieces.append(
                    "【别整段复述】以上是规则原文，留给你核对与算数用，**不要照抄进回复**。"
                    "群聊里按你自己的说话风格，压缩成一两句结论＋关键原因就够；"
                    "只有玩家明确要细节（「展开说」「给我算一遍」）时才铺开。"
                )
                rendered = "\n".join(pieces)
                parts.append(TextPart(text=rendered))
                # ★ 2026-11-09（用户反馈「还是需要提醒一下才反应过来」）：
                #   光把「整词不要拆」写进 seer_mintmark 的 docstring 不够 ——
                #   实测 bot 收到「火王冠名」还是拆成「火焰」「王」「火之狂野」去查，
                #   「火焰」会命中「火焰喷射刻印」，是**完全不同的印**，越查越偏。
                #   所以消息里只要出现「冠名」就直接把对照表塞进去，别让它猜。
                if "冠名" in msg:
                    parts.append(TextPart(text=(
                        "【刻印·冠名类别名】「XX冠名」是**完整的一个刻印别名**，"
                        "不是「XX」+「冠名」两个词，**别拆**。对照表：\n"
                        "  火王冠名=焚世之炎　水王冠名=沧海之笙　冰王冠名=寂杀之凌\n"
                        "  飞王冠名=拥风之羽　混飞冠名=黯辰之羽　草王冠名=繁馨之芳\n"
                        "  混地冠名=无间之岳　魂地冠名=悼亡之岳　邪灵王冠名=冥妖之寂\n"
                        "  秘战冠名=源生之契　古龙冠名=龙荒之息　暗冰冠名=寒渊之月\n"
                        "  火电冠名=灼雷之迅　圣逼冠名=圣耀之霆　暴逼冠名=轰天之霆\n"
                        "  普王冠名=不败之冕　次王冠名=源星之埃　混暗冠名=永噤之夜\n"
                        "查的时候 keyword 传整词（如 keyword=\"火王冠名\"）。"
                        "拆词会查到别的刻印——比如「火焰」会命中「火焰喷射刻印」，"
                        "那是完全不同的东西。整词查不到再说查不到，别自己造词重试。"
                    )))
                logger.info(
                    f"[赛尔注入] 已注入 {len(chat_facts)} 条规则（{used} 字"
                    f"{'，有截断' if truncated else '，全部完整'}）"
                    f" 示例标注={'有' if '【示例·非默认前提】' in rendered else '无'}"
                    f" 召回={'｜'.join(t[:16] for t, _ in chat_facts[:3])}"
                )
            # 原话里出现精灵名 → 自动附一张精简卡片，避免模型不查就编
            try:
                found = self.kb.pets_in_text(msg, 2)
            except Exception:
                found = []
            if found:
                cards = []
                for pid, pname in found:
                    try:
                        cards.append(f"- {pname}（#{pid}）：{self.kb.pet_card(pid)}")
                    except Exception:
                        continue
                if cards:
                    parts.append(TextPart(text=(
                        "【本条提到的精灵·本地库卡片】\n" + "\n".join(cards) +
                        "\n说明：以上是库里的原始字段，只准在此基础上回答；"
                        "库里没写的机制、倍率、概率、判定顺序一律不要补充或推测。"
                        "要更细的技能/魂印原文再调 seer_skill / seer_soulmark。"
                    )))
            hits = self.kb.term_hits(msg, 4)
            if hits:
                piece = "\n".join(f"- {h['name']}：{h['desc']}（{h['src']}）" for h in hits)
                parts.append(TextPart(text="【本条提到的基础概念·直接用这个定义】\n" + piece))
            # 问到「精灵王」→ 用第五技能「不微弱」判据直接核定，别让模型凭名字猜
            if found and any(k in msg for k in ("精灵王", "是不是王", "什么王", "王者", "王吗")):
                try:
                    _king_lines = []
                    for _pid, _pname in found[:2]:
                        _ks = self.kb.king_status(_pid)
                        if _ks.get("is_king"):
                            _why = "；".join(_ks.get("why") or []) or "第五带不微弱"
                            _king_lines.append(
                                f"- {_pname}（#{_pid}）是精灵王"
                                + (f"，身份={_ks['role']}" if _ks.get("role") else "")
                                + f"。依据：{_why}"
                            )
                        else:
                            _king_lines.append(
                                f"- {_pname}（#{_pid}）不是精灵王"
                                + ("（名册里也没有）" if not _ks.get("known") else "")
                                + "。依据：第五不带「伤害不会出现微弱」"
                            )
                    if _king_lines:
                        parts.append(TextPart(text=(
                            "【精灵王核定·按第五技能判据，直接采用】\n" + "\n".join(_king_lines)
                        )))
                except Exception:
                    pass

            parts.append(TextPart(text=(
                "【数据源纪律·赛尔号】本条涉及赛尔号，事实必须来自本地 SeerAPI 库："
                "先调 seer_pet / seer_skill / seer_moveset / seer_soulmark / seer_effect / seer_term 等工具。"
                "严禁调用联网搜索工具（如 grok_web_search），严禁凭训练记忆编造数值。"
                "属性克制倍率、伤害数值一律以 seer_type_chart / seer_damage 的返回为准，"
                "禁止自己在脑内做倍率合成（含免疫、双双克制这些特殊分支极易算错）。"
                "【算伤害·先套抗性】默认对手三项抗性点满 35%（暴击伤害 / 固定伤害 / 百分比伤害），"
                "调用 seer_damage 时不传抗性参数即按满抗性计算；"
                "抗性口径：实扣 = floor(伤害量×抗性)，余数保留（1 点粉伤过 35% 仍打出 1 点）；"
                "多段固伤/粉伤必须用 flat_segments / percent_segments 逐段传参、逐段过抗再相加，"
                "禁止先合计再乘一次抗性。回答里要先写明「按对手满抗性」这个前提。"
                "只有玩家明确说对手没点抗性时，才把三项抗性传 0 并在回答中说明。"
                "【体力前提】算百分比/固伤类伤害时，对手体力必须用玩家给的实际值（H）；"
                "玩家没给也没说「按1100算」时，先问一句对手多少血，"
                "禁止把之前例子里用过的1100体、238、137、163套到别的对手身上；换体力就重算 floor(H/3)、floor(H/8)。"
                "【通道清单】算伤害先把四条通道过一遍：红伤 / 固伤 / 粉伤 / 真伤，再加一条「每回合持续」（吸血、每回合固伤、异常伤害）按回合数累加；多回合连招要逐回合列，别只报红伤。"
                "【增减伤】增伤与减伤不许相减抵扣：先分别按通用/额外乘区算，总减伤 = 1-(1-通用)×(1-额外)，最后伤害 = 基础 × 增伤乘区 × (1-总减伤)；晨曦这类减伤归减伤侧，别当负增伤扣在增伤上。"
                "【进化形态优先】玩家只给基础名（尤纳斯/索伦森/谱尼这类）时，"
                "默认讲种族值最高的那个形态（原始黑洞·尤纳斯 / 混沌魔君索伦森 / 圣灵谱尼），"
                "不要拿最初版本当答案；确实要聊老版本时再说明区别。"
                "【通用名词先查词条】问「吸血/减伤/护盾/致命一击/烧伤」这类通用效果名，"
                "先调 seer_glossary 拿手册词条与语法模板；专属效果用 seer_effect。"
                "【术语/判定先搜规则库】遇到「未触发」「触发」「回合数」「取整」这类判定词，"
                "先调 seer_rules(keyword=那个词) 看玩家确认过的定义，再回答；"
                "不要在没搜的情况下说「库里没给定义」。"
                "工具返回「未收录」时，直接说明未收录并向玩家确认，不要改用搜索或猜测。"
                "【不熟悉的精灵·只表态不展开】遇到库里资料不全、或你本身没把握的精灵（例如希拓、星礼这类），"
                "不要自由发挥、不要编机制与数值、不要拼凑看似合理的判定顺序；"
                "有人问起就简单说一句自己的看法（定位/强不强/常见玩法印象），"
                "并说明「具体数据我没细看/库里不全」，把细节留给玩家补充。"
                "宁可少说，也不要为了显得懂而胡说——乱说会让群友反感。"
                "给配招建议时默认按「先手 / 强化 / 免控 / 150大招 / 第五技能」五格排，先查 seer_moveset 对号；空元这类有既定流派的按流派摆位走。"
            )))

            # ── 条件注入（2026-11-06 从固定底座下沉）─────────────────────
            # 原先把这几条塞在底座里，每轮都发，实测占底座 71%。
            # 下沉后底座从 2193 字降到 ~1360 字，简单问句一条都不命中。
            # 判断用完整 message_str：msg 被截到 300 字，长消息会漏判。
            _full = getattr(event, "message_str", "") or ""

            if any(k in _full for k in ("血色", "战之狂热", "战刀", "炸盾", "碎盾", "英卡洛斯")):
                parts.append(TextPart(text=(
                    "【战刀盾量】玩家说「血色和战之狂热都点了」时，护盾一律按 700 算（血色400 + 战之狂热6种强化×50=300），"
                    "炸盾 = 700吃盾 + 400碎盾 = 1100 固伤（过抗后按段算），并且必须额外计入碎盾效果（400固伤 + 流血80/回合）；"
                    "只有玩家只说点了血色时才按 400 盾、800 固伤。不许已点两层仍按400算，也不许漏碎盾。"
                )))

            if "晨曦" in _full:
                parts.append(TextPart(text=(
                    "【晨曦分档】算红伤时若对手是晨曦套：先看粉伤有没有把对手压到半血以下——"
                    "粉伤在红伤之前结算，压过半线则晨曦减伤由10%翻为20%，保底红按20%扣，否则只按10%。"
                    "这20%是从「粉伤先结算→对手过线→减伤翻倍」推出来的结果，不是玩家给定或默认套用的常数："
                    "禁止说成『按你说的20%减伤』『按默认20%算』，必须把因果链讲出来。"
                )))

            if "空元" in _full or "告命" in _full:
                parts.append(TextPart(text=(
                    "【空元】先确认「告命」有没有失力宝石；失力流派摆位＝二号叵告命（绑失力宝石）/ 三号乂免控 / 四号卌阅世 / 一号仐150 / 五号卪第五，"
                    "免控＝敕界归元、先手＝阅世、150／大招＝神忏福音之章、强化＝告命诗途。"
                "代指（150/130/85、免控/先手/强化/大招/吸血）要按该精灵技能表对号，别跨精灵套；无失力传统摆位只作参考。"
                    "失力宝石本身只挂一回合减伤、几乎无用，关键是 0 级弱化也能挂回合效果来触发告命锁切，所以一级就够。"
                )))

            if any(k in _full for k in ("战绩", "段位", "星数", "胜率", "胜场", "阵容", "出战", "待命", "米米号")):
                parts.append(TextPart(text=(
                    "【游戏内实时查询·两个工具绝不合并】问巅峰战绩/段位/胜率/打了几场 → 只调 seer_pvp；"
                    "问阵容/出战/待命/带了哪几只 → 只调 seer_roster。"
                    "两件事要分开问就分开答，禁止一次把两个工具都调、也禁止把战绩和阵容拼在同一条回复里。"
                    "群里没人 @我 时不要调这两个工具，只回「要查这个请 @我」。"
                )))
        except Exception as exc:  # noqa: BLE001
            # ★ 2026-11-04：原来是 logger.debug，导致 term_hits 缺失这种
            #   「整段注入静默失效」藏了很久没人发现（L789 抛异常后，
            #   基础概念、精灵王判定、以及整段数据源纪律全部被跳过）。
            #   改为 warning，下次再缺会直接在日志里看到。
            logger.warning(f"[赛尔知识库] 数据源纪律注入失败: {exc}")


    # ---------------- 私聊自动记规则：确定语句直接落 facts ----------------
    _RULE_VERBS = (
        "必须是", "必须", "就是", "算作", "按", "只认", "只看", "只算",
        "默认", "优先", "不能", "不算", "算", "规则", "定义", "定死",
        "记住", "确认", "定稿", "以后", "一律",
    )
    _QUESTION_HINTS = ("吗", "呢", "?", "？", "多少", "是不是", "有没有", "为什么", "怎么")

    @filter.on_llm_request()
    async def auto_fact_private(self, event: AstrMessageEvent, req=None):
        """私聊里玩家说出确定规则时，自动写入 facts（不额外调模型）。"""
        try:
            if not self._c("auto_fact_from_private", True):
                return
            umo = getattr(event, "unified_msg_origin", "") or ""
            if "FriendMessage" not in umo:
                return
            msg = (getattr(event, "message_str", "") or "").strip()
            if len(msg) < 25 or len(msg) > 600:
                return
            if any(q in msg for q in self._QUESTION_HINTS):
                return
            if not any(v in msg for v in self._RULE_VERBS):
                return
            # 去重：已有高度重合的正文就跳过
            if self.kb.fact_exists_by_content(msg[:40]):
                return
            topic = "私聊确认·" + msg[:18].replace("\n", " ")
            self.kb.remember(topic, msg, source="私聊自动记录")
            logger.info(f"[自动记规则] 已写入 facts：{topic}")
            try:
                from astrbot.core.agent.message import TextPart

                parts = getattr(req, "extra_user_content_parts", None)
                if parts is not None:
                    parts.append(TextPart(text=(
                        f"【已自动记入规则库】{topic}（内容按玩家原话保存）"
                        "。后续回答涉及同一机制时以此为准。"
                    )))
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[自动记规则] 跳过：{exc}")

    @filter.on_llm_request()
    async def focus_guard(self, event: AstrMessageEvent, req=None):
        """专注模式：压掉其他插件塞进来的噪音，并声明本轮只谈赛尔机制。"""
        if req is None or not self._focus_active(event):
            return
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            patterns = self._strip_patterns()
            if parts and patterns:
                kept = []
                for part in parts:
                    text = getattr(part, "text", "") or ""
                    if any(pat in text for pat in patterns):
                        continue
                    kept.append(part)
                req.extra_user_content_parts = kept
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None:
                from astrbot.core.agent.message import TextPart
                parts.append(TextPart(text=(
                    "【专注模式·赛尔机制】本轮只讨论赛尔号的机制与判定。"
                    "回答前先调用 seer_rules 看解析节点，用 seer_* 工具查事实；"
                    "原文里无法确定的条件不要猜，调用 seer_ask 记下并向玩家提问。"
                )))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[赛尔知识库] 专注模式注入失败: {exc}")

    @staticmethod
    def _split(text: str, size: int = 900) -> list[str]:
        """按行切块，避免 QQ 单条消息过长被截断。"""
        text = str(text or "")
        if len(text) <= size:
            return [text]
        chunks, buf = [], ""
        for line in text.splitlines(keepends=True):
            if len(buf) + len(line) > size and buf:
                chunks.append(buf.rstrip())
                buf = ""
            buf += line
            while len(buf) > size:
                chunks.append(buf[:size])
                buf = buf[size:]
        if buf.strip():
            chunks.append(buf.rstrip())
        return chunks or [text]

    def _build_moveset_detail(self, pet_name: str, skills: list[str], pet_id: int = 0) -> str:
        """把固定配招里每个技能的名字/威力/效果原文拼成一段，定型时存库。"""
        cn = {"atk": "攻击", "def": "防御", "sp_atk": "特攻", "sp_def": "特防", "spd": "速度", "hp": "体力"}
        lines = [f"【{pet_name}】定型配招（第五 1 格 + 普通技能）"]
        for i, sk in enumerate(skills, 1):
            # ★ 传 pet_id：解决重名技能认错（「虚妄幻境」有 3 个同名，
            #   艾夏拉的是 #26274 弹控版，不是魔钰那个 85 威力版）
            info = self.kb.skill_info(sk, pet_id=pet_id) or {}
            head = f"{i}. {sk}"
            # ★ 2026-11-06：定型配招也必须标先制/属性，
            #   否则「协议I·激(先制+3)」和「协议II·限(属性先制+1)」看起来一样
            bits = []
            if info.get("power"):
                bits.append(f"威力{info['power']}")
            else:
                bits.append("属性")
            if info.get("priority"):
                bits.append(f"先制{info['priority']:+d}")
            if info.get("pp"):
                bits.append(f"PP{info['pp']}")
            if info.get("accuracy"):
                bits.append(f"命中{info['accuracy']}")
            if bits:
                head += "（" + "，".join(str(b) for b in bits) + "）"
            lines.append(head)
            for eff in info.get("effects") or []:
                lines.append(f"   - {eff}")
        return "\n".join(lines)

    # ---------------- 学习辅助 ----------------

    def _is_private(self, event: AstrMessageEvent) -> bool:
        try:
            return not (event.get_group_id() or "")
        except Exception:
            return False

    def _rules_text(self) -> str:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.md")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return ""
        limit = int(self._c("rules_max_chars", 1200))
        return text[:limit]

    async def _summarize_bg(self, key: str, kind: str, raw: str, src_hash: str) -> None:
        """后台补摘要，失败只写日志。"""
        try:
            computed = await self._summarize(raw)
            if computed:
                self.kb.put_summary(key, kind, src_hash, computed)
                logger.debug(f"[赛尔知识库] 摘要已补齐: {key}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[赛尔知识库] 后台摘要失败 {key}: {exc}")

    async def _summarize(self, raw: str) -> str:
        provider = self.context.get_using_provider()
        if provider is None:
            return ""
        prompt = (
            "下面是赛尔号魂印/技能的原文与一份解析节点清单。请把它整理成便于判断的结构化结论。\n"
            "只输出 JSON：{\"trigger\":\"\",\"condition\":\"\",\"effect\":\"\","
            "\"stack\":\"\",\"pve\":\"\",\"unknown\":[\"\"]}\n"
            "要求：每项不超过 20 字；原文里已经写明的（含专属效果的独立定义）必须直接采用，"
            "只有真正缺失的才写进 unknown；禁止猜测；"
            "unknown 最多 3 条，写清要问人的问题。\n\n"
            f"【解析节点】\n{self._rules_text()}\n\n【原文】\n{raw[:1200]}"
        )
        try:
            resp = await provider.text_chat(prompt=prompt, contexts=[])
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[赛尔知识库] 总结失败: {exc}")
            return ""
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return ""
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return ""
        parts = []
        labels = {"trigger": "触发", "condition": "条件", "effect": "效果", "stack": "叠加", "pve": "PVE"}
        for k, label in labels.items():
            v = str(data.get(k) or "").strip()
            if v:
                parts.append(f"{label}={v}")
        unknown = data.get("unknown") or []
        if isinstance(unknown, list) and unknown:
            parts.append("存疑=" + "；".join(str(u)[:30] for u in unknown[:3]))
        return "；".join(parts)

    async def _learn_and_render(self, key: str, kind: str, raw: str, event: AstrMessageEvent) -> str:
        """返回原文；摘要只作为补充，绝不替代原文。

        之前的实现是「有摘要就直接返回摘要」，导致数据库里的原文被永久遮住，
        模型只能看到压缩后的几行字。现在改为：原文永远返回，摘要附在末尾。
        """
        src_hash = hashlib.sha1(raw.encode()).hexdigest()[:16]
        cached = self.kb.get_summary(key)
        summary = ""
        if cached:
            text, old_hash = cached
            if old_hash == src_hash:
                summary = text
            else:
                logger.info(f"[赛尔知识库] {key} 原文有更新，摘要作废")
                self.kb.delete_summary(key) if hasattr(self.kb, "delete_summary") else None
        self.kb.mark_seen(key, kind)

        # 摘要改为后台任务：不再阻塞本次返回（之前每次都要等一次模型调用）
        if not summary and self._c("summary_enabled", True):
            if not (self._c("summary_only_private", True) and not self._is_private(event)):
                if not any(t.get_name() == f"sum:{key}" for t in self._bg_tasks if not t.done()):
                    task = asyncio.create_task(self._summarize_bg(key, kind, raw, src_hash))
                    task.set_name(f"sum:{key}")
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)

        if summary:
            return raw + "\n【结构化摘要（仅供快速判断，细节以上方原文为准）】" + summary
        return raw

    # ---------------- 工具：精灵 ----------------

    @llm_tool(name="seer_pet")
    async def seer_pet(self, event: AstrMessageEvent, name: str) -> str:
        """查询赛尔号精灵资料：属性、种族值、魂印、主要技能、图鉴简介。

        Args:
            name(string): 精灵名，支持简称与模糊匹配，如「环源」「北鸟」
        """
        _miss = self._db_missing()
        if _miss:
            return _miss

        # 数据与判定都来自 pet_view（语义层，和 CLI 共用一份）。
        # 2026-11-03 之前这里和 cmd_pet 各写一份判定，改一处漏一处：
        # CLI 加了形态提示 bot 没有、bot 加了老形态警告 CLI 没有，两头都翻过车。
        v = self.kb.pet_view(name)
        if not v:
            return f"未收录精灵「{name}」。请先向在场玩家确认正式名字或编号，再用 seer_remember 记录。"

        sep = chr(10)
        pid = v["pid"]
        pets = v["candidates"]
        max_skills = int(self._c("max_skills", 8))
        lines = []

        if v["moveset"]:
            lines.append("已定型配招：" + "、".join(v["moveset"]))

        # 判定字段：语义层统一给出，两边都必须渲染
        for key in ("alias_chain_hint", "alias_note", "other_aliases_hint",
                    "form_hint", "exact_warn", "sparse_warn"):
            if v.get(key):
                lines.append(v[key])

        # 前两个形态并排（觉醒前/后对照）—— bot 侧特有的排版，只影响详略
        cn = {"atk": "攻击", "def": "防御", "sp_atk": "特攻",
              "sp_def": "特防", "spd": "速度", "hp": "体力"}
        for fpid, fpname in pets[:2]:
            both = self.kb.pet_stats_both(fpid)
            stats = both.get("awakened") or both.get("base") or {}
            groups = self.kb.pet_skills_grouped(fpid)
            skills = (groups["fifth_new"] + groups["fifth_old"]
                      + groups["new_skills"] + groups.get("lv0", []) + groups["legacy"])[:max_skills]
            lines.append(f"【{fpname}】#{fpid} 属性：{self.kb.pet_type(fpid) or '未知'}")
            if stats:
                label = "种族值（觉醒后）" if "awakened" in both else "种族值"
                lines.append(label + "：" + "　".join(f"{cn[k]} {stats.get(k)}" for k in cn))
                if "awakened" in both and "base" in both:
                    b = both["base"]
                    lines.append("觉醒前：" + "　".join(f"{cn[k]} {b.get(k)}" for k in cn))
            for f in (self.kb.pet_soulmark_forms(fpid) or []):
                tag = "当前生效" if f.get("primary") else "原始/参考"
                body = f["desc"] if f.get("primary") else f["desc"][:90] + "…"
                lines.append(f"魂印（{tag}·{f['form']}·{f['pve']}）：{body}")
            for u in (self.kb.pet_partner_upgrade(fpid) or []):
                lines.append(
                    f"羁绊强化「{u['group']}」（需{u['need']}只）："
                    f"原→{u['before'][:70]}｜后→{u['after'][:70]}"
                )
            expanded = []
            for m in (self.kb.pet_soulmarks(fpid) or []):
                for eff in (self.kb.mentioned_effects(fpid, m) or []):
                    expanded.append(f"{eff['name']}={eff['desc']}")
            if expanded:
                lines.append("魂印里提到的专属效果：" + "；".join(expanded))
            effects = self.kb.pet_effects(fpid)
            if effects:
                lines.append("专属效果：" + "；".join(f"{e['name']}={e['desc']}" for e in effects))
            if skills:
                # ★ Lv0 技能单独点名（2026-11-03）：
                #   Lv0 = 靠道具/兑换开启的后期补强（零时诳语、王·联盟审判…），
                #   混在普通技能里会被忽略，所以先单列再列其余。
                _lv0 = [s for s in skills if s.get("level") == 0]
                _rest = [s for s in skills if s.get("level") != 0]
                if _lv0:
                    lines.append(
                        "★ Lv0·道具开启的补强技能（重点看，容易漏）："
                        + "；".join(str(s["name"]) for s in _lv0)
                    )
                brief = "；".join(
                    f"{s['name']}({'PP' + str(s['pp']) if s['pp'] else ''}"
                    + (f"，威力{s['power']}" if s["power"] else "")
                    + ")"
                    for s in _rest
                )
                lines.append("技能（第五只带一个，一般带新）：" + brief)
            intro = self.kb.pet_intro(fpid)
            if intro:
                lines.append("简介：" + intro[:80])

        raw = sep.join(lines)
        result = await self._learn_and_render(f"pet:{pid}", "pet", raw, event)
        if v["pet_rules"]:
            rules = "；".join(
                f"{str(t).split('·')[-1]}={str(c)[:120]}" for t, c in v["pet_rules"]
            )
            result += sep + "【玩家已确认的规则】" + rules
        effects = self.kb.pet_effects(pid)
        if effects and "专属效果" not in result:
            result += sep + "专属效果：" + "；".join(
                f"{e['name']}={e['desc'][:90]}" for e in effects
            )
        return result


    @llm_tool(name="seer_skill")
    async def seer_skill(self, event: AstrMessageEvent, name: str) -> str:
        """查询技能详情：威力、命中、PP、先制、效果文本（含〔固有〕〔命中〕标记）。

        Args:
            name(string): 技能名，支持模糊匹配
        """
        _miss = self._db_missing()
        if _miss:
            return _miss
        # 基础信息与「查不到时说什么」都来自 skill_view（语义层，和 CLI 共用）
        v = self.kb.skill_view(name)
        if not v["found"]:
            return v.get("empty_hint") or f"未收录技能「{name}」。"

        parts = [f"【{v['name']}】{v['type']}系"]
        if v["power"]:
            parts.append(f"威力{v['power']}")
        if v["pp"]:
            parts.append(f"PP{v['pp']}")
        if v["accuracy"] is not None:
            parts.append(f"命中{v['accuracy']}")
        if v["priority"]:
            parts.append(f"先制{v['priority']}")
        if v.get("atk_num") and v["atk_num"] != 1:
            parts.append(f"攻击{v['atk_num']}次")
        if v["must_hit"]:
            parts.append("必中")
        if v["category"] and v["category"] != "?":
            parts.append(v["category"])
        crit = v.get("crit_rate")
        if crit == 6.25:
            parts.append("暴击率 1/16")
        elif crit and crit != 1:
            parts.append(f"暴击率 ×{crit}")
        head = "｜".join(parts)

        effects = v["effects"]
        body = ("\n效果：\n" + "\n".join(f"- {x}" for x in effects)) if effects else (
            ("\n效果：" + v["info"]) if v["info"] else "\n（该技能无附加效果文本）"
        )
        # 连击提示：句首那个数字经常不是最终次数
        _ch = v.get("combo_hint")
        if _ch:
            body += ("\n连击：句首 " + str(_ch["base"])
                     + ("（固定）" if _ch["fixed"] else "（**带修正，不能直接用**）")
                     + "\n  原文：" + _ch["text"])
            if _ch.get("warn"):
                body += "\n  ⚠ " + _ch["warn"]

        # 复合技能：本技能「拥有别的技能的效果」时，把被引用的技能一并列出。
        # 不做这步，模型只能看到一句「拥有 A、B、C 中前 N 个」，
        # 然后要去逐个调 seer_skill（7 个就是 7 次），实测极易漏查或只答前半句。
        _refs = v.get("referenced") or []
        if _refs:
            _kind = "（构造效果）" if any(r.get("via") == "构造" for r in _refs) else ""
            body += f"\n★ 本技能引用了 {len(_refs)} 个技能{_kind}："
            for _r in _refs:
                _pw = f"威{_r['power']}（按25%计）" if _r.get("power") else "威0"
                body += f"\n- {_r['name']} {_pw}"
            body += "\n⚠ 只继承「效果」与 25% 威力值，先制/命中/PP 这些技能属性不继承"
            body += ("\n★ 按原文「每获得1层」＝累计获得计数：获得过 N 层就永久拥有前 N 个，"
                     "层数每回合减半只影响神灵系/免PP/反伤，不回退已解锁的效果。")

        raw = ("【数据源】SeerAPI 本地库 " + str(v["name"]) + chr(10) + head + body)
        result = await self._learn_and_render(f"skill:{v['name']}", "skill", raw, event)
        words = [v["name"]] + [w for w in ("魇昧", "异常", "镜", "衍化", "先制") if w in (raw or "")]
        rules = self.kb.facts_search(" ".join(words), 5)
        if rules:
            result += chr(10) + "【玩家已确认的规则（优先于推断）】" + chr(10) + chr(10).join(
                f"- {t}：{c[:200]}" for t, c in rules
            )
        ann = self.kb.annotate_terms(raw)
        if ann:
            result += chr(10) + "【赛尔专门词·读原文必看】" + chr(10) + chr(10).join(ann)
        result += self._glossary_block(raw)
        return result


    def _glossary_block(self, raw: str, max_n: int = 4) -> str:
        """把文本里出现的官方词条定义拼成附加块（无命中时返回空串）。

        为什么不靠模型自觉：模型对「最优系别 / 未触发 / 之后」这类术语
        极易凭日常语感解释。这里由系统强制附上手册 glossary_entry 的官方定义。
        kind=1（专属名词）是归一化概要，可能与具体魂印原文有出入，故特别标注。

        ★ 2026-11-04 补回：本方法在某次 main.py 合并时连同定义一起丢失，
          但 seer_skill 等三处仍在调用，导致每次查技能都抛
          AttributeError: 'SeerExpertPlugin' object has no attribute '_glossary_block'，
          bot 只能对外说「库抽风了」。定义从 main.py.bak-beforemerge2 原样恢复。
        """
        try:
            terms = self.kb.glossary_terms_in(raw, max_n=max_n)
        except Exception:
            return ""
        if not terms:
            return ""
        lines = ["\n【官方词条定义·以此为准】"]
        for t in terms:
            lines.append(f"- {t['name']}（{t['kind_label'][:30]}）：{t['desc'][:200]}")
        if any(t["kind"] == 1 for t in terms):
            lines.append(
                "※ 上面标「专属名词·归一化描述」的是概要，**具体精灵以魂印原文为准**"
                "（词条可能放大主体范围）。"
            )
        return "\n".join(lines)

    @llm_tool(name="seer_moveset")
    async def seer_moveset(self, event: AstrMessageEvent, name: str) -> str:
        """查询精灵的配招参考：第五技能（新旧二选一）、本次新增技能、其余可选技能。

        Args:
            name(string): 精灵名，支持简称
        """
        pets = self.kb.find_pets(name)
        if not pets:
            return f"未收录精灵「{name}」。"
        pid, pname = pets[0]
        fixed = self.kb.get_moveset(pid)
        if fixed:
            detail = self.kb.get_moveset_detail(pid) or self._build_moveset_detail(pname, fixed, pid)
            return detail + (
                "\n（玩家已定型：这里只确定「带哪几个技能」，"
                "**不代表摆放位次** —— 位次按玩法定（空元尤其如此，失力流/免控流摆法不同），"
                "别自己排；要调整先问玩家）")
        g = self.kb.pet_skills_grouped(pid)

        def tag(x):
            """技能标签：先制/属性/威力/效果条数 —— 不加标记 LLM 看不出哪条是先手、哪条是白板"""
            bits = []
            if x.get("priority"):
                bits.append(f"先制{x['priority']:+d}")
            if not x.get("power"):
                bits.append("属性")
            if x.get("power"):
                bits.append(f"威{x['power']}")
            # ★ 效果条数（2026-11-06 玩家规则）：
            #   旧技能效果少于 3 条的基本不用看，标出来让 LLM 直接跳过，
            #   不用逐条读描述再判断。
            n_eff = len(x.get("effects") or [])
            bits.append(f"{n_eff}条" if n_eff else "白板")
            return x["name"] + (f"({'·'.join(bits)})" if bits else "")

        # ═══════════════════════════════════════════════════════════
        # 配招模板规则（2026-11-06）
        # 常规 5 格：先手 / 强化 / 免控(弹控) / 大招 / 第五
        # 觉醒精灵的规律：新第五 + 新先手 + 新属性 + 新大招，替换 4 个，
        # 旧技能只留 1 个可选。所以判断「该留哪个旧技能」= 看觉醒四件套
        # 缺哪一格，再从旧技能里补。
        # ═══════════════════════════════════════════════════════════
        FUNC_PAT = {
            "免控": [r"免疫(?:并反弹)?(?:所有)?(?:受到的)?异常", r"免疫所有异常", r"免疫异常"],
            "弹控": [r"(?:免疫并)?反弹(?:所有)?(?:受到的)?异常"],
            # ★ 2026-11-06 玩家澄清：「次免」只算**无条件**的那种
            #   （混元天尊「虚空桎梏」/ 弑序神罗「命轮无常」：属性词条直接写
            #    「免疫下2次自身受到的异常状态」），
            #   **有条件触发的不算免控词条**：
            #     「消除成功则己方免疫下1次受到的异常状态」← 要消成功
            #     「未触发则免疫下1次受到的攻击伤害」       ← 要前一条没中
            #     「反转成功则己方免疫下1次受到的异常状态」 ← 要反转成功
            #   所以「次免」不放进正则表，改用 funcs() 里的条件判断（见下）。
            "弱免": [r"免疫(?:能力下降|受到的?弱化|所有能力下降)"],
            "强化": [r"全属性\+(?:1|2)", r"全属性提升",
                     # ★ 2026-11-08 补：游戏有两种写法，原来只认第一种
                     #   简写「全属性+1」
                     #   完整「技能使用成功时，100%改变自身特攻等级+2」← 中间有「等级」
                     #   漏了完整写法 → 拂晓兔「永不分离」（改特攻+2/防御+1/特防+1）
                     #   被当成没有强化，推荐五格判「强化=缺」。
                     r"(?:攻击|防御|特攻|特防|速度|命中)(?:等级)?\+(?:1|2)"],
            "吸血": [r"吸取对手(?:最大)?体力", r"吸取对手最大体力的"],
            "回血": [r"恢复自身(?:最大)?体力", r"恢复自身全部体力"],
            "控制": [r"(?:令|使)对手[^，。；]{0,8}(?:睡眠|麻痹|害怕|疲惫|石化|冰冻|"
                     r"烧伤|中毒|混乱|寄生|诅咒|空定)"],
        }

        # 无条件次免的判定：找「免疫下N次」，看它前面 12 个字里有没有条件词。
        # ★ 2026-11-06 玩家规则 —— 只有无条件的算免控：
        #     混元天尊「虚空桎梏」：免疫下2次自身受到的异常状态   → 算
        #     弑序神罗「命轮无常」：免疫下2次自身受到的异常状态   → 算
        #     瀚宇星皇「瀚空之门」：…；己方免疫下2次受到的异常状态；… → 算（前面是分号）
        #   有条件的**不算**：
        #     消除成功则…免疫下1次 / 未触发则…免疫下1次 / 反转成功则…免疫下1次
        _COND_WORDS = ("则", "若", "成功", "失败", "未触发", "低于", "高于", "消耗", "命中后")

        def _has_uncond_cimian(e: str) -> bool:
            import re as _re
            # ★ 2026-11-08 修：必须后面跟「异常」才算免控。
            #   原来只匹配「免疫下N次」，把「免疫下1次对手的攻击」也算进去了 ——
            #   那是免**伤害**，不是免**异常**。
            #   实例：灵巢「无念归空净」〔固有〕技能无效时免疫下1次对手的攻击
            #        → 被误判成「次免」，导致它被当成免控技。
            for m in _re.finditer(r"免疫下?\d?次[^，。；]{0,8}异常", e):
                prefix = e[max(0, m.start() - 12):m.start()]
                if not any(w in prefix for w in _COND_WORDS):
                    return True
            return False

        def funcs(x):
            """给一条技能打功能标签

            ★ 2026-11-08 修「强化」假阳性：
              「令对手全属性+1」是**给对手强化**（负面效果），不是自己的强化。
              实例：灵巢「无念归空净」〔固有〕…免疫成功则令对手全属性+1
                    → 被误判成「强化」，跟真正的强化技「魂殒若梦」抢位置。
            """
            import re as _re
            hit = set()
            for e in (x.get("effects") or []):
                for name, pats in FUNC_PAT.items():
                    if name == "强化":
                        # ★ 必须用 pats（FUNC_PAT["强化"]），不能硬编码正则 ——
                        #   2026-11-08 就在这里踩过：FUNC_PAT 补了「…等级+2」的完整写法，
                        #   这里还是老的 `全属性\+(?:1|2)|全属性提升`，
                        #   结果拂晓兔「永不分离」仍然判成没强化。
                        for _p in pats:
                            if _re.search(_p, e):
                                head = e[max(0, e.find("+") - 12):e.find("+")]
                                if "对手" in head:
                                    continue      # 给对手的，不算
                                hit.add("强化")
                                break
                        continue
                    if any(_re.search(p, e) for p in pats):
                        hit.add(name)
                # 无条件次免单独判（不能用纯正则，要看上下文条件词 + 必须是「异常」）
                if _has_uncond_cimian(e):
                    hit.add("次免")
            return hit

        # 觉醒核心（第五 + Lv0 + 进阶）覆盖了什么
        core = []
        core += g.get("fifth_new") or []
        core += g.get("lv0") or []
        core += g.get("new_skills") or []
        covered = set()
        slots = set()
        # ★ 2026-11-06 玩家定：**不要用威力判大招**，按词条数排。
        #   理由（实测）：
        #   · 爱夏拉：移形换影 威140 但只 1 条效果（辅助），魅影清歌 威130 有 2 条，
        #     净灵咒 威90 先制+3 有 4 条——按威力会把辅助当大招。
        #   · 爱夏拉的「先手」有两个（净灵咒 4条 / 灵剑封魂 2条），都是先制+3，
        #     只能靠词条数分主次。
        #   · 有 0 威力属性技、低威力先手、高威力但白板技能混在一起，威力完全不可靠。
        #
        # ★ 攻击/属性 用 category_id 区分（1=物理 2=特殊 4=属性），
        #   不能用 power>0 —— 变威力技能威力显示为 0 但仍是攻击技能。
        def is_atk(x):
            return (x.get("category_id") or 0) in (1, 2)

        core_atk = [x for x in core if is_atk(x) and not x.get("fifth")]
        core_all_uniq = {x["id"]: x for x in core}
        for x in core_all_uniq.values():
            if x.get("fifth"):
                slots.add("第五")
            if "强化" in funcs(x):
                slots.add("强化")
            if "免控" in funcs(x) or "弹控" in funcs(x) or "次免" in funcs(x):
                slots.add("免控/弹控")
        # 先手：先制>=3 的攻击技能（可能多条，只要有一条就算占位）
        if any((x.get("priority") or 0) >= 3 for x in core_atk):
            slots.add("先手")
        # 大招：攻击技能里 age 排除先手(priority>=3) 之后，词条数最多的那条
        #   ★ +1/+2 先制的也算大招候选（玩家 2026-11-06 明确）
        big_atk = [x for x in core_atk if (x.get("priority") or 0) < 3]
        if big_atk:
            best = max(big_atk, key=lambda x: (len(x.get("effects") or []), x.get("power") or 0))
            if len(best.get("effects") or []) >= 3:
                slots.add("大招")
        missing = [s for s in ("先手", "强化", "免控/弹控", "大招", "第五") if s not in slots]
        # ★ 只有「有觉醒」的精灵才出五格模板（2026-11-06）
        #   模板的语义是「觉醒四件套覆盖了哪几格，缺哪格从旧技能补」。
        #   没觉醒的精灵（lv0 / new_skills 都空）不存在这个前提，
        #   硬套会把 legacy 里的先手/强化全判成缺失，反而误导。
        #
        # ★ 2026-11-08 修（配招规则.md 边界③）：原来只看「lv0/new_skills 非空」，
        #   但核心只有 2 个的精灵（战之狂热·英卡洛斯 = 第五 + 虐饕伏苍斩）也会触发，
        #   于是被判「❌先手 ❌强化 ❌免控」，实际它靠旧技能和魂印 —— 纯误报。
        #   改成：觉醒核心（第五+lv0+进阶新增，去重）**≥3 个**才出模板。
        _core_n = len(core_all_uniq)
        has_awaken = _core_n >= 3

        # ★ 一格多用（配招规则.md 边界②）
        #   沧岚「水月幽溟」= 全属性+1（强化）+ 免疫并反弹异常（免控/弹控），
        #   一个技能顶两格。不标出来会以为五格是 5 个技能填的，实际只用 4 个。
        _multi_skill = []
        for x in core_all_uniq.values():
            _f = funcs(x)
            _n = 0
            if "强化" in _f:
                _n += 1
            if ("免控" in _f or "弹控" in _f or "次免" in _f):
                _n += 1
            if _n >= 2:
                _multi_skill.append(x["name"])

        lines = [f"【{pname}】配招参考"]
        _remain_slots = 5          # 五格之外还剩几个技能位（无觉醒时按 5 算）
        if has_awaken:
            lines.append(
                "【五格模板】" + "  ".join(
                    ("✅" + s) if s not in missing else ("❌" + s)
                    for s in ("先手", "强化", "免控/弹控", "大招", "第五")
                )
            )
            if missing:
                note = ""
                if "大招" in missing:
                    note = "（该精灵没有 140+ 的非第五攻击技能时，第五兼大招位，不用硬补）"
                lines.append(f"【缺口】{'、'.join(missing)} → 优先从旧技能里补这几项{note}")
            else:
                lines.append("【缺口】无，五格已满，旧技能只按手感微调")
            # ★ 一格多用提示（边界②）
            if _multi_skill:
                lines.append(
                    f"⚠ {'、'.join(_multi_skill)} 一个技能同时顶多格 —— "
                    f"别当成两个技能；五格看着满，实际技能位还有富余")
            # ★ 剩余技能位建议：五格覆盖不到的功能（回血/吸血/控制/弱免）
            #   判定依据：先看 core 已经占了哪些 FUNC_PAT 标签，没被占的才推荐。
            #   实例：沧岚五格里「回血/护盾」没人管 → 第 5 格带沧海永存（补控/续航/护盾）。
            _covered = set()
            for x in core_all_uniq.values():
                _covered |= funcs(x)
            _gap_funcs = [f for f in ("回血", "吸血", "控制", "弱免") if f not in _covered]
            if _gap_funcs:
                _own_sk = [x for v in g.values() for x in v if (x.get("owners") or 1) == 1]
                _sugg = []
                for fn in _gap_funcs:
                    _h = [x for x in _own_sk
                          if fn in funcs(x) and not x.get("fifth")
                          and x["id"] not in core_all_uniq]
                    _h.sort(key=lambda x: -len(x.get("effects") or []))
                    if _h:
                        _sugg.append(f"{fn}→{_h[0]['name']}({len(_h[0].get('effects') or [])}条)")
                if _sugg:
                    lines.append("【剩余技能位建议】按没被覆盖的功能挑（独占·词条多的优先）："
                                 + "；".join(_sugg))

            # ★ 推荐五格（2026-11-08）：直接把「该带哪几条」列出来。
            #   群友问「该带哪五个」时，✅/❌ 不如名字直接。
            #   缺口处理：大招缺但第五威力 >=140 → 标「第五兼」，不算真缺。
            _own_pool = [x for v in g.values() for x in v if (x.get("owners") or 1) == 1]
            _pools = (list(core_all_uniq.values()), _own_pool)
            _is_atk2 = lambda x: (x.get("category_id") or 0) in (1, 2)

            def _first(pred):
                for pool in _pools:
                    c = sorted([x for x in pool if pred(x)],
                               key=lambda x: -len(x.get("effects") or []))
                    if c:
                        return c[0]
                return None

            _chosen = {}
            if g.get("fifth_new"):
                _chosen["第五"] = g["fifth_new"][0]
            _sp2 = _first(lambda x: (x.get("priority") or 0) >= 3 and _is_atk2(x))
            if _sp2:
                _chosen["先手"] = _sp2
            _big2 = _first(lambda x: _is_atk2(x) and not x.get("fifth")
                           and (x.get("priority") or 0) < 3
                           and len(x.get("effects") or []) >= 3)
            if _big2:
                _chosen["大招"] = _big2
            _qh2 = (_first(lambda x: "强化" in funcs(x) and (x.get("category_id") or 0) == 4)
                    or _first(lambda x: "强化" in funcs(x)))
            if _qh2:
                _chosen["强化"] = _qh2
            _mk2 = (_first(lambda x: bool({"免控", "弹控", "次免"} & funcs(x))
                           and (x.get("category_id") or 0) == 4)
                    or _first(lambda x: bool({"免控", "弹控", "次免"} & funcs(x))))
            if _mk2:
                _chosen["免控"] = _mk2

            _fifth_pw = (g["fifth_new"][0].get("power") or 0) if g.get("fifth_new") else 0
            _parts = []
            for _s in ("先手", "强化", "免控", "大招", "第五"):
                _x = _chosen.get(_s)
                if _x:
                    _parts.append(f"{_s}={_x['name']}")
                elif _s == "大招" and _fifth_pw >= 140:
                    _parts.append("大招=第五兼（威%d）" % _fifth_pw)
                else:
                    _parts.append(f"{_s}=缺")
            _in_use = len({id(x) for x in _chosen.values()})
            _remain_slots = max(0, 5 - _in_use)
            lines.append("【推荐五格】" + " / ".join(_parts))
            _miss_real = [s for s in ("先手", "强化", "免控") if s not in _chosen]
            if _miss_real:
                # ★ 真缺时直接给候选（2026-11-08）：只提示「缺」没用，要能挑
                _gap_pick = []
                for _s2 in _miss_real:
                    if _s2 == "强化":
                        _pred2 = lambda x: "强化" in funcs(x)
                    elif _s2 == "免控":
                        _pred2 = lambda x: bool({"免控", "弹控", "次免"} & funcs(x))
                    else:
                        _pred2 = lambda x: (x.get("priority") or 0) >= 3 and _is_atk2(x)
                    _c2 = [x for x in _own_pool
                           if _pred2(x) and (x.get("category_id") or 0) == 4]
                    if not _c2:
                        _c2 = [x for x in _own_pool if _pred2(x) and not x.get("fifth")]
                    _c2.sort(key=lambda x: -len(x.get("effects") or []))
                    if _c2:
                        _gap_pick.append(
                            f"{_s2}→{_c2[0]['name']}({len(_c2[0].get('effects') or [])}条)")
                lines.append(
                    f"（真缺：{'、'.join(_miss_real)}"
                    + (f" —— 候选：{'；'.join(_gap_pick)}" if _gap_pick else " —— 库里没有可补的")
                    + f"；已用 {_in_use} 个技能位）")
            else:
                lines.append(f"（已用 {_in_use} 个技能位，游戏共 5 格"
                             + (f"，剩 {5-_in_use} 位按上面建议补）" if _in_use < 5 else "）"))

            # ★ 第五转化（2026-11-08，阴灵魂姬踩出来的）
            #   手册 pets.ExtraMoves 里的第五才是真正带的；ShowExtraMoves 是转化产物。
            #   这类精灵的第五**带哪条都一样**，战斗时按专属效果变成指定的那条，
            #   所以不能只说「第五=XXX」，必须把转化链摊开。
            _conv = []
            try:
                import re as _re_conv
                for _e in (self.kb.pet_effects(pid) or []):
                    _txt = (_e.get("desc") or "")
                    _m = _re_conv.search(r"第五技能转化为([^；;。|]+)", _txt)
                    if _m:
                        _conv.append((_e.get("name") or "", _m.group(1).strip()))
            except Exception:
                _conv = []
            if _conv:
                lines.append("【第五转化】这只的第五战斗中会变，带哪条都一样：")
                for _cn, _ct in _conv:
                    lines.append(f"  · 获得「{_cn}」→ 第五转化为 {_ct}")

        # ── 代称对照（2026-11-08 新增）─────────────────────────────
        # 群友常按「150 / 先手 / 强化 / 免控」叫技能。让 LLM 从 20+ 条里自己扫容易漏，
        # 这里直接给出**这只精灵**的映射。
        #
        # ★ 候选池必须和下面 legacy 处理**同一口径**，否则会把不该看的也列出来：
        #   ① 排除系列共用技能（owners > 1）—— 圣光灵神SP 那批前置老技能就是共用的
        #   ② Lv0 / 觉醒新增 / 第五 优先（core）
        #   ③ 词条数降序（一般来说条数越多越实在，但有特例）
        #   ④ 同一功能 core 已有，就不再列旧技能（新旧重复带新的）
        _all_own = [x for v in g.values() for x in v if (x.get("owners") or 1) == 1]
        _all_shared = [x for v in g.values() for x in v if (x.get("owners") or 1) > 1]
        _core = ((g.get("fifth_new") or []) + (g.get("lv0") or [])
                 + (g.get("new_skills") or []))
        _leg_hi = [x for x in _all_own
                   if len(x.get("effects") or []) >= 3 and not x.get("priority")
                   and x["id"] not in {y["id"] for y in _core}]

        def _pick(pred, limit=3):
            """三级兜底：core → 独占旧技能 → 系列共用。

            ★ 2026-11-08：不能只认 owners==1。
              圣光灵神SP 有独占的强化/免控（驱魇逐邪/圣堂之护），应该用它们，
              别去列前置老形态那批共用技能；
              但天启星魂的「万象星罗/残星稀落」本身就是共用技能（owners>1），
              一刀切会把它的强化/免控判成「无」。
              所以：**独占里有就用独占，独占没有才退到共用**。
            """
            for pool in (_core, _leg_hi, _all_shared):
                hits = sorted([x for x in pool if pred(x)],
                              key=lambda x: -len(x.get("effects") or []))
                if hits:
                    return hits[:limit]
            return []

        _alias_lines = []
        # 威力代称：独占技能里找，按词条数降序（大招通常是词条最多的那条）
        _p150 = sorted([x for x in _all_own if (x.get("power") or 0) == 150],
                       key=lambda x: -len(x.get("effects") or []))
        if _p150:
            _alias_lines.append("  150 → " + " / ".join(
                f"{x['name']}({len(x.get('effects') or [])}条)" for x in _p150[:3]))
        # 先手：全量独占里找先制 ≥3（不经过 effects 过滤，先制本身就是硬指标）
        _sp3 = sorted([x for x in _all_own if (x.get("priority") or 0) >= 3],
                      key=lambda x: (-(x.get("priority") or 0), -(x.get("power") or 0)))
        if _sp3:
            _alias_lines.append("  先手 → " + " / ".join(
                f"{x['name']}(先制{x['priority']:+d})" for x in _sp3[:3]))
        _qh = _pick(lambda x: "强化" in funcs(x) and (x.get("category_id") or 0) == 4)
        if not _qh:      # 属性技里没有，才退回任意技能的强化效果
            _qh = _pick(lambda x: "强化" in funcs(x))
        _alias_lines.append(
            "  强化 → " + (" / ".join(x["name"] for x in _qh)
                          if _qh else "（技能里没有；看魂印或旧技能）"))
        _mk = _pick(lambda x: ("免控" in funcs(x) or "弹控" in funcs(x)
                               or "次免" in funcs(x))
                    and (x.get("category_id") or 0) == 4)
        if not _mk:
            _mk = _pick(lambda x: ("免控" in funcs(x) or "弹控" in funcs(x)
                                   or "次免" in funcs(x)))
        _alias_lines.append(
            "  免控 → " + (" / ".join(x["name"] for x in _mk)
                          if _mk else "（技能里没有，但**魂印可能有**，别直接说这只没免控）"))
        # ★ 2026-11-08：一格多用检测（配招规则.md 边界②）
        #   实例：沧岚「水月幽溟」= 全属性+1（强化）+ 免疫并反弹异常（免控/弹控）。
        #   不标出来的话 bot 会当成两个技能，以为五格满了，实际只用掉 1 格。
        _dual = {x["name"] for x in _qh} & {x["name"] for x in _mk}
        if _dual:
            _alias_lines.append(
                f"  ⚠ {'、'.join(sorted(_dual))} 一个技能同时顶「强化+免控」两格 —— "
                f"别当成两个技能，五格模板看着满了，实际还会空出一格")
        if _alias_lines:
            lines.append("【代称对照】（按这只精灵对号；已排除系列共用与白板技能）")
            lines.extend(_alias_lines)

        if g.get("lv0"):
            lines.append(
                "★ Lv0·道具开启（不用练级，优先看这组——觉醒给的新技能基本都在这里）："
                + "、".join(tag(x) for x in g["lv0"])
            )
            # ★ 2026-11-08：Lv0 是「可选补位」，不是「都要带」。
            #   实例：拂晓兔给了 4 个 Lv0（心灵转换/制约/净化/相通），
            #   但五格已经用 4 个技能填满，只剩 1 位 —— 是 4 选 1，不是全带。
            #   「新技能优先」的正确含义是：**同一功能**有新旧两条时带新的，
            #   不是「新技能都塞进去」。
            if len(g["lv0"]) > 1:
                _lv0_hint = (
                    f"  ↳ 这 {len(g['lv0'])} 个是**可选补位**，不必全带"
                )
                if _remain_slots > 0:
                    _lv0_hint += f"（五格之外还剩 {_remain_slots} 位 → {len(g['lv0'])} 选 {_remain_slots}）"
                else:
                    _lv0_hint += "（五格已占满，这组是备选/替换用）"
                _lv0_hint += "。「新技能优先」= 同一功能新旧二选一带新的，不是全塞"
                lines.append(_lv0_hint)
        sp = sorted(
            [x for v in g.values() for x in v if x.get("priority")],
            key=lambda x: (-x["priority"], -(x.get("power") or 0)),
        )
        if sp:
            lines.append("★ 先制技能（按先制度排序，先手优先看这里）：" + "、".join(tag(x) for x in sp))
        if g["fifth_new"]:
            f = g["fifth_new"][0]
            lines.append(f"第五（一般带这条）：{tag(f)}")
        if g["fifth_old"]:
            names = "、".join(tag(f) for f in g["fifth_old"])
            lines.append(f"旧第五（可替换，但通常不用）：{names}")
        if g["new_skills"]:
            lines.append("本次新增技能：" + "、".join(tag(x) for x in g["new_skills"]))
        if g["legacy"]:
            # ★ 2026-11-06 玩家规则：
            #   ① 只看**独占技能**（owners == 1）。骁勇战魂/斗气护体这类
            #      英卡洛斯系列共用的技能（3 只都有）不算候补，会误导配招。
            #   ② 效果少于 3 条的基本不用看，直接不列
            #   ③ 有先制的旧技能已经在上面的「★ 先制技能」行里出现过，不重复列
            #   觉醒技能不套②——「王·天冕奉世」只有 1 条效果但仍是必带。
            legacy_all = g["legacy"]
            legacy_shared = [x for x in legacy_all if (x.get("owners") or 1) > 1]
            legacy_hi = [
                x for x in legacy_all
                if (x.get("owners") or 1) == 1
                and len(x.get("effects") or []) >= 3
                and not x.get("priority")
            ]
            if legacy_hi:
                # ★ 排序规则（2026-11-06 玩家定）：词条数优先，满足模板次之
                #   ① 效果条数降序 —— 条数多说明功能实在，是最硬指标
                #   ② 同等条数时，能补五格缺口的排前面
                #   ③ 再同等看威力
                def fills_score(x):
                    f = funcs(x)
                    s = 0
                    if "免控/弹控" in missing and ("免控" in f or "弹控" in f):
                        s += 1
                    if "强化" in missing and "强化" in f:
                        s += 1
                    return s

                def fills(x):
                    f = funcs(x)
                    got = []
                    if "免控" in missing and "免控" in f:
                        got.append("免控")
                    if "免控/弹控" in missing and ("免控" in f or "弹控" in f):
                        got.append("免控/弹控")
                    if "强化" in missing and "强化" in f:
                        got.append("强化")
                    return got

                legacy_hi.sort(
                    key=lambda x: (
                        -len(x.get("effects") or []),
                        -fills_score(x) if has_awaken else 0,
                        -(x.get("power") or 0),
                    )
                )
                parts = []
                for x in legacy_hi[:8]:
                    fi = fills(x)
                    parts.append(tag(x) + (f"→补{'+'.join(fi)}" if fi and has_awaken else ""))
                lines.append("其余可选（独占·按词条数排序；带「→补」的是能填缺口的）：" + "、".join(parts))
            if legacy_shared:
                shared_names = "、".join(x["name"] for x in legacy_shared[:10])
                lines.append(f"（已排除 {len(legacy_shared)} 个系列共用技能，如 {shared_names}）")
        lines.append("提醒：第五只能带一个；新技能优先；威力不是唯一标准，控制/续航/先制按场合配。")
        lines.append("★ 跟觉醒新技能效果类型重复的旧技能不要带（强化/免控/弹控/吸血这类同一功能只留一个）。")
        lines.append("要看某条技能的完整效果，用 seer_skill 查它（很多技能 info 字段为空，效果在 effects 里）。")
        return "\n".join(lines)

    @llm_tool(name="seer_set_moveset")
    async def seer_set_moveset(self, event: AstrMessageEvent, pet: str, skills: str) -> str:
        """把玩家确认过的配招固定下来（第五 1 格 + 普通技能最多 4 格，共 5 格）。

        Args:
            pet(string): 精灵名
            skills(string): 技能名，用空格或顿号分隔，例如「王·冥津哑噤 丧月之夜 雾影界天」
        """
        pets = self.kb.find_pets(pet)
        if not pets:
            return f"没查到精灵「{pet}」，先确认名字。"
        pid, pname = pets[0]
        raw = str(skills or "").replace("、", " ").replace("，", " ").replace(",", " ")
        picks = [x for x in raw.split() if x]
        if not picks:
            return "没解析出技能名。"
        groups = self.kb.pet_skills_grouped(pid)
        owned = {x["name"] for v in groups.values() for x in v}
        fifth_names = {y["name"] for y in groups["fifth_new"] + groups["fifth_old"]}
        unknown = [x for x in picks if x not in owned]
        if unknown:
            return "这些技能不属于该精灵：" + "、".join(unknown)
        fifths = [x for x in picks if x in fifth_names]
        if len(fifths) > 1:
            return "第五只能带一个：" + "、".join(fifths)
        if len(picks) - len(fifths) > 4:
            return "普通技能最多 4 个，第五单独占一格。"
        self.kb.set_moveset(pid, picks, detail=self._build_moveset_detail(pname, picks, pid))
        return f"已定型「{pname}」配招：" + "、".join(picks)

    @llm_tool(name="seer_soulmark")
    async def seer_soulmark(self, event: AstrMessageEvent, keyword: str) -> str:
        """按关键词检索魂印效果文本，例如「受到攻击伤害减少」。

        Args:
            keyword(string): 精灵名或魂印效果里的关键词
        """
        pets = self.kb.find_pets(keyword)
        if pets:
            pid, pname = pets[0]
            # ① 手册优先：effectIcon 里的魂印条目（tips + come）
            manual = self.kb.soulmark_manual_by_pet(pid, 6)
            if manual:
                # 规则：有觉醒/强化版就取觉醒后那版，觉醒前不参与判定
                adv = [
                    m for m in manual
                    if (m.get("is_adv") or 0) or (m.get("intensify") or 0)
                ]
                picked, skipped = (adv, len(manual) - len(adv)) if adv else (manual, 0)
                lines = [f"【{pname}】魂印（手册）"]
                if adv:
                    lines.append(
                        f"（已取觉醒/强化后版本；觉醒前 {skipped} 条按规则不参与判定）"
                    )
                for m in picked:
                    extra = []
                    if m.get("intensify"):
                        extra.append("强化版")
                    if m.get("is_adv"):
                        extra.append("觉醒/进阶")
                    if m.get("come"):
                        extra.append(f"来源：{m['come']}")
                    suffix = f"（{'，'.join(extra)}）" if extra else ""
                    lines.append(f"- {m['desc']}{suffix}")
                # 手册命中后，再用 SeerAPI 补强化/羁绊信息（有就加，没有不报错）
                try:
                    forms = self.kb.pet_soulmark_forms(pid)
                except Exception:
                    forms = []
                if forms:
                    prim = [f for f in forms if f.get("primary")]
                    shown = prim if prim else forms[:4]
                    lines.append("〔SeerAPI 补充〕")
                    for f in shown:
                        flag = "当前生效" if f.get("primary") else "原始/参考"
                        lines.append(f"- [{flag}·{f['form']}] {f['desc']}")
                effects = self.kb.pet_effects(pid)
                if effects:
                    lines.append("〔专属效果〕")
                    lines += [f"- {e['name']}：{e['desc']}" for e in effects]
                raw = "\n".join(lines)
                result = await self._learn_and_render(f"soulmark:{pid}", "soulmark", raw, event)
                rules = self.kb.facts_for_pet(pname)
                if rules:
                    result += "\n【玩家已确认的规则（优先于原文推断）】\n" + "\n".join(
                        f"- {t}：{c[:200]}" for t, c in rules
                    )
                ann = self.kb.annotate_terms(raw)
                if ann:
                    result += "\n【赛尔专门词·读原文必看】\n" + "\n".join(ann)
                result += self._glossary_block(raw)
                return result

            # ② SeerAPI 兜底
            forms = self.kb.pet_soulmark_forms(pid)
            if forms:
                raw = f"【{pname}】魂印：\n" + "\n".join(
                    f"- [{"当前生效" if f.get('primary') else "原始/参考"}·{f['form']}·{f['pve']}] {f['desc'] if f.get('primary') else f['desc'][:90] + '…'}" for f in forms
                )
                for u in self.kb.pet_partner_upgrade(pid):
                    raw += (
                        f"\n羁绊强化「{u['group']}」（需{u['need']}只）："
                        f"\n  原：{u['before']}\n  后：{u['after']}"
                    )
                effects = self.kb.pet_effects(pid)
                if effects:
                    raw += "\n专属效果：\n" + "\n".join(f"- {e['name']}：{e['desc']}" for e in effects)
                result = await self._learn_and_render(f"soulmark:{pid}", "soulmark", raw, event)
                rules = self.kb.facts_for_pet(pname)
                if rules:
                    result += "\n【玩家已确认的规则（优先于原文推断）】\n" + "\n".join(
                        f"- {t}：{c[:200]}" for t, c in rules
                    )
                if effects and "专属效果" not in result:
                    result += "\n专属效果：" + "；".join(
                        f"{e['name']}={e['desc'][:90]}" for e in effects
                    )
                ann = self.kb.annotate_terms(raw)
                if ann:
                    result += "\n【赛尔专门词·读原文必看】\n" + "\n".join(ann)
                result += self._glossary_block(raw)
                return result
        manual = self.kb.soulmark_manual_search(keyword, 5)
        if manual:
            lines = [f"【魂印·{keyword}（手册）】"]
            for m in manual:
                src = f"（来源：{m['come']}）" if m.get("come") else ""
                lines.append(f"- {m['desc']}{src}")
            return "\n".join(lines)
        hits = self.kb.term(keyword)
        marks = [h for h in hits if h["kind"] == "魂印"]
        if marks:
            return "\n".join(f"- {h['desc']}" for h in marks[:5])
        return f"没有找到与「{keyword}」相关的魂印。可以问一下具体是哪只精灵的魂印，我再查。"


    @llm_tool(name="seer_mintmark")
    async def seer_mintmark(self, event: AstrMessageEvent, keyword: str,
                            pet: str = "") -> str:
        """查询刻印（全能刻印/专属刻印）的完整数值，含隐藏(额外)数值。

        Args:
            keyword(string): 刻印名或关键字。
                ★ **整词传进来，不要拆**。下面几种都是**完整的一个名字**：
                  · 完整刻印名：九曲之刃 / 焚世之炎 / 圣战之渊α
                  · 系列+方向：九天攻 / 圣战体 / 卡布达体（会按系列挑该项最高的）
                  · 单只外号：梅花 / 毛毛 / 青龙
                  · **冠名类**：火王冠名 / 水王冠名 / 暴逼冠名 / 圣逼冠名 / 普王冠名
                    —— 「XX冠名」是一个整体，**别拆成「火焰」「王」「火之狂野」去查**，
                    直接 `keyword="火王冠名"` 就能查到「焚世之炎」。
                    这类名字库里存的就是全称，拆了反而查不到。
            pet(string): 可选。给精灵名时**按这只精灵的流派**选物攻/特攻版
                （空元行者是特攻向 → 圣战之鸿α；伏尔格安是物攻向 → 圣战之渊α）。
                不给就两个版本都列出来，别只报物攻版。

        提示：
          · 查不到时**别自己拆词重试**，先把整词原样传一次；
            整词不行再考虑查 seer_glossary 看是不是术语。
          · 只问"XX是什么刻印/多少属性"用本工具；
            问"某精灵该装哪三个"用 seer_mint。
        """
        kw = str(keyword or "").strip()
        if not kw:
            return "请给出刻印名，例如「圣战无限」。"

        # ★ 2026-11-08：给了精灵名就先按这只精灵的流派分流，
        #   否则「圣战体」这种成对系列永远返回物攻版（bot 实测：
        #   空元行者和伏尔格安都出圣战之渊α，特攻精灵直接白给）。
        _pet_style = ""
        _pet_name = str(pet or "").strip()
        if _pet_name:
            try:
                _hits = self.kb.find_pets(_pet_name, limit=1)
                if _hits:
                    _st = self.kb.pet_stats(_hits[0][0]) or {}
                    _pet_style = self.kb.pick_style_by_stats(_hits[0][1])
                    _pet_name = _hits[0][1]
            except Exception:
                _pet_style = ""

        # ① 手册优先：MintMark 含 BaseAttriValue / MaxAttriValue / ExtraAttriValue
        manual = []
        try:
            manual = self.kb.mintmark_manual_search(kw, 5, style=_pet_style)
        except Exception:
            manual = []
        if manual:
            lines = [f"【刻印·{kw}（手册）】"]
            for r in manual:
                name = r["name"]
                tag = ""
                if "α" in name or "β" in name:
                    tag = "（进阶/升级版，α 打不出来就说「进阶版」或用 a 代替）"
                if r.get("hide"):
                    tag += "（隐藏刻印）"
                # ★ 2026-11-09（P7）：标出几角 —— 玩家说「五角/二角」就是按这个分的。
                #   非全能刻印没有几角，但要说清是什么类型：
                #   有几个名字里带「全能」的其实是老版能力刻印（高级全能刻印 /
                #   全国巅峰联赛专属全能刻印 / 王者级均衡刻印…），不说会误会。
                _cn = r.get("corner")
                if _cn:
                    tag += f"（{_cn}角）"
                elif r.get("kind"):
                    tag += f"（{r['kind']}）"
                lines.append(f"- {name}{tag}")
                # ★ 2026-11-09：技能刻印六项全 0，真正的信息在效果上。
                #   不显示这行的话，查「撞击刻印」只能看到「合计 0」，等于没查。
                if r.get("effect") and not _cn:
                    lines.append(f"    效果：{r['effect']}")
                lines.append(f"    满级：{self.kb._fmt_attrs(r['max'], 0)}")
                extra_txt = self.kb._fmt_attrs(r["extra"], 0)
                if extra_txt != "（无）":
                    lines.append(f"    隐藏额外：{extra_txt}")
                lines.append(
                    f"    合计：可见满级 {r['total']} → 含隐藏 {r['total_with_extra']}"
                )
                # ★ 2026-11-08：给了精灵名就按流派分流（上面已处理），
                #   没给就把**两版都列出来并各带数值** —— 光提示"另一版叫什么"
                #   不够，bot 实测还是会照抄物攻版（「特攻精灵直接白给」）。
                try:
                    pair = self.kb.mint_paired_version(name)
                except Exception:
                    pair = ""
                if pair:
                    _v = r["max"]
                    _kind = ("物攻版" if (_v[0] > 0 and _v[2] == 0)
                             else ("特攻版" if (_v[2] > 0 and _v[0] == 0) else ""))
                    if _pet_style:
                        lines.append(
                            f"    （已按「{_pet_name}」的流派选了这版；要另一版直说）")
                    else:
                        _pv = self.kb.mint_vals(pair) or {}
                        _pvals = _pv.get("vals") or {}
                        if _pvals:
                            def _ord(idx):
                                return ["atk", "def", "sp_atk", "sp_def", "spd", "hp"][idx]
                            _ptxt = "，".join(
                                f"{self.kb._STAT_CN.get(k, k)}{int(_pvals.get(k) or 0)}"
                                for k in ("atk", "def", "sp_atk", "sp_def", "spd", "hp")
                                if int(_pvals.get(k) or 0))
                            lines.append(
                                f"    ★ 另一版（{pair}）：{_ptxt}｜合计 {sum(int(x or 0) for x in _pvals.values())}")
                        lines.append(
                            f"    ⚠ 上面这条是{_kind}。**特攻精灵用另一版，物攻精灵用这条** ——"
                            f"不确定就把精灵名带上重问（pet=精灵名）")
            lines.append(
                "（本地库已有隐藏数值，直接以此为准；已按含隐藏合计数排序，不要再联网查）"
            )
            return "\n".join(lines)

        # ② SeerAPI 兜底（只有 desc，不含隐藏值）
        _miss = self._db_missing()
        if _miss:
            return _miss
        rows = self.kb.mintmark_search(kw, 5)
        if not rows:
            return (
                f"刻印库里没有匹配「{kw}」的记录。"
                "换个写法（去掉「之/专属/全能」，或用「进阶版」代替 α）再试，不要联网搜索。"
            )
        lines = [f"【刻印·{kw}（本库 desc，未含隐藏值）】"]
        for r in rows:
            total = r.get("total") or 0
            name = str(r["name"])
            tag = "（进阶/升级版）" if ("α" in name or "β" in name) else ""
            lines.append(f"- {name}：{r['desc']}｜可见合计 {total}{tag}")
        lines.append("（此条来自 SeerAPI，缺额外/隐藏数值；如需完整数值可再问一次换个写法）")
        return "\n".join(lines)

    @llm_tool(name="seer_gem")
    async def seer_gem(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """查宝石的逐级数值（36 类 / 245 条，本地库直读，不用联网）。

        # 能查到
        · 老 13 类（各 10 级）：冻伤 / 烧伤 / 中毒 / 速度+1 / 攻击+1 / 特攻+1 / 防御+1 /
          特防+1 / 固定伤害3 / 致命一击 / 疲惫 / 麻痹 / 威力
        · 二代 23 类（各 5 级，罗马数字）：活力维持 / 活力消耗 / 活力恢复 / 追命 / 逆命 /
          绝命 / 失明 / 失神 / 冰封 / 焚烬 / 疾风 / 迅雷 / 真实伤害 / 真目 / 强能 / 感染 /
          竭力 / 爆伤 / 秘咒 / 失力 / 活力保留 / 护盾回溯 / 护罩回溯

        # ⚠️ 两个已经出过错的地方
        · 「威力宝石 Lv6」= 技能威力 **+17 点**，不是「攻击强化 +6」
        · 「真伤宝石」库里正式名叫 **「真实伤害宝石」**，搜简称可能 0 条 —— 换写法再试

        # 什么时候用
        群友问「威力宝石6级加多少」「真伤宝石Ⅴ多少点」「强能宝石满级多少」→ 用这个。
        问「这个精灵该绑什么宝石」→ 用 seer_mint 或 seer_config_card。

        Args:
            keyword(string): 留空 = 列出 36 类目录；填类别名或宝石名查逐级数值，
                如「威力」「真实伤害」「强能」「爆伤」
        """
        _miss = self._db_missing()
        if _miss:
            return _miss
        v = self.kb.gem_view(keyword or "")
        if v.get("error"):
            return f"查宝石失败：{v['error']}"

        if v.get("mode") == "catalog":
            lines = [f"【宝石目录】共 {v['total_categories']} 类 / {v['total_items']} 条"]
            for c in v["catalog"]:
                lines.append(f"- {c['category']}（{c['levels']} 级）：{c['range']}")
            lines.append("")
            lines.append(v.get("hint") or "")
            return "\n".join(lines)

        if not v.get("categories"):
            return v.get("hint") or f"没有匹配「{keyword}」的宝石。"

        lines = [f"【宝石·{v['keyword']}】（匹配 {v['matched']} 类）"]
        for c in v["categories"]:
            lines.append("")
            lines.append(f"◆ {c['category']}")
            for it in c["levels"]:
                lines.append(f"  {it['name']}：{it['info']}")
        if v.get("truncated"):
            lines.append("")
            lines.append("（匹配到多个类别，只显示前 3 类；换个更准的词可以缩小）")
        return "\n".join(lines)

    @llm_tool(name="seer_effect")
    async def seer_effect(self, event: AstrMessageEvent, name: str) -> str:
        """查询专属效果（魂印/技能里出现的名词，如「弥沦之镜」「没夜之月」）及其所属精灵。

        Args:
            name(string): 效果名或关键词
        """
        hits = self.kb.effect_lookup(name)
        # 魂印正文也算「这个效果出现在哪」——像「非真实伤害减少」这类效果
        # 大量散在魂印里，只查专属效果表会漏。
        try:
            soul = self.kb.soulmark_effect_search(name, 4)
        except Exception:
            soul = []
        if not hits and not soul:
            return f"没有查到专属效果「{name}」。可能是新精灵，请向玩家确认后记录。"
        lines = []
        for h in hits[:5]:
            via = f"（表述：{h['via']}）" if h.get("via") else ""
            lines.append(
                f"【{h['effect']}】" + (f"（{h['pet']}）" if h["pet"] else "") + f"{via}：{h['desc']}"
            )
        for s in soul:
            tag = "·觉醒后" if s.get("tier") == "觉醒后" else ""
            via = f"（表述：{s['via']}）" if s.get("via") else ""
            lines.append(f"【魂印·{s['pet']}{tag}】{via}{s['snippet']}")
        if soul:
            lines.append("（以上魂印条目来自 soulmark 表，说明该效果也存在于魂印，不只专属效果。）")
        if any(x.get("via") for x in hits) or any(x.get("via") for x in soul):
            lines.append("（标了「表述：」的条目是同一机制的不同写法，别因为字面不一样就当成不同效果。）")
        return "\n".join(lines)

    @llm_tool(name="seer_term")
    async def seer_term(self, event: AstrMessageEvent, term: str) -> str:
        """查询赛尔号术语/异常状态/机制说明，如「中毒」「锁伤」「免控」。

        Args:
            term(string): 术语关键词
        """
        hits = self.kb.term(term)
        if not hits:
            return f"术语库未收录「{term}」。如果玩家解释了它的机制，用 seer_remember 记下来。"
        return "\n".join(f"【{h['kind']}】{h['name']}：{h['desc']}" for h in hits[:4])

    # ---------------- 工具：属性与伤害 ----------------

    @llm_tool(name="seer_pet_terms")
    async def seer_pet_terms(self, event: AstrMessageEvent, name: str) -> str:
        """列出某只精灵的全部专属名词（专属效果/机制名词）及其解释，含文中提到但库中没定义的。

        Args:
            name(string): 精灵名
        """
        hit = self.kb.term_lookup_any(name)
        if hit:
            lines = [f"【名词·{name}】"]
            for it in hit["items"]:
                who = f"（{it['pet']}）" if it.get("pet") else ""
                lines.append(f"- {it['name']}{who}：{it['desc']}")
            return "\n".join(lines)
        pets = self.kb.find_pets(name)
        if not pets:
            return f"未收录精灵「{name}」。"
        pid, pname = pets[0]
        data = self.kb.pet_terms(pid)
        lines = [f"【{pname}·专属名词】"]
        if data["defined"]:
            for item in data["defined"]:
                lines.append(f"- {item['name']}：{item['desc']}")
        else:
            lines.append("- 库里没有收录这只精灵的专属效果")
        if data["mentioned"]:
            lines.append("文中还提到（库中暂无独立定义）：" + "、".join(x["name"] for x in data["mentioned"]))
        return "\n".join(lines)

    @llm_tool(name="seer_tag")
    async def seer_tag(self, event: AstrMessageEvent, tag: str = "", pet: str = "") -> str:
        """按效果标签反查技能/魂印；或查某只精灵的功能覆盖。

        用法三种：
          ① tag 填、pet 空  → 全库哪些技能/魂印带这个功能
          ② pet 填、tag 空  → 这只精灵的技能池＋魂印覆盖了哪些功能、哪项是空白
          ③ 两个都填        → 这只精灵身上具体哪些技能带这个功能

        Args:
            tag(string): 标签名，可模糊匹配。常见：恢复/强化/弱化/先制/免疫异常/
                固定伤害/百分比伤害/真实伤害/附加伤害/增伤/减伤/致命一击/反伤/
                闪避/免伤/穿防/回PP/清PP/施加异常
            pet(string): 精灵名或俗称（如「古龙」「龙王」）
        """
        # 三种模式的判定全部来自 tag_view（语义层，和 CLI 共用）
        v = self.kb.tag_view(tag, pet)
        sep = chr(10)

        if v.get("empty_hint"):
            return v["empty_hint"]

        if v["mode"] == "intersect":
            lines = [f"{r['kind']}｜{r['name']}" for r in v["rows"]]
            return f"【{v['pname']}·{v['tag']}】共 {len(lines)} 条：" + sep + sep.join(lines)

        if v["mode"] == "coverage":
            cov = v["coverage"] or {}
            lines = [f"【{v['pname']}（#{v['pid']}）·功能覆盖】"]
            if cov.get("skills"):
                lines.append("〔技能〕")
                for t, names in sorted(cov["skills"].items(), key=lambda x: -len(x[1])):
                    uniq = list(dict.fromkeys(n for n in names if n))
                    lines.append(f"- {t} ×{len(uniq)}：{'、'.join(uniq[:6])}")
            if cov.get("soulmarks"):
                lines.append("〔魂印〕")
                for t, names in cov["soulmarks"].items():
                    lines.append(f"- {t}：{'、'.join(names)}")
            if v["missing"]:
                lines.append(f"〔空白·完全没有〕{'、'.join(v['missing'])}")
            lines.append("说明：空白项＝该精灵技能池与魂印都没有这个功能，是它的定位短板。")
            return sep.join(lines)

        # reverse
        lines = [f"{'技能' if r.get('kind') == 'skill' else '魂印'}｜{r.get('name')}"
                 for r in v["rows"]]
        return f"【标签·{v['tag']}】共 {v['total']} 条，示例：" + sep + sep.join(lines)


    @llm_tool(name="seer_pool")
    async def seer_pool(self, event: AstrMessageEvent, pool: str = "", pet: str = "") -> str:
        """查询巅峰池：某池子有哪些精灵，或某精灵进了哪些池。

        Args:
            pool(string): 池子，如「35」「专家」「禁用」「限2」
            pet(string): 精灵名，查它在哪些池
        """
        _miss = self._db_missing()
        if _miss:
            return _miss
        # 三种模式的判定都来自 pool_view（语义层，和 CLI 共用）
        v = self.kb.pool_view(pool, pet)
        sep = chr(10)
        if v.get("empty_hint"):
            return v["empty_hint"]

        if v["mode"] == "pet":
            return sep.join(
                f"{p['label']}（{len(p['pets'])} 只，截止 {p['deadline'] or '未标注'}）"
                for p in v["hits"]
            )
        if v["mode"] == "list":
            _ver = v.get("version") or ""
            _head = f"【巅峰池】数据版本 {_ver}" + sep if _ver else "【巅峰池】" + sep
            return _head + sep.join(
                f"- {p['label']}：{len(p['pets'])} 只（档期 {p['deadline'] or '未标注'}）"
                for p in v["pools"]
            )
        mp = v["matched"]
        # 2026-11-14：带上真实数据版本。模型以前在档期为空时会自己编「10-30 那期」，
        # 而 pvp.db 的 ban_* 档期字段本来就是空的。给死数据源，它就不能瞎猜。
        ver = v.get("version") or ""
        tail = f"{sep}（数据版本 {ver}）" if ver else f"{sep}（数据源未标注版本）"
        return f"【{mp['label']}】{len(mp['pets'])} 只" + sep + "、".join(mp["pets"]) + tail


    @llm_tool(name="seer_doc")
    async def seer_doc(self, event: AstrMessageEvent, keyword: str) -> str:
        """检索机制专栏原文（168 篇：赛学必修/选修/扩展 + 单只精灵的机制解析）。

        库里的两批：
          · 赛学系列：时点、免死、抗性概率、伤害流程、宝石、连击……
          · 机制解析—<精灵名>：瀚宇星皇、混元天尊、圣灵谱尼、混沌魔君索伦森 等 137 只
        想问「这只精灵的魂印为什么这么判」「这条效果什么时候触发」，用它查原文。

        Args:
            keyword(string): 关键词。机制词（「免死」「伤害流程」「抗性概率」）
                或精灵名（「瀚宇星皇」「混元天尊」「谱尼」）都行
        """
        concepts = self.kb.doc_concepts_search(keyword, 3)
        rules = self.kb.doc_rules_search(keyword, 5)
        rows = self.kb.doc_search(keyword, 2)
        if not concepts and not rules and not rows:
            return f"专栏里没搜到「{keyword}」。"
        lines = [f"【专栏·{keyword}】"]
        if concepts:
            lines.append("〔概念〕")
            for c in concepts:
                lines.append(f"- {c['term']}：{c['definition']}（据{c['title']}）")
        if rules:
            lines.append("〔判定〕")
            for r in rules:
                lines.append(f"- 条件：{r['condition']}\n  结论：{r['conclusion']}（据{r['title']}）")
        if rows:
            lines.append("〔原文〕")
            for r in rows:
                lines.append(f"- 《{r['title']}》{r['snippet'][:140].strip()}")
        return "\n".join(lines)

    @llm_tool(name="seer_status")
    async def seer_status(self, event: AstrMessageEvent, category: str = "") -> str:
        """列出异常状态及其分类与效果。可按「控制类 / 弱化类 / 附属类」筛选。

        Args:
            category(string): 留空=全部；可填「控制」「弱化」「附属」
        """
        _miss = self._db_missing()
        if _miss:
            return _miss
        items = self.kb.status_list(category)
        if not items:
            return f"没有匹配「{category}」的异常状态。可填：控制 / 弱化 / 附属。"
        lines = [
            (f"{x['category']}｜{x['name']}" + (f"｜抗性{x['resist']}" if x.get('resist') else "")
             + f"：{x['desc']}")
            for x in items
        ]
        head = f"【异常状态·{category or '全部'}】共 {len(items)} 条\n"
        return head + "\n".join(lines[:20]) + ("\n…（仅列前 20 条）" if len(lines) > 20 else "")

    @llm_tool(name="seer_type_chart")
    async def seer_type_chart(self, event: AstrMessageEvent, attack_type: str, defend_type: str) -> str:
        """查询属性克制倍率。

        Args:
            attack_type(string): 攻击方属性，如「火」
            defend_type(string): 防守方属性，支持双属性，如「草/冰」
        """
        # 倍率、拆解、合成规则、缺失警告全部来自 type_view（语义层，和 CLI 共用）
        v = self.kb.type_view(attack_type, defend_type)
        if v.get("error") or not v["atk"] or not v["dfd"]:
            names = "、".join((v.get("available") or self.kb.type_names())[:30])
            return f"【属性克制·用法提示】{v.get('error') or '属性名解析失败'}。可用属性：{names}"
        if v["mult"] is None:
            names = "、".join(self.kb.type_names()[:30])
            return f"没查到 {attack_type}→{defend_type} 的克制关系。可用属性：{names}"

        sep = chr(10)
        lines = [f"{'/'.join(v['atk'])} → {'/'.join(v['dfd'])}：×{v['mult']}（{v['feel']}）"]
        if v.get("missing_hint"):
            lines.append(v["missing_hint"])
        pairs = v.get("pairs") or []
        if len(pairs) > 1:
            lines.append("拆解：" + "｜".join(f"{a}→{dd} ×{m}" for a, dd, m in pairs))
        if v.get("rule_hint"):
            lines.append("合成规则：" + v["rule_hint"])
        return sep.join(lines)


    @llm_tool(name="seer_build")
    async def seer_build(
        self,
        event: AstrMessageEvent,
        name: str,
        preset: str = "爆攻",
        vs: str = "",
        talent: int = 31,
        nature: str = "",
        suit: str = "",
        title: str = "",
        eye: str = "",
    ) -> str:
        """一键配装：按预设挑三刻印 + 算最终面板。群友问「给最好的爆攻是什么面板」用这个。

        Args:
            name(string): 精灵名
            preset(string): 预设，可选 爆攻 / 爆体 / 爆盾 / 高速
            vs(string): 对线特化。留空=通用；phys=极限抗物理；spec=极限抗特殊
            talent(int): 天赋，满个体 31
            nature(string): 性格。留空按预设自动选（物攻向爆攻=固执、爆盾=顽皮等）
            suit(string): 套装名，如「晨曦」
            title(string): 称号名，如「神话」
            eye(string): 目镜名，如「漫游者-AH」
        """
        _miss = self._db_missing()
        if _miss:
            return _miss
        v = self.kb.build_view(
            name, preset, vs=vs, talent=talent, nature=nature,
            suit=suit, title=title, eye=eye,
        )
        if not v["found"]:
            return v.get("empty_hint") or f"配装失败：{name}"

        sep = chr(10)
        lines = []
        head = f"【{v['name']}】#{v['pid']}·{preset}预设"
        if vs:
            lines.append(head + f"·对线抗{'物理' if vs == 'phys' else '特殊'}")
        else:
            lines.append(head)
        lines.append(
            f"种族 攻{v['atk_base']} / 特攻{v['spa_base']} → 自动判为 {v['dir_cn']}"
        )
        lines.append("刻印：" + " + ".join(v["mints"]))
        lines.append(f"性格：{v['nature'] or '中性'}" + (f" ｜ 套装：{suit}" if suit else ""))
        if v.get("ev_note"):
            lines.append(v["ev_note"])

        st = v.get("stat") or {}
        if st.get("found"):
            lines.append("")
            lines.append("最终面板：")
            lines.append("  " + "  ".join(f"{r['cn']}{r['final']}" for r in st["rows"]))
            if st.get("total"):
                lines.append(f"  六项和：{st['total']}")
            if st.get("missing_mints"):
                lines.append("  ⚠ 未找到的刻印：" + "、".join(st["missing_mints"]))
            if st.get("overlay_hint"):
                lines.append("  " + st["overlay_hint"])
        lines.append("")
        lines.append("（同系列最多装 2 个，所以「该维度最高×2 + 另一系列一个」）")
        return sep.join(lines)

    @llm_tool(name="seer_mint")
    async def seer_mint(
        self,
        event: AstrMessageEvent,
        name: str,
        preset: str = "爆攻",
    ) -> str:
        """刻印推荐：按精灵种族自动判流派，给三刻印方案（同系列最多 2 个）。

        Args:
            name(string): 精灵名
            preset(string): 方向预设，可选 爆攻 / 爆体 / 爆盾 / 高速
        """
        _miss = self._db_missing()
        if _miss:
            return _miss
        v = self.kb.mint_view(name, preset)
        if not v["found"]:
            return v.get("empty_hint") or f"找不到精灵：{name}"
        sep = chr(10)
        lines = [
            f"【{v['name']}】#{v['pid']} 刻印推荐",
            v["note"],
            "",
            "方案：" + " / ".join(v["mints"]),
        ]
        return sep.join(lines)

    async def _solve_ev_targets(self, name: str, spec: str, *, nature="", mints=None,
                               suit="", title="", eye="", team_skip="", talent=31,
                               hpup=20) -> dict:
        """把「atk=max, sp_def=430, hp=1040, rest=spd」反推成学习力字符串。

        ★ 硬约束（绝不突破）：
          · 单项 <= 252（游戏上限 255，但 252//4 == 255//4，多刷无效）
          · 总和 <= 510
          · 目标不可达时**不硬凑**，按顺序尽量满足，并明确告诉调用方「刷满也就 X」

        处理顺序：先 max（固定 252），再按声明顺序解目标值，最后 rest 收尾。
        """
        import re as _re
        alias = {"攻": "atk", "防": "def", "特攻": "sp_atk", "特防": "sp_def",
                 "速": "spd", "体": "hp"}
        EV_ITEM_CAP = 252      # 预算计算用 252（252/254/255 的 //4 都是 63，效果等价）
        EV_MAX_WRITE = 255     # 「拉满」时写进结果的值（尊重玩家习惯写法）
        EV_TOTAL_CAP = 510     # 总上限

        fixed: dict[str, int] = {}
        max_keys: set[str] = set()        # ★ 哪些项是 max 语法（显式写 252 的不算）
        want: list[tuple[str, int]] = []      # 保持声明顺序
        rest_key = ""

        for piece in _re.split(r"[,，;；\s]+", str(spec or "")):
            piece = piece.strip()
            if not piece or "=" not in piece:
                continue
            k, _, v = piece.partition("=")
            k = alias.get(k.strip(), k.strip())
            v = v.strip()
            if k in ("rest", "剩", "剩下"):
                rest_key = alias.get(v, v)
                continue
            if v.lower() in ("max", "满", "拉满"):
                # ★ max 存 252：252/254/255 的 //4 都是 63，效果完全等价，
                #   但预算计算必须按 252 算（写 255 会白白少算 3 点）。
                fixed[k] = 252
                max_keys.add(k)
            elif v.isdigit():
                n = int(v)
                if n <= 255:
                    # 学习力写法（含玩家习惯的 254/255）→ 直接采用，
                    # 但注意 252 以上对 //4 无增益，所以预算按 min(n,252) 算才准
                    fixed[k] = n
                else:
                    want.append((k, n))       # >255 才是目标面板值
        if not fixed and not want and not rest_key:
            return {}

        def _render(m: dict[str, int]) -> str:
            return ",".join(f"{a}={b}" for a, b in m.items())

        def _probe(m: dict[str, int]) -> dict[str, int]:
            v = self.kb.config_view(
                name, talent=talent, nature=nature or "", ev=_render(m),
                mints=list(mints or []), suit=suit, title=title, eye=eye,
                team_skip=team_skip, hpup=hpup,
            )
            if not v.get("found"):
                return {}
            return {b["key"]: b["value"] for b in v["battle"]}

        # ★ max 的取值不能拍 252：
        #   库里有权威表 tools/ev_table.py（鱼片 2026-11-01 提供），
        #   按「种族值末位」给出该项的学习力有效上限：
        #     末位 0/5 → 253 ｜ 1/6 → 252 ｜ 2/7 → 255 ｜ 3/8 → 254 ｜ 4/9 → 254
        #   南霜攻种族 148（末位 8）的极限就是 254，直接写 252 会少 2 点。
        if fixed:
            try:
                _hits = self.kb.find_pets(name)
                _st = self.kb.pet_stats(_hits[0][0]) if _hits else {}
                # ★ 2026-11-08：改用 kb.ev_limit()（表已内联到 db.EV_LIMIT_TABLE）。
                #   原来这里 sys.path 插硬编码路径再 import ev_table，
                #   路径一变就静默退回 252。
                for _k in list(max_keys):
                    _v = fixed.get(_k, 0)
                    if _v >= 252:            # 只处理「拉满」这一档
                        _base = int((_st or {}).get(_k) or 0)
                        if _base:
                            _lim = self.kb.ev_limit(_base)
                            if _lim:
                                # ★ 末位 0/5：表值是 253（「不越过加成 70 的最后一格」），
                                #   但「面板最大化」应该刷 254 —— 254 会跳过加成 70 直接到 71，
                                #   多 2 点面板。facts「学习力分配两张原表·末位 0 / 5 为什么差 1」
                                #   和 pet_build 里无极圣武（125，末位 5）写的 254 都指向这个。
                                if _base % 10 in (0, 5):
                                    # 但要看预算：玄武 def=max + sp_def=252 + hp=5
                                    # 用 254 就是 511 超上限，只能退回表值 253。
                                    _others = sum(v for k2, v in fixed.items() if k2 != _k)
                                    if _others + 254 <= EV_TOTAL_CAP:
                                        _lim = max(int(_lim), 254)
                                fixed[_k] = _lim
                        else:
                            # 种族值 0（无为觉者这类，面板全靠刻印）：
                            # 末位表没意义，但也不能停在 252 —— 面板公式保留小数，
                            # 252/4=63.0、254/4=63.5，多这 0.5 六项和才够 1934，
                            # 均分后才是 967 而不是 966。
                            fixed[_k] = 254
            except Exception as _e:
                logger.debug("[seer_expert] ev_table 查表失败，退回 252：%s", _e)

        base = dict(fixed)
        notes: list[str] = []
        unreachable: list[str] = []

        # 先检查 max 项本身有没有超预算
        if sum(base.values()) > EV_TOTAL_CAP:
            return {"error": f"光是刷满的项就已经 {sum(base.values())} 点，超过上限 510。"}

        for kk, target in want:
            budget_left = EV_TOTAL_CAP - sum(base.values())
            if budget_left <= 0:
                unreachable.append(f"{kk} 目标{target}（预算已用完）")
                continue
            cap = min(EV_ITEM_CAP, (budget_left // 4) * 4)

            best: tuple[int | None, int | None, int | None] = (None, None, None)

            def _try_ev(ev_try: int, best):
                trial = dict(base)
                trial[kk] = ev_try
                got = _probe(trial)
                if not got:
                    return best, True
                gap = abs(got.get(kk, 0) - target)
                if best[2] is None or gap < best[2]:
                    best = (ev_try, got.get(kk, 0), gap)
                return best, gap == 0

            # ★ 2026-09（P6）两轮搜：
            #   原来只有 `range(0, cap+1, 4)` —— **步长 4 会漏掉 254**。
            #   学习力不必须是 4 的倍数：254/4=63.5 比 252/4=63 多 0.5，
            #   面板公式保留小数，252 以上对部分种族确实还有增益
            #   （南霜攻种族 148 末位 8，极限就是 254）。
            #   254/255 这种非 4 倍数在粗搜里永远取不到，必须精修一轮。
            #
            #   第一轮：步长 4 粗定位（最多 64 次）
            #   第二轮：best ± 4 范围内步长 1（最多 9 次）
            stop = False
            for ev_try in range(0, cap + 1, 4):
                best, stop = _try_ev(ev_try, best)
                if stop:
                    break
            if not stop and best[0] is not None:
                _lo = max(0, best[0] - 4)
                _hi = min(cap, best[0] + 4)
                for ev_try in range(_lo, _hi + 1):
                    if ev_try % 4 == 0:
                        continue          # 粗搜已经试过
                    best, stop = _try_ev(ev_try, best)
                    if stop:
                        break

            if best[0] is None:
                unreachable.append(f"{kk} 目标{target}（算不出）")
                continue

            ev_used, actual, gap = best
            base[kk] = ev_used
            if gap == 0:
                notes.append(f"{kk} {target}→ev{ev_used}")
            elif ev_used >= cap:
                # ★ 到了本次可用上限还差 → 明说上限，不硬凑
                unreachable.append(
                    f"{kk} 最多到 {actual}（差 {target - actual} 点；"
                    f"该配置下刷 {ev_used} 已是极限）"
                )
            else:
                notes.append(f"{kk} 目标{target}→ev{ev_used}（实际{actual}，差{gap}）")

        # 剩余给 rest
        if rest_key:
            used = sum(base.values())
            left = max(0, EV_TOTAL_CAP - used)
            left = (left // 4) * 4
            left = min(left, EV_ITEM_CAP)
            if left > 0:
                base[rest_key] = left
                notes.append(f"{rest_key} 剩余{left}")

        total = sum(base.values())
        if total > EV_TOTAL_CAP:
            return {"error": f"学习力总和 {total} 超过上限 510。"}

        # 预算口径：252 以上对 //4 无增益，按 252 折算后再校验
        _eff_total = sum(min(v, 252) for v in base.values())
        if _eff_total > EV_TOTAL_CAP:
            return {"error": f"有效学习力 {_eff_total} 超过上限 510。"}

        out = {"ev": _render(base), "note": "；".join(notes)}
        if unreachable:
            out["note"] = (out["note"] + "；" if out["note"] else "") + \
                          "⚠ 达不到：" + "；".join(unreachable)
            out["unreachable"] = unreachable
        return out

    @llm_tool(name="seer_config_card")
    async def seer_config_card(
        self,
        event: AstrMessageEvent,
        name: str,
        preset: str = "",
        nature: str = "",
        ev: str = "",
        mints: str = "",
        gems: str = "",
        suit: str = "",
        title: str = "",
        eye: str = "",
        team_skip: str = "",
        soulmod: str = "",
        talent: int = 31,
        hpup: int = 20,
    ) -> str:
        """给精灵生成一张**数据配置图**（面板数值表）。不是查群友上传的截图，也**不是绘画**。

        【本工具产出什么】
        一张 1040×760 的信息图：立绘 + 六项面板 + 三个刻印 + 绑定宝石 + 套装/称号/目镜加成。
        全部是按本地库数据算出来、用 HTML 模板渲染的**数值表**，
        里面没有任何 AI 绘画成分。

        【不要用图像生成工具做这件事】
        群友要的是「这只精灵的配装数值长什么样」，不是立绘/插画/二次元图。
        看到「配置图」「面板图」「配装图」这类词时用本工具，
        不要调 gemini 那类图像生成工具（那会画出一只立绘而不是数值表）。

        【★ 只传你能确定的，其余全部自动补 —— 不要试错】
        下面这些参数**留空就会自动推**，不需要一个个试：
        ```
        nature    留空 → 按流派自动选（物攻向=固执、特攻向=保守、爆盾=顽皮/大胆）
        ev        留空 → 按预设自动分配（爆攻=atk252/hp252/spd4）
        mints     留空 → 按预设推荐三件（同系列最多两个，会摆成 A/B/A）
        preset    留空 → 默认「爆攻」
        team_skip 留空 → 按种族自动判（物攻向跳特攻、双攻型不跳、种族全0不跳）
        soulmod   留空 → 从魂印原文自动识别（体力换属性/总和均分/英雄之耀）
        ```
        **所以「南霜的配置图」只需要 `name="南霜"` 一个参数就能出图。**
        只有群友明确说了某项（「开朗性格」「刻印用九天攻」「体力刷到1100」）才传对应参数。

        【参数传错会被挡下，不会出错图】
        · 刻印名写错 → 直接返回错误 + 相近候选，**不出图**（用 seer_mint 查该装哪三个）
        · 这样不会出现「少装了两个刻印但图照出」的情况

        【什么时候用本工具】
        · 群友说「看看顶配XX的配置图」「生成一下XX的配置图」「XX这配置长什么样」
        · 群友给了一整套配置，想确认最终面板
        · 讨论配装时想把结果画出来给人看

        【什么时候不用本工具】
        · 群友问「XX的配置图」而他之前**上传过**自己的截图 → 那是「配置图 XX」命令的事
          （本工具会按数据现场画新图，不是取他的截图）
        · 只想看六项数值 → 用 seer_stat（快得多，不出图）

        Args:
            name(string): 精灵名
            preset(string): 配装预设，可留空。填写则用 seer_build 那套（爆攻/爆体/爆盾/高速）
            nature(string): 性格。留空按预设自动选，或按「中性」
            ev(string): 学习力。两种写法都支持：
                · 直接给学习力："atk=254,hp=252,spd=4"
                · 给目标面板值（群友常这么说）："atk=max, sp_def=430, hp=1040, rest=spd"
                  atk=max → 刷满 252
                  sp_def=430 → 反推出学习力（430 是**最终面板值**）
                  rest=spd → 剩下的预算全给速度
                  ★「攻击254拉满、特防430、体力1040、剩下给速度」照抄成
                    "atk=max, sp_def=430, hp=1040, rest=spd" 即可，不用自己换算
            mints(string): 刻印，逗号分隔最多 3 个。支持简称（九天攻/圣战体/v8）

                【★ 同系列不同名 = 方向完全不同，别凭名字眼熟挑】
                群友说「一龙子」「双黄金」这类系列名时必须按方向选：
                ```
                赤洲龙子系列（八条，数值差极大）
                  狴犴  攻0  防60 特攻35 特防60 速25 体100  ← 双防向
                  蒲牢  攻75 防38 特攻0  特防38 速37 体92   ← 物攻向
                  嘲风  攻0  防38 特攻75 特防38 速37 体92   ← 特攻向
                  囚牛  攻60 防40 特攻0  特防40 速45 体95   ← 物攻向
                  睚眦  攻0  防40 特攻60 特防40 速45 体95   ← 特攻向
                  狻猊  攻0  防32 特攻47 特防32 速34 体135  ← 爆体
                  赑屃  攻47 防32 特攻0  特防32 速34 体135  ← 爆体+攻
                  螭吻  攻55 防40 特攻55 特防40 速0  体120  ← 双攻，总和高（三310）
                ```
                拿双防向的精灵硬套蒲牢（攻75）会让双防各少 22 点，面板直接崩。
                **不确定方向就查 seer_mintmark，或者干脆不传 mints 走专属配装。**

                【★ 群友只说了系列名时，直接传系列名，别自己补后缀】
                群友说「双威临一龙子」→ mints 就传 "刑天·威临,刑天·威临,赤洲龙子"
                （**不要**自己猜成「赤洲龙子·蒲牢」——那是爆攻印）
                系统会依据「已给刻印的方向 + 学习力分配」自动补全，
                并在结果里标注「※ 推理补全：赤洲龙子→狴犴（按物防向推的，你没说具体哪条）」。
                这样群友一眼能看出哪条是推的，要改也有依据。
            gems(string): 刻印绑定的宝石，逗号分隔，与 mints 一一对应。
                写法 "威力宝石Lv7@星纪"（@ 后面是绑定的技能名），不需要就留空位
            suit(string): 套装名，如「晨曦之星战甲」
            title(string): 称号名，如「神话」「星光之旅」
            eye(string): 目镜名，如「漫游者-AH」
            team_skip(string): 战队不点的项，如「特攻」（极差向配置）
            soulmod(string): 魂印派生。三种写法：
                "体力20"        → 消耗体力上限20%，其他五项各加等量（天堂极龙兽）
                "双攻差5"       → |攻-特攻|×15%×5层 → 加体力/防御/特防/速度（无极圣武）
                "均分"          → 每项变成六项之和/2（无为觉者）
            talent(int): 天赋，满个体 31
            hpup(int): 体力上限药剂，最多 20
        """
        _miss = self._db_missing()
        if _miss:
            return _miss

        mint_list = [x.strip() for x in str(mints or "").replace("，", ",").split(",") if x.strip()]
        gem_list = [x.strip() for x in str(gems or "").replace("，", ",").split(",")]

        # ★ ev 支持「目标面板值」写法（2026-11-03 补）
        #   群友常这么说：「攻击254拉满、特防430、体力1040、剩下给速度」
        #   430/1040 是**最终面板值**，不是学习力，必须反推。
        #   语法：atk=max, sp_def=430, hp=1040, rest=spd
        #         （rest= 表示「剩下的全给这项」）
        _raw_ev = str(ev or "").strip()
        # 只在这三种情况走反推（普通学习力写法 atk=254,hp=252 直接透传）：
        #   ① 含 rest=      ② 含 max/满/拉满     ③ 有某个数值 > 252（学习力不可能超过 252）
        _is_target_spec = False
        if _raw_ev:
            _flat = _raw_ev.replace(" ", "")
            if "rest=" in _flat or any(w in _flat.lower() for w in ("max", "满", "拉满")):
                _is_target_spec = True
            else:
                import re as _re0
                for _n in _re0.findall(r"=(\d+)", _flat):
                    # ★ 学习力范围 0~255；超过 255 才一定是「目标面板值」。
                    #   254/255 这种玩家习惯写法要按学习力处理，别误当目标值。
                    if int(_n) > 255:
                        _is_target_spec = True
                        break
        if _is_target_spec:
            _res = self._solve_ev_targets(name, _raw_ev, nature=nature,
                                         mints=mint_list, suit=suit, title=title,
                                         eye=eye, team_skip=team_skip, talent=talent,
                                         hpup=hpup)
            if _res.get("ev"):
                ev = _res["ev"]
                if _res.get("note"):
                    logger.info("[seer_expert] ev 目标值反推：%s", _res["note"])
            if _res.get("error"):
                return _res["error"]

        # ★ 2026-11-09（用户反馈「配置图学习力第一次还是没刷满」）：
        #   LLM 把「攻体拉满」自己换算成 `sp_atk=252,hp=252,spd=4`（合计 508），
        #   压根没查极限表 —— 怒涛·沧岚特攻种族 153（末位 3）实际能到 **254**，
        #   252 和 254 差 1 点面板（470 → 471）。用户还得追问一句「没刷满吧」
        #   才触发 seer_config_tweak 修正。
        #
        #   这里兜住：**只要某项写了 252 而它还能更高，就自动提到极限值**，
        #   前提是不超 510。同时在 note 里说清楚，不偷偷改。
        #   LLM 想精确控制就传 254/255 或 max，不会被这条拦。
        _bump_notes: list[str] = []
        if _raw_ev and not _is_target_spec:
            try:
                import re as _re1
                _hits = self.kb.find_pets(name, limit=1)
                _st0 = self.kb.pet_stats(_hits[0][0]) if _hits else {}
                _pairs = _re1.findall(r"([a-zA-Z_]+)=(\d+)", _raw_ev.replace(" ", ""))
                _alias1 = {"攻": "atk", "防": "def", "特攻": "sp_atk",
                           "特防": "sp_def", "速": "spd", "体": "hp"}
                _cur = {}
                for _k0, _v0 in _pairs:
                    _kk = _alias1.get(_k0, _k0)
                    if _kk in _st0 or _kk in ("atk", "def", "sp_atk", "sp_def", "spd", "hp"):
                        _cur[_kk] = int(_v0)
                if _cur and _st0:
                    _total = sum(_cur.values())
                    for _k1 in list(_cur):
                        if _cur[_k1] < 252:      # 只处理「拉满」这一档
                            continue
                        _b1 = int(_st0.get(_k1) or 0)
                        if not _b1:
                            continue
                        _lim1 = self.kb.ev_limit(_b1) or 0
                        # 末位 0/5 的表值是 253，但面板最大化要 254（同 _preset_ev 的规则）
                        if _b1 % 10 in (0, 5) and _total - _cur[_k1] + 254 <= 510:
                            _lim1 = max(int(_lim1), 254)
                        if _lim1 <= _cur[_k1]:
                            continue
                        _add = _lim1 - _cur[_k1]
                        if _total + _add > 510:  # 预算不够就少加，别超
                            _add = 510 - _total
                            _lim1 = _cur[_k1] + _add
                        if _add <= 0:
                            continue
                        _cur[_k1] = _lim1
                        _total += _add
                        _bump_notes.append(
                            f"{_k1} {_lim1 - _add}→{_lim1}（种族{_b1}末位{_b1 % 10}极限{_lim1}）")
                    if _bump_notes:
                        ev = ",".join(f"{a}={b}" for a, b in _cur.items())
                        logger.info("[seer_expert] ev 自动补满：%s", "；".join(_bump_notes))
            except Exception as _e1:  # noqa: BLE001
                logger.debug("[seer_expert] ev 自动补满跳过：%s", _e1)

        # ★ 只写了系列名（如「一龙子」）时，按上下文推方向补全
        #   依据：已给刻印的方向 + 学习力分配
        #   推出来的会在返回里明确标注「这是我推的，你没说」
        _inferred: list[dict] = []
        if mint_list:
            # 没有学习力也没给具体刻印时，complete_mint_series 推不出方向就会跳过，
            # 然后 check_mints 把「九天」模糊匹配成「太玄九天」（完全另一颗印）。
            # 所以这里先用**精灵自己的种族值**补一个方向依据。
            _ev_for_fill = str(ev or "")
            if not _ev_for_fill.strip() and not any("·" in str(m or "") for m in mint_list):
                try:
                    _hits = self.kb.find_pets(name, limit=1)
                    if _hits:
                        _st = self.kb.pet_stats(_hits[0][0]) or {}
                        _atk = int(_st.get("atk") or 0)
                        _spa = int(_st.get("sp_atk") or 0)
                        if _atk or _spa:
                            # 用具体数值而不是 "max"：infer_main_direction 只认
                            # v.isdigit()，"atk=max" 会被整条跳过。
                            _ev_for_fill = "atk=254" if _atk >= _spa else "sp_atk=254"
                            logger.info(
                                "[seer_expert] 系列补全方向按种族推：%s（攻%d/特攻%d）",
                                _ev_for_fill, _atk, _spa,
                            )
                except Exception:  # noqa: BLE001
                    pass
            # ★ 传 name：简称里的「攻/体/速/防」在成对系列里要按精灵流派落地
            #   （九天攻 @特攻精灵 → 九曲之光；九天体 @特攻精灵 → 九霄之灵）
            _fill = self.kb.complete_mint_series(mint_list, _ev_for_fill, name)
            if _fill.get("inferred"):
                mint_list = _fill["mints"]
                _inferred = _fill["inferred"]
                logger.info("[seer_expert] 系列名补全：%s",
                            "；".join(f"{x['series']}→{x['picked']}({x['reason']})"
                                     for x in _inferred))

        # ★ 刻印严格校验：写错就报错，绝不静默少装
        #   （之前 LLM 把「赤洲龙子·蒲牢」写成别的，图上少两颗，面板全错）
        _chk = self.kb.check_mints(mint_list)
        if _chk["bad"]:
            _msg = "刻印名对不上，先确认再出图：\n" + "\n".join(
                f"- {h}" for h in (_chk["hints"] or [f"「{b}」查不到" for b in _chk["bad"]])
            )
            _msg += "\n（用 seer_mint 可以看这只精灵该装哪三个）"
            return _msg
        if mint_list and len(_chk["ok"]) < len(mint_list):
            return "刻印数量对不上，重新确认一下要装哪三个。"

        # ★ 同系列最多装 2 个（2026-11-06 补）
        #   规则一直写在推荐逻辑的注释里，但没有强制校验 —— 三颗同系列的印照样能出图。
        if mint_list:
            _series_cnt: dict[str, int] = {}
            for _m in mint_list:
                try:
                    _cls = self.kb.mint_class(_m)
                except Exception:  # noqa: BLE001
                    _cls = ""
                if _cls:
                    _series_cnt[_cls] = _series_cnt.get(_cls, 0) + 1
            _over = [f"{k}×{v}" for k, v in _series_cnt.items() if v > 2]
            if _over:
                return (
                    f"同系列最多装 2 个，你现在是 {'、'.join(_over)}。\n"
                    "换掉一个（或者告诉我换成哪个系列）再出图。"
                )

        # ★ 战队跳过：留空时按定位自动推断
        if not team_skip:
            _auto_skip = self.kb.suggest_team_skip(name, nature, ev)
            if _auto_skip:
                team_skip = _auto_skip
                logger.info("[seer_expert] 战队自动跳过：%s（%s）", _auto_skip, name)

        # ★ 魂印派生：留空时从魂印原文自动识别
        if not str(soulmod or "").strip():
            _auto_sm = self.kb.detect_soulmod(name)
            if _auto_sm.get("average"):
                soulmod = "均分"
            elif _auto_sm.get("pct"):
                soulmod = f"体力{int(_auto_sm['pct'] * 100)}"
            elif _auto_sm.get("atk_diff_flag"):
                # 英雄之耀：层数无法从文本推，默认按满 5 层，并在返回里说明
                soulmod = "双攻差5"
                logger.info("[seer_expert] 英雄之耀按满 5 层计（%s）", name)
            if soulmod:
                logger.info("[seer_expert] 魂印派生自动识别：%s（%s）", soulmod, _auto_sm.get("reason"))


        # ★ 缺省自动补全（2026-11-03：之前 LLM 不传 nature/ev/mints 时，
        #   图上就是「中性 + 学习力空 + 没刻印」，它只能一口气试好几版才凑对。
        #   现在缺什么就按流派自动补，一次出对。）
        _preset = str(preset or "").strip() or "爆攻"     # 留空默认爆攻（最常见）
        _own = self.kb.pet_build(name) or {}               # 专属配装（用于对比/说明）
        b = self.kb.build_view(name, _preset, nature=nature, ev=ev,
                               suit=suit, title=title, eye=eye, talent=talent)
        if b.get("found"):
            # 性格：没给就先按「学习力侧重」推，推不出再用预设的
            #   ★ 为什么要在预设之前：预设的性格是「按精灵定位」定的，
            #     但用户实际配的学习力可能换了方向（比如玄武改刷特防），
            #     那时性格得跟着换，否则加成本吃不到。
            _nat_note = ""
            if not str(nature or "").strip():
                _picked = self.kb.pick_nature_by_ev(
                    b.get("pid") or 0, str(ev or ""), fallback=b.get("nature") or ""
                )
                if _picked.get("nature"):
                    nature = _picked["nature"]
                    _nat_note = _picked.get("reason") or ""
                    logger.info("[seer_expert] 性格按学习力侧重：%s", _nat_note)
                else:
                    nature = b.get("nature") or ""
            # 学习力：没给就用预设分配
            if not str(ev or "").strip():
                # 优先用专属表的原始写法（支持 "atk=max,rest=spd" 这类目标值语法）
                if b.get("ev_raw"):
                    ev = b["ev_raw"]
                else:
                    _plan = b.get("ev_plan") or {}
                    if _plan:
                        ev = ",".join(f"{k}={v}" for k, v in _plan.items())
            # 刻印：没给就用预设推荐的三件
            if not mint_list:
                mint_list = [x for x in (b.get("mints") or []) if x]
            # ★ 精灵专属配装的魂印/战队也要一起带过来
            #   （无极要「双攻差5」、无为要「均分」、玄武要「双防15」、
            #     多数物攻向要「跳特攻」——这些只在 pet_build 里才有）
            if not str(soulmod or "").strip():
                soulmod = b.get("soulmod") or ""
            if not str(team_skip or "").strip():
                team_skip = b.get("team_skip") or ""
            # 套装/称号/目镜同理
            if not suit:
                suit = b.get("suit") or ""
            if not title:
                title = b.get("title") or ""
            if not eye:
                eye = b.get("eye") or ""
        elif preset:
            # 用户显式给了 preset 但查不到这个精灵 → 报错，别硬算
            return b.get("empty_hint") or f"配装失败：{name}"

        # ★ 魂印派生：字符串来源三选一（没传就用 pet_build 的，再没有就自动识别）
        #   然后**统一解析** —— 解析必须放在所有赋值之后，
        #   之前放在前面导致 pet_build 的 soulmod 根本没被解析（玄武没转化就是这原因）。
        import re as _re2
        if not str(soulmod or "").strip():
            _auto = self.kb.detect_soulmod(name)
            if _auto.get("average"):
                soulmod = "均分"
            elif _auto.get("double_def_pct"):
                soulmod = f"双防{int(round(_auto['double_def_pct'] * 100))}"
            elif _auto.get("pct"):
                soulmod = f"体力{int(round(_auto['pct'] * 100))}"
            elif _auto.get("atk_diff_flag"):
                soulmod = "双攻差5"

        sm_pct = 0.0        # 消耗体力上限 N% 换其他五项
        sm_diff = 0         # 英雄之耀层数
        sm_avg = False      # 总和均分
        sm_ddef = 0.0       # 双防 X% 转化
        _sm = str(soulmod or "").strip()
        if _sm:
            # ★「双防」必须排在「体力」前面判断：两者都带数字，
            #   "双防15" 落到 else 会被当成体力换属性 pct=0.15。
            if "双防" in _sm or "转化" in _sm:
                _n = _re2.search(r"(\d+(?:\.\d+)?)", _sm)
                if _n:
                    _v = float(_n.group(1))
                    sm_ddef = _v / 100.0 if _v > 1 else _v
            elif "均分" in _sm or "总和" in _sm:
                sm_avg = True
            elif "双攻差" in _sm or "英雄之耀" in _sm:
                _n = _re2.search(r"(\d+)", _sm)
                sm_diff = int(_n.group(1)) if _n else 5
            elif "体力" in _sm:
                _n = _re2.search(r"(\d+(?:\.\d+)?)", _sm)
                if _n:
                    _v = float(_n.group(1))
                    sm_pct = _v / 100.0 if _v > 1 else _v
            else:
                _n = _re2.search(r"(\d+(?:\.\d+)?)", _sm)
                if _n:
                    _v = float(_n.group(1))
                    sm_pct = _v / 100.0 if _v > 1 else _v
            logger.info("[seer_expert] 魂印派生解析：%s → pct=%.2f diff=%d avg=%s ddef=%.2f",
                        _sm, sm_pct, sm_diff, sm_avg, sm_ddef)

        # ★ 2026-11-08：套装/称号/目镜默认给顶配，别留空。
        #   原因（用户报的）：空着的时候右下角的小鱼装饰图会错位 ——
        #   模板是按「三件都有」排版的，缺那几行高度就对不上。
        #   而且群友要的就是「顶配该长什么样」，空着反而还得多问一轮。
        #   传空/未传都回落默认；要「明确不带」传 "无" 或 "0"。
        def _default_eq(_val: str, _fallback: str) -> str:
            _s = str(_val or "").strip()
            if _s in ("无", "0", "none", "None", "-"):
                return ""
            return _s or _fallback

        suit = _default_eq(suit, "晨曦")
        title = _default_eq(title, "神话")
        eye = _default_eq(eye, "漫游者-AH")

        view = self.kb.config_view(
            name, talent=talent, nature=nature or "",
            ev=ev or 0, mints=mint_list, gems=gem_list,
            suit=suit, title=title, eye=eye,
            team_skip=team_skip, hpup=hpup,
            soulmod_pct=sm_pct, soulmod_atk_diff=sm_diff, soulmod_average=sm_avg,
            soulmod_double_def_pct=sm_ddef,
        )
        if not view.get("found"):
            return view.get("empty_hint") or f"找不到精灵：{name}"

        try:
            from .render_card import render_config_card
            data = await render_config_card(view)
        except Exception as exc:
            return f"配置图渲染失败：{exc}"

        # 存临时文件并发图
        import tempfile
        fd, path = tempfile.mkstemp(prefix="seer_config_", suffix=".png")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)
        _sent = False
        try:
            from astrbot.core.message.message_event_result import MessageChain
            chain = MessageChain(chain=[ImageComp.fromFileSystem(path)])
            await self.context.send_message(event.unified_msg_origin, chain)
            _sent = True
        except Exception as exc:
            logger.warning("[seer_expert] 配置图发送失败: %s", exc)
        # 记住这次用的参数，供后续增量修改
        try:
            _key = str(getattr(event, "unified_msg_origin", "") or "default")
            self._config_cache[_key] = {
                "name": name, "talent": talent, "nature": nature or "",
                "ev": ev or "", "mints": list(mint_list), "gems": list(gem_list),
                "suit": suit, "title": title, "eye": eye,
                "team_skip": team_skip, "hpup": hpup,
                "soulmod_pct": sm_pct, "soulmod_atk_diff": sm_diff,
                "soulmod_average": sm_avg,
            }
        except Exception:
            pass

        _six = " / ".join(f"{b['cn']}{b['value']}" for b in (view.get("battle") or []))
        _auto = []
        if team_skip:
            _auto.append(f"战队跳{team_skip}")
        if soulmod:
            _auto.append(f"魂印派生={soulmod}")
        _note = ("（自动：" + "、".join(_auto) + "）") if _auto else ""
        # ★ 把实际用上的配置原样回报 —— 之前只说「六项：xxx」，
        #   LLM 选错刻印（比如拿爆攻印顶双防位）外面完全看不出来，
        #   只能等群友看图发现。这不是「纠正用户」，是让他看见自己传了什么。
        _mint_line = " / ".join(
            (m.get("name") or "") for m in (view.get("mintmarks") or []) if m
        ) or "（无）"
        _diff = ""
        if _own.get("mints"):
            _want = list(_own["mints"])
            _used = [m.get("name") for m in (view.get("mintmarks") or []) if m]
            # ★ 比较要忽略顺序：刻印会按 A/B/A 对称重排，
            #   ["威临","威临","信仰"] 重排后变 ["威临","信仰","威临"]，
            #   直接比 list 会误报「与专属推荐不同」。
            if _used and sorted(_used) != sorted(_want):
                _diff = (f"　※ 与专属推荐不同（推荐：{' / '.join(_want)}）"
                         f"—— 按你给的来，没替换")
        # 推理出来的刻印要单独说明：这是按上下文推的，不是你说的
        _infer_line = ""
        if _inferred:
            _infer_line = "\n※ 推理补全：" + "；".join(
                f"{x['series']}→{x['picked']}（{x['reason']}，你没说具体哪条）"
                for x in _inferred
            )
        _head = f"（已生成 {view['pet']['name']} 的配置图" if _sent else "配置图渲染好了但发送失败"
        return (f"{_head}{_note}。{_infer_line}\n"
                f"刻印：{_mint_line}{_diff}\n"
                f"性格：{nature or '中性'} ｜ 学习力：{ev or '0'}\n"
                f"六项：{_six}）")

    @llm_tool(name="seer_config_tweak")
    async def seer_config_tweak(
        self,
        event: AstrMessageEvent,
        changes: str,
        name: str = "",
    ) -> str:
        """在**上一次生成的配置图**基础上改数值，重新出一张数据图（不是改画）。

        群友看过图说「速度拉满」「体力改到1100」「第2个刻印换九天攻」时用这个，
        不用重新问一遍配装。

        【changes 语法】（多项目用逗号分隔）
        · 学习力（直接指定）    "ev:spd=252"  "ev:atk=252"  "ev:hp=252"
        · 目标面板值（自动反推学习力，最多 252）
              "hp=1100"      体力最终值改到 1100
              "spdef=450"    特防最终值改到 450
              "spd=600"      速度最终值改到 600
              可用：atk / def / sp_atk / sp_def / spd / hp（也认中文：攻/防/特攻/特防/速/体）
        · 把某项刷满            "ev:spd=max"
        · 换刻印                "mint1=九天攻"  "mint2=赤洲龙子·蒲牢"（1~3）
        · 换宝石                "gem1=威力宝石Lv8@星纪"  "gem2="（清空）
        · 换装备                "nature=开朗"  "suit=裁决"  "title=星光之旅"  "eye=漫游者-AH"
        · 换魂印派生            "soulmod=均分"  "soulmod=体力20"  "soulmod=双攻差5"

        Args:
            changes(string): 修改项，见上。例："ev:spd=max,hp=1100"
            name(string): 精灵名。留空 = 用上一次那只
        """
        _miss = self._db_missing()
        if _miss:
            return _miss
        _key = str(getattr(event, "unified_msg_origin", "") or "default")
        base = self._config_cache.get(_key)
        if not base:
            return "没有上一张配置图可以改。先让我生成一张（seer_config_card），再说要改哪项。"

        cur = dict(base)
        if name and name.strip():
            cur["name"] = name.strip()

        # ── 解析 changes ──
        alias = {"攻": "atk", "防": "def", "特攻": "sp_atk", "特防": "sp_def",
                 "速": "spd", "体": "hp"}
        ev_map: dict[str, int] = {}
        for piece in str(cur.get("ev") or "").replace("，", ",").split(","):
            piece = piece.strip()
            if "=" in piece:
                kk, vv = piece.split("=", 1)
                kk = alias.get(kk.strip(), kk.strip())
                if vv.strip().lstrip("-").isdigit():
                    ev_map[kk] = int(vv)

        applied: list[str] = []
        failed: list[str] = []
        targets: dict[str, int] = {}

        for raw in str(changes or "").replace("，", ",").replace(";", ",").split(","):
            piece = raw.strip()
            if not piece or "=" not in piece:
                continue
            key, _, val = piece.partition("=")
            key = key.strip()
            val = val.strip()
            kl = key.lower()

            if kl.startswith("ev:"):
                kk = alias.get(key[3:].strip(), key[3:].strip())
                if val.lower() in ("max", "满", "拉满"):
                    ev_map[kk] = 252
                    applied.append(f"{kk} 学习力拉满 252")
                elif val.isdigit():
                    ev_map[kk] = int(val)
                    applied.append(f"{kk} 学习力 → {val}")
                else:
                    failed.append(piece)
                continue

            if kl.startswith("mint") and kl[4:].isdigit():
                idx = int(kl[4:]) - 1
                if 0 <= idx < 3:
                    mm = list(cur.get("mints") or [])
                    while len(mm) < 3:
                        mm.append("")
                    mm[idx] = val
                    cur["mints"] = mm
                    applied.append(f"第{idx+1}个刻印 → {val}")
                continue

            if kl.startswith("gem") and kl[3:].isdigit():
                idx = int(kl[3:]) - 1
                if 0 <= idx < 3:
                    gg = list(cur.get("gems") or [])
                    while len(gg) < 3:
                        gg.append("")
                    gg[idx] = val
                    cur["gems"] = gg
                    applied.append(f"第{idx+1}颗宝石 → {val or '（清空）'}")
                continue

            if kl in ("nature", "性格"):
                cur["nature"] = val
                applied.append(f"性格 → {val}")
                continue
            if kl in ("suit", "套装"):
                cur["suit"] = val
                applied.append(f"套装 → {val}")
                continue
            if kl in ("title", "称号"):
                cur["title"] = val
                applied.append(f"称号 → {val}")
                continue
            if kl in ("eye", "目镜"):
                cur["eye"] = val
                applied.append(f"目镜 → {val}")
                continue
            if kl in ("team_skip", "战队"):
                cur["team_skip"] = val
                applied.append(f"战队跳过 → {val}")
                continue
            if kl in ("soulmod", "魂印"):
                cur["soulmod_pct"] = 0.0
                cur["soulmod_atk_diff"] = 0
                cur["soulmod_average"] = False
                if "均分" in val or "总和" in val:
                    cur["soulmod_average"] = True
                elif "双攻差" in val:
                    import re as _re2
                    _m = _re2.search(r"(\d+)", val)
                    cur["soulmod_atk_diff"] = int(_m.group(1)) if _m else 5
                elif val:
                    import re as _re2
                    _m = _re2.search(r"(\d+(?:\.\d+)?)", val)
                    if _m:
                        _v = float(_m.group(1))
                        cur["soulmod_pct"] = _v / 100.0 if _v > 1 else _v
                applied.append(f"魂印派生 → {val}")
                continue

            # 目标面板值（不带前缀）
            kk = alias.get(key, kl)
            if kk in ("atk", "def", "sp_atk", "sp_def", "spd", "hp") and val.lstrip("-").isdigit():
                targets[kk] = int(val)
                continue
            failed.append(piece)

        # ── 目标面板值反推学习力 ──
        #    ★关键：学习力总预算 510。给某一项加点时，必须从其他项里挪，
        #      否则总和超限算出来的数是假的（实测 spd=252 但总和 606 会虚高）。
        EV_CAP = 510
        if targets:
            for kk, want in targets.items():
                # 其他项按原比例让出预算
                others = {a: b for a, b in ev_map.items() if a != kk}
                other_sum = sum(others.values())

                def _budget(ev_try):
                    """把 ev_try 给目标项后，其他项按比例缩到 EV_CAP 以内。"""
                    room = max(0, EV_CAP - ev_try)
                    if other_sum <= room or other_sum == 0:
                        return dict(others)
                    ratio = room / other_sum
                    scaled = {a: int(b * ratio) for a, b in others.items()}
                    # 余数补给最大项，别浪费
                    left = room - sum(scaled.values())
                    if scaled and left > 0:
                        big = max(scaled, key=lambda x: scaled[x])
                        scaled[big] += left
                    return scaled

                best_ev, best_val, best_gap = None, None, None
                for ev_try in range(0, 253, 4):
                    trial = _budget(ev_try)
                    trial[kk] = ev_try
                    ev_str = ",".join(f"{a}={b}" for a, b in trial.items())
                    tv = self.kb.config_view(
                        cur["name"], talent=int(cur.get("talent") or 31),
                        nature=cur.get("nature") or "", ev=ev_str,
                        mints=list(cur.get("mints") or []),
                        gems=list(cur.get("gems") or []),
                        suit=cur.get("suit") or "", title=cur.get("title") or "",
                        eye=cur.get("eye") or "", team_skip=cur.get("team_skip") or "",
                        hpup=int(cur.get("hpup") or 20),
                        soulmod_pct=float(cur.get("soulmod_pct") or 0),
                        soulmod_atk_diff=int(cur.get("soulmod_atk_diff") or 0),
                        soulmod_average=bool(cur.get("soulmod_average")),
                    )
                    if not tv.get("found"):
                        break
                    row = next((b for b in tv["battle"] if b["key"] == kk), None)
                    if not row:
                        break
                    gap = abs(int(row["value"]) - want)
                    if best_gap is None or gap < best_gap:
                        best_ev, best_val, best_gap = ev_try, int(row["value"]), gap
                    if best_gap == 0:
                        break
                if best_ev is not None:
                    ev_map[kk] = best_ev
                    if best_gap == 0:
                        applied.append(f"{kk} 目标 {want} → 学习力 {best_ev}（正好 {best_val}）")
                    elif best_ev >= 252:
                        applied.append(
                            f"{kk} 刷满 252 也只能到 {best_val}（差 {want - best_val}）"
                            f"——这个配置该项上限就是 {best_val}"
                        )
                    else:
                        applied.append(
                            f"{kk} 目标 {want} → 学习力 {best_ev}（实际 {best_val}，差 {best_gap}）"
                        )

        # ── 重新生成 ──
        ev_str = ",".join(f"{a}={b}" for a, b in ev_map.items()) or "0"
        cur["ev"] = ev_str
        view = self.kb.config_view(
            cur["name"], talent=int(cur.get("talent") or 31),
            nature=cur.get("nature") or "", ev=ev_str,
            mints=list(cur.get("mints") or []),
            gems=list(cur.get("gems") or []),
            suit=cur.get("suit") or "", title=cur.get("title") or "",
            eye=cur.get("eye") or "", team_skip=cur.get("team_skip") or "",
            hpup=int(cur.get("hpup") or 20),
            soulmod_pct=float(cur.get("soulmod_pct") or 0),
            soulmod_atk_diff=int(cur.get("soulmod_atk_diff") or 0),
            soulmod_average=bool(cur.get("soulmod_average")),
        )
        if not view.get("found"):
            return view.get("empty_hint") or "改完之后算不出来了"

        try:
            from .render_card import render_config_card
            data = await render_config_card(view)
        except Exception as exc:
            return f"配置图渲染失败：{exc}"

        import tempfile
        fd, path = tempfile.mkstemp(prefix="seer_config_", suffix=".png")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)

        _sent = False
        try:
            from astrbot.core.message.message_event_result import MessageChain
            chain = MessageChain(chain=[ImageComp.fromFileSystem(path)])
            await self.context.send_message(event.unified_msg_origin, chain)
            _sent = True
        except Exception as exc:
            logger.warning("[seer_expert] 配置图发送失败: %s", exc)

        self._config_cache[_key] = cur
        _six = " / ".join(f"{b['cn']}{b['value']}" for b in view.get("battle") or [])
        _done = "；".join(applied) if applied else "没有识别到有效修改"
        _bad = ("　未识别：" + "、".join(failed)) if failed else ""
        if _sent:
            return f"（已按 {_done} 重新出图。六项：{_six}{_bad}）"
        return f"改完了但发送失败。{_done}。六项：{_six}{_bad}"

    @llm_tool(name="seer_stat")
    async def seer_stat(
        self,
        event: AstrMessageEvent,
        name: str,
        talent: int = 31,
        nature: str = "",
        ev: str = "0",
        mints: str = "",
        suit: str = "",
        title: str = "",
        eye: str = "",
    ) -> str:
        """算精灵的满级面板：种族值 → 六项能力值（可叠刻印/套装/称号/目镜）。

        Args:
            name(string): 精灵名
            talent(int): 天赋/个体值，满个体 31
            nature(string): 性格，如「固执」「开朗」；空=中性
            ev(string): 学习力。整数=六项同刷；分项写 "atk=254,hp=148,spd=76"
            mints(string): 刻印，逗号分隔最多 3 个。支持简称如「九天攻」「圣战体」「v8」
            suit(string): 套装名，如「晨曦」
            title(string): 称号名，如「神话」
            eye(string): 目镜名，如「漫游者-AH」
        """
        _miss = self._db_missing()
        if _miss:
            return _miss
        mint_list = [x.strip() for x in str(mints or "").replace("，", ",").split(",") if x.strip()]
        v = self.kb.stat_view(
            name, talent=talent, nature=nature, ev=ev,
            mints=mint_list, suit=suit, title=title, eye=eye,
        )
        if not v["found"]:
            return v.get("empty_hint") or f"找不到精灵：{name}"

        sep = chr(10)
        lines = []
        head = f"【{v['name']}】#{v['pid']}"
        if nature:
            head += f"｜{nature}性格"
        head += f"｜天赋{talent}"
        lines.append(head)
        if v.get("mint_names"):
            lines.append("刻印：" + " / ".join(v["mint_names"]))
        if v.get("missing_mints"):
            lines.append("⚠ 未找到的刻印：" + "、".join(v["missing_mints"]))
        if v.get("overlay_hint"):
            lines.append(v["overlay_hint"])

        lines.append("")
        for r in v["rows"]:
            seg = f"{r['cn']} {r['value']}"
            if r["mint"]:
                seg += f" +刻印{r['mint']}"
            if r["overlay"]:
                seg += f" +其他{r['overlay']}"
            if r.get("flat"):
                seg += f" +战队年费{r['flat']}"
            if r["mint"] or r["overlay"] or r.get("flat"):
                seg += f" = {r['final']}"
            lines.append(f"- {seg}")
        if v.get("total"):
            lines.append(f"六项和：{v['total']}")
        _flat_note = "已含战队+年费" if v.get("flat_total") else "不含战队/年费"
        lines.append(f"（口径：体力=种族×2+学习力÷4+个体+110；其余=(种族×2+学习力÷4+个体+5)×性格；{_flat_note}）")
        return sep.join(lines)

    @llm_tool(name="seer_pct")
    async def seer_pct(self, event: AstrMessageEvent,
                       target_max_hp: int,
                       ratio: str = "1/3",
                       segments: int = 1,
                       resist: int = 35,
                       kind: str = "pct",
                       caster_cur_hp: int = 0,
                       caster_max_hp: int = 0) -> str:
        """算百分比伤害 / 吸取 / 固定伤害 / 真实伤害的**实际数值**。

        ★ 什么时候用它：
          问「这条粉打多少」「吸取回多少」「会不会被斩杀」这类**纯百分比伤害**的问题。
          问「一刀打多少」用 seer_damage（那个算攻击伤害）。

        ★ 口径已经写死在工具里，不用你算、也不要去查规则：
          · 粉/吸取按 **对手** 最大体力算（不是施法者的）
          · 两步取整：① 量 = floor(对手体力 × 系数 × 翻倍) ② 实扣 = 量 − floor(量 × 抗性)
          · 满抗性 35；真伤不吃抗性
          · 吸取 = 扣对手等量 + 自己回等量，回血量按折算后的算
          · 吸取时施法者低于半血 → 系数先×2（这一步工具会自动判）

        Args:
            target_max_hp(int): **对手**最大体力。粉和吸取都按这个算。
            ratio(string): 比例。吃「1/3」「三分之一」「1/6」「33%」「0.33」四种写法。
                kind="true" 时这里填**点数**（如 "50" = 50 点真伤）。
            segments(int): 几段。神罡 + 魂印同时点触发就是 2 段。默认 1。
            resist(int): 抗性百分比，满抗性 35。真伤忽略此项。
            kind(string): pct=百分比伤害（默认）/ drain=吸取 / fixed=固定伤害 / true=真实伤害
            caster_cur_hp(int): 只在 kind=drain 时给，施法者当前体力（判半血翻倍）。
            caster_max_hp(int): 同上，施法者最大体力。

        Returns:
            str: 单段/合计的实际数值与拆解。

        示例：
            「灭盖神罡两段 1/3 粉，对手 1000 血」
              → seer_pct(target_max_hp=1000, ratio="1/3", segments=2)
              → 单段 217 / 合计 434

            「槟榔 466/1000 血，吸取对手 1165 的 1/3」
              → seer_pct(target_max_hp=1165, ratio="1/3", kind="drain",
                          caster_cur_hp=466, caster_max_hp=1000)
              → 低于半血翻倍，扣 505 回 776（回血不过抗，取吸取的量本身）
        """
        r = self.kb.pct_calc(
            target_max_hp, ratio=ratio, segments=segments, resist=resist,
            kind=kind, caster_cur_hp=caster_cur_hp, caster_max_hp=caster_max_hp,
        )
        if not r.get("ok"):
            return r.get("error") or "算不出来，检查参数"

        sep = chr(10)
        kd = r["kind"]
        label = {"pct": "百分比伤害", "drain": "吸取", "fixed": "固定伤害",
                 "true": "真实伤害"}.get(kd, "百分比伤害")
        lines = [f"【{label}】按对手最大体力 {r['target_max_hp']}"]
        if kd != "true":
            lines.append(f"  基数：对手【最大】体力 {r['target_max_hp']}"
                         f"（跟对手当前剩多少血无关）")

        if kd == "true":
            lines.append(f"  {r['amount']} 点（真伤不吃抗性）")
            if r["segments"] > 1:
                lines.append(f"  {r['segments']} 段：{r['total_dealt']}")
        else:
            det = r.get("detail") or []
            if len(det) > 1:
                # ★ 多段必须逐段过抗，把每段都摊开（不然看不出是不是合并算了）
                for i, d in enumerate(det, 1):
                    lines.append(
                        f"  第{i}段：{d['raw']} → 取整 {d['amount']} → "
                        f"扣抗 {d['resist_cut']} → 实扣 {d['actual']}"
                    )
                lines.append(f"  合计：{r['total_dealt']}（逐段过抗，不是合并后过一次）")
            else:
                lines.append(f"  原始 {r['raw']} → 取整 {r['amount']}")
                lines.append(
                    f"  过抗 {r['resist']}%：{r['amount']} − floor({r['amount']}×{r['resist']}%)"
                    f" = {r['amount']} − {r['resist_cut']} = {r['per_segment']}"
                )
                lines.append(f"  实扣：{r['total_dealt']}")
        if kd == "drain":
            # ★ 2026-11-10：扣对手过抗，自己回血**不过抗**（取吸取的量本身）
            lines.append(f"  自己回血：{r['healed']}"
                         + ("（取吸取的量，不过抗）" if r["healed"] != r["total_dealt"] else ""))
        if r.get("note"):
            lines.append(f"  （{r['note']}）")
        return sep.join(lines)

    @llm_tool(name="seer_speed")
    async def seer_speed(self, event: AstrMessageEvent,
                         left_name: str = "左方", left_spd: int = 0,
                         left_pri: int = 0, left_must_first: bool = False,
                         right_name: str = "右方", right_spd: int = 0,
                         right_pri: int = 0, right_must_first: bool = False,
                         left_rank: int = 0, right_rank: int = 0,
                         tie_winner: str = "",
                         left_move: str = "", right_move: str = "") -> str:
        """判定**本回合**谁先手：必先等级 > 必先 > 先制等级 > 速度。

        ★ 为什么每回合都要调一次：
          先制等级是动态的。技能自带的先制只在用它的那回合有；
          Buff 写的「下N回合先制+2」只覆盖 N 回合，过期就归零。
          例：丧月之夜给「下2回合先制+2」→ T2/T3 有 +2，T4 就回到 0。
          **不要沿用上一回合的判定结果**，每回合拿当前先制等级重算。

        ★ 先制等级怎么算（传进来之前先加好）：
          当前先制 = 本回合所用技能自带的先制 + 身上还挂着的先制 Buff
          例：槟榔 T1 用丧月之夜（自带 0）→ 先制 0
              槟榔 T2 用寒渊之殇（自带 0）+ 丧月之夜 Buff（+2）→ 先制 2
              槟榔 T4 用寒渊之殇（自带 0）+ Buff 已过期 → 先制 0

        ★ 必先等级（2026-11-10）：
          必先不是「有/没有」两态，而是 11 级——rank 1 最高、11 最低。
          **等级由本工具自己查表**：把精灵名（外号也行，如「光螳螂」「表姐」）
          或本回合用的技能名填进 left_name / left_move，rank 自动解析出来。
          不需要先去调别的工具再回来传参。
          left_rank / right_rank 仍然可以显式指定（优先级最高，用于表里没有的情况）。
          常见几档：
            rank 1  圣灵谱尼「永恒」／鲜血女王「燃血状态」（魂印特性类）
            rank 2  缔笙重返初识／天幕神龙天幕威仪／艾恩斯王·焚世烈焰 等
            rank 3  帝皇之御守御八方（次数必先）
            rank 4  幻影蝶竭血残蝶／刘备白帝托孤／费德约克钢铁熔炉（次数必先）
            rank 5~6 蒂朵幻梦芳逝／核天使之核天使之陨（次数必先）
            rank 7~8 英卡洛斯无始源光／王·雷伊雷霆凯歌
            rank 9~11 体力阈值类：月照星魂(<200)／潜灵灵蛇(<250)／库贝萨(<300)
          双方同级时按规则③由房主先手，用 tie_winner 指定（"left"/"right"）。

        ★ 三条容易忘的规则：
          · 嗑药、切换等弃权操作【不】令必先生效
          · 次数必先只在出手时消耗，被控可「生效但不消耗」
          · 必先无视双方一切先制与速度

        Args:
            left_name(string): 左方名字，只用于输出。
            left_spd(int): 左方**当前**速度面板值。
            left_pri(int): 左方**当前**先制等级（含 Buff，可为负）。
            left_must_first(boolean): 左方是否有「必定先手」类效果（不知道具体等级时用）。
            right_name(string): 右方名字，只用于输出。
            right_spd(int): 右方**当前**速度面板值。
            right_pri(int): 右方**当前**先制等级（含 Buff，可为负）。
            right_must_first(boolean): 右方是否有「必定先手」类效果（不知道具体等级时用）。
            left_rank(int): 左方必先等级 1~11（显式指定；0=交给工具自动查表）。
                一般不用填——把精灵名或技能名传进 left_name / left_move 即可。
            right_rank(int): 右方必先等级 1~11（显式指定；0=自动查表）。
            tie_winner(string): 双方必先同级时，**房主在哪一侧**："left" 或 "right"。
                ★ 不知道房主是谁就留空 —— 工具会明说「按左方=房主暂判」并提醒你确认，
                  不会默默替你决定。同级时房主换边，先手方就换人。
                ★ 游戏里怎么认房主：血条上带【小旗子】的一方是房主（2026-11 上旬 UI 更新后）。
                  旧的 H5 / Flash 方法（记录栏登场顺序 / 药剂界面头像）已基本没人用，
                  只在 docs 里作为历史资料保留，别拿它回答现行对局。
            left_move(string): 左方本回合使用的技能名，可留空。填了查必先等级更准。
            right_move(string): 右方本回合使用的技能名，可留空。

        Returns:
            str: 谁先手 + 按哪一层判的。

        示例（灭盖 vs 槟榔那次推演）：
            T1 槟榔先制0（丧月之夜）vs 灭盖+1（战·神罡不灭）→ 灭盖先手
            T2 槟榔先制+2（丧月之夜 Buff 生效）vs 灭盖+1 → 槟榔先手
            T4 槟榔先制0（Buff 过期）vs 灭盖+1 → 灭盖先手 ← 就是这一回合翻的盘

        示例（必先等级）：
            月照星魂 rank9 vs 王·雷伊 rank8 → 王·雷伊先手（8 比 9 优先）
            双方都 rank2 → 按 tie_winner 判给房主
        """
        # ★ 2026-11-10（鱼片指出）：不让模型在「查 rank → 传 rank」之间搬数字。
        #   名字或技能名进来，等级在这里自己解析；显式 rank 仍然优先。
        lres = self.kb.first_move_resolve(left_name, left_move, left_rank)
        rres = self.kb.first_move_resolve(right_name, right_move, right_rank)
        r = self.kb.speed_order(
            left_name=left_name, left_spd=left_spd, left_pri=left_pri,
            left_must_first=left_must_first,
            right_name=right_name, right_spd=right_spd, right_pri=right_pri,
            right_must_first=right_must_first,
            left_rank=lres["rank"], right_rank=rres["rank"], tie_winner=tie_winner,
        )
        sep = chr(10)
        L, R = r["left"], r["right"]
        lines = ["【先手判定】必先等级 > 必先 > 先制等级 > 速度"]
        for side, res in ((L, lres), (R, rres)):
            if side.get("rank"):
                extra = f"（必先 rank {side['rank']}｜{res['hint']}）"
            elif side["must_first"]:
                extra = "（有必先但等级未收录 → 按最低档处理）"
            else:
                extra = ""
            lines.append(
                f"  {side['name']}：速度 {side['spd']}，先制等级 {side['pri']:+d}{extra}")
        who = {"left": L["name"], "right": R["name"], "random": "随机"}[r["first"]]
        lines.append(f"  → **{who} 先手**（{r['layer']}层）")
        lines.append(f"  依据：{r['reason']}")
        if r["layer"] == "先制":
            lines.append("  （先制等级每回合要重算；Buff 只管「下N回合」）")
        elif r["layer"] == "必先同级":
            if r.get("tie_specified"):
                lines.append("  （房主已在参数里指定；房主换到另一边，先手方就换人）")
            else:
                lines.append("  ⚠ 你没说谁是房主。规则③是「同级必先由【房主】先手」，")
                lines.append("    上面是按「左方=房主」暂判的 —— 房主在右边的话先手方就换成对方。")
                lines.append("    请确认房主在哪一侧（tie_winner 填 left / right）。")
                lines.append("    （识别房主：血条上带【小旗子】的一方 —— 2026-11 上旬 UI 更新后）")
        elif r["layer"] == "必先等级":
            lines.append("  （必先 rank 1 最高、11 最低；等级表见 kb.first_move_rank）")
        # 查不到等级、但调用方明确说有必先 → 说清楚是按最低档处理的
        missing = []
        if left_must_first and lres["rank"] == 0:
            missing.append(left_name)
        if right_must_first and rres["rank"] == 0:
            missing.append(right_name)
        if missing:
            lines.append(f"  ⚠ 必先等级表里没有「{'」「'.join(missing)}」，"
                         f"已按最低档（rank 99）处理；知道准确等级请告诉我补录")
        return sep.join(lines)

    @llm_tool(name="seer_first_move")
    async def seer_first_move(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """查「必定先手」的等级表（11 级，rank 1 最高、11 最低）。

        ★ 什么时候必须查：
          问「XX 必先是第几级」「XX 的必先和 YY 的必先谁快」「谁先手」时，
          先查这张表拿到 rank，再把 rank 传给 seer_speed。
          **不要只看「有必先/没必先」——同一档必先之间也要比等级。**

        ★ 为什么不查就会答错（真实例子）：
          光螳螂（光之惩戒·英卡洛斯）无始源光 = rank 7
          表姐（帝皇之御）守御八方               = rank 3
          光螳螂速度 140 > 表姐 126，若只按「都有必先」退回比速度，
          会判光螳螂先手 —— 错。按等级应该是 rank 3 的表姐先手。

        ★ 表下 6 条规则（本工具会在需要时一并给出）：
          必先无视一切先制与速度；同级由房主先手；嗑药/切换不令必先生效；
          次数必先只在出手时消耗，被控可「生效但不消耗」。

        Args:
            keyword(string): 精灵名/外号/技能名/描述关键词，如「光螳螂」「守御八方」「体力低于」。
                留空则列出全部 22 条 + 规则。

        Returns:
            str: 命中的必先条目（rank/精灵/技能/次数或回合/原文），以及规则。
        """
        v = self.kb.first_move_lookup(keyword or "")
        pv = self.kb.first_move_of_pet(keyword or "") if (keyword or "").strip() else \
            {"ok": False, "items": [], "pet": ""}
        sep = chr(10)
        kw = keyword or ""
        if not v["items"] and not pv.get("items"):
            lines = [f"必先表里没有匹配「{kw}」的条目。"]
            lines.append("  （表是人工整理的已知清单，查不到≠这技能没有必先，"
                         "请按原文判断后告诉我补录）")
            return sep.join(lines)

        out = []
        if v["items"]:
            head = ("【必先等级表】rank 1 最高、11 最低；数字越小越优先"
                    + (f"｜关键词「{kw}」命中 {len(v['items'])} 条" if kw else
                       f"｜共 {len(v['items'])} 条"))
            if v.get("resolved") and v["resolved"] != kw:
                head += f"（「{kw}」按外号解析为 {v['resolved']}）"
            out += [head, ""]
            for it in v["items"]:
                eid = f"｜模板 {it['effect_id']}" if it.get("effect_id") else "｜（魂印/特性，无模板）"
                out.append(f"  rank {it['rank']:<2} {it['pet']}·{it['move']}"
                           f"｜{it['kind']}必先｜{it['scope']}{eid}")
                if it.get("note"):
                    out.append(f"        ⚠ {it['note']}")
                out.append(f"        {it['desc']}")
            out.append("")

        # 这只精灵自己有哪些必先技能（含表里没收录、但复用了同模板的技能）
        if pv.get("items"):
            out.append(f"【{pv['pet']} 的必先技能】")
            for it in pv["items"]:
                if it["via"] == "template":
                    out.append(f"  {it['move']}｜rank {it['rank']}（{it['kind']}必先）"
                               f"｜模板 {it['effect_id']} 与表内同代码，等级直接继承")
                else:
                    out.append(f"  {it['move']}｜判定为必先，但等级未收录"
                               f"（模板 {it['effect_id']}）——知道等级告诉我补录")
            out.append("")
            out.append("→ 本回合用哪一招就把它传进 seer_speed 的 left_move / right_move，"
                       "等级会自动对上。")

        if v["items"] and v["rules"]:
            out.append("【规则】")
            for i, rr in enumerate(v["rules"], 1):
                out.append(f"  {i}. {rr}")
        return sep.join(out)

    @llm_tool(name="seer_damage")
    async def seer_damage(
        self,
        event: AstrMessageEvent,
        power: int,
        atk: int,
        defense: int,
        level: int = 100,
        attack_type: str = "",
        defend_type: str = "",
        type_mult: float = 0.0,
        type_mult_cap: float = 0.0,
        stab: float = 1.0,
        crit: float = 1.0,
        other: float = 1.0,
        random: str = "range",
        power_boost: float = 0.0,
    power_flat: float = 0.0,
    variable_power: bool = False,
        general_add: float = 0.0,
        extra_add: str = "",
        general_reduce: float = 0.0,
        extra_reduce: str = "",
        resist: str = "full",
        flat: str = "",
        percent: str = "",
        true_damage: float = 0.0,
        attr_damage: float = 0.0,
        attr_type: str = "",
        atk_stage: int = 0,
        defense_stage: int = 0,
        hits: int = 1,
        target_hp: float = 0.0,
    ) -> str:
        """估算一次攻击的伤害并给出乘区拆解。魂印/特性请折算成具体参数。

        Args:
            power(int): 技能面板威力
            atk(int): 攻击/特攻面板
            defense(int): 防守方防御/特防面板
            level(int): 攻击方等级，满级 100
            attack_type(string): 技能属性，支持双属性如「战斗/龙」
            defend_type(string): 防守方属性，支持双属性如「草/冰」
            type_mult(float): 克制倍率。留空（0）且给了 attack_type+defend_type 时自动算
            stab(float): 本系 1.5，否则 1.0
            crit(float): 暴击 2.0，不暴击 1.0
            other(float): 其他乘算修正（优先用下面具体参数）
            random(string): 浮动。'range'=区间 / 'min' / 'max' / 'avg' 或 217~255 的整数档。
                ★浮动是 (217~255)/255 的 39 个离散档，每档 1/39，不是 [0.85,1.0] 连续区间
            power_boost(float): 威力加成比例（冰霜这类「X属性技能威力+N%」）。改的是威力，不是总伤
    power_flat(float): 威力固定加成点数（威力宝石走这里：Lv1~10 = 1/4/7/10/14/17/20/24/27/30）。
        加在百分比之后，因为原文写明「不受其他威力提升/下降效果加成」
    variable_power(bool): 这一发是不是「变威力技能」。为真时 power_boost 和 power_flat 一律不生效
        —— 依据变威力的覆盖清单，威力宝石和特性单属性增伤都在被覆盖之列。
        库里的效果文本没有「变威力」字样（0 条命中），只能由调用方判断后传
            general_add(float): 通用增伤，同类【相加】。强能/绝命/爆伤（+25%）走这里
            extra_add(string): 额外增伤，逗号分隔，多个【连乘】。如 "0.2,1.0"
                强袭/精神（二者互斥，合并算一个乘区）、傲视100%、技能「下N回合额外+100%」走这里
            general_reduce(float): 通用减伤，同类【相加】
            extra_reduce(string): 额外减伤，逗号分隔，多个【连乘】。坚硬走这里；如 "0.10"
            resist(string): 抗性档位 'full'(0.35) / 'none'(0) / 具体值或 "0.35,0.2,0.1"
            flat(string): 固定伤害，'5' 或 '1,1,1'（多段各自过抗性）
            percent(string): 百分比伤害系数，'0.125' 或 '0.125,0.0625'
            true_damage(float): 真实伤害，不吃任何减免
            attr_damage(float): 属性伤害数值（只吃克制，不吃抗性/本系/增伤）
            attr_type(string): 属性伤害系别，空则用 attack_type
            atk_stage(int): 攻击等级 -6~+6（先换算面板再进公式）
            defense_stage(int): 防御等级 -6~+6
            hits(int): 连击次数（>1 走「单段取整、浮动只掷一次」口径）
            target_hp(float): 防守方最大体力，用于百分比伤害
        """
        _miss = self._db_missing()
        if _miss:
            return _miss

        # 全部判定（克制、浮动档位、乘区归区）来自 dmg_view（语义层，和 CLI 共用）
        v = self.kb.dmg_view(
            power=power, atk=atk, defense=defense, level=level,
            attack_type=attack_type, defend_type=defend_type, type_mult=type_mult,
            type_mult_cap=type_mult_cap,
            stab=stab, crit=crit, other=other, random=random,
            power_boost=power_boost,
            power_flat=power_flat,
            variable_power=variable_power,
            general_add=general_add, extra_add=extra_add,
            general_reduce=general_reduce, extra_reduce=extra_reduce,
            resist=resist, flat=flat, percent=percent,
            true_damage=true_damage, attr_damage=attr_damage, attr_type=attr_type,
            atk_stage=atk_stage, defense_stage=defense_stage,
            hits=hits, target_hp=target_hp,
        )
        if not v["ok"]:
            return v.get("empty_hint") or "计算失败。"

        sep = chr(10)
        r = v["result"]
        inp = v["inputs"]
        lines = []
        lines.append(
            "【伤害计算】威力{p}｜攻{a}｜防{d}｜克制×{t}｜本系{s}｜连击{h}段".format(
                p=inp["power"], a=inp["atk"], d=inp["defense"],
                t=inp["type_mult"], s="1.5" if inp["stab"] == 1.5 else "1.0", h=inp["hits"],
            )
        )
        if inp.get("power_real") and inp["power_real"] != inp["power"]:
            lines.append(f"威力加成后：{inp['power']} → {inp['power_real']:.0f}")
        if inp.get("atk_real") != inp["atk"] or inp.get("def_real") != inp["defense"]:
            lines.append(
                f"等级修正：攻 {inp['atk']}→{inp['atk_real']:.0f}｜防 {inp['defense']}→{inp['def_real']:.0f}"
            )

        if v.get("slot_hints"):
            lines.append("〔乘区〕" + sep + sep.join(f"- {h}" for h in v["slot_hints"]))
        if v.get("float_hint"):
            lines.append("〔浮动〕" + v["float_hint"])

        # 多通道结果（settle 的分通道口径）
        for key, label in (("attack_channel", "红伤"), ("flat_channel", "固伤"),
                           ("percent_channel", "百分比"), ("attr_channel", "属性伤害"),
                           ("true_channel", "真伤")):
            val = r.get(key)
            if val:
                _mn = r.get(key + "_min")
                lines.append(f"{label}：{val}" if _mn is None else f"{label}：{_mn}~{val}")
        if r.get("total") is not None:
            # ★ 2026-11-08：range 模式现在有下限了（total_min），输出要显示区间，
            #   否则 bot 只看到上限，还以为"只给单值"。
            _tmin = r.get("total_min")
            if _tmin is not None and _tmin != r["total"]:
                lines.append(f"合计：{_tmin}~{r['total']}"
                             f"（浮动 {r.get('float_range_pct') or ''}）".replace("（浮动 ）", ""))
            else:
                lines.append(f"合计：{r['total']}")
        if inp.get("target_hp") and r.get("total"):
            _tmin2 = r.get("total_min")
            if _tmin2 is not None and _tmin2 != r["total"]:
                lines.append(f"占目标体力：{_tmin2 / inp['target_hp']:.1%}~{r['total'] / inp['target_hp']:.1%}")
            else:
                lines.append(f"占目标体力：{r['total'] / inp['target_hp']:.1%}")
        if r.get("total_reduce"):
            lines.append(f"总减伤：{r['total_reduce']:.0%}")

        lines.append("（口径：通用增伤相加、额外增伤连乘；总减伤=1-(1-通用)×(1-额外)；魂印/特性请折算成参数）")
        return sep.join(lines)


    @llm_tool(name="seer_recall")
    async def seer_recall(self, event: AstrMessageEvent, topic: str) -> str:
        """检索玩家此前补充过的赛尔号知识。

        Args:
            topic(string): 关键词，如「锁伤判定」
        """
        rows = self.kb.recall(topic)
        if not rows:
            return f"没有关于「{topic}」的历史补充。"
        return "\n".join(f"- {t}：{c}" for t, c in rows)

    @llm_tool(name="seer_remember")
    async def seer_remember(self, event: AstrMessageEvent, topic: str, content: str) -> str:
        """把玩家说过的机制/判定/数值记进知识库，以后优先使用。

        Args:
            topic(string): 主题词，如「锁伤判定」
            content(string): 一句话结论，尽量写明条件
        """
        t = str(topic or "").strip()
        c = str(content or "").strip()
        if len(t) < 2:
            return "主题太短，给一个有意义的名词再记（如「锁伤判定」）。"
        if len(c) < 8:
            return "结论太短，请把条件和数值写清楚再记。"
        # 来源由会话类型决定，不允许模型自称"玩家确认"——
        # 之前所有条目都写成 chat，导致 85% 的知识无法溯源。
        origin = "玩家口述" if self._is_private(event) else "群内转述·待核对"
        self.kb.remember(t, c, source=origin)
        self.kb.resolve_pending(t)
        pet_name = t.split("·")[0]
        if pet_name:
            self.kb.resolve_pending_by_pet(pet_name)
        return f"已记录（来源：{origin}）：{t} → {c}"


    @llm_tool(name="seer_glossary")
    async def seer_glossary(self, event: AstrMessageEvent, keyword: str, kind: str = "") -> str:
        """查官方词条（手册 effectDes）：语法判定用语 / 时点 / 专属名词 / 异常状态 / 规则名词 / 伤害类型 / 免疫。

        Args:
            keyword(string): 要查的名词，如「未触发」「吸血」「没夜之月」。留空且给了 kind 时按类别列出。
            kind(string): 可选，类别名：语法 / 时点 / 专属 / 异常 / 规则 / 伤害 / 免疫
        """
        # 类别枚举、空结果提示、参数占位说明全部来自 glossary_view（和 CLI 共用）
        v = self.kb.glossary_view(keyword, kind, limit=6)
        sep = chr(10)

        if v["mode"] == "categories":
            names = "、".join(c[0] for c in v["categories"])
            return (f"请给出要查的名词，例如「吸血」「致命一击」。" + sep
                    + f"也可以按类别列：{names}（用 kind 参数）")

        if v.get("empty_hint"):
            return v["empty_hint"]

        if v["mode"] == "kind":
            lines = [f"【{v['kind']}·共 {len(v['rows'])} 条】"]
            for r in v["rows"][:20]:
                lines.append(f"- {r.get('name')}：{str(r.get('desc') or '')[:150]}")
            return sep.join(lines)

        if v["mode"] == "gem":
            lines = [f"【宝石·{v['keyword']}】"]
            for g in v["rows"]:
                _nm = str(g.get("gem") or "")
                lv = "" if "lv" in _nm.lower() else (f"lv{g['level']}" if g.get("level") else "")
                lines.append(f"- {g.get('gem')}{lv}：{g.get('effect')}（参数 {g.get('args')}）")
            return sep.join(lines)

        lines = [f"【词条·{v['keyword']}】"]
        for r in v["rows"]:
            desc = r.get("desc")
            if desc:
                lines.append(f"- [{r.get('src')}] {r.get('name')}：{desc}")
            else:
                lines.append(f"- [{r.get('src')}] {r.get('name')}")
        if v.get("args_hint"):
            lines.append(v["args_hint"])
        return sep.join(lines)


    @llm_tool(name="seer_rules")
    async def seer_rules(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """查「玩家已确认的规则」与解析节点。给了 keyword 就先按关键词搜规则库。

        Args:
            keyword(string): 可选，如「未触发」「抗性取整」「弥沦之镜」。留空则只返回解析节点清单。
        """
        # 命中与解析节点都来自 facts_view（语义层，和 CLI 共用）
        v = self.kb.facts_view(keyword, limit=5)
        sep = chr(10)
        lines = []
        kw = v["keyword"]
        if kw:
            if v["hits"]:
                lines.append(f"【规则库·{kw}】")
                for h in v["hits"]:
                    lines.append(f"- {h['topic']}：{str(h['content'])[:400]}")
            else:
                # 用语义层给的 empty_hint 原文，别自己另写一套文案
                lines.append("【规则库】" + (v.get("empty_hint") or f"没搜到「{kw}」相关条目。"))
        if v.get("rules_text"):
            lines.append("〔解析节点清单〕")
            lines.append(v["rules_text"][:1200])
        return sep.join(lines) or "解析节点文件缺失。"


    @llm_tool(name="seer_ask")
    async def seer_ask(self, event: AstrMessageEvent, topic: str, question: str) -> str:
        """把无法从资料确定的判定点记成待确认，并请玩家解答。

        Args:
            topic(string): 主题，如「灭盖·魂印·免疫范围」
            question(string): 要问玩家的具体问题，一句话
        """
        self.kb.add_pending(topic, question)
        return f"已记为待确认：{topic}。请在回复里直接问玩家：{question}（得到答案后用 seer_remember 保存）"

    def _icon_path(self, pet_id: int) -> str:
        """把库里的魂印图标 blob 落到缓存文件，返回路径（失败返回空串）。"""
        try:
            got = self.kb.soulmark_icon_png(pet_id)
            if not got:
                return ""
            smid, blob = got
            cache_dir = os.path.join(self.data_dir, "icons")
            os.makedirs(cache_dir, exist_ok=True)
            path = os.path.join(cache_dir, f"{pet_id}_{smid}.png")
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(blob)
            return path
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[赛尔知识库] 图标落盘失败: {exc}")
            return ""

    def _icon_result(self, event: AstrMessageEvent, text: str, pet_id: int):
        """带图标的回复：图片 + 文本；没有图标就纯文本。"""
        path = self._icon_path(pet_id) if self._c("attach_icon", True) else ""
        if not path:
            return event.plain_result(text)
        try:
            chain = event.make_result()
            chain.file_image(path)
            chain.message(text)
            return chain
        except Exception:  # noqa: BLE001
            return event.plain_result(text)

    # ---------------- 命令 ----------------

    @filter.command("深挖开", alias={"专注"})
    async def cmd_focus_on(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("专注模式只在私聊里用，群里请直接 /深挖 精灵名。")
            return
        minutes = int(self._c("focus_minutes", 30))
        text = (getattr(event, "message_str", "") or "").strip()
        tail = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if tail.isdigit():
            minutes = max(5, min(180, int(tail)))
        set_focus(self._data_root, self._umo(event), minutes)
        yield event.plain_result(
            f"已进入专注模式（{minutes} 分钟）：这段时间只谈赛尔机制，"
            f"其他插件的注入会被压掉。发 /深挖关 可提前退出。"
        )

    @filter.command("深挖关", alias={"结束深挖"})
    async def cmd_focus_off(self, event: AstrMessageEvent):
        had = clear_focus(self._data_root, self._umo(event))
        yield event.plain_result("已退出专注模式。" if had else "当前不在专注模式。")

    @filter.command("配招")
    async def cmd_show_moveset(self, event: AstrMessageEvent):
        """查看库里已定型的配招原文（不查外部数据源）。"""
        text = (getattr(event, "message_str", "") or "").strip()
        name = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if not name or name == text:
            yield event.plain_result("用法：/配招 精灵名")
            return
        pets = self.kb.find_pets(name)
        if not pets:
            yield event.plain_result(f"没查到精灵「{name}」。")
            return
        pid, pname = pets[0]
        fixed = self.kb.get_moveset(pid)
        if not fixed:
            yield event.plain_result(f"「{pname}」还没有定型配招，用 /定型 精灵 技能1 技能2… 设置。")
            return
        detail = self.kb.get_moveset_detail(pid) or self._build_moveset_detail(pname, fixed, pid)
        if not self.kb.get_moveset_detail(pid):
            self.kb.set_moveset(pid, fixed, detail=detail)
        for chunk in self._split(detail, 900):
            yield event.plain_result(chunk)

    @filter.command("同步数据")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_sync_manual(self, event: AstrMessageEvent):
        yield event.plain_result("开始从手册仓库同步数据，请稍候…")
        try:
            result = await sync_manual(self.kb.manual_db_dir())
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"同步失败：{exc}")
            return
        lines = [f"{k}: {v}" for k, v in result.items()]
        try:
            ids = self.kb.status_icon_ids()
            ok, total = await fetch_icons(ids, os.path.join(self.kb.manual_db_dir(), "icons"))
            lines.append(f"异常图标: {ok}/{total}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"异常图标下载失败：{exc}")
        yield event.plain_result("同步完成：\n" + "\n".join(lines))

    @filter.command("名词")
    async def cmd_terms(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        name = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if not name or name == text:
            yield event.plain_result("用法：/名词 精灵名")
            return
        # 先按「名词」精确查，查不到再当精灵名
        hit = self.kb.term_lookup_any(name)
        if hit:
            lines = [f"【名词·{name}】"]
            for it in hit["items"]:
                who = f"（{it['pet']}）" if it.get("pet") else ""
                lines.append(f"- {it['name']}{who}：{it['desc']}")
            text = "\n".join(lines)
            # 名词本身没有可靠的独立图标（库里的 effectIcon 是按 icon_id 存的，
            # 与 glossary id 不同源，硬配会张冠李戴），所以只发文字。
            for chunk in self._split(text, 900):
                yield event.plain_result(chunk)
            return
        pets = self.kb.find_pets(name)
        if not pets:
            yield event.plain_result(f"没查到「{name}」。可以试试精灵名，或者直接问这个词的意思。")
            return
        pid, pname = pets[0]
        data = self.kb.pet_terms(pid)
        lines = [f"【{pname}·专属名词】"]
        for item in data["defined"]:
            lines.append(f"- {item['name']}：{item['desc']}")
        if not data["defined"]:
            lines.append("- 库里没收录")
        if data["mentioned"]:
            lines.append("文中还提到：" + "、".join(x["name"] for x in data["mentioned"]))
        chunks = self._split("\n".join(lines), 900)
        for i, chunk in enumerate(chunks):
            yield (self._icon_result(event, chunk, pid) if i == 0 else event.plain_result(chunk))

    @filter.command("标签")
    async def cmd_tag(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        tag = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if not tag or tag == text:
            stats = self.kb.tag_stats()[:12]
            body = "｜".join(f"{t} {c}" for t, c in stats) or "（还没建标签）"
            yield event.plain_result("标签统计：" + body + "\n用法：/标签 免疫异常")
            return
        total, rows = self.kb.tag_search(tag, 20)
        if not total:
            yield event.plain_result(f"没有标签「{tag}」。")
            return
        lines = [f"{'技能' if k == 'skill' else '魂印'}｜{n}" for k, n, _ in rows]
        for chunk in self._split(f"【标签·{tag}】共 {total} 条\n" + "\n".join(lines), 900):
            yield event.plain_result(chunk)

    @filter.command("池子")
    async def cmd_pool(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        arg = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if arg == text:
            arg = ""
        pools = self.kb.pool_list()
        ranks = self.kb.rank_ban_list()
        if not pools and not ranks:
            yield event.plain_result("还没有池子数据，先跑 /同步数据。")
            return
        if not arg:
            lines = ["【大师模式·费用/禁用池】"]
            for p in pools:
                lines.append(f"- {p['label']}：{len(p['pets'])} 只（截止 {p['deadline'] or '未标注'}）")
            if ranks:
                lines.append("【竞技模式·限制池】")
                for r in ranks[:4]:
                    flag = "（进行中）" if r["active"] else ""
                    start = datetime.fromtimestamp(r["start"]).strftime("%m-%d") if r["start"] else "?"
                    end = datetime.fromtimestamp(r["end"]).strftime("%m-%d") if r["end"] else "?"
                    lines.append(
                        f"- 第{r['period']}期：{len(r['pets'])} 只｜{start}~{end}{flag}"
                    )
            lines.append("用法：/池子 35 或 /池子 竞技 或 /池子 盖亚")
            yield event.plain_result("\n".join(lines))
            return
        if arg in ("竞技", "限制", "竞技池"):
            if not ranks:
                yield event.plain_result("还没有竞技限制池数据。")
                return
            r = ranks[0]
            head = f"【竞技模式·限制池 第{r['period']}期】{len(r['pets'])} 只\n"
            for chunk in self._split(head + "、".join(r["pets"]), 900):
                yield event.plain_result(chunk)
            return
        matched = self.kb.pool_match(arg)
        if matched:
            head = f"【{matched['label']}】{len(matched['pets'])} 只"
            for chunk in self._split(head + "\n" + "、".join(matched["pets"]), 900):
                yield event.plain_result(chunk)
            return
        found = self.kb.pool_search(arg)
        if found:
            lines = [f"【{arg} 所在池】"]
            for p in found:
                lines.append(f"- {p['label']}（{len(p['pets'])} 只，截止 {p['deadline'] or '未标注'}）")
            yield event.plain_result("\n".join(lines))
            return
        yield event.plain_result(f"没查到「{arg}」，既不是池子名也不在池子里。")

    @filter.command("专栏")
    async def cmd_doc(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        kw = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if not kw or kw == text:
            yield event.plain_result("用法：/专栏 关键词（如 免死、时点、伤害流程）")
            return
        concepts = self.kb.doc_concepts_search(kw, 4)
        rules = self.kb.doc_rules_search(kw, 6)
        rows = self.kb.doc_search(kw, 2)
        if not concepts and not rules and not rows:
            yield event.plain_result(f"专栏里没搜到「{kw}」。")
            return
        lines = [f"【专栏·{kw}】"]
        if concepts:
            lines.append("〔概念〕")
            for c in concepts:
                lines.append(f"- {c['term']}：{c['definition']}（{c['title'].replace('【赛尔号】','')}）")
        if rules:
            lines.append("〔判定〕")
            for r in rules:
                lines.append(f"- 条件：{r['condition']}\n  结论：{r['conclusion']}（{r['title'].replace('【赛尔号】','')}）")
        for r in rows:
            lines.append(f"- 原文：《{r['title']}》{r['snippet'][:160].strip()}")
        for chunk in self._split("\n".join(lines), 900):
            yield event.plain_result(chunk)

    @filter.command("异常")
    async def cmd_status_list(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        arg = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if arg == text:
            arg = ""
        # 先按「状态名」精确查
        one = [x for x in self.kb.status_list(limit=200) if arg and x["name"] == arg]
        if one:
            x = one[0]
            body = f"【异常状态·{x['name']}】\n分类：{x['category']}"
            if x.get("resist"):
                body += f"｜抗性：{x['resist']}"
            body += f"\n效果：{x['desc']}"
            path = self.kb.status_icon_path(x["name"]) if self._c("attach_icon", True) else ""
            if path:
                try:
                    chain = event.make_result()
                    chain.file_image(path)
                    chain.message(body)
                    yield chain
                    return
                except Exception:  # noqa: BLE001
                    pass
            yield event.plain_result(body)
            return
        cat = arg
        items = self.kb.status_list(cat)
        if not items:
            yield event.plain_result("没有匹配的异常状态。用法：/异常 控制 或 /异常 弱化 或 /异常 附属")
            return
        lines = [
            (f"{x['category']}｜{x['name']}" + (f"｜抗性{x['resist']}" if x.get('resist') else "")
             + f"：{x['desc']}")
            for x in items
        ]
        for chunk in self._split("【异常状态】" + "\n".join(lines), 900):
            yield event.plain_result(chunk)

    @filter.command("忘记")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_forget_fact(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        kw = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if not kw or kw == text:
            yield event.plain_result("用法：/忘记 关键词（会删掉 topic 或内容里含该词的规则）")
            return
        removed = self.kb.delete_facts(kw)
        if not removed:
            yield event.plain_result(f"没有匹配「{kw}」的规则。")
            return
        yield event.plain_result(f"已删除 {len(removed)} 条：\n" + "\n".join(f"- {t}" for t in removed[:8]))

    @filter.command("别名", alias={"记外号", "加外号"})
    async def cmd_alias_add(self, event: AstrMessageEvent):
        """群里加/改俗称：/别名 <外号> <精灵名或ID> [备注]"""
        text = (getattr(event, "message_str", "") or "").strip()
        rest = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        parts = [p for p in rest.split() if p]
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/别名 外号 精灵名或ID [备注]\n"
                "例：/别名 巢子 灵巢\n"
                "     /别名 巢子 4599 群友叫法"
            )
            return
        alias, target = parts[0], parts[1]
        note = " ".join(parts[2:]).strip() or "群聊登记"

        # 解析目标：ID → 俗称 → 名字/模糊
        pid = None
        if target.isdigit():
            row = self.kb._q("select id from pet where id=?", (int(target),))
            if row:
                pid = int(row[0][0])
        if pid is None:
            pid = self.kb.alias_lookup(target)
        if pid is None:
            hits = self.kb.find_pets(target)
            if hits:
                pid = int(hits[0][0])
        if pid is None:
            yield event.plain_result(f"找不到精灵「{target}」，确认下名字或换成 pet_id。")
            return

        row = self.kb._q("select name from pet where id=?", (pid,))
        pname = row[0][0] if row else f"#{pid}"
        old = self.kb.alias_lookup(alias)
        self.kb.add_alias(alias, pid, note)
        msg = f"✅ 已记：「{alias}」→ {pname}（#{pid}）"
        if old and old != pid:
            row2 = self.kb._q("select name from pet where id=?", (old,))
            msg += f"\n（原来指向 {row2[0][0] if row2 else '#' + str(old)}，已覆盖）"
        yield event.plain_result(msg)

    @llm_tool(name="seer_alias_add")
    async def seer_alias_add(
        self, event: AstrMessageEvent, alias: str, pet: str, note: str = ""
    ) -> str:
        """记录玩家给的俗称/外号 → 精灵，写进 kb.pet_alias（同名会覆盖）。

        玩家说「XX 就是 YY」「XX 是 YY 的外号」「以后说 XX 指 YY」时调用。

        Args:
            alias(string): 俗称/外号，如「巢子」
            pet(string): 目标精灵名或 ID，如「灵巢」或「4599」
            note(string): 备注，可留空
        """
        a = str(alias or "").strip()
        t = str(pet or "").strip()
        if not a or not t:
            return "需要同时给出外号和目标精灵。"
        pid = None
        if t.isdigit():
            row = self.kb._q("select id from pet where id=?", (int(t),))
            if row:
                pid = int(row[0][0])
        if pid is None:
            pid = self.kb.alias_lookup(t)
        if pid is None:
            hits = self.kb.find_pets(t)
            if hits:
                pid = int(hits[0][0])
        if pid is None:
            return f"找不到精灵「{t}」，请确认正式名字或 pet_id 后再记。"
        row = self.kb._q("select name from pet where id=?", (pid,))
        pname = row[0][0] if row else f"#{pid}"
        self.kb.add_alias(a, pid, str(note or "").strip() or "群聊登记")
        return f"已记录：外号「{a}」→ {pname}（#{pid}）"

    @filter.command("技能")
    async def cmd_skill_probe(self, event: AstrMessageEvent):
        """直接打印 seer_skill 会返回的原文，便于排查。"""
        text = (getattr(event, "message_str", "") or "").strip()
        name = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if not name or name == text:
            yield event.plain_result("用法：/技能 技能名")
            return
        info = self.kb.skill_info(name)
        if not info:
            yield event.plain_result(f"未收录技能「{name}」。")
            return
        _t = info.get("skill_type") or info.get("type") or ""
        parts = [f"【{info['name']}】{_t}系"]
        if info["power"]:
            parts.append(f"威力{info['power']}")
        if info["pp"]:
            parts.append(f"PP{info['pp']}")
        if info["accuracy"] is not None:
            parts.append(f"命中{info['accuracy']}")
        if info.get("atk_num"):
            parts.append(f"攻击{info['atk_num']}次")
        if info.get("must_hit"):
            parts.append("必中")
        cat = {1: "物理", 2: "特殊", 4: "属性"}.get(info.get("category_id"))
        if cat:
            parts.append(cat)
        body = "\n".join(f"- {x}" for x in info.get("effects") or []) or ("（无独立效果条目）" + (info.get("info") or ""))
        yield event.plain_result("｜".join(parts) + "\n效果：\n" + body)

    @filter.command("定型")
    async def cmd_set_moveset(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        parts = text.split()
        if len(parts) < 3:
            yield event.plain_result(
                "用法：/定型 精灵名 第五技能 技能1 技能2 技能3 技能4\n"
                "赛尔号配招是 4 个普通技能 + 1 个第五（第五单独占一格，共 5 格）。\n"
                "例：/定型 柯尔霍德 王·冥津哑噤 丧月之夜 雾影界天 王·融冬诉死 寒渊之殇"
            )
            return
        name, skills = parts[1], parts[2:]
        pets = self.kb.find_pets(name)
        if not pets:
            yield event.plain_result(f"没查到精灵「{name}」。")
            return
        pid, pname = pets[0]
        _sg = self.kb.pet_skills_grouped(pid)
        owned = {x["name"] for x in
                 (_sg["fifth_new"] + _sg["fifth_old"] + _sg["new_skills"] + _sg.get("lv0", []) + _sg["legacy"])}
        unknown = [x for x in skills if x not in owned]
        if unknown:
            yield event.plain_result(
                f"这些技能不在「{pname}」的技能表里：" + "、".join(unknown)
                + "\n用 /深挖 " + pname + " 可以看到全部可选技能。"
            )
            return
        groups = self.kb.pet_skills_grouped(pid)
        fifth_names = {y["name"] for y in groups["fifth_new"] + groups["fifth_old"]}
        fifths = [x for x in skills if x in fifth_names]
        if len(fifths) > 1:
            yield event.plain_result("第五技能只能带一个，请去掉多余的那条：" + "、".join(fifths))
            return
        normal = [x for x in skills if x not in fifth_names]
        if len(normal) > 4:
            yield event.plain_result(
                f"普通技能最多 4 个（现在 {len(normal)} 个）。第五单独占一格，总共 5 格。"
            )
            return
        if len(skills) > 5:
            yield event.plain_result("配招最多 5 格：4 个普通技能 + 1 个第五。")
            return
        self.kb.set_moveset(pid, skills)
        tail = "" if fifths else "\n（这次没带第五，要带的话用 /定型 重新写一遍）"
        yield event.plain_result(
            f"已把「{pname}」的配招定型为：" + "、".join(skills)
            + f"\n结构：第五 1 格 + 普通 {len(normal)} 格" + tail
            + "\n以后 /深挖 会直接按这个显示。"
        )

    @filter.command("解除定型")
    async def cmd_clear_moveset(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        name = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if not name or name == text:
            yield event.plain_result("用法：/解除定型 精灵名")
            return
        pets = self.kb.find_pets(name)
        if not pets:
            yield event.plain_result(f"没查到精灵「{name}」。")
            return
        ok = self.kb.clear_moveset(pets[0][0])
        yield event.plain_result(("已解除「%s」的定型配招。" % pets[0][1]) if ok else "这只精灵没有定型配招。")

    @filter.command("深挖状态", alias={"赛尔状态"})
    async def cmd_status(self, event: AstrMessageEvent):
        pet_n = "可用" if self.kb.available() else "未找到 SeerAPI 库"
        yield event.plain_result(
            f"赛尔知识库：{pet_n}｜玩家补充 {self.kb.fact_count()} 条"
            f"｜待确认 {len(self.kb.list_pending(50))} 条"
            f"｜专注模式 {'开' if self._focus_active(event) else '关'}\n"
            "用法：/深挖 精灵名（群里看数据用「精灵」卡片，想要分析用 /深挖 或私聊）"
        )

    @filter.command("深挖", alias={"赛尔"})
    async def cmd_pet(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        name = text.split(maxsplit=1)[-1].strip() if " " in text else ""
        if not name or name == text:
            yield event.plain_result("用法：/深挖 精灵名\n深挖会列出属性、种族值、魂印与技能，之后可以继续追问机制。")
            return
        text_out = self._pet_text(name)
        for chunk in self._split(text_out, 900):
            yield event.plain_result(chunk)

    def _pet_text(self, name: str) -> str:
        pets = self.kb.find_pets(name)
        if not pets:
            return f"没查到「{name}」，换个写法试试？"
        pid, pname = pets[0]
        both = self.kb.pet_stats_both(pid)
        stats = both.get("awakened") or both.get("base") or {}
        marks = self.kb.pet_soulmarks(pid)
        line = f"【{pname}】{self.kb.pet_type(pid)}"
        fixed = self.kb.get_moveset(pid)
        if fixed:
            detail = self.kb.get_moveset_detail(pid) or self._build_moveset_detail(pname, fixed, pid)
            if not self.kb.get_moveset_detail(pid):
                self.kb.set_moveset(pid, fixed, detail=detail)
            line += "\n✅ 已定型配招（固定，直接给原文）：\n" + detail
        if stats:
            cn = {"atk": "攻击", "def": "防御", "sp_atk": "特攻", "sp_def": "特防", "spd": "速度", "hp": "体力"}
            label = "种族值（觉醒后）" if "awakened" in both else "种族值"
            line += "\n" + label + "：" + "　".join(f"{cn[k]} {stats.get(k)}" for k in cn)
            if "awakened" in both and "base" in both:
                b = both["base"]
                line += "\n觉醒前：" + "　".join(f"{cn[k]} {b.get(k)}" for k in cn)
        if marks:
            line += "\n魂印：" + "；".join(marks)
        rules = self.kb.facts_for_pet(pname)
        if rules:
            line += "\n📌 已确认规则（最多列 3 条，完整版私聊问）：\n" + "\n".join(
                f"- {t.split('·')[-1]}：{c[:100]}" for t, c in rules[:3]
            )

        # 专属效果（魂印里提到的也展开）
        effects = self.kb.pet_effects(pid)
        if effects:
            line += "\n专属效果：" + "；".join(f"{e['name']}={e['desc'][:90]}" for e in effects)
        elif marks:
            mentioned = []
            for m in marks:
                for eff in self.kb.mentioned_effects(pid, m):
                    mentioned.append(f"{eff['name']}={eff['desc'][:90]}")
            if mentioned:
                line += "\n魂印提到的效果：" + "；".join(mentioned)

        # 技能：第五二选一，新增优先，其余按学习等级
        g = self.kb.pet_skills_grouped(pid)
        if g["fifth_new"]:
            f = g["fifth_new"][0]
            line += f"\n第五（一般带这条）：{f['name']}" + (f"({f['power']})" if f["power"] else "")
        if g["fifth_old"]:
            line += "\n旧第五（可替换，通常不用）：" + "、".join(x["name"] for x in g["fifth_old"])
        if g["new_skills"]:
            line += "\n新增技能：" + "、".join(
                f"{x['name']}" + (f"({x['power']})" if x["power"] else "") for x in g["new_skills"]
            )
        if g.get("lv0"):
            line += "\n★ Lv0·道具开启的补强技能（重点看）：" + "、".join(
                f"{x['name']}" + (f"({x['power']})" if x["power"] else "") for x in g["lv0"]
            )
        if g["legacy"]:
            line += "\n其余可选（按需求，不按威力）：" + "、".join(
                f"{x['name']}" + (f"({x['power']})" if x["power"] else "") for x in g["legacy"][:5]
            )
        line += "\n（想看机制分析：私聊问「" + pname + "的魂印怎么判」，我会按解析节点拆解；不确定的会问你）"
        return line

    # ══════════════════════════════════════════════════════════
    # 游戏内实时查询：巅峰战绩 / 12 只阵容
    #   · 两个工具彻底分开，用户只问哪个就调哪个
    #   · 群里必须 @我；私聊放行
    #   · 服务账号同一时刻只能一个在线 → 全部走 _seer_io_lock 串行
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _need_at(event) -> bool:
        """群里必须有人 @ 我；私聊直接放行。"""
        try:
            if event.is_private_chat():
                return True
            from astrbot.core.message.components import At
            sid = str(event.get_self_id())
            for seg in event.get_messages():
                if isinstance(seg, At) and str(getattr(seg, "qq", "")) == sid:
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def _seer_creds(self) -> tuple[str, str]:
        """服务账号：插件配置优先，环境变量兜底。不写日志、不回显。"""
        acc = str(self._c("seer_account", "") or "").strip()
        pwd = str(self._c("seer_password", "") or "").strip()
        if not acc:
            acc = (os.environ.get("SEER_ACCOUNT") or "").strip()
        if not pwd:
            pwd = (os.environ.get("SEER_PASSWORD") or "").strip()
        return acc, pwd

    def _seer_cached(self, key: str):
        hit = self._seer_cache.get(key)
        if hit and time.time() - hit[0] < self._SEER_CACHE_TTL:
            return hit[1]
        return None

    @llm_tool(name="seer_pvp")
    async def seer_pvp(self, event: AstrMessageEvent, account: str) -> str:
        """查询某个米米号的巅峰战绩（段位 / 星数 / 胜场 / 胜率）。

        # ⚠️ 使用条件
        必须有人 @ 我 才能用。没被 @ 时**不要调这个工具**，直接回「要查战绩请 @我」。
        用户只问胜率/段位/打了几场时用这个；**不要顺带查阵容**。

        # 能查到
        · 竞技 / 狂野 / 专家 / 大师 四个模式
        · 每个模式：当前段位、历史最高、胜场、总场、胜率
        · 段位：学徒 / 猛将 / 天骄 / 王者 / 圣皇；圣皇满 100 星显示「宇宙圣皇」
        · 专家模式是积分制，不是星星

        Args:
            account(string): 目标米米号，纯数字，如 "258806935"
        """
        if not self._need_at(event):
            return "查游戏内数据要 @我 才行。"
        acc, pwd = self._seer_creds()
        if not acc or not pwd:
            return "查询失败：服务账号没配。请在插件配置里填 seer_account / seer_password。"
        try:
            target = int(str(account).strip())
        except (TypeError, ValueError):
            return f"查询失败：`{account}` 不是有效米米号（只要纯数字）。"
        if not 0 < target <= 0xFFFFFFFF:
            return "查询失败：米米号超出范围。"

        from .seer_pvp import query_pvp_async, format_pvp

        key = f"pvp:{target}"
        cached = self._seer_cached(key)
        if cached is not None:
            return format_pvp(cached)
        async with self._seer_io_lock:
            cached = self._seer_cached(key)
            if cached is not None:
                return format_pvp(cached)
            data = await query_pvp_async(target, acc, pwd)
            if not data.get("error") and data.get("pvp"):
                self._seer_cache[key] = (time.time(), data)
        return format_pvp(data)

    @llm_tool(name="seer_roster")
    async def seer_roster(self, event: AstrMessageEvent, account: str) -> str:
        """查询某个米米号的 12 只精灵阵容，并生成一张阵容图。

        # ⚠️ 使用条件
        必须有人 @ 我 才能用。没被 @ 时**不要调这个工具**，直接回「要查阵容请 @我」。
        用户问阵容/出战/带了哪几只时用这个；**不要顺带查战绩**。

        # 产出
        一张阵容图（出战 6 + 待命 6），穿皮肤的角色显示皮肤头像。
        图之外补一句：米米号 + 获取时间。
        刻印槽只能显示装了几颗（拿不到刻印名），别编名字。

        Args:
            account(string): 目标米米号，纯数字
        """
        if not self._need_at(event):
            return "查游戏内数据要 @我 才行。"
        acc, pwd = self._seer_creds()
        if not acc or not pwd:
            return "查询失败：服务账号没配。请在插件配置里填 seer_account / seer_password。"
        try:
            target = int(str(account).strip())
        except (TypeError, ValueError):
            return f"查询失败：`{account}` 不是有效米米号（只要纯数字）。"
        if not 0 < target <= 0xFFFFFFFF:
            return "查询失败：米米号超出范围。"

        from .seer_pvp import query_roster_async, format_roster

        key = f"roster:{target}"
        data = self._seer_cached(key)
        if data is None:
            async with self._seer_io_lock:
                data = self._seer_cached(key)
                if data is None:
                    data = await query_roster_async(target, acc, pwd)
                    if not data.get("error") and data.get("roster", {}).get("pets"):
                        self._seer_cache[key] = (time.time(), data)
        if data.get("error"):
            return f"查询失败：{data['error']}"

        # 发图放在锁外，别让发送把下一次查询堵住
        path = data.get("image")
        if path and os.path.isfile(path):
            try:
                from astrbot.core.message.message_event_result import MessageChain
                chain = MessageChain(chain=[ImageComp.fromFileSystem(path)])
                await self.context.send_message(event.unified_msg_origin, chain)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[seer_roster] 阵容图发送失败: %s", exc)
                return format_roster(data) + f"\n（图没发出去：{exc}）"
        return format_roster(data)

    # ── 夯拉榜（分级榜梗图）──────────────────────────────────
    def _tier_resolve(self, key) -> dict | None:
        """把一个名字/id 解析成 {id, name, resource_id}。

        ★ 精确名优先，再走 find_pets。
          2026-11-13 踩的：直接 find_pets('皮皮') 会返回「赤胆游龙·赵云」，
          find_pets('小火猴') 会返回「星光·魔焰猩猩」—— 它按「同名多形态取种族值
          最高」+ 模糊匹配办事，适合「查这只精灵的最强形态」，但不适合榜单：
          榜上写「小火猴」就该是小火猴，不是它祖宗。

        所以顺序：
          ① 纯数字 → pet_id 直查
          ② 精确同名 → 命中即用（皮皮→皮皮、小火猴→小火猴）
          ③ find_pets → 别名（战螳螂/环源）+ 模糊 + 错字纠正（龙啖→龙琰）
        """
        raw = str(key or "").strip()
        if not raw:
            return None

        pid = None
        if raw.isdigit():
            pid = int(raw)
        else:
            rows = self.kb._q("select id from pet where name=? limit 1", (raw,))
            if rows:
                pid = int(rows[0][0])
            if not pid:
                hits = self.kb.find_pets(raw, limit=1)
                if hits:
                    pid = int(hits[0][0])
        if not pid:
            return None

        rows = self.kb._q("select id, name, resource_id from pet where id=?", (pid,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "name": r[1], "resource_id": r[2] or r[0]}

    # 编号 → 分级（群友常按 1. 2. 3. 这样一档一档说）
    _TIER_BY_ORDINAL = {
        "1": "夯", "2": "顶级", "3": "人上人", "4": "NPC", "5": "拉完了",
        "①": "夯", "②": "顶级", "③": "人上人", "④": "NPC", "⑤": "拉完了",
        "一": "夯", "二": "顶级", "三": "人上人", "四": "NPC", "五": "拉完了",
    }

    @classmethod
    def _tier_parse(cls, spec: str) -> list[dict]:
        """解析分级榜的输入。

        支持两种写法，可以混着用：

        A. 显式分级（推荐，最不容易错）
           夯:无极圣武,艾夏拉 顶级:谱尼 NPC:皮皮 拉完了:小火猴
           分隔：行内用冒号（中英都行），同级精灵用逗号/顿号/斜杠，
                 行与行用空格/分号/竖线/换行。

        B. 编号顺序（群友常这么列）
           1.无极圣武,艾夏拉 2.谱尼 3.环源 4.皮皮 5.小火猴
           1、xxx / 1)xxx / ①xxx / 一、xxx 都认。
           编号 → 档位固定按 1=夯 2=顶级 3=人上人 4=NPC 5=拉完了。

        纯精灵列表（`无极圣武 谱尼 环源`）解析不出档位，返回空列表 ——
        这种情况调用方该去问用户怎么分档，别瞎猜。
        """
        import re as _re

        text = str(spec or "").strip()
        if not text:
            return []

        # ── 先试「编号顺序」：整段里出现 1./1、/1)/① 这类序号 ──
        # 三种写法要求不同：
        #   · 阿拉伯数字后面必须有标点（1. / 1、/ 1) / 1:）
        #   · 圈号 ①②③ 自己就是序号，后面不用标点
        #   · 中文数字要带顿号/点号（一、/ 一.），否则「一」会和正文混淆
        num_pat = _re.compile(
            # 阿拉伯数字 / 中文数字：前面必须有分隔符，否则「无极圣武2」会被误切
            r"(?:^|[\s;|；，,])"
            r"(?:(?P<arabi>[1-5１２３４５])\s*[.．、)\）：:]"
            r"|(?P<hans>[一二三四五])\s*[、.．,，])"
            r"\s*"
            # 圈号自己就是分隔符，紧贴汉字也认（①无极圣武②谱尼）
            r"|(?P<circ>[①②③④⑤])\s*"
        )
        marks = list(num_pat.finditer(text))
        if len(marks) >= 2:
            rows: list[dict] = []
            seen_tier = set()
            for idx, m in enumerate(marks):
                ordinal = m.group("arabi") or m.group("circ") or m.group("hans") or ""
                ordinal = ordinal.translate(str.maketrans("１２３４５", "12345"))
                tier = cls._TIER_BY_ORDINAL.get(ordinal)
                if tier is None:
                    continue
                start = m.end()
                end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
                body = text[start:end].strip().rstrip(";；|")
                pets = [p.strip() for p in _re.split(r"[,，、/／\s]+", body) if p.strip()]
                pets = [p for p in pets if _re.search(r"[\w\u4e00-\u9fff]", p)]
                if not pets:
                    continue
                if tier in seen_tier:
                    rows[[r["tier"] for r in rows].index(tier)]["pets"].extend(pets)
                else:
                    rows.append({"tier": tier, "pets": pets})
                    seen_tier.add(tier)
            if rows:
                return rows

        # ── 再试「显式分级」：夯:xxx 顶级:yyy ──
        chunks = _re.split(r"[\s;|；]+", text)
        rows = []
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            m = _re.match(r"^(.{1,6}?)[:：](.+)$", chunk)
            if not m:
                continue
            tier, rest = m.group(1).strip(), m.group(2).strip()
            pets = [p.strip() for p in _re.split(r"[,，、/／]+", rest) if p.strip()]
            if pets:
                rows.append({"tier": tier, "pets": pets})
        return rows

    @llm_tool(name="seer_tier_list")
    async def seer_tier_list(
        self, event: AstrMessageEvent, tiers: str, title: str = "", subtitle: str = ""
    ) -> str:
        """画一张「从夯到拉」的精灵分级榜（梗图），把精灵按强度/风评排进五档。

        # 什么时候用
        群友说「排个榜」「做个梯度图」「盘点谁强谁弱」「从夯到拉」「哪些是神哪些是下水道」
        「谁是版本答案」「哪些已经废了」时用这个。**不需要任何人记指令**，你判断该排就排。

        # 标题按群友的话来（重要）
        title 不要固定写「从夯到拉」，要看群友问的是什么，照着那个意思起：
          群友「盘点跌落神坛的」   → title「从夯到拉，盘点赛尔号那些跌落神坛的精灵」
          群友「现在版本谁最强」   → title「当前版本梯度榜」
          群友「这几个哪个值得练」 → title「练谁不练谁：本次候选梯度」
          群友「新手打PVP带哪些」  → title「PVP 上手梯度（新手向）」
        subtitle 用来补一句限定：「（限 PVP）」「（按 11 月版本）」「（群友投票）」。
        群友没给语气就留空，用默认「从夯到拉」。

        # 选谁进榜也按群友说的来
        群友点名了就排他们点的；说「随便排几个」才自己挑当前版本的典型精灵。
        **别自作主张塞进一堆没人提的精灵** —— 榜是给群友看的，不是给你刷的。

        # 你的核心职责：把群友的任意说法转成标准格式
        群友不会按格式说。他们可能这样列出：
          ① 编号顺序      「1.无极圣武 2.谱尼 3.环源」
          ② 编号+多只     「1.无极圣武、艾夏拉 2.谱尼、灵巢」
          ③ 中文顿号      「一、无极圣武 二、谱尼」
          ④ 纯列表无分档  「无极圣武 谱尼 环源」
          ⑤ 口语评价      「无极圣武是神，谱尼还行，皮皮基本废了」
          ⑥ 带情绪        「这版本艾夏拉烂完了，无极圣武还是爹」
        **你要先自己判断每只该进哪一档，再转成**
          `夯:无极圣武 顶级:谱尼 拉完了:皮皮`
        编号 1-5 按顺序对应 夯/顶级/人上人/NPC/拉完了。
        拿不准群体共识的（比如某只没听过风评）就放进「NPC」，别硬吹成夯。
        完全看不出谁强谁弱的纯列表，先用 kb 的种族值/技能判断一个大致梯度，
        或直接问群友「你是想按强度排还是按版本风评排」。

        # 五档（从高到低，写错会自动纠正）
        夯 / 顶级 / 人上人 / NPC / 拉完了
        也接受别名：神·幻神·T0·S → 夯；T1·A → 顶级；T2·B → 人上人；
                    普通·T3 → NPC；拉·下水道·T4 → 拉完了

        # 精灵名
        支持正式名、简称、俗称（走别名表）、错字（龙啖→龙琰）。
        查不到的会画成占位还在图上标名字，不会整张失败。
        **排完在文字里说一句「XX 没查到，用了占位」**。

        Args:
            tiers(string): 分级与精灵。格式 `夯:精灵A,精灵B 顶级:精灵C NPC:精灵D 拉完了:精灵E`。分级别用冒号，同级精灵用逗号，行与行用空格分开
            title(string): 图顶部标题。**按群友问的意思起，别固定写「从夯到拉」**；留空则用默认
            subtitle(string): 标题下的小字限定，如「（限 PVP）」「（11月版本）」，可留空
        """
        rows = self._tier_parse(tiers)
        if not rows:
            return (
                "没解析出分级。格式要写成 `夯:无极圣武,艾夏拉 顶级:谱尼 NPC:皮皮 拉完了:小火猴`，"
                "分级别用冒号、同级精灵用逗号、行与行用空格分开。"
            )

        # 先解析一遍，收集查不到的，好用文字回报
        resolved: dict[str, list[dict]] = {}
        missing: list[str] = []
        for row in rows:
            items = []
            for name in row["pets"]:
                meta = self._tier_resolve(name)
                if meta is None:
                    missing.append(name)
                    items.append({"name": name, "id": None})
                else:
                    items.append({"name": meta["name"], "id": meta["id"]})
            resolved.setdefault(row["tier"], []).extend(items)

        merged_rows = [{"tier": k, "pets": v} for k, v in resolved.items()]

        def lookup(key):
            return self._tier_resolve(key)

        try:
            from .render_tier_list import render_tier_list
            data = await render_tier_list(
                merged_rows,
                lookup=lookup,
                title=title or "从夯到拉",
                subtitle=subtitle or "",
                footer_left="赛尔号 · 神代·欧鲸鲸",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[seer_tier_list] 渲染失败: %s", exc)
            return f"榜单渲染失败：{exc}"

        import tempfile

        fd, path = tempfile.mkstemp(prefix="seer_tier_", suffix=".jpg")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)

        sent = False
        try:
            from astrbot.core.message.message_result import MessageChain  # 兼容旧路径
        except Exception:
            try:
                from astrbot.core.message.message_event_result import MessageChain
            except Exception:
                MessageChain = None  # type: ignore
        if MessageChain is not None:
            try:
                chain = MessageChain(chain=[ImageComp.fromFileSystem(path)])
                await self.context.send_message(event.unified_msg_origin, chain)
                sent = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("[seer_tier_list] 榜单图发送失败: %s", exc)

        # 文字部分：只报状态，不重复图里的内容
        total = sum(len(v) for v in resolved.values())
        line = f"榜单画好了，{len(resolved)} 档共 {total} 只。"
        if missing:
            line += "没查到的用了占位：" + "、".join(missing[:6])
            if len(missing) > 6:
                line += f" 等 {len(missing)} 只"
        if not sent:
            line += f"（图没发出去，文件在 {path}）"
        return line

    # ── 巅峰圣战精灵池出图（2026-11-14 加）───────────────────────────
    # 和 seer_pool 的分工：
    #   seer_pool       文本查询（限2 有哪些 / 某精灵在哪些池）
    #   seer_pool_card  出图（竞技一整张 / 专家 / 大师），照手册那个页面的样子
    # 数据源同一个：manual_db/pvp.db

    def _pool_groups_for_card(self) -> dict[str, list[dict]]:
        """读 pvp.db 成 {池名: [{id}]}，供渲染器用。"""
        import json as _json
        groups: dict[str, list[dict]] = {}
        try:
            rows = self.kb._manual_q("pvp", "select name, pet from pvp")
        except Exception:
            rows = []
        for name, pets in rows or []:
            try:
                ids = _json.loads(str(pets or "[]").replace("'", '"'))
            except Exception:
                ids = []
            groups[str(name)] = [{"id": int(i)} for i in ids]
        return groups

    def _pool_lookup(self, pid):
        """精灵 id → {name, resource_id}。走 seerapi，回落手册 pets.db。"""
        try:
            r = self.kb._q("select id, name, resource_id from pet where id=?", (int(pid),))
            if r:
                return {"name": r[0][1], "resource_id": int(r[0][2] or r[0][0])}
        except Exception:
            pass
        try:
            r = self.kb._manual_q("pets", "select ID, Name from pets where ID=?", (int(pid),))
            if r:
                return {"name": r[0][1], "resource_id": int(pid)}
        except Exception:
            pass
        return None

    async def _render_pool(self, mode: str):
        """渲染一张池子图，返回 (bytes, 错误信息)。"""
        from .render_pool_card import normalize_mode, render_pool_card
        m = normalize_mode(mode) or "arena"
        groups = self._pool_groups_for_card()
        if not groups:
            return None, "手册 pvp.db 里没有池子数据，先跑一次手册同步。"
        try:
            data = await render_pool_card(
                m,
                groups=groups,
                lookup=self._pool_lookup,
                version=self.kb.pool_data_version(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pool_card] 渲染失败 mode=%s: %s", m, exc, exc_info=True)
            return None, f"池子图渲染失败：{exc}"
        return data, ""

    async def _send_image(self, event, data: bytes, prefix: str) -> bool:
        """把图片字节发到当前会话。返回是否成功。"""
        import tempfile
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=".jpg")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)
        try:
            from astrbot.core.message.message_result import MessageChain
        except Exception:
            try:
                from astrbot.core.message.message_event_result import MessageChain
            except Exception:
                MessageChain = None  # type: ignore
        if MessageChain is None:
            return False
        try:
            from astrbot.api.message_components import Image as ImageComp
            chain = MessageChain(chain=[ImageComp.fromFileSystem(path)])
            await self.context.send_message(event.unified_msg_origin, chain)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[pool_card] 发送失败: %s", exc)
            return False

    @llm_tool(name="seer_pool_card")
    async def seer_pool_card(self, event: AstrMessageEvent, mode: str = "") -> str:
        """画一张「巅峰圣战精灵池」的图，照游戏内那个页面的样子。

        # 什么时候用
        群友说「看看巅峰池」「竞技池有哪些」「专家池」「大师池什么价」
        「池子榜」「发张图看看」时用它。**比 seer_pool 的纯文字更适合看全貌。**

        # 三个模式（对应游戏内三个 tab）
          「竞技」「限制」「巅峰池」  → 竞技限制池：禁止池(限0) + 限制池(限2) + 准限制池(限3)
          「专家」                   → 专家禁用池
          「大师」「竞技点」          → 大师竞技点（35/20/10/6/0 点分档）

        mode 留空按「竞技」处理。

        # 数据来源
        手册 pvp.db（和游戏内页面同源，图上有数据版本时间戳）。
        **不要自己补日期** —— 图上写的就是真的。
        """
        data, err = await self._render_pool(mode or "arena")
        if data is None:
            return err
        sent = await self._send_image(event, data, "seer_pool_")
        from .render_pool_card import POOL_MODES, normalize_mode
        m = normalize_mode(mode) or "arena"
        label = POOL_MODES[m]["label"]
        ver = self.kb.pool_data_version()
        tail = f"（数据版本 {ver}）" if ver else ""
        if sent:
            return f"{label}的图画好了{tail}"
        return f"{label}的图渲染出来了但没发出去，数据版本 {ver}"

    # ── 巅峰投票榜出图（2026-11-14 加）─────────────────────────────
    # 数据来自游戏协议 cmd 4481（seer_vote），不是本地库。
    # 实测规律：限2 和限3 交替投票，一期只投一个池。
    #   20261030 期 → 限2 有数据
    #   20261106 期 → 限3 有数据
    # 服务端 sub_key=0 不给默认期次，所以只能自动探测（probe_latest，约 0.5s）。

    async def _render_vote(self, mode: str):
        """查+画一张投票榜。返回 (bytes|None, 数据, 错误)。"""
        from .render_vote_card import render_vote_card
        from .seer_vote import VOTE_KEYS, probe_latest

        m = str(mode or "").strip()
        # 说法 → key
        key = 191
        if any(w in m for w in ("限3", "限三", "准限", "192")):
            key = 192
        elif any(w in m for w in ("限2", "限二", "限俩", "191")):
            key = 191
        elif "限" in m or not m:
            key = 191            # 默认限2
        if "限3" in m or "限三" in m or "准限" in m:
            key = 192
        label = VOTE_KEYS.get(key, ("", 0))[0]

        data = await probe_latest(key)
        if data.get("error"):
            return None, data, data["error"]
        rows = data.get("rows") or []
        if not rows:
            tried = len(data.get("tried") or [])
            return None, data, (
                f"{label} 投票榜目前没有数据（试了 {tried} 个期次）。\n"
                "池子刚更新时投票还没开始，等官方开投票再来。"
            )

        from .seer_vote import _fmt_period
        note = (
            "限2 和限3 是交替投票的，一期只投一个池；"
            "榜上票数最高的前 12 名（限2）/ 前 25 名（限3）就是下一期生效的池子。"
        )
        if not data.get("current_open"):
            note = (
                f"⚠ 当期（{_fmt_period(data.get('current_period') or 0)}）的投票还没开始，"
                f"这张是最近一期有数据的 {_fmt_period(data.get('sub_key') or 0)} 期。\n"
                + note
            )
        try:
            blob = await render_vote_card(
                key, rows,
                sub_key=data.get("sub_key") or 0,
                lookup=self._pool_lookup,
                note=note,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vote_card] 渲染失败 key=%s: %s", key, exc, exc_info=True)
            return None, data, f"投票榜图渲染失败：{exc}"
        return blob, data, ""

    @llm_tool(name="seer_vote")
    async def seer_vote(self, event: AstrMessageEvent, pool: str = "") -> str:
        """查巅峰圣战投票榜（玩家投票结果），直接出一张排名图。

        # 什么时候用
        群友说「看看投票」「巅峰投票」「投票榜」「限2 投得怎么样」「限3 票数」
        「谁票最高」时用它。**这是玩家投票，不是官方池子名单**
        （官方名单用 seer_pool）。

        # 参数
        pool: 「限2」「限3」或留空。留空默认限2。
              **注意：限2 和限3 是交替投票的**，一期只投一个池。
              如果问的池子当期没投，会自动找到最近一期投过它的。

        # 数据
        游戏协议 cmd 4481 的日榜，实时拉取（不是本地缓存）。
        图上有期次日期和票数条形。
        """
        blob, data, err = await self._render_vote(pool)
        if blob is None:
            return err
        sent = await self._send_image(event, blob, "seer_vote_")
        from .seer_vote import format_vote
        label = data.get("label") or ""
        period = data.get("sub_key") or 0
        from .seer_vote import _fmt_period
        head = f"{label} 投票榜（{_fmt_period(period)} 期，{len(data.get('rows') or [])} 项）"
        if not data.get("current_open"):
            head += f"（当期 {_fmt_period(data.get('current_period') or 0)} 还没开投票，这是最近一期）"
        if sent:
            return head + "\n图已发出。前三名：" + "、".join(
                f"{r.get('name') or r.get('id')}({r.get('score')}票)"
                for r in (data.get("rows") or [])[:3]
            )
        # 图没发出去就退化成文字
        return head + "\n" + format_vote(data, limit=10)

    @filter.command("巅峰投票", alias={"投票榜", "投票"})
    async def cmd_vote_card(self, event: AstrMessageEvent):
        """巅峰圣战投票榜出图：/巅峰投票 [限2|限3]"""
        text = (getattr(event, "message_str", "") or "").strip()
        arg = text
        for kw in ("巅峰投票", "投票榜", "投票"):
            if kw in arg:
                arg = arg.split(kw, 1)[-1]
                break
        arg = arg.strip().lstrip("：: ").strip()
        blob, data, err = await self._render_vote(arg)
        if blob is None:
            yield event.plain_result(err)
            return
        sent = await self._send_image(event, blob, "seer_vote_cmd_")
        from .seer_vote import format_vote
        label = data.get("label") or ""
        period = data.get("sub_key") or 0
        from .seer_vote import _fmt_period
        tail = "" if data.get("current_open") else (
            f"（当期 {_fmt_period(data.get('current_period') or 0)} 还没开投票，"
            f"这是最近一期 {_fmt_period(period)}）"
        )
        if sent:
            yield event.plain_result(f"{label} 投票榜（{_fmt_period(period)} 期）{tail}")
        else:
            yield event.plain_result(
                f"{label} 投票榜（{_fmt_period(period)} 期）的图没发出去，先给你文字版：\n"
                + format_vote(data, limit=10)
            )

    @filter.command("巅峰池", alias={"竞技池", "专家池", "大师池", "圣战池"})
    async def cmd_pool_card(self, event: AstrMessageEvent):
        """巅峰圣战精灵池出图：/巅峰池 竞技 | 专家 | 大师"""
        text = (getattr(event, "message_str", "") or "").strip()
        arg = text
        for kw in ("巅峰池", "竞技池", "专家池", "大师池", "圣战池"):
            if kw in arg:
                arg = arg.split(kw, 1)[-1]
                break
        arg = arg.strip().lstrip("：: ").strip()
        if not arg:
            # 没给参数：默认出竞技
            arg = "竞技"
        from .render_pool_card import normalize_mode
        m = normalize_mode(arg)
        if m is None:
            yield event.plain_result(
                "用法：/巅峰池 竞技 ｜ 专家 ｜ 大师\n"
                "  竞技 → 禁止池(限0) + 限制池(限2) + 准限制池(限3)\n"
                "  专家 → 专家禁用池\n"
                "  大师 → 大师竞技点（35/20/10/6/0 点）\n"
                "纯文字查询用 /池子 或直接问「限2 有哪些」。"
            )
            return
        data, err = await self._render_pool(m)
        if data is None:
            yield event.plain_result(err)
            return
        sent = await self._send_image(event, data, "seer_pool_cmd_")
        from .render_pool_card import POOL_MODES
        label = POOL_MODES[m]["label"]
        ver = self.kb.pool_data_version()
        if sent:
            yield event.plain_result(f"{label}（数据版本 {ver}）" if ver else label)
        else:
            yield event.plain_result(f"{label}的图没发出去，可以重试一次。")

    @filter.command("夯拉榜", alias={"榜单", "排榜", "梯度图"})
    async def cmd_tier_list(self, event: AstrMessageEvent):
        """直接打字排榜：/夯拉榜 夯:精灵A,精灵B 顶级:精灵C ...

        和 seer_tier_list 工具共用同一套解析与渲染，区别只是入口：
        工具给模型调，命令给人手打。
        """
        text = (getattr(event, "message_str", "") or "").strip()
        # 去掉命令词本身（夯拉榜 / 榜单 / 排榜 / 梯度图，可能带 / 前缀）
        spec = text
        for kw in ("夯拉榜", "榜单", "排榜", "梯度图"):
            if kw in spec:
                spec = spec.split(kw, 1)[-1]
                break
        spec = spec.strip()

        if not spec:
            yield event.plain_result(
                "用法：/夯拉榜 夯:精灵A,精灵B 顶级:精灵C NPC:精灵D 拉完了:精灵E\n"
                "五档：夯 / 顶级 / 人上人 / NPC / 拉完了（也认 T0-S / T1-A / T2-B / T3 / T4-下水道）\n"
                "想自定义标题就在前面加一段，用 | 隔开：\n"
                "  /夯拉榜 盘点跌落神坛的 | 夯:无极圣武 拉完了:小火猴\n"
                "例：/夯拉榜 夯:无极圣武,艾夏拉 顶级:谱尼 NPC:皮皮 拉完了:小火猴"
            )
            return

        # 标题可以用 `|` 分隔放在最前面：`/夯拉榜 盘点跌落神坛的 | 夯:xxx`
        custom_title = ""
        if "|" in spec:
            head, tail = spec.split("|", 1)
            custom_title = head.strip()
            spec = tail.strip()
        elif "｜" in spec:
            head, tail = spec.split("｜", 1)
            custom_title = head.strip()
            spec = tail.strip()

        rows = self._tier_parse(spec)
        if not rows:
            yield event.plain_result(
                "没解析出分级。要写成 `夯:精灵A,精灵B 顶级:精灵C`——"
                "分级别用冒号、同级精灵用逗号、行与行用空格分开。"
            )
            return

        resolved: dict[str, list[dict]] = {}
        missing: list[str] = []
        for row in rows:
            items = []
            for name in row["pets"]:
                meta = self._tier_resolve(name)
                if meta is None:
                    missing.append(name)
                    items.append({"name": name, "id": None})
                else:
                    items.append({"name": meta["name"], "id": meta["id"]})
            resolved.setdefault(row["tier"], []).extend(items)

        try:
            from .render_tier_list import render_tier_list
            data = await render_tier_list(
                [{"tier": k, "pets": v} for k, v in resolved.items()],
                lookup=self._tier_resolve,
                title=custom_title or "从夯到拉",
                subtitle="",
                footer_left="赛尔号 · 神代·欧鲸鲸",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cmd_tier_list] 渲染失败: %s", exc)
            yield event.plain_result(f"榜单渲染失败：{exc}")
            return

        import tempfile

        fd, path = tempfile.mkstemp(prefix="seer_tier_", suffix=".jpg")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)
        try:
            yield event.image_result(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cmd_tier_list] 发图失败: %s", exc)
            yield event.plain_result(f"图没发出去（{exc}），文件在 {path}")
            return

        total = sum(len(v) for v in resolved.values())
        line = f"排好了：{len(resolved)} 档共 {total} 只。"
        if missing:
            line += "没查到的用了占位：" + "、".join(missing[:6])
            if len(missing) > 6:
                line += f" 等 {len(missing)} 只"
        yield event.plain_result(line)

    async def terminate(self):
        task = getattr(self, "_proxy_guard_task", None)
        if task is not None and not task.done():
            task.cancel()
            logger.info("[代理守护] 随 AstrBot 停止")
        ptask = getattr(self, "_platform_guard_task", None)
        if ptask is not None and not ptask.done():
            ptask.cancel()
            logger.info("[平台守护] 随 AstrBot 停止")
        atask = getattr(self, "_autologin_future", None)
        if atask is not None and not atask.done():
            atask.cancel()
        msync = getattr(self, "_manual_sync_task", None)
        if msync is not None and not msync.done():
            msync.cancel()
            logger.info("[手册同步] 随 AstrBot 停止")
        self.kb.close()
