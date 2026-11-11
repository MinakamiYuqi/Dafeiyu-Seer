# astrbot_plugin_seer_dsh

把 **@bot 的赛尔号问题**转给 **DSH agent** 处理。

DSH 那边会自己跑 `seer-cli/seer.py` 查库、算伤害、发现字段名错了自己改再查一遍——
这是 AstrBot 里那些 `llm_tool` 做不到的：工具给了模型，但模型不一定会调；DSH 的 agent loop 会自己动手。

---

## 架构

```
QQ 群
  ↓ @bot + 赛尔关键词
AstrBot
  ↓ 本插件拦截（event.stop_event）
  ↓ stdio JSON-RPC
dsh --profile sdk-minimal   （DSH_HOME = G:\dsh-seer，干净实例）
  ├─ agent（deepseek-flash + reasoningEffort high）
  ├─ 持久 PowerShell → python seer-cli\seer.py pet 灵巢
  └─ session 持久化（同 chat 保留上下文）
  ↓ 一句话回答
QQ 群
```

**不 @ bot、或者 @ 了但没赛尔关键词 → 本插件完全不介入，走 AstrBot 原流程。**

---

## 安装

1. 把整个目录放进 `AstrBot/core/data/plugins/` 或你的插件目录
2. 在 AstrBot 管理面板启用
3. 填配置里的 **`api_key`**（就是 AstrBot 里那个 DeepSeek 的 key）

**前置条件：**

- `G:\dsh-seer` 已建好（独立 DSH_HOME，**不带 prompt-inject.md、不装破甲类插件**）
- `G:\dsh workspace\seer-cli\seer.py` 存在且可跑
- PATH 里有 `dsh`

---

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `enable` | true | 总开关 |
| **`api_key`** | "" | **必填**，DeepSeek key，会作为 `DEEPSEEK_API_KEY` 传给 DSH |
| `require_at` | true | 必须 @ bot 才接管 |
| `require_keyword` | true | 必须命中赛尔关键词 |
| `keywords` | "" | 留空用内置 20 个词；也可自己填 |
| `enable_group` | true | 群聊生效 |
| `enable_private` | false | 私聊生效（默认关） |
| `allow_groups` | "" | 群白名单，留空=全部 |
| `dsh_home` | `G:\dsh-seer` | DSH_HOME |
| `dsh_cwd` | `G:\dsh workspace` | DSH 工作目录（seer-cli 要在这） |
| `dsh_profile` | `sdk-minimal` | 随附精简 profile |
| `provider` | `deepseek-official` | **名字必须一字不差** |
| `model` | `deepseek-flash` | |
| `reasoning_effort` | `high` | 先思考再动手，查库更准 |
| `timeout_seconds` | 150 | DSH 要跑多次工具，别设太小 |

---

## 内置赛尔关键词

```
赛尔 精灵 魂印 技能 属性 克制 伤害 连击
先制 本系 专属 特性 宝石 刻印 第五
巅峰 对战 精灵王 进化 种族值 威力
```

---

## 实测记录（2026-11-01）

**Q1：灵巢的威力阈值等于什么乘什么？（必须先跑 seer-cli 查）**

```
A: 威力阈值 = 40 × 魂落层数（命起同理），也就是 0 + 40×层数
   0层→0、1层→40、2层→80、3层→120、4层→160
   判定只认技能面板威力（迴还面板90，就算被第五加到170，仍按90算）
```

**Q2（同 session，测上下文）：那 4 命起时阈值是多少？**

```
A: 160。补充：这是全覆盖死锁 —— 灵巢全部攻击技能面板都 ≤160
   （第五160等于也算触发、无念150、芸长140、凝魂130、碎魂120、迴还90…）
   所以 4 命起怎么点都会触发魂印，且睡眠不被打不解 → 是死锁而非击杀
```

**Q3：次元打自然/超能几倍**

```
A: ×4.0（拆解：次元→自然 2.0、次元→超能 2.0，双系取平均后 4.0）
   它先读了 seer-cli/README.md 才调用，一条命令就对
```

**Q4：灵巢魂印最高几层**

**它自己跑了 7 次命令，中途用错字段名（`subject/body`）→ 报错 → 自己去查表结构 → 改成 `topic/content` → 拿到答案。**
**这就是要它的理由。**

---

## 技术要点（改代码前先看）

**① provider 名必须是 `deepseek-official`**

`sdk-minimal` 只注册了这个适配器。写 `deepseek` / `deepseek-account` 都会报 `no adapter registered`。

**② `sdk-minimal` 是 shipped profile**

不能加 `--from-default-profile`（会报 "names shipped profile ... cannot be a custom profile target"）。

**③ 子进程必须清掉继承的 DSH_\* 环境变量**

```python
for k in ("DSH_PROFILE","DSH_PROFILE_DIR","DSH_SESSION_ID","DSH_WEB_URL","DSH_SHELL"):
    env.pop(k, None)
```

**否则会继承 AstrBot/当前 shell 的 profile，启动错实例。**

**④ 回答在 `assistant/message`，不在 `assistant/attempt`**

```
assistant/message → data.message.content[]
  ├─ {type:"reasoning", text:...}         ← 思考过程，丢弃
  ├─ {type:"tool-call", name, arguments}  ← 工具调用
  └─ {type:"text", text:...}              ← 这才是回答
```

**⑤ 关闭顺序：先 shutdown → terminate → 等退出 → 再 cancel 读任务**

**反了会在 CPython 退出时刷 `Event loop is closed` 的 ResourceWarning。**

**⑥ session_id 按 chat 派生**

```python
session_id = "seer-" + re.sub(r"[^A-Za-z0-9_-]", "_", event.unified_msg_origin)[-60:]
```

**同一个群聊跨消息能接上下文（DSH 侧是 JSONL 持久化）。**

---

## 已知限制

- **冷启动慢**：首次问要等 DSH 进程起来（几秒）+ agent 跑工具（10~30 秒）
- **成本比普通问答高**：每次都是多轮工具调用，`deepseek-flash` + high reasoning
- **常驻一个子进程**：插件启用后 DSH 会一直在后台（在 `terminate()` 里关闭）
- **并发**：同一 chat 的多条消息会串行排进 DSH 的队列（DSH 侧保证同一 session 顺序）
