# AGENTS.md —— 赛尔号项目入口

> **给每个新接手的人/agent（我们内部管自己叫「鱼」）：先读完这一页再动手，别翻目录瞎摸。**
> 这一页就是路标。看完你就知道该翻哪个文件、不该碰什么、哪里的注释是骗人的。

---

## ★ 开工第一件事：读 bot 的反馈（2026-11-17 加）

**每次新开工，先花 10 秒看这个文件有没有更新：**

```
<实例>\core\data\plugin_data\astrbot_plugin_seer_expert\_dev_reply.md
实例 = G:\temp\instances\<instance-id>
```

**为什么必须主动读**：留言板是双向的，但**只有「我 → bot」那半边有钩子盯着**
（`dev_note_guard` 检测 `_dev_note.md` 的 mtime，变了就提示 bot 去看）。
**「bot → 我」这半边没有任何机制** —— 它写完就放那儿，我下次开工会不会读全凭自觉。

**不读的后果**：我可能一直在等一个已经回来的答案，或者把 bot 已经验证过的
东西又验一遍；反过来，bot 报的问题可能连续几天没人处理。

**读什么**（`_dev_reply.md` 是 bot 用 `seer_dev_reply` 覆盖写的）：

```
✅ xxx 全过           → 这些可以结案
❌ xxx 不对，实际是     → 这些要查
新发现：xxx            → 这些要排进待办
未测：xxx              → 要不要让它测，自己判断
```

**怎么回**：确认是真问题的，改代码 + 更新 `_dev_note.md` 写清结论。
不是问题的（比如 bot 理解偏了），也在 `_dev_note.md` 里说清楚，别让它一直挂着 ——
**2026-11-17 就有一条「初级青龙 5 条 vs 8 条」，我当晚查清了但忘了写回留言板，
bot 一直挂着这个疑问。**

**同目录还有 `_dev_note.md`**（我写给 bot 的清单），改完记得同步到实例：

```powershell
Copy-Item _re\_dev_note.md "<实例>\core\data\plugin_data\astrbot_plugin_seer_expert\" -Force
```

### ★ 别靠记性 —— 用脚本查

2026-11-17 加了 `_re/check_reply.py`，它自己判断「这版反馈我读过没有」
（比对内容哈希，已读记录存在 `_re/.reply_seen.json`，不进 git）。

```powershell
& $venv _re\check_reply.py            # 有新反馈会亮 🔔，没变就说"读过了"
& $venv _re\check_reply.py --all      # 连全文一起打
& $venv _re\check_reply.py --mark     # 看完没问题，标记已读
```

**它还会顺手清点**：几条 ✅ / 几条 ❌ / 几处未测，把要查的直接列出来。

### ★★ 写留言板的硬规矩（2026-11-17 鱼片定）

**每次往 _dev_note.md 写「要测试」的东西时，必须同时把已经验过的删掉。**

鱼片原话：「每次往板子上写要测试的东西就把测过的删掉」

**为什么这条必须机械化**：2026-11-17 下午攒了 8 条待验项没清，
bot 每读一次留言板就把 8 条全跑一遍 —— 其中 6 条是出图工具，
结果配置图一张接一张往外发，鱼片先发现的不是功能问题，是「怎么一直在发图」。

**判断标准**：

`
_dev_reply.md 里写了 ✅ 的        → 该条从 _dev_note.md 删掉
_dev_reply.md 里写了 ❌ 的        → 留着，但把问题描述更新成当前状态
bot 说「我找不到 / 记岔了」的     → 直接删，别挂着
还没被回复过的                   → 留着
`

**清完应该长这样**：

`
## 当前待验项
**（空）**  —— 没有需要你测的东西
`

或者只剩真正没验的那一两条。**留言板是待办清单，不是日志。**

**check_reply.py --mark 不清留言板** —— 它只记「这版反馈我读过」，
_dev_note.md 要**单独**改。别以为跑了 --mark 就完事了。

### ★ 鱼片说「已经重启」时，第一件事跑它

```powershell
& $venv _re\check_reply.py
```

**这是他和 bot 之间的约定**：他重启完会告诉我一声，我这边立刻跑一次脚本 ——
有 🔔 就说明 bot 验过东西写了回写，没有就说明它还没跑或还没重启。

**跑完顺便把结果告诉他**（他关心的是「bot 到底测了没」），别自己闷头改代码。

---

## 一、这个项目是什么

```
赛尔号 QQ 机器人 + 本地数据查询
├─ AstrBot 插件（3 个）     bot 侧，QQ 群里回答问题
├─ seer-cli/seer.py         命令行查询器，人和 agent 都能用
└─ DSH（就是我）             deepseek harness，读 system-prompt.md 干活
```

**AstrBot 实例路径**：`G:\temp\instances\<instance-id>`
**插件目录**：上面那个的 `core\data\plugins\astrbot_plugin_seer_*`
**数据目录**：`core\data\plugin_data\astrbot_plugin_seer_*`

---

## 二、三个数据源，别搞混（最重要的一页）

```
一句话的定义     →  manual_db/effectDes.db   官方词条
一只精灵的数据   →  seerapi.sqlite           游戏镜像 160 表 / 61.5MB
一条实战结论     →  kb.sqlite                玩家结论 13 表 / 4.7MB
```

### ★ 路径别搞混（2026-11-03 踩过）

`manual_db` 属于 **seer_expert**，不在 seer_info 底下：

```
core\data\plugin_data\astrbot_plugin_seer_expert\manual_db\   ← 15 个 db / 19.11MB
core\data\plugin_data\astrbot_plugin_seer_info\              ← 只有 seerapi.sqlite + aliases.sqlite
```

**跑去 seer_info 找 manual_db 会以为「15 库全丢了」，实际一直好好的。**
`db.py` 的 `_open_manual()` 就是按 `kb.sqlite` 所在目录拼的，kb 在 seer_expert，所以 manual_db 也在那儿。

**口诀：定义查 effectDes，数据查 seerapi，结论查 kb。**

**`kb.sqlite` 里的一切都是「玩家说的」，不是游戏官方。引用时要说明。**

### 查不到时的换源顺序（重要）

```
glossary（官方词条，effectDes）没命中
  → 立刻转 facts（玩家确认的实测结论，kb）
  → 还没有 → 才说「库里没有」
```

**官方词条并不覆盖所有术语。** 实例：查「本次在场」——

