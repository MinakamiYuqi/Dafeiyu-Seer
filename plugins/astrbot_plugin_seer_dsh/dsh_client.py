"""dsh_client.py — 异步 JSON-RPC 客户端，驱动 `dsh --profile sdk-minimal`

用法：
    client = DshClient(home=r"G:\\dsh-seer", cwd=r"G:\\dsh workspace",
                       api_key="sk-...", provider="deepseek-official",
                       model="deepseek-flash")
    await client.start()
    answer = await client.ask("chat-123", "灵巢魂印最高几层", timeout=120)
    await client.close()

设计要点：
  · 子进程常驻，首次 ask 时懒启动，挂掉自动重启（带退避）
  · 每个 chat_id 一个 sessionId，跨消息保上下文
  · 只取 assistant/message 里的 text 块，reasoning 丢弃
  · 进程异常 / 超时都会返回可读错误串，不抛给上层
"""
from __future__ import annotations

import asyncio
import json
import time
import os
import shutil
from typing import Any, Optional

import logging

log = logging.getLogger("seer_dsh")


class DshError(RuntimeError):
    pass


class DshClient:
    def __init__(
        self,
        *,
        home: str,
        cwd: str,
        api_key: str,
        provider: str = "deepseek-official",
        model: str = "deepseek-flash",
        reasoning_effort: str = "high",
        profile: str = "sdk-minimal",
        max_tokens: int = 0,
        system_prompt: str = "",
    ) -> None:
        self.home = home
        self.cwd = cwd
        self.api_key = api_key
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.profile = profile
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._err_task: Optional[asyncio.Task] = None
        self._stderr_tail: list[str] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._streams: dict[str, dict] = {}
        # 会话别名：逻辑 chat_key → 实际可用的 sessionId。
        # DSH 把会话落盘到 DSH_HOME，重启后同名 id 会被判「已存在」（-32603），
        # 这时换一个带时间戳的新 id 重试，并把映射记下来，后续沿用。
        self._sid_alias: dict[str, str] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._ready = False
        self._starting: Optional[asyncio.Task] = None
        self._closed = False

    # ── 生命周期 ────────────────────────────────────────────────
    def _exe(self) -> str:
        return shutil.which("dsh") or os.path.join(
            os.environ.get("APPDATA", ""), "DSH", "data", ".bin", "dsh.cmd"
        )

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # 关键：清掉从 AstrBot / 当前 shell 继承的 DSH_* ，否则会用到错 profile
        for k in (
            "DSH_PROFILE",
            "DSH_PROFILE_DIR",
            "DSH_SESSION_ID",
            "DSH_WEB_URL",
            "DSH_SHELL",
        ):
            env.pop(k, None)
        env["DSH_HOME"] = self.home
        env["DEEPSEEK_API_KEY"] = self.api_key
        env["PYTHONIOENCODING"] = "utf-8"
        # 关键：sdk-minimal 的 system prompt 只从环境变量读（personaPrefix），
        # 不会去读任何 md 文件。不设这个变量，agent 就是裸跑的。
        if self.system_prompt:
            env["DSH_SYSTEM_PROMPT"] = self.system_prompt
        return env

    async def start(self) -> None:
        async with self._lock:
            if self._ready and self._proc and self._proc.returncode is None:
                return
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        if self._closed:
            raise DshError("客户端已关闭")
        cmd = [self._exe(), "--profile", self.profile]
        log.info("启动 DSH: %s  (DSH_HOME=%s)", " ".join(cmd), self.home)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
            cwd=self.cwd,
        )
        self._stderr_tail.clear()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._err_task = asyncio.create_task(self._read_stderr())

        params: dict[str, Any] = {
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
        }
        if self.reasoning_effort:
            params["reasoningEffort"] = self.reasoning_effort
        if self.max_tokens > 0:
            params["maxTokens"] = self.max_tokens

        try:
            res = await self._request("initialize", params, timeout=90)
        except Exception as e:
            msg = str(e)
            # DeepSeek 只认 off/low/high/max；填错会被 UNSUPPORTED_REASONING_EFFORT 拒掉。
            # 这里自动退回不传该参数（服务端走默认 high），保证还能用。
            if "reasoning effort" in msg.lower():
                log.warning("[seer_dsh] reasoningEffort=%r 不被支持，改用服务端默认：%s",
                            self.reasoning_effort, msg)
                params.pop("reasoningEffort", None)
                res = await self._request("initialize", params, timeout=90)
            else:
                raise
        info = (res or {}).get("serverInfo") or {}
        if info.get("name") != "deepseek-harness-sdk-runtime":
            raise DshError(f"initialize 返回异常: {res}")
        self._ready = True
        log.info("DSH 就绪: %s v%s", info.get("name"), info.get("version"))

    async def ensure(self) -> None:
        if self._ready and self._proc and self._proc.returncode is None:
            return
        if self._starting and not self._starting.done():
            await self._starting
            return
        self._starting = asyncio.create_task(self.start())
        try:
            await self._starting
        finally:
            self._starting = None

    async def close(self) -> None:
        """优雅关闭：先 shutdown → terminate → 等退出 → 再收读任务。

        顺序很重要：先 cancel 读任务会留下未关闭的 pipe，CPython 退出时会刷
        `Event loop is closed` 的 ResourceWarning。
        """
        self._closed = True
        proc = self._proc
        if not proc:
            return
        try:
            if proc.returncode is None:
                await self._request("shutdown", None, timeout=5)
        except Exception:
            pass
        try:
            if proc.returncode is None:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass
        for t in (self._reader_task, self._err_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._proc = None
        self._reader_task = None
        self._err_task = None
        self._ready = False


    # ── 低层读写 ────────────────────────────────────────────────
    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                log.debug("非 JSON 输出: %s", text[:200])
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if not fut.done():
                    fut.set_result(msg)
            elif "method" in msg:
                self._on_notify(msg)
        # 进程结束：把所有等待中的请求叫醒
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(DshError("DSH 进程已退出"))
        self._pending.clear()
        self._ready = False

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self._stderr_tail.append(text)
            if len(self._stderr_tail) > 60:
                del self._stderr_tail[0]
            log.debug("dsh stderr: %s", text[:300])

    async def _request(self, method: str, params: Any, timeout: float = 120.0) -> Any:
        if not self._proc or not self._proc.stdin:
            raise DshError("DSH 进程未启动")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise DshError(f"{method} 超时 {timeout}s")
        if "error" in resp:
            err = resp["error"]
            raise DshError(f"{method} 失败: {err.get('message')} (code={err.get('code')})")
        return resp.get("result")

    # ── 事件收集 ────────────────────────────────────────────────
    def _on_notify(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method != "session.event":
            return
        ev = params.get("event") or {}
        et = ev.get("type") or ""
        data = ev.get("data") or {}
        sid = params.get("sessionId") or ""
        state = self._streams.get(sid)
        if state is None:
            # DSH 回来的 sessionId 可能与登记的不一致（换新 id 重试后尤其如此）。
            # 策略1：前缀匹配——新 id 是 "{base}-{timestamp}"，base 是原 chat_key。
            for key, s in list(self._streams.items()):
                if sid.startswith(key + "-") or key.startswith(sid + "-"):
                    state = s
                    self._streams[sid] = s   # 把真实 id 也登记进去，后续事件直接命中
                    log.warning("[seer_dsh] 未知会话 id %r → 前缀匹配到 %r", sid, key)
                    break
            # 策略2：唯一请求兜底（单群场景仍可用）
            if state is None:
                uniq = {id(v): v for v in self._streams.values()}
                if len(uniq) == 1:
                    state = list(uniq.values())[0]
                    log.warning("[seer_dsh] 未知会话 id %r，按唯一待处理请求派发", sid)
                else:
                    log.warning("[seer_dsh] 未知会话 id %r（%d 个并发），事件丢弃",
                                sid, len(uniq))
                    return

        if et == "assistant/message":
            for block in (data.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    t = (block.get("text") or "").strip()
                    if t:
                        state["texts"].append(t)
                elif bt == "tool-call":
                    state["tools"].append(block.get("name") or "?")
        elif et == "turn/end":
            reason = data.get("reason") or {}
            if reason.get("kind") == "error":
                e = reason.get("error") or {}
                state["error"] = e.get("message") or "turn error"
            fut = state.get("future")
            if fut and not fut.done():
                fut.set_result(True)

    # ── 对外：问一句 ────────────────────────────────────────────
    async def ask(
        self,
        session_id: str,
        text: str,
        *,
        timeout: float = 150.0,
    ) -> str:
        """发一条 prompt，等到 turn 结束，返回助手文本。

        出错返回以「⚠」开头的可读串，不抛异常（上层直接原样发出去）。

        会话复用：DSH 把会话落盘在 DSH_HOME，进程重启后再用同名 id 会被判
        「session already exists」(-32603)。这里遇到就换一个带时间戳的新 id 重试一次，
        并把 chat_key → 实际 sid 的映射记进 _sid_alias，后续消息沿用。
        """
        try:
            await self.ensure()
        except Exception as e:
            return f"\u26a0 DSH 启动失败：{e}"

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        state = {"texts": [], "tools": [], "error": None, "future": fut}

        sid = self._sid_alias.get(session_id, session_id)
        # 逻辑 id 和实际 id 都登记，session.event 里带的是实际 id
        self._streams[session_id] = state
        self._streams[sid] = state

        async def _prompt(use_sid: str) -> None:
            await self._request(
                "session/prompt",
                {
                    "sessionId": use_sid,
                    "contentBlocks": [{"type": "text", "text": text}],
                },
                timeout=30,
            )

        try:
            try:
                await _prompt(sid)
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                if "already exists" in low or "-32603" in msg:
                    fresh = f"{session_id}-{int(time.time())}"
                    log.warning("[seer_dsh] 会话 %s 已存在，改用 %s 重试", sid, fresh)
                    sid = fresh
                    self._streams[sid] = state
                    try:
                        await _prompt(sid)
                    except Exception as e2:
                        return f"\u26a0 DSH 提交失败：{e2}"
                    self._sid_alias[session_id] = sid
                else:
                    return f"\u26a0 DSH 提交失败：{msg}"

            try:
                await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return f"\u26a0 DSH 超时（{int(timeout)}s），可能卡在工具调用上。"
        finally:
            self._streams.pop(session_id, None)
            self._streams.pop(sid, None)

        if state.get("error"):
            return f"\u26a0 DSH 运行出错：{state['error']}"
        answer = "\n".join(state.get("texts") or []).strip()
        if not answer:
            tools = "、".join(state.get("tools") or []) or "无"
            return f"\u26a0 DSH 没有输出文本（调用过的工具：{tools}）"
        return answer

