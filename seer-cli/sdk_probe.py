#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sdk_probe.py — 探测 dsh --profile sdk-minimal 的 JSON-RPC 链路

用法：
    python sdk_probe.py                # 只做 initialize + shutdown
    python sdk_probe.py --ask "问题"   # initialize + prompt + 收回答

环境：
    DSH_HOME 默认 G:\\dsh-seer
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

DSH_HOME = os.environ.get("DSH_HOME", r"G:\dsh-seer")
PROFILE = os.environ.get("SEER_DSH_PROFILE", "sdk-minimal")
CWD = os.environ.get("SEER_WS", r"G:\dsh workspace")
PROVIDER = os.environ.get("SEER_PROVIDER", "deepseek-account")
MODEL = os.environ.get("SEER_MODEL", "deepseek-flash")


class RpcProc:
    def __init__(self, cmd: list[str], env: dict[str, str], cwd: str):
        self.p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.q: queue.Queue = queue.Queue()
        self.err: list[str] = []
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self._id = 0

    def _read_out(self) -> None:
        assert self.p.stdout
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except json.JSONDecodeError:
                self.q.put({"_raw": line})

    def _read_err(self) -> None:
        assert self.p.stderr
        for line in self.p.stderr:
            self.err.append(line.rstrip())
            if len(self.err) > 200:
                del self.err[0]

    def send(self, method: str, params=None) -> int:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        assert self.p.stdin
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return self._id

    def wait_result(self, rid: int, timeout: float = 120.0) -> dict:
        """等指定 id 的响应，途中把通知丢进回调列表。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                item = self.q.get(timeout=min(2.0, max(0.1, deadline - time.time())))
            except queue.Empty:
                if self.p.poll() is not None:
                    raise RuntimeError(f"进程已退出 code={self.p.returncode}\nstderr:\n" + "\n".join(self.err[-30:]))
                continue
            if item.get("id") == rid:
                return item
            self.on_notify(item)
        raise TimeoutError(f"等 id={rid} 超时 {timeout}s")

    def on_notify(self, item: dict) -> None:
        pass

    def close(self) -> None:
        try:
            if self.p.poll() is None:
                self.send("shutdown")
                time.sleep(0.3)
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass


def main() -> None:
    ask = None
    if "--ask" in sys.argv:
        ask = sys.argv[sys.argv.index("--ask") + 1]

    env = dict(os.environ)
    # 关键：清掉从当前 web 会话继承的 profile 变量，否则子进程会用错 profile
    for k in ("DSH_PROFILE", "DSH_PROFILE_DIR", "DSH_SESSION_ID", "DSH_WEB_URL", "DSH_SHELL"):
        env.pop(k, None)
    env["DSH_HOME"] = DSH_HOME
    env["PYTHONIOENCODING"] = "utf-8"

    exe = shutil.which("dsh") or os.path.join(
        os.environ.get("APPDATA", ""), "DSH", "data", ".bin", "dsh.cmd"
    )
    cmd = [exe, "--profile", PROFILE]  # sdk-minimal 是 shipped profile，不能加 --from-default-profile
    print(f"[probe] DSH_HOME={DSH_HOME}")
    print(f"[probe] {' '.join(cmd)}")
    print(f"[probe] cwd={CWD}")
    print(f"[probe] provider={PROVIDER} model={MODEL}\n")

    rpc = RpcProc(cmd, env, CWD)

    # ── initialize ──────────────────────────────────────────────
    rid = rpc.send(
        "initialize",
        {"cwd": CWD, "provider": PROVIDER, "model": MODEL, "reasoningEffort": "high"},
    )
    try:
        res = rpc.wait_result(rid, timeout=90)
    except Exception as e:
        print(f"[FAIL] initialize: {e}")
        print("stderr 末尾：")
        for ln in rpc.err[-25:]:
            print("  " + ln)
        rpc.close()
        return
    if "error" in res:
        print(f"[FAIL] initialize error: {json.dumps(res['error'], ensure_ascii=False)}")
        for ln in rpc.err[-25:]:
            print("  " + ln)
        rpc.close()
        return
    print(f"[OK] initialize → {json.dumps(res.get('result'), ensure_ascii=False)}")

    if not ask:
        rpc.close()
        print("\n[OK] 链路通。加 --ask \"问题\" 试完整回合。")
        return

    # ── prompt ─────────────────────────────────────────────────
    sid = "probe-" + str(int(time.time()))
    texts: list[str] = []
    statuses: list[str] = []

    def on_notify(item: dict) -> None:
        m = item.get("method")
        if m == "session.event":
            ev = (item.get("params") or {}).get("event") or {}
            et = ev.get("type") or ""
            data = ev.get("data") or {}
            if et == "assistant/message":
                # content 里可能同时有 reasoning / tool-call / text
                for block in (data.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "text":
                        t = block.get("text") or ""
                        if t.strip():
                            texts.append(t)
                    elif bt == "tool-call":
                        name = block.get("name")
                        args = block.get("arguments") or ""
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                pass
                        cmd = ""
                        if isinstance(args, dict):
                            cmd = args.get("command") or args.get("code") or ""
                        print(f"[tool] {name}: {str(cmd)[:120]}")
            elif et == "tool/result":
                pass
            elif et == "turn/end":
                reason = data.get("reason") or {}
                if reason.get("kind") == "error":
                    e = reason.get("error") or {}
                    print(f"[FAIL] turn 结束(错误): {e.get('message')}  code={e.get('code')}")
                else:
                    print(f"[ok] turn 结束: {reason.get('kind')}")
        elif m == "session.status":
            st = (item.get("params") or {}).get("status")
            statuses.append(st)
            print(f"[event] status={st}")

    rpc.on_notify = on_notify

    print(f"\n[probe] prompt: {ask}")
    rid2 = rpc.send(
        "session/prompt",
        {"sessionId": sid, "contentBlocks": [{"type": "text", "text": ask}]},
    )
    try:
        res2 = rpc.wait_result(rid2, timeout=60)
        print(f"[OK] prompt queued → {json.dumps(res2.get('result'), ensure_ascii=False)}")
    except Exception as e:
        print(f"[FAIL] prompt: {e}")
        for ln in rpc.err[-25:]:
            print("  " + ln)
        rpc.close()
        return

    # ── session/wait ───────────────────────────────────────────
    rid3 = rpc.send("session/wait", {"sessionId": sid})
    try:
        rpc.wait_result(rid3, timeout=300)
        print("[OK] turn 结束")
    except Exception as e:
        print(f"[warn] wait: {e}")

    print("\n=== 回答 ===")
    print("".join(texts).strip() or "(没收到文本，看上面 status)")
    rpc.close()


if __name__ == "__main__":
    main()