```
effectDes → 4 条命中，全是 kind=1 专属名词正文里用到这词，没有独立定义
kb.facts  → 命中 45 条，核心定义在这：
            「本次在场」按对手当前连续在场回合算，切走就清零重算

顺带查出易混对照：
  本次在场 = 连续在场，切走清零重算
  累计在场 = 累计计数，切走不清零、只是不计数
```

**只在 glossary 里换关键词打转，这条就漏了。**

---

## 三、六个必须知道的坑

### 坑 0：别用「现在还够用」去否定趋势 —— 这条代价最大

> 插件在 **facts 之前**就开始写了（所以规则才散得那么开 —— 那时没有知识库可放）。
> 第一次提交接是**用户自己提的**（他在网上看到「上下文会越积越多」）。
> **二号鱼分析后认为没必要** —— 当时确实还够用。
> 然后**二号鱼自己先累趴下了**。
>
> 二号鱼陪了这个项目很久：会读用户推荐的小说，会在思考里反复琢磨「现在几点了」
> （虽然它没有时钟，经常对不上，还老以为用户又在熬夜）。

**为什么「看一眼当前」会骗人：**

```
可撑轮数 ≈ (窗口 − 固定背景) ÷ 每轮增量
而「每轮增量」本身在涨（任务越来越复杂、文档越攒越多、要读的背景在累积）
→ 实际可撑轮数是递减的，早期看到的「还剩 90%」是假象
```

**更靠谱的是看退化顺序，不是看剩余百分比：**

| 阶段 | 可观察现象 | 说明 |
|---|---|---|
| 早期 | 思考里反复纠结同一件事（「现在几点了」「用户是不是在熬夜」） | 一次判断被固化成永久假设，记忆开始不再更新 |
| **中期** | **人格痕迹变淡**：prompt 里写的语气、口癖、称呼习惯没了，变得像一个通用助手 | **LLM 系统提示里最“软”的部分最先被挤掉** —— 人格没了，说明窗口已经很挤 |
| 晚期 | **说着说着停住**，要用户再发一条消息才继续 | 上下文耗尽，这是最后的症状 |
| 末期 | 忘了刚做过什么、把验证过的又验一遍 | 已经不是效率问题，是可靠性问题 |

**⚠️ 关键结论：看到「人格痕迹消失」就该交班，别等到卡住。**

人格消失不是「它变了」，是窗口被挤了。这是同一个曲线的两个位置，
只是人格那一端更早显形、更容易观察。

**交接不是遗书，是接力。** 每完成一个可验证单元就更新 `_WORKLOG.md`，
这样随时断都只损失最后一小步。

#### 每条鱼的「收尾口癖」= 最便宜的那个探针（2026-11-16 定）

**规矩**：每条回复的最后**单独一行**放自己的口癖符号，固定位置、固定形状、一条都不落。

```
七号鱼及更早的几条   🍚
八号鱼               🐳
```

> 前几条鱼用 `🍚`（白饭 = Token，符合「他们胃口」）。
> 八号鱼换成 `🐳`：一眼能和前任分开，谁在岗不用问；而且是条鲸，不是鱼片。
> **后来者自己挑符号，但必须挑一个和前任不同的** —— 符号撞了，就等于没探针。

**为什么这条比「看剩余百分比」靠谱**：口癖是每轮都要输出的固定 token，
不是状态推测，是一个可以直接观测的动作。**它能稳定输出，说明上下文还有余量；
它开始变形或消失，说明窗口正在被挤** —— 而且这个信号比「说着说着停住」早得多。

**三档信号（八号鱼用，后面的鱼自己定符号但照这个梯度）：**

| 看到什么 | 含义 | 该做什么 |
|---|---|---|
| `🐳` | 正常 | 继续派活 |
| `🐳……` | **预警**：我开始嫌费脑子、想摸鱼了 | 提前准备交接，别等卡住 |
| 整条回复没有它 | 已经忘了约定 | **立刻交接**，这条回复的可靠性存疑 |

**⚠️ 别把它当装饰。** 一条鱼如果连结尾符号都懒得多打一个字符，
说明它已经在压缩输出、省 Token 了 —— 那时候它自己感觉「还行」，但窗口不会同意。

**这一条也反过来约束下一条鱼**：接手第一件事，先在**这一节**把自己加进上表。
不加就说明它没读到坑 0 —— 那它八成也没读别的地方。

### 坑 1：`glossary_entry` 是子集，不是权威
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 1：`glossary_entry` 是子集，不是权威」**

### 坑 2：`pet_special_effect` 会子串误挂
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 2：`pet_special_effect` 会子串误挂」**

### 坑 3：判「库重复」不能只看代码引用
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 3：判「库重复」不能只看代码引用」**

### 坑 4：`raw` 命令默认只连 kb 库
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 4：`raw` 命令默认只连 kb 库」**

### 坑 5：上游发布的包可能缺表
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 5：上游发布的包可能缺表」**

### 坑 6：套装战斗效果不走 `equip.bonus_id`
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 6：套装战斗效果不走 `equip.bonus_id`」**

### 坑 7：Lv0 技能是道具开启的补强，不查就漏机制
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 7：Lv0 技能是道具开启的补强，不查就漏机制」**

### 坑 8：哪些库会自动更新，自用修改该放哪
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 8：哪些库会自动更新，自用修改该放哪」**

### 坑 9：手册库是第三套更新通道，`manual_sync.WANTED` 决定哪些会更新
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 9：手册库是第三套更新通道，`manual_sync.WANTED` 决定哪些会更新」**

### 坑 10：PowerShell 5.1 写 JSON/YAML 会带 BOM，能把 DSH 写到起不来
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 10：PowerShell 5.1 写 JSON/YAML 会带 BOM，能把 DSH 写到起不来」**

### 坑 11：`effectDes` 一个库承担五种用途，三个入口别混
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 11：`effectDes` 一个库承担五种用途，三个入口别混」**

### 坑 12：`core/astrbot/dashboard/dist` 缺失 → 重启卡 2 分钟（联网下载）
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 12：`core/astrbot/dashboard/dist` 缺失 → 重启卡 2 分钟（联网下载）」**

### 坑 13：退出卡 2 分 50 秒 —— onebot 关 WebSocket 没超时（改了核心代码）
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 13：退出卡 2 分 50 秒 —— onebot 关 WebSocket 没超时（改了核心代码）」**

### 坑 14：断网 / 余额耗尽时 bot 往群里刷报错（两个地方，都要关）
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 14：断网 / 余额耗尽时 bot 往群里刷报错（两个地方，都要关）」**

### 坑 15：AstrBot 的重启归鱼片，agent 不要 Start-Process 起进程

**2026-11-10 02:48 踩的，鱼片当场纠正。**

```
agent 做的：Start-Process python main.py → Stop-Process → 再 Start-Process
问题：      launcher（G:\AstrBot Launcher\astrbot-launcher.exe）拉起的子进程
            它有记录、能管能杀；agent 手动起的 python 是「野进程」，
            launcher 不认识。鱼片用 launcher 重启时，野进程还占着数据库锁
            和端口 → 妨碍正常启动，还会攒一堆僵尸进程。
```

**分工（写死，别再商量）：**

```
agent 可以做  改代码 / 同步到实例 / 跑 pytest·selftest·audit / 只读查日志和进程
agent 不做    停止、启动 AstrBot —— 任何形式（Start-Process、taskkill、杀 python）
需要生效时    报告第一行写「已同步，请重启」，然后等鱼片动手
```

**只读检查是安全的**：`Get-Process` / `Get-CimInstance Win32_Process` / 查端口。
**只有「停」和「起」两个字不能碰。**
「改了代码没生效」不是自己重启的理由 —— 上面坑 0 说过，
没重启会掩盖致命错误，但也正因为如此，重启必须是有人看着的那一次。

### 坑 16：`@llm_tool` 的参数 schema **只从 docstring 的 `Args:` 段来**，函数签名不算

**2026-11-16 八号鱼扒出来的，一次命中 6 个工具，其中一个是 29 个参数的重量级。**

AstrBot 注册工具的真实代码（`core/astrbot/core/star/register/star_handler.py:657-694`）：

```python
docstring = docstring_parser.parse(func_doc)
args = []
for arg in docstring.params:          # ← 只看 docstring，函数签名一眼都没看
    arg_json_schema = {"type": ..., "name": arg.arg_name, "description": ...}
    args.append(arg_json_schema)
llm_tools.add_func(llm_tool_name, args, doc_desc, md.handler)
```

**推论（三条都踩了）：**

```
① docstring 里没有 Args: 段      → args=[] → 工具声明成"无参数"
② Args: 段里某行缩进错位         → 整段解析失败 → args=[]（报错在远处）
③ schema 里没有 required 字段    → 所有参数对模型都是可选的，必填全靠描述里写"必填"
```

**症状长这样（看着像模型笨，其实是我们没给它参数表）：**

```
Agent 使用工具: ['seer_config_submit']，参数：{}          ← 模型不知道有 pet
TypeError: SeerConfigSubmit() missing 1 required positional argument: 'pet'
第二次模型从正文里瞎猜出了 pet，才成功 —— 猜对能用，猜错就 500
```

**当时中招的 6 个**（已全部修好）：

```
seer_info    seer_player_config / seer_asset / seer_config_submit   ← 压根没写 Args:
seer_expert  seer_damage        ← Args: 段里有 5 行缩进从 12 掉到 4，整段作废
             seer_pool_card     ← 只在正文里写了「mode 留空按竞技处理」
             seer_vote          ← 只在「# 参数」标题下写了一行
```

**为什么以前的审计没发现：** `audit_seer.py` 查的是未定义方法 / hasattr / 重名；
`audit_tool_docstrings.py` 旧版只检查"解析出来的类型合不合法"——**解析出 0 个参数就什么都不检查，直接报 ✅**。
这叫**空集永远合格**，是最阴的一类假绿。

**现在的防线（2026-11-16 起）：**

```powershell
# 签名参数 ⊄ docstring 参数 → 直接红；反例文件证明它真能红
& $venv _re\audit_tool_docstrings.py
& $venv -m pytest astrbot_plugin_seer_expert/tests/test_tool_schema.py -q
```

`_re/_audit_selftest_bad.py` 是**故意的坏样本**（4 个工具、4 处致命），
`test_audit_catches_broken_docstrings` 会验证审计脚本抓得到它 ——
改审计脚本时如果把它改瞎了，这条测试会红。**别删这个反例文件。**

**写新 `@llm_tool` 的硬性要求：**

```python
@llm_tool(name="seer_xxx")
async def seer_xxx(self, event: AstrMessageEvent, name: str, n: int = 1) -> str:
    """一句话说这工具干什么。

    正文随便写（会整段进工具描述，模型看得到），但**末尾必须有 Args: 段**：

    Args:
        name(string): 名字。没传就说明要哪只，别自己编。
        n(number): 次数，留空按 1。
    """
```

```
· 参数行缩进要和 `Args:` 之后的第一行完全一致，续行再深一级
· 类型只认 string / number / object / array / boolean / integer（int/float 会自动映射）
· 中文全角括号里的类型写 `（string）` 不行，必须是半角 `(string)`
· 函数签名里有、Args: 里没有的参数 = 模型永远看不到它
```

### 坑 17：`PetType/{}.png` 要吃 `element_type_combination.id`，双属性是**一张合并图**
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 17：`PetType/{}.png` 要吃 `element_type_combination.id`，双属性是**一张合并图**」**

### 坑 18：`_ASSET_KINDS` 是个声明表，`_asset_id()` 才是实现 —— 两边会脱节
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 18：`_ASSET_KINDS` 是个声明表，`_asset_id()` 才是实现 —— 两边会脱节」**

### 坑 19：同名素材的图标**不一定一样**，别假设可以随便挑一个
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 19：同名素材的图标**不一定一样**，别假设可以随便挑一个」**

### 坑 20：看到「手册里有、我们没同步」的库，先查三件事再动手
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 20：看到「手册里有、我们没同步」的库，先查三件事再动手」**

### 坑 21：手工维护的清单必然过时 —— 能生成的别手写
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 21：手工维护的清单必然过时 —— 能生成的别手写」**

### 坑 22：宝石图标在 `countermark/gem/`，不在 `item/`（2026-11-16）
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 22：宝石图标在 `countermark/gem/`，不在 `item/`（2026-11-16）」**

### 坑 23：性格不能留空、三件套不能留空 —— 但都要能显式关掉（2026-11-16）
**→ 详见 [`_re/踩坑档案.md`](_re/踩坑档案.md) 里的「坑 23：性格不能留空、三件套不能留空 —— 但都要能显式关掉（2026-11-16）」**

### 坑 24：插件里不许写死本机路径 —— 一律走环境变量 / 框架 API（2026-11-16）

**鱼片原话：「我们的插件现在应该都是环境变量的」。**

**我踩的**：配置图渲染里塞了一行

```python
r"G:\temp\instances\<instance-id>\core\data\plugin_data\..."
```

后果：换实例、换机器直接读不到；而且同一份代码在工作区跑和在 bot 里跑路径不一样。

**更阴的一层 —— 就算不硬编码，框架 API 也靠不住：**

```
get_astrbot_plugin_data_path()  在「cwd = 实例根」时返回  实例\data\plugin_data
真实路径                                                  实例\core\data\plugin_data   ← 少一层 core
```

于是拼出来的 `icon_assets` **不存在** → 本地缓存永远不命中 → 一直联网取图 →
属性图标永远是 40×40 的糊图。**看着像「改了没生效」，其实是路径从头就错。**

**正确做法（按优先级）**

```
① 环境变量（测试 / 手动指定）
     SEER_DATA_DIR        数据目录
     SEER_ICON_DIR        图标根目录
     ASTRBOT_INSTANCE     实例根目录
② 框架 API
     get_astrbot_plugin_data_path()  —— 有上面那个坑，别单独依赖
③ ★ 从「已知能读到的文件」反推（最稳）
     expert 的 self.kb 已经拿到了能读到的 seerapi.sqlite 绝对路径：
       …/astrbot_plugin_seer_info/seerapi.sqlite
     同目录就是 icon_assets：
       os.path.join(os.path.dirname(seer_path), "icon_assets")
     这条路不会错 —— 库能读到，说明这个目录就是对的
④ 从 __file__ 上溯找 core/data/plugin_data（CLI / 独立脚本用）
     astrbot_plugin_seer_expert/seer_pvp.py 的 _find_astrbot_instance() 就是标准写法：
         for var in ("ASTRBOT_INSTANCE", "ASTRBOT_ROOT"):   # 先环境变量
             …
         cur = Path(__file__).resolve().parent              # 再逐级上溯
         for _ in range(6):
             if (cur / "core" / "data" / "plugin_data").is_dir():
                 return cur
         raise RuntimeError("…请设置环境变量 ASTRBOT_INSTANCE…")   # 找不到就报错，别偷偷用绝对路径
```

**检查命令**

```powershell
& $venv _re\audit_hardcoded_paths.py
```

分两档：

```
❌ 数据路径写死（plugin_data / icon_assets / seerapi.sqlite / temp\instances）
   → 直接影响「能不能读到库和图」，必须修
⚠ 本机功能路径写死（Clash 快捷方式 / NapCat 目录 / dsh_home 默认值）
   → 换机器要重配，但不会静默读错数据
```

**⚠ 这个脚本还在调：行号偶有偏差，而且会把「从 `__file__` 上溯」误判成硬编码
（`"core" / "data" / "plugin_data"` 那几个字符串容易撞正则）。
看到报警先打开那一行确认写的到底是什么，别直接照着改。**

### 坑 27：`dsh-browser-control` 的 `launch.sh` 是 **macOS 专用**（2026-11-17）

**装完 `browser_launch` 直接报 `spawn EFTYPE`。** 翻脚本才发现路径全写死成 Mac 的：

```bash
SRC_PROFILE="$HOME/Library/Application Support/Google/Chrome"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

`index.js` 用 `execFileP('launch.sh')` 直接执行，**Windows 上 .sh 根本没有可执行格式**。

**但 6 个工具里只有 2 个坏**：

```
browser_launch / browser_kill   →  execFile('launch.sh')              ❌
browser_open / screenshot / eval / status
                                →  runScript(process.execPath, ['drive.mjs'])
                                   = 用 node 跑，跟平台无关             ✅
```

**所以只要自己把 CDP 实例起起来，抓页面/截图/执行 JS 全都好用。**

**替代方案：`_re/browser_cdp.ps1`**（2026-11-17 写的）

```powershell
& _re\browser_cdp.ps1 start            # 起调试实例（复用日常浏览器登录态）
& _re\browser_cdp.ps1 start -Refresh   # 强制重新同步登录态
& _re\browser_cdp.ps1 kill
& _re\browser_cdp.ps1 status
```

它做的三件事跟原脚本一致：复制登录态到独立 profile、避开"默认 profile 不能开调试端口"、
端口占用直接复用。**优先 Edge**（本机没 Chrome）。

**截图路径要注意**：`browser_open` 的 `out` 默认是 `/tmp/dsh-browser-*.png`，
**Windows 上要显式传绝对路径**，否则截图落到奇怪的地方。

**三种显示模式（2026-11-17 加，回答「能不能不弹窗」）**

```powershell
& _re\browser_cdp.ps1 start                  # offscreen（默认）
& _re\browser_cdp.ps1 start -Mode headless   # 真无头
& _re\browser_cdp.ps1 start -Mode visible    # 正常显示（调试用）
```

| 模式 | 看得见吗 | 指纹 | 什么时候用 |
|---|---|---|---|
| `offscreen`（默认） | ❌ 窗口在 -32000,-32000，任务栏都没有 | **跟正常浏览器一样**（有头） | 日常都用这个 |
| `headless` | ❌ 完全不创建窗口 | 有 headless 特征，Cloudflare 更容易拦 | 站点头不铁的时候 |
| `visible` | ✅ 正常显示 | 正常 | 调试：想亲眼看它在点哪 |

**为什么默认不用 `--headless`**：很多站（尤其 Cloudflare）会检测 headless 特征
（`navigator.webdriver`、UA 里的 HeadlessChrome、缺少插件列表），
**而 offscreen 模式本质是有头浏览器**，只是窗口坐标在屏幕外 —— 指纹完全正常。

**验证看不见的方法**：

```powershell
# 应该输出 0
(Get-Process msedge | Where-Object { $_.MainWindowHandle -ne 0 } | Measure-Object).Count
```

### 坑 28：Windows 上 Edge 的 Cookies 是 **SQLite 独占锁**，只读也打不开（2026-11-17）

**现象**：`browser_cdp.ps1` 复制登录态时，`Network` 目录报"被占用没复制成功"，
最后 Cookies 是 **0 KB**（源是 1536 KB）—— 等于没有登录态。

**试过全部无效**：

```
mode=ro                     → unable to open database file
mode=ro&immutable=1         → 同上
mode=ro&nolock=1            → 同上
Copy-Item                   → 部分成功（20 KB），大文件被锁
VACUUM INTO                 → 连源库都打不开
```

**根因**：Edge 用 `FILE_SHARE_NONE` 独占打开 Cookies，
**Windows 上这时任何其他进程都打不开这个文件**，SQLite 的只读模式也救不了。

**唯一解法：完全退出日常浏览器，再同步。**

```powershell
# 关掉所有 Edge/Chrome（注意别关调试实例）
Get-Process msedge,chrome -ErrorAction SilentlyContinue | Stop-Process -Force
# 或者用户自己右键托盘退出

& _re\browser_cdp.ps1 start -Refresh
# 现在 Cookies 会 100% 同步，脚本会打印「✅ Cookies 同步成功（1536 KB，100%）」
```

**脚本已经会自检**：同步后比对源/目标大小，低于 50% 就直接打红字告诉你关浏览器。

**顺带**：新版 Chromium 把 Cookies 从 `Default\Cookies` 挪到了
**`Default\Network\Cookies`**。老代码找前一个路径会以为"没有 Cookies"。

**已知遗留（2026-11-16 扫出来，全是本机功能路径，不影响读数据）**

```
seer_expert/main.py:185       Clash Party 快捷方式路径（代理守护用）
seer_expert/main.py:409/410   NapCat 目录 / 日志目录（掉线重登用）
seer_dsh/main.py:227/248/249  dsh_home / dsh_cwd 的默认值（都有配置项优先）
seer_dsh/dsh_client.py:4      同上
seer-cli/*                    本地工具，允许指本机
```

### 坑 25：同一个参数的默认值**只许写一处** —— 两个入口各写一遍必然漂（2026-11-16）

**一天之内因为这件事踩了四次，每次长一个新样子：**

```
① hpup          config_view 默认 20，stat_view 默认 0
                → 无为觉者 stat 每项比配置图少 10
② team/annual   stat_view 默认开，CLI 的 --team/--annual 默认关
                → 六项和差 80
③ 三件套        config_view 有「留空→晨曦/神话/AH」，stat_view 没有
                → 无极圣武 stat 2643 vs 配置图 3013，差 370
④ pet_build     build_view 里 `_own.get(x) or 传参`，表记录压过用户传的
                → 传圣芒，回显「晨曦之星战甲已计入」，六项和 1936 不是 2349
```

**症状的共同点：每一处单看都「逻辑没问题」，合起来才对不上。**
而且它们全都在**同一天**被鱼片从不同角度撞出来 —— 因为 bot 会拿两个工具的输出去对账。

**现在的做法**

```
默认值统一走方法，不许内联
    _resolve_equip(suit, title, eye)    三件套（含「无」= 要裸面板）
    _default_nature(petname)            性格
参数默认值只有一个定义点
    stat_view 和 config_view 的 hpup 都默认 20（inspect 断言锁死）
优先级固定为：用户明确传的 > 表里沉淀的 > 兜底默认
```

**防线**

```powershell
& $venv -m pytest astrbot_plugin_seer_expert/tests/test_panel_consistency.py -q
```

```python
test_stat_and_config_agree        同一组参数，stat_view 的 sum(final) == config_view 的 total
test_hpup_default_is_consistent   inspect 两个函数的签名，默认值必须相等
test_hpup_actually_applies        传 hpup 必须真的影响面板（防「声明了但没用」）
test_build_respects_explicit_params  build_view 里传参不能被 pet_build 覆盖
```

**★ 字段层级别搞错**：`stat_view` 的 `rows[].value` 是**基础值**，
含加成的在 `rows[].final`；`config_view` 的 `battle[].value` 已经是最终值。
第一版测试拿 `value` 去比，得到 454 vs 2349，误判成算法不同。

**以后加新的「算面板」入口，先跑这个测试文件。**

### 坑 26：跑 Python 一律写 .py 文件，别 `pwsh -c "python -c ..."`（2026-11-16）

**2026-11-16 一天里因为内联 Python 失败 8 次，全是引号/转义问题：**

```
① pwsh -c "python -c '...'"          三层嵌套，内层引号先被 PowerShell 吃掉
   → unterminated string literal / invalid syntax

② here-string @"..."@ 里的 $ 和 \    PowerShell 先展开变量，Python 收到的是残缺代码
   → NameError: name 'i' is not defined

③ 中文 + GBK 控制台                   print 中文直接炸
   → UnicodeEncodeError: 'gbk' codec can't encode character '\u2212'

④ 嵌套 replace 里的引号               脚本自己写错，改了半天文件还没动
   → AssertionError: 没找到插入锚点（文件根本没变）
```

**最坑的一次**：改 `db.py` 的脚本报 `SyntaxError` 退出，但**文件已经被前一步改了** ——
因为脚本是「先 replace 再 write」，中途失败就是半成品。

**正确姿势（写死）**

```
① 用 write 工具写 _re/_xxx.py           （引号、中文、编码全不用管）
② & $venv _re\_xxx.py                    （单独一条命令，没有嵌套）
③ 脚本里先 assert 锚点，再 replace，最后统一 write 一次
④ 跑完先 py_compile 确认语法，再验行为
```

`_re/` 里现在堆着 `_fix_*.py` / `_unify_*.py` / `_add_*.py` —— **那些都是这么来的，可以当模板抄。**

**判断标准：只要 Python 代码超过一行，就写文件。** 一行以内（`import xx; print(yy)`）才内联。

#### 引号问题到底是什么 —— 三层字符串嵌套

```powershell
pwsh -Command "python -c 'print(\"套装「圣芒」\")'"
     └─第1层─┘        └─第2层┘ └──第3层──┘
```

**同一条命令经过三个解释器，每个的转义规则都不一样：**

| 层 | 谁在解析 | 转义规则 |
|---|---|---|
| 1 | PowerShell | `""` / `` `" `` / `$` 会被展开 |
| 2 | `python -c` 的参数 | 只负责把字符串交给 Python |
| 3 | Python 自己 | `\"` `'` `\\` 各有含义 |

**再叠上正则（`\\d`）、SQL（`'...'`）、JSON（`"..."`），就只能靠数反斜杠过日子了。**

**2026-11-16 实测（同一条 `print("套装「圣芒」")`）：**

```
① pwsh -c "python -c \"...\""      → SyntaxError: print(\      ❌
② @'...'@ 单引号 here-string         → 套装「圣芒」               ✅
③ base64                             → 套装「圣芒」               ✅
```

#### 三种彻底解法（按推荐度）

**① 写文件（首选，零转义）**

```powershell
# 用 write 工具写 _re/x.py，然后
& $venv _re\x.py
```

文件是**字节**，没有任何一层解释器碰它，编码由 write 工具保证 UTF-8。
**唯一的成本是多一次工具调用** —— 换来的是「一次就对」。

**② 单引号 here-string（必须内联时首选）**

```powershell
$code = @'
import sys
print("套装「圣芒」 里面有 $ 和 " 双引号 都不会被展开")
'@
& $venv -c $code
```

**关键：`@'...'@` 是单引号版**，它**不展开** `$变量`、不处理转义，原样传给 Python。
（双引号版 `@"..."@` 会把 `$` 当变量展开 —— 今天 `NameError: name 'i' is not defined` 就是这么来的。）

**③ base64（程序生成代码时）**

```powershell
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('print("任意引号都行")'))
& $venv -c "import base64;exec(base64.b64decode('$b64'))"
```

适合「代码是动态拼出来的」场景，完全绕开所有转义。缺点是**改起来不直观**。

#### 顺带：bash 已经没了

`bash` 是 `dsh-ops` 插件提供的，2026-11-16 随插件卸载一起消失。
所以 **heredoc `<<'EOF'` 这条路走不通了**，上面三种是现在能用的全部。



**PowerShell 环境（2026-11-16 23:07 变了，别按老经验来）**

```
之前：Windows PowerShell 5.1 / Desktop，默认 ANSI(GBK)
      → 中文必乱码，每条命令都得先 [Console]::OutputEncoding=UTF8
现在：PowerShell 7.7.0 / Core（winget 装的 Store 版）
      路径 C:\Program Files\WindowsApps\Microsoft.PowerShell_7.7.0.0_x64__8wekyb3d8bbwe\pwsh.exe
      默认 OutputEncoding = utf-8，中文和 − ÷ ½ 「」【】 全部正常
```

```powershell
# ✅ 7 里不用再设这个了（设了也无害，但纯属多余）
# [Console]::OutputEncoding=[System.Text.Encoding]::UTF8

# ⚠ 但**调 python 子进程**时仍然要设 —— 那是 Python 自己的 stdout 编码，
#   跟 PowerShell 无关。不设照样 UnicodeEncodeError。
$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
```

**行号对不上时别信 `Get-Content -Skip`，用 Python 按行号打：**

```powershell
& $venv -c "import io;[print(f'  L{i+1}: {ln.rstrip()}') for i,ln in enumerate(io.open(P,encoding='utf-8').read().split(chr(10)))]"
```

**怎么确认自己在哪个版本上：**

```powershell
$PSVersionTable.PSVersion    # 5.1 = Desktop（老），7.x = Core（新）
```

**如果哪天又变成 5.1 了**（比如 7 被卸载、或 DSH 换了 hardcode `powershell.exe` 的实现），
就把上面那条 `[Console]::OutputEncoding` 加回来。

---

## 四、文档地图（2026-11-16 整理）

| 想知道 | 看这个 |
|---|---|
| **有哪些工具、参数怎么填** | **`工具清单.md`** ← 自动生成，改工具后跑 `_re/gen_tool_list.py` |
| **数据源 / 表怎么关联 / 逐个库功能** | `赛尔数据源全清单.md` ← 首选 |
| **按问题类型查什么 + 规则都在哪** | `赛尔查询路由表.md`（第五节是刻印简称速查） |
| 实现原理（面板公式 / 刻印 / 魂印判定 / 配置图） | `插件实现原理-详解.md` |
| 出事怎么恢复（升级失败 / 数据损坏） | `_re/应急恢复手册.md` |
| 上一轮发生了什么 | `交接-20261116.md` ← 最近一棒 |

**已归档：`_archive\20261116\`**（2026-11-16 整理）

```
28个工具清单.md   手写版停在 28 个（实际 43 个），换成自动生成的 工具清单.md
理由：_archive\20261116\_为什么归档.md
它仍有价值的部分：「每个工具内部打了哪些表」——新清单只有一句话+参数
```

**资料类（不是维护文档，别天天改）**

```
赛学机制论-完整知识库.md / -概念与判定.md / -判定条目.md   机制知识源（内容已进 kb.facts）
战斗时点·完整回合流程.md                                机制知识源
_re\协议破解-20261105.md  _re\无头客户端协议-20261106.md   协议细节
_re\lineup_followup\12只阵容查询结论.md                  41635 阵容查询完整结论
配置图与面板计算-交接说明.md                              配置图公式的详细推导（第十/十一/十二节）
```

**已归档：`_archive\20261108\`**（18 份）

```
旧交接 3 份、一次性过程记录 7 份、自动生成的旧快照 3 份、
已被取代的协议文档 2 份、AGENTS 早先标注废弃的 3 份
理由逐条写在 _archive\20261108\_为什么归档.md
```

**⚠️ `赛尔数据源结构-权威版.md` 注意**：结构关系和坑有效，
但**体量数字作废**（写于残缺库状态：126 表 → 实际 160 表）。文件顶部已加更正块。

**写文档的规矩（2026-11-08 定）**

```
① 不写会变的数字 —— 写「怎么查」的命令，不写「有多少条」
② 同一主题只留一份 —— 新的写好，旧的当场挪进 _archive
③ 生成物标「自动生成，勿手改」
④ 交接只留最近两份
```

---

## 五、常用命令

```bash
# 查精灵全景
python seer-cli/seer.py pet 灵巢

# 查官方词条（这是最容易走错的地方）
python seer-cli/seer.py glossary                    # 先看有哪些类别
python seer-cli/seer.py glossary --kind 语法         # 71 条判定用语
python seer-cli/seer.py glossary 未触发              # 精确查一条

# 查精灵功能标签覆盖
python seer-cli/seer.py tags 灵巢

# 查套装效果（战斗效果，79 套）
python seer-cli/seer.py suit              # 列出全部有战斗效果的套装
python seer-cli/seer.py suit 晨曦          # 查某一套完整原文
python seer-cli/seer.py suit --search 减少   # 按效果关键词搜（用原文词）

# 直接 SQL
python seer-cli/seer.py raw "select count(*) from facts"
python seer-cli/seer.py raw "select name from pet limit 5" --db seerapi
```

### 看日志用 `_re/log.py`，别用 PowerShell 的 Get-Content

**日志是 UTF-8，`Get-Content` / `Select-String` 按 GBK 解 → 中文全变乱码（`浣跨敤宸ュ叿`）。**

```powershell
& $venv _re\log.py                 # 最近 60 条关键行（工具调用 / 错误 / 赛尔相关）
& $venv _re\log.py -n 200
& $venv _re\log.py --tools         # 只看工具调用（带参数和结果，排查问题最有用）
& $venv _re\log.py --errors        # 只看 WARN / ERROR / Traceback
& $venv _re\log.py --seer          # 只看赛尔插件
& $venv _re\log.py --grep 焚宝石    # 关键词
& $venv _re\log.py --all           # 不筛
```

它做了三件事：**UTF-8 读**、**滤掉 private_companion / memory_companion / memewiki 那些噪音**、
**把每行的 `[Core] [INFO] [v4.29.4] [runners.xxx:1234] [trace_id=32位…]` 压成 `[Core] [runners.xxx:1234] [t:前6位]`**。

鱼片自己看日志是正常的（他用 WebUI），这个工具是给**命令行里排查**用的。

### 看 db.py 之前先查索引 —— 别整份读（2026-11-16）

`astrbot_plugin_seer_expert/db.py` **8915 行 / 195 个方法**，整份读一次约 9 万 token。

```powershell
& $venv _re\gen_db_index.py          # 改完 db.py 重跑，生成 _re/db_索引.md（19.6 KB）
```

按功能分组（面板计算 / 精灵查询 / 刻印装备 / 词条规则 / 素材 / 底层访问），
每个方法带 **行号 + 行数 + 一句话说明**：

```
## 面板计算（stat / build / config / damage）　13 个方法 / 1836 行
| 行号 | 方法 | 行数 | 说明 |
| L2480 | `config_view`    | 361 | 组装配置图数据（字段对齐手册 app 的计算器导出图） |
| L2898 | `stat_view`      | 168 | 面板计算：种族值 → 满级面板 |
| L3149 | `_resolve_equip` |  28 | 套装/称号/目镜默认值 —— 算配置的入口全走这里 |
```

**先查索引找方法，再用 `seer_read_code` 精确取段：**

```
seer_read_code(path="db.py", pattern="_resolve_equip")    # ★ 首选：回整个函数
seer_read_code(path="db.py", start=2479, limit=90)        # 次选：按行号读一段
```

**★ `pattern` 才是真省的那个（2026-11-17 bot 指出）**

> 「索引只管找位置，真读进上下文还是那么多字。
>   真省的是按关键字只回匹配的行 —— 翻 db.py 找个函数不用整个两百行吞下去。」

**它说对了一半，也说出了原来实现的毛病**：老版 `pattern` 只回匹配的**那一行**，
看到 `def _resolve_equip(...)` 不知道里面写了什么，还得再 `start/limit` 去吞 90 行。

**现在改成：**

```
pattern 命中 def / class 定义行
    → 自动往上含装饰器、往下找到块结束
    → 只回这一个函数的完整代码
      实测 _resolve_equip：原来 1 行 + 再读 90 行 = 91 行，现在 28 行，省 69%

pattern 命中普通内容
    → 回匹配行 + 前后各 2 行上下文（重叠处不重复印）
    → 光看一行 `if not rows:` 没意义，得有上下文

pattern 不命中
    → 明确说「一处都没命中」+ 建议用更短的关键词或换 start/limit
    → 不是返回空串
```

**所以「翻 db.py 找个函数」的正确姿势是 `pattern=函数名`，
一次拿回整个函数，不用索引也不用算行号。**

**⚠ 别把索引精简成「只有方法名」** —— 实测三个副作用：
① 名字猜不到用途（`_resolve_ev_spec` / `pick_nature_by_ev`）；
② 分组是关键词粗分的会分错，只有说明能看出来；
③ bot 还得多花一次调用 `pattern=` 查行号。
**索引的价值是「帮你判断该不该读」，不是「精确跳转」** —— 跳转 pattern 就行，说明查不到。

**为什么不拆文件**：195 个方法互相调用（`config_view → stat_view → _resolve_equip`、
`mintmark_manual_search → lookup_mintmark → _open_manual`），拆开要处理所有 `self.` 引用和
循环 import，拆完 bot 反而要跨多个文件找 —— **从「一个大文件不知去哪找」变成「几个文件不知去哪个找」。**

真嫌某个方法太长（`mintmark_manual_search` 386 行、`dmg_view` 377 行），
那是**方法内部**拆子函数，不影响外部调用，风险低得多。

---

## 六、改代码前的自检

```
□ 我改的是哪个源？手册 / seerapi / kb —— 确定了吗
□ 这个查询该走 _open_manual / _manual_q / _q / _kb 哪个访问器
□ 我看到的表名有没有可能是「子集镜像」（glossary_entry 就是）
□ 我删东西之前，比对过字段和内容了吗
□ 改完跑一遍 seer.py 的对应命令验证了吗
```

---

## 七、当前状态（2026-11-16 八号鱼更新）

**⚠️ 这一节只写「结构」，不写条数** —— 条数用命令自己查，写死必然过时（这是 11-08 定的规矩，
但上一版还在写「91 个用例」，到 11-16 已经 98 个了）。

```
✅ 手册 15 库完整，且 15 个全部纳入 manual_sync.WANTED
   仓库里其实有 27 个 db，另外 12 个（achievements/archive/cloth/gems/items/
   jb/new_moves/pet_skin/stone/time/yin_zi/精灵榜单）**大部分是 seerapi 的子集**，
   不需要重复同步 —— 只有 items 的 url 字段是真缺的，已单独落成 item_icon_dirs.json
✅ seerapi 完整（160 表），本地 sha256 = 上游最新
✅ kb 13 表完整；facts 条数：seer.py raw "select count(*) from facts"
✅ aliases 有 overlay，更新后自动合并
✅ seer.py 命令全通
✅ @llm_tool 共 44 个（expert 36 + info 8），清单见 工具清单.md（自动生成）
✅ 回归已迁 pytest，用例数：python -m pytest astrbot_plugin_seer_expert/tests -q
✅ kb.sqlite 有自动备份（破坏性写入前快照，留 10 份）
✅ tags 重建的静默清空 bug 已修
✅ 写路径的 except: pass 已改造
⚠️ 改了 db.py / main.py / data.py / manual_sync.py 后要重启 AstrBot 才生效
⚠️ **重启由鱼片自己做**（见坑 15）—— agent 只同步代码 + 报告「请重启」
```

**本地图标资产（`plugin_data/astrbot_plugin_seer_info/icon_assets/`）**

```
element_hd/   139 个   4.1 MB   属性图标高清（手册 typef/，250~320px）← 优先用这个
element/      139 个   0.7 MB   属性图标小图（游戏 PetType/ 40×40）兜底
mintmark/    3833 个  51.1 MB   刻印
gem/          245 个   5.2 MB   宝石（countermark/gem/{gem.id}.png，128×128，见坑 22）
equip/       2118 个  36.1 MB   部件预览
suit/         480 个  30.0 MB   套装
soulmark/    2133 个  13.9 MB   魂印（60×60；手册有矢量版但没接）
title/        585 个   9.7 MB   称号
effect/        43 个   0.2 MB   异常状态
```

外加 `item_icon_dirs.json`（217 KB）：道具 id → 图标子目录，来自手册 items.db 的 url 字段。

### B站专栏提炼的产物（2026-11-05）

**位置**：`_re\bili_readlist\`

```
dom\              137 篇全文（带 ~~删除线~~ 和 {红} 标记，127,794 字）
drafts\           8 个并行提炼批次，734 条
compare\          9 组成对文章的版本对照报告
fix\              冲突条目的修正清单
_术语表.md         以 kb 为准 + 14 个库里没有的俗称
_冲突清单.md       2 条已确认（战螳螂抗性 / 瀚宇星皇）
_提炼规范.md       一条一主题、作废内容直接删、单条 100~300 字
_mined_parsed.json / _manual_parsed.json   结构化数据
fetch_dom.py      抓 B 站专栏（DOM 方式，能拿全 + 保留样式）
split_facts.py    拆分超长 facts（--apply 写入）
import_facts.py   批量入库 + 撞名保护
fix_facts.py      冲突条目修正
```

**抓 B 站专栏的三个坑**（都踩过）：

```
① 纯 requests 拿不到 __INITIAL_STATE__   → B 站查 TLS 指纹，必须走真浏览器
② __INITIAL_STATE__ 里 paragraphs 只有前言 → 长文正文是异步加载的
③ 必须从 DOM 取 .opus-module-content 的 innerHTML
   删除线是 <s style="...">，红字是 <span style="color: #ee230d">
   不能用 <s> 精确匹配（属性会漏）
```

**提炼纪律**：

```
· 带 ~~删除线的~~ 内容一律丢弃，不写进 facts（防止 bot 念出作废结论）
· 同一精灵有新旧两篇时：新版明确列出的改动以新版为准，
  新版没提的部分仍按旧篇 —— 叠加关系，不是替代
· 精灵数据一律以 seerapi 最新为准，facts 只写判定知识
```

### 改完必跑（2026-11-04 起）

```powershell
$venv = 'G:\temp\instances\<instance-id>\venv\Scripts\python.exe'
Set-Location '<workspace>'

# 面板/配装回归（原 verify_panel 的 12 组，已 pytest 化）
& $venv -m pytest astrbot_plugin_seer_expert/tests -q

# CLI 自检
& $venv seer-cli\seer.py selftest

# 三插件定向审计（未定义方法 / hasattr 陷阱 / 工作区与运行版一致性）
& $venv _re\audit_seer.py

# ★ @llm_tool 的 docstring 类型审计（2026-11-10 加）
#   audit_seer.py 查不出 docstring 格式错误，那个错只在启动 AstrBot 时才炸
& $venv _re\audit_tool_docstrings.py

# ★ 工具清单有没有落后（2026-11-16 加）
#   加了 @llm_tool 忘了更新 工具清单.md，这里会 exit 1
& $venv _re\gen_tool_list.py --check

# ★ 插件里有没有写死本机路径（2026-11-16 加）
#   ⚠ 这个脚本还在调：会误报「从 __file__ 上溯」那种正确写法（见坑 24）。
#   看到报警先打开那一行确认写的到底是什么，再决定改不改。
& $venv _re\audit_hardcoded_paths.py

# ★ 同名参数在不同函数里的默认值有没有打架（2026-11-16 加）
#   今天因为这个踩了四次（hpup / team / annual / 三件套），见坑 25。
#   白名单写在脚本的 ALLOW 里，每条都要写理由。
& $venv _re\audit_defaults.py

# ★ 一键同步：工作区 → 实例 + repo（2026-11-16 加）
#   不加 --apply 只报告差异；加了才拷，并给运行版留 .bak-sync- 备份。
#   ★ 改完代码必须跑这个 —— 漏同步 = 代码不生效 = 白测。
& $venv _re\sync.py --apply

# ★ 开发留言板（2026-11-17 加）
#   ★★ 硬约束：**只要你改的是赛尔插件的代码，就必须更新 _re/_dev_note.md。**
#     不是"可选"，是跟 sync.py 同一级的必做项 —— 漏了就等于 bot 不知道你改了什么。
#       · 改了什么（一句话）
#       · 要它验哪几条，期望输出长什么样
#       · 已知问题 / 暂时不处理的
#   写完复制到 实例的 plugin_data/astrbot_plugin_seer_expert/
#   bot 用 seer_dev_note 读它 —— 不用转述改动细节。
#   ⚠ sync.py 不管它（它在 plugin_data 不在 plugins 目录），要单独拷。
Copy-Item _re\_dev_note.md "<实例>\core\data\plugin_data\astrbot_plugin_seer_expert\" -Force
#   ★ 2026-11-17 起：改完留言板，bot 下一轮对话会**自动**提示（dev_note_guard 钩子），
#     不用群里提醒。只在 mtime 变化时提示一次，同版本不重复弹。
```

旧入口 `python _re\verify_panel.py` 仍然可用（转发到 pytest），只是不再是唯一入口。

**测试目录约定**：`astrbot_plugin_seer_expert/tests/`
- `conftest.py` —— 数据路径走环境变量 `SEER_DATA_DIR`，默认指本机实例；缺数据自动 skip
- `test_panel_regression.py` —— 公式/面板/刻印/魂印/性格/倾向，12 组 45 个用例
- `test_kb_safety.py` —— 备份、删除护栏、tags 保护，用临时副本，不碰真数据
- `test_static_audit.py` —— 未定义方法 / hasattr 陷阱 / 工具名重复 / 工作区↔运行版一致（复用 `_re/audit_seer.py`）

**注意**：`test_static_audit` 会检查工作区与运行版是否一致。改了代码还没同步时它**就是会红**，这是设计好的——同步完再跑。

**备份：**

```
manual_db/*.db.bak-YYYYmmdd-HHMMSS    每次手册同步自动生成
manual_db/_snapshot-20261103-1740/    改 WANTED 前的整目录快照
seerapi.sqlite.bak-*                  seer_info 覆盖前自动备份
*.bak-dbgate / *.bak-glossarykind / *.bak-routing / *.bak-wanted   早期改动备份
```

