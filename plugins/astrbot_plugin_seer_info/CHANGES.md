# 指令识别改造说明：严格 → 宽松 → AI 兜底

针对 `MengNianxiaoyao/astrbot_plugin_seer_info`（v1.0.8，main 分支 `e5c3bb7`）的指令匹配层改造。
目标：**不再要求「指令名 + 空格」的严格格式**，规则识别不了时再由 AI 兜底，
同时保证不在闲聊里误触发、不重复回复、不改变原有查询能力。

---

## 一、为什么会「必须严格按格式」

### 1. 严格匹配来自 AstrBot 的 `CommandFilter`

`main.py` 里所有指令都是这样注册的：

```python
@filter.command("精灵", alias={"查询精灵信息", "魂印", "技能"}, desc="查询精灵基础信息", ignore_prefix=True)
async def pet_info(self, event: AstrMessageEvent, arg: str = ""):
```

而 AstrBot 的 `CommandFilter.filter()`（`astrbot/core/star/filter/command.py`）判断是这样的：

```python
message_str = re.sub(r"\s+", " ", event.get_message_str().strip())
for full_cmd in self.get_complete_command_names():
    if message_str.startswith(f"{full_cmd} ") or message_str == full_cmd:
        ok = True
        message_str = message_str[len(full_cmd):].strip()
```

也就是说，**消息必须以「指令名 / 别名 + 一个空格」开头，或与指令名完全相同**，否则整条消息都不算指令。
别名只是多几个可接受的「指令名」，并不改变这个空格要求。

### 2. `ignore_prefix=True` 其实没有让匹配变宽松

`register_command()`（`astrbot/core/star/register/star_handler.py`）把多余的 `**kwargs` 直接塞进
`handler_md.extras_configs`，`CommandFilter` 本身**没有** `ignore_prefix` 参数，匹配逻辑里也没用到它。
它既不改变「要不要唤醒前缀」，也不改变空格要求——写不写没有区别。

### 3. 参数只取第一个词

`arg: str = ""` 是普通 `str` 默认值，`validate_and_convert_params()` 会走
「默认值是字符串 → 直接赋值 `params[i]`」这一支，因此只拿第一个词。
只有声明成 `GreedyStr` 才会把剩下的全部拼进来。所以 `精灵 雷 伊` 只会查询「雷」。

### 4. 名字检索其实已经很宽松，卡点只在指令层

`data.py` 里的 `NameResolver` 用 `LIKE %关键词%`，还叠了别名表（`AliasResolver`）和拼音 FTS5
（`PinyinResolver`，`leiyi`、`ly` 都能查）。所以「搜不到」的问题基本都出在指令没被识别上。

### 5. 具体失效的写法

| 输入 | 原版 | 原因 |
| --- | --- | --- |
| `精灵雷伊` | ❌ | 指令名后没有空格 |
| `雷伊精灵` | ❌ | 指令名不在开头 |
| `精灵：雷伊` | ❌ | 指令名后是冒号不是空格 |
| `查一下精灵 雷伊` | ❌ | 开头有修饰词 |
| `查询雷伊的精灵信息` | ❌ | 指令名在结尾 |
| `赛尔号精灵雷伊` | ❌ | 开头有「赛尔号」 |
| `今天开服了吗` | ❌ | 指令名在结尾 |
| `雷伊的技能都有啥` | ❌ | 名字后面跟的是句子 |
| `精灵 雷伊` | ✅ | 标准格式 |

---

## 二、三层识别

```
消息
 ├─ ① 严格层  CommandFilter（原样保留，12 个指令全在）
 ├─ ② 宽松层  TolerantCommandFilter → 规则解析 core/command_parser.py
 └─ ③ AI 层   event_message_type(ALL) + AiFallbackFilter → core/ai_fallback.py → 大模型
```

三层**互斥**（后面的层只处理前面处理不了的），所以不会出现一次提问回两条消息：

| 层 | 放行条件 | 命中后 |
| --- | --- | --- |
| 严格 | 消息以「指令名 + 空格」开头或完全相等 | 原 handler，行为完全不变 |
| 宽松 | 被唤醒 + 严格层不认 + 规则能解析 | 新 `tolerant_query` |
| AI | 被唤醒 + 前两层都不认 + 像在问赛尔号数据 | 新 `ai_fallback` |

被唤醒 = 群里 `@bot` / 唤醒前缀，或私聊（`is_at_or_wake_command`）。

### 改动清单

| 文件 | 改动 |
| --- | --- |
| `core/command_parser.py` | **新增**，规则解析器（纯标准库） |
| `core/ai_fallback.py` | **新增**，AI 兜底的提示词 / 输出解析 / 触发判断（纯标准库） |
| `main.py` | 新增 `TolerantCommandFilter`、`AiFallbackFilter`、`tolerant_query`、`ai_fallback`、`inject_capability_prompt`、`_ask_llm`、`_classify_intent`；原有 12 个 `@filter.command` 一行未改 |
| `config.py` | `PluginConfig` 增加 5 个可选配置字段 |
| `_conf_schema.json` | 增加对应 5 项配置 |
| `core/__init__.py` | 导出解析器 |
| `core/misc_handler.py` | `/帮助` 补充宽松写法说明 |
| `data.py` | 新增「本地库先行加载」：启动/重载时不再卡在下载窗口里提示「数据库未加载」 |
| `tests/` | 5 个测试（解析 / AI 兜底 / 接线 / 真机集成 / 数据库预加载） |

---

## 三、宽松层

### 解析优先级

1. 指令名/别名/同义词 **+ 分隔符**（空格、`：`、`，`、`/`、`「」` 等）——等价于原行为
2. 指令名与名字 **直接拼接**（`精灵雷伊`、`魂印雷伊`）
3. 名字在前、指令词在后（`雷伊精灵`、`雷伊的精灵`、`雷伊 精灵`、`雷伊的属性`）
4. 先剥掉 `帮我 / 查一下 / 赛尔号 / 图鉴 / 请 / 查询 …` 等修饰词，再重复 1–3
5. 可选：整条消息就是一个名字（默认关闭，需开 `bare_name_search`）

规范化处理：`NFKC` 全角转半角（`精灵：雷伊`、`精灵　雷伊` 都能过）、去零宽字符、压缩空白、去首尾标点。
长词优先匹配，所以 `刻印宝石` 不会被 `刻印` 抢走、`属性克制表` 不会被 `属性` 抢走。

请求动词也算修饰词：`发 / 来 / 整 / 弄 / 给 / 上 / 搞 + 量词（一下、一张、个、张…）`，
所以 `给我发一下南霜的立绘`、`来一张南霜的皮肤`、`弄个南霜立绘` 都能取到 `南霜`。
量词是必需的，这样 `发条鸟`、`来古`、`上古炎兽` 这类名字不会被吃掉开头。

句子形式也能被规则层吃掉，不必依赖 AI：

* 句尾疑问成分会被剥掉：`是什么 / 是啥 / 有什么 / 有哪些 / 有什么用 / 效果 / 怎么样 / 咋样`，
  于是 `雷伊的魂印是什么`、`雷伊魂印效果`、`雷伊的魂印有哪些` 都能得到 `pet_info / 雷伊`；
* 名字前面残留的说明词会被剥掉：`效果 / 技能 / 魂印 / 属性 / 信息 / 资料`，
  于是 `魂印效果雷伊`、`魂印技能雷伊` 得到 `雷伊` 而不是 `效果雷伊`；
* 但剥掉疑问尾巴后只剩裸指令词（`精灵怎么样`、`魂印是什么`）时按闲聊处理，不误触发。

### 支持的写法

| 输入 | 结果 |
| --- | --- |
| `精灵雷伊` / `精灵：雷伊` / `精灵「雷伊」` / `【精灵】雷伊` | 精灵 `雷伊` |
| `雷伊精灵` / `雷伊的精灵` / `雷伊 精灵` | 精灵 `雷伊` |
| `查精灵雷伊` / `查一下精灵 雷伊` / `帮我查一下精灵雷伊` | 精灵 `雷伊` |
| `赛尔号精灵雷伊` / `赛尔号 精灵 雷伊` | 精灵 `雷伊` |
| `查询雷伊的精灵信息` / `雷伊精灵信息` / `魂印雷伊` / `精灵雷伊技能` | 精灵 `雷伊` |
| `雷伊的属性` / `火 属性` / `属性克制表火` | 属性克制表 |
| `刻印宝石火` / `今天开服了吗` / `下期预告` / `菜单` | 对应指令 |
| `精灵好强` / `这只精灵` / `精灵怎么获得` / `火克什么` | **不触发** |

### 裸名自动识别（auto_dispatch）

套装、部件的名字往往不带任何指令词，比如 `影翼裁决`、`漫游者-AH`——规则层认不出，AI 层也因为
「没有领域关键词」不触发，结果就是**发了个名字什么也没发生**。

现在加了一层：整条消息看着像个名字时，按下面的顺序拿它去各表里查，

```python
_AUTO_GETTERS = (
    ("suit", SuitDataGetter), ("equip", EquipDataGetter),
    ("pet_image", PetSkinDataGetter), ("pet_info", PetDataGetter),
    ("mintmark", MintmarkDataGetter), ("gem", GemDataGetter),
    ("title", TitleDataGetter), ("battle_effect", BattleEffectDataGetter),
)
```

**精确同名优先**，否则取第一个有结果的；都查不到就静默返回，消息照常走普通聊天。
判定依据是数据库本身，不是猜的：

| 输入 | 命中表 | 结果 |
| --- | --- | --- |
| `影翼裁决` | `suit` | 套装 |
| `漫游者-AH` | `equip` | 部件 |
| `查一下漫游者` | `equip` | 部件（先用清洗后的「漫游者」） |
| `这个真不错` | 无 | 不触发 |

`查一下漫游者` 这种情况要特别处理：整句拿去 LIKE 是查不到的。所以 `bare_name_candidates()`
会给出两个候选——**原样**和**去掉修饰词后**，依次尝试：

* 先试原样，保证 `查尔顿`（真名字以「查」开头）不会被剥成 `尔顿`；
* 再试清洗后的，`查一下漫游者` → `漫游者`。

配套地，`patch_capability_denial()` 的触发门槛也放宽了：原来要求消息里有赛尔号关键词，
现在「像个名字」也算——否则 `查一下漫游者` 被模型回「查不了」时，兜底那句不会补。

默认开启（`auto_dispatch`），自检行里有「裸名命中 N 次」。

### 外号查询（`外号` / `别名`）

别名库本身有数据（当前 360 条、覆盖 240 只精灵），但之前只能「用外号查精灵」，
看不了「这只精灵有哪些外号」。新增：

```
外号 雷伊        # 这只精灵有哪些外号
外号 811         # 用 ID 也行
别名 草马        # 别名等价
雷伊的外号       # 宽松层认这种写法
外号             # 不带参数：别名库概况（条数 / 覆盖精灵数）
```

- 自定义外号（`pet_aliases` 配置）也写进同一张表，所以会一起列出来
- 没有外号、精灵不存在、重名过多都有对应提示
- `core/alias_handler.py` 纯查询逻辑，真机测试 [20] 覆盖五条路径

### 自定义外号（pet_aliases）

插件本来就有别名库（`aliases.sqlite` 的 `pet_aliases` 表，从 `Nattsu39/ironsbot` 同步），
但那是远程数据、每次同步会整个覆盖。所以自定义外号**不写文件**，而是：

1. 启动时解析配置项 `pet_aliases`（每行 `外号=精灵名或ID`）；
2. 写入**内存别名库**的 `pet_aliases` 表（外号 → 解析出的精灵 ID）；
3. 挂一个 `post_load_hook`，别名库每次重新加载（远程同步）后自动补回。

因为走的是插件原有的 `AliasResolver`，所以**所有查询入口**都生效：`精灵`/`立绘`/`配置图`/`魂印`…
不用给每个 handler 单独加逻辑。

格式：

```text
二哈=雷伊
奶妈=谱尼
南霜=4511
# 注释行会被忽略
```

- 分隔符 `=` `:` `：` 都行；多条可以用换行、`;`、`；`、`,`、`，` 隔开
- 目标可以是**精灵名**（会走模糊匹配，优先精确同名）或**精灵 ID**
- 外号与已有别名冲突时，自定义的覆盖远程的
- 目标找不到会在日志里 warning 跳过，不影响其他条目

真机测试 [19] 覆盖「→ 写入 2 条 → `二哈` 查到雷伊 → ID 型外号生效 →
重新加载别名库后外号仍然生效」。

### 精灵配置图（pet_config）

借鉴 `Murmansk5000/IronsBot` 的 `pet_config`：精灵资料卡之外，再给一张**本地维护的配装图**。

- 指令：`配置图 雷伊` / `配置 雷伊` / `雷伊配置图` / `配置图 雷伊 2`（指定第几套）
- 存放：**一只精灵一个目录、多套配置各占一个序号**
  `<插件数据目录>/pet_configs/<精灵序号>/<序号>.<后缀>` + 同序号 `.json`，
  支持 `png / webp / jpg / jpeg`
- 旧布局 `<id>.png` + `<id>.json` 仍可读，第一次通过指令收录时自动迁移成 `<id>/1.<后缀>`
- 收录：发「收录配置图 雷伊 [作者:xxx] [备注:...]」+ **同一条消息带图**，超过 8 MB 拒绝
  - **按作者分套**：新作者 = 新增一套，同作者 = 替换他自己那套
  - 查询多套时**先问「想看哪套」**（序号 + 作者 + 备注），回序号才发那一张；
    60 秒无应答自动超时，输入 `0` 退出；`配置图 艾夏拉 2` 可直接跳过提问
  - 多套时改备注必须带 `作者:` 指定是哪一套，否则提示从现有几套里选
- 谁能收录（`can_upload`，任一条成立即可）：
  1. AstrBot 管理员（`admins_id`）
  2. 平台上报的群主/群管理员（`aiocqhttp` 这类适配器会设置 `event.role`）
  3. 插件配置 `pet_config_uploaders` 里的 ID 白名单（逗号分隔，`/sid` 查看自己的 ID）

  没用 `@filter.permission_type(PermissionType.ADMIN)` 的原因：`PermissionTypeFilter` 里
  `GROUP_ADMIN` 分支仍然只调用 `event.is_admin()`，而 `is_admin()` 只看 `role == "admin"`，
  这个 role 只在唤醒阶段按 `admins_id` 设置；QQ 官方适配器也不上报群成员角色。
  换句话说「群管理员」在内置权限里是拿不到的，只能自己判定。

为什么放插件数据目录而不是 AstrBot 的 `data/temp`：后者是缓存区，
`temp_dir_max_size` 一到就会按最老优先删文件，配置图绝不能放那儿。

### 配置图的作者与备注

图片和文字分开存，改备注不用重传图：

```
pet_configs/70.png      # 图
pet_configs/70.json     # {"author": "张三", "note": "物攻流", "uploader": "...", "updated_at": "..."}
```

* 收录时直接带：`收录配置图 雷伊 作者:张三 备注:物攻流，配泰坦刻印`
  （`作者`/`来源`、`备注`/`说明` 都认，`:`/`：`/`=` 任意；不写 key 的剩余文字自动当备注）
* 只改文字：`配置备注 雷伊 备注:改成双防流`，图片不动
* 换图：对同一只精灵再收录一次即可覆盖，回复会区分「已收录 / 已替换」；
  **没重新填的作者/备注保留旧值**，不会因为只换图把备注弄丢（旧图不保留历史版本）
* 出图时的文案：`🧩【雷伊配置】` + 作者 + 备注 + 收录时间，空字段自动省略
* 没写作者时自动用**发消息的人昵称**（拿不到就用 UID 后 6 位）兜底；
  昵称是纯空白字符（QQ 上常见的 `U+3164` 这类）时也按「没昵称」处理
* 手动丢图不写 `70.json` 也能正常查，只是没有作者/备注

**踩过的坑**：`@filter.command` 的 `arg: str = ""` 不是 `GreedyStr`，
`validate_and_convert_params()` 只把**第一个空格前的词**传进来——
`收录配置图 艾夏拉 [作者:佑栖]` 到 handler 里 `arg` 只有「艾夏拉」，
作者备注全被截掉，而且没有任何报错，表现得像「没解析出来」。
现在 `PetConfigHandler._full_arg()` 直接读 `event.get_message_str()`、
去掉指令词后自己解析，不依赖 `arg` 的切词结果。

**另一个坑**：管理指令也要挡在宽松层和 AI 层之外。`收录配置图 南霜 [作者:佑栖]`
被严格指令处理完之后，AI 兜底又看到里面的「配置图」关键词，把整句当自然语言再查一遍，
于是回完「✅已收录」紧跟着来一句「❌未找到匹配的精灵: 南霜 [作者:佑栖] …」。
现在 `command_parser.is_known_command()` 会把 `ADMIN_COMMANDS`（`收录配置图` / `配置备注`）
也算作已注册指令，两层都会直接让路。

真机测试 [14] 覆盖了「写入一张 1×1 PNG → `配置图 70` 返回 Image 组件 → 删掉后提示暂未收录」；
[15] 覆盖四种身份的收录权限判定（管理员 / 群主 / 群管理 / 白名单）与两种拒绝（路人 / 拿不到 ID）；
[16] 覆盖「两套配置按作者分流、同作者替换、菜单选择、连续查看、会话不被生成器关闭影响」；
[17] 覆盖「`收录配置图 艾夏拉 [作者:佑栖] [备注:…] [图片]` 能提取完整参数并正确解析」。

---

## 四、AI 兜底层

### 4.1 为什么不能只挂 `@filter.event_message_type(filter.EventMessageType.ALL)`

这一点很关键。看 `astrbot/core/pipeline/waking_check/stage.py`：

```python
for handler in star_handlers_registry.get_handlers_by_event_type(...):
    passed = True
    for filter in handler.event_filters:
        if not filter.filter(event, self.ctx.astrbot_config):
            passed = False
            break
    if passed:
        is_wake = True
        event.is_wake = True
        activated_handlers.append(handler)
```

而 `EventMessageTypeFilter(ALL)` 对群聊/私聊/其他消息**一律返回 True**。
所以「只挂一个 ALL 监听器」的结果是：**群里任何一条消息都会把机器人唤醒并交给这个 handler**，
事件也就不会再走正常聊天流程——机器人会变成对每句话都插嘴。

正确做法是 `ALL` 只负责「广撒网」，再用一个自定义过滤器把闸门关上：

```python
@filter.event_message_type(filter.EventMessageType.ALL)   # 收所有类型的消息
@filter.custom_filter(AiFallbackFilter)                    # 但必须同时满足五个条件
async def ai_fallback(self, event: AstrMessageEvent):
```

`AiFallbackFilter` 放行条件（`core/ai_fallback.py: should_ask_ai`）：

1. `is_at_or_wake_command` 为真——群里必须 `@bot` 或用唤醒前缀，私聊才直接生效；
2. `matches_strict_command()` 为假——标准指令能处理的让给原 handler；
3. `parse_query()` 为 None——宽松层能解析的让给宽松层；
4. 消息长度 ≤ `ai_fallback_max_length`（默认 120，防止长文浪费 token）；
5. 模式匹配：
   * `keyword`（默认）——消息里出现 `精灵 / 刻印 / 套装 / 属性 / 开服 / 赛尔号 …` 等词才问 AI；
   * `always`——只要被唤醒就问 AI（覆盖最全，也最贵）。

不满足就返回 False，事件照常交给后续阶段，**普通聊天不受任何影响**。

### 4.2 模型怎么用

用 AstrBot 的 `Context.llm_generate()`（≥4.5.7）发起一次**无历史**的独立请求：

```python
await self.context.llm_generate(
    chat_provider_id=provider_id,   # 留空则取当前会话的模型
    prompt=user_prompt,
    system_prompt=system_prompt,
    contexts=[],                    # 不带上下文，纯分类
)
```

* `provider_id` 取 `ai_fallback_provider_id`，为空则 `get_current_chat_provider_id(umo)`，
  即沿用该会话当前的对话模型；
* 旧版本 AstrBot 没有 `llm_generate` 时自动回退到 `context.get_using_provider(umo).text_chat(...)`，
  两条路径都是 `getattr` 探测，不会因为版本差异 ImportError；
* 全部失败（无模型 / 超时 / 报错）都只记一条 `warning`，**用户侧静默**，不会把异常抛到聊天里。

### 4.3 提示词与输出协议

`build_messages()` 会**从 `INTENTS` 自动生成**可选 intent 列表（含每个指令的中文说明和玩家常用说法），
所以以后加指令只要改一处。要求模型只输出一行 JSON：

```json
{"intent": "pet_info", "keyword": "雷伊"}
```

参数解析（`parse_intent_json`）做了充分容错，这些都能认：

| 模型输出 | 结果 |
| --- | --- |
| `{"intent":"pet_info","keyword":"雷伊"}` | 精灵 `雷伊` |
| 用 ` ```json ` 代码块包裹 | 剥掉代码块后再解析 |
| `好的，解析结果如下：{…}` | 正则抠出第一个 JSON 对象 |
| `{"intent":"精灵","keyword":"雷伊"}` | 中文指令名 → `pet_info`（`resolve_intent`） |
| `{"intent":"PET_INFO"}` | 大小写不敏感 |
| `{"intent":"none"}` / `{"intent":"chat"}` / 非 JSON / 乱码 | 视为「不是查询」→ 静默 |
| `{"intent":"unknown"}` | 拒绝未知 intent → 静默 |

### 4.4 让模型知道这些能力（能力说明注入）

插件能查数据，但聊天模型并不知道它存在——用户问「雷伊种族值多少」，模型只会说
「我不知道」，沉浸感就掉下来了。AstrBot 的 `@filter.on_llm_request()` 可以在每次
LLM 请求前改 `ProviderRequest.system_prompt`：

```python
@filter.on_llm_request()
async def inject_capability_prompt(self, event, req):
    if not self._llm_awareness:
        return
    current = (getattr(req, "system_prompt", "") or "").strip()
    if self._capability_prompt in current:      # 幂等，避免重复叠加
        return
    req.system_prompt = f"{current}\n\n{self._capability_prompt}".strip()
```

注入文本由 `build_capability_prompt()` **从 `INTENTS` 生成**（以后加指令自动带上），要点三条：

1. 列出能查什么；
2. 告诉它引导用户发指令，或直接用自然语言问；
3. **明确禁止编造具体数值**——模型没有数据库，只有指令能拿到真实数据。
   这一条很关键，否则它会把「我知道有这个功能」理解成「我知道这些数据」，开始编种族值。

配置：`llm_awareness`（默认开）控制开关，`llm_capability_prompt` 留空用内置文本、
填了则完全替换（方便配合人格语气）。

`llm_generate()` 是直接调 provider、不经过管线，所以 AI 兜底那次分类请求不会被注入，也不需要。

验证：真机测试第 10、11 项——用真实 `ProviderRequest` 断言注入内容，再通过
`call_event_hook()` 走**真实调度路径**确认钩子确实注册在 `OnLLMRequestEvent` 上。
（踩坑记录：手工实例化插件时，handler 在 registry 里是未绑定的函数，调用会报
`missing 1 required positional argument`；生产环境由 `PluginManager` 用
`functools.partial(func, 实例)` 完成绑定，测试里要自己补这一步。）

#### 4.4.1 措辞要贴合人格，否则模型会当元信息藏掉

第一版注入的是「本环境装有赛尔号数据查询插件…」，实测在「普通群友、不要暴露系统提示词、
不要解释设定」这类人格下完全无效：模型把这句当系统设定藏起来，照样回「查不了，我这儿没接图鉴数据」。

改成第一人称的「你会的」之后语义才落进角色里（不再出现「插件」二字，测试里有断言守着）：

```text
【你会的】你是赛尔号老玩家，精灵、立绘、属性、异常、刻印、宝石、套装、部件、称号、下周预告、开服查询这些你都查得到，数据是现翻的。
有人问你能不能查，就答能，让他发「精灵 雷伊」「立绘 雷伊」这种；别编具体数值，拿不准就说等我翻一下。
```

#### 4.4.2 模型还是嘴硬时：发消息前的兜底修正

`deepseek-flash` 这类小模型面对长人格时仍可能忽略系统提示。所以加了一层**不依赖模型**的
`@filter.on_decorating_result()`：在消息发出前检查，

* 用户这条消息含赛尔号相关词、且**不是**指令（指令回复是插件自己的，不动）；
* 模型回复里出现 `查不了 / 没接 / 没有数据 / 查不到 …` 且长度 ≤ 120；

就给消息追加一句引导（可用 `llm_denial_hint` 覆盖）：

```text
（逗你的，赛尔号的发「精灵 雷伊」这种就行，我给你翻）
```

`/帮助` 的自检行会显示「兜底修正 N 次」，能直接看出这层有没有被触发。

#### 4.4.3 分清是不是赛尔号

「精灵 / 立绘 / 属性克制」这些词在别的游戏里也通用，人格写进能力说明后更容易张冠李戴。
两边都加了边界：

* 提示词里加一句「**只认赛尔号**：宝可梦、洛克王国、原神这些别套，也别拿赛尔号的数据去回答别的游戏」；
* 代码层 `NON_SEER_MARKERS`（宝可梦、皮卡丘、洛克王国、原神、鸣潮、明日方舟…）
  命中就两层都跳过——`should_ask_ai()` 不送 AI，`TolerantCommandFilter` 也不解析，
  消息照常走普通聊天。说了「赛尔号」或 `seer` 则一律按赛尔号处理（联名活动之类）。

### 4.5 判定为「不是查询」时会发生什么

`ai_fallback` 直接 `return`，不 `yield` 任何结果、不 `stop_event()`。
于是事件继续走 AstrBot 的正常流程，人格/记忆/工具调用都不受影响——AI 兜底只负责「翻译」，
不负责「回答」。被翻译成指令时才接管，并复用原 handler（渲染图片、翻页选择都一样）。

---

## 四·五、渲染缓存与并发闸门（借鉴上游 IronsBot）

上游用 `FileRenderCache` + `RenderCoordinator` 把「渲染什么」和「渲染几次」拆开；
这里把同样的思路压进 `core/renderer.py`，不引入新依赖：

```text
key = sha256(最终 HTML + 图片格式 + 质量 + 视口宽 + 模板/渲染器源码指纹)
```

* 命中 → 直接返回上次的字节，SQLite、HTTP 贴图、Chromium 全部跳过
* 未命中 → 走 `Semaphore(1)` 串行截图，外面套 `asyncio.wait_for` 超时
  （上游同样是 `Semaphore(1)`；我们多了超时，卡死的页面不会一直占着页面池）
* 模板或 `renderer.py` 一改，指纹变、key 变，旧图自动失效，不会出现「改了模板不生效」
* 缓存放在 `plugin_data/<插件>/render_cache/`，按 mtime 做 LRU，超容量自动淘汰
* 配置项：`render_cache`（默认开）、`render_cache_max_size_mb`（默认 300，填 0 即关闭）
* `/帮助` 的自检行会显示「命中 / 未命中 / 文件数 / 占用」

真机测试 [21]：同一只精灵连续渲染两次，第二次命中缓存（实测 0.7s → 0.04s）。

---

## 四·六、专属效果与技能开启消耗（用新数据源的新表）

数据源换成 `Murmansk-Seer/seerapi` 之后，多了两张关键表：

| 表 | 内容 | 用法 |
| --- | --- | --- |
| `pet_special_effect` + `_source` | 专属效果（993 条）与「关联」来源（2773 条） | 精灵图左侧新增「专属效果」区块，效果名后面跟「关联：技能名」 |
| `item_exchange_price` | 道具兑换价/限购（297 条） | 技能卡上补一行「开启消耗：共鸣锚点 ×1000（限购 6 次）」 |

实现上**没有升级 `seerapi-models`**：用 `sqlalchemy.text()` 直接查这两张表，
查不到就静默跳过（`logger.debug`），所以旧数据源、旧模型下模板依旧能渲染，
只是没有这两块内容——升级数据源属于「可选增强」而不是「硬依赖」。

真机验证：`狂沙之主·萨特` 出图能看到「专属效果：黯天之尘（关联：地动六合覆）」，
`枯骨遗沙镇` 技能下方出现「开启消耗：共鸣锚点 ×1000（限购 6 次）」。

### 裸名只认精确同名 + 选择菜单按人隔离

**问题一**：群里有人 @ 机器人喊了声「肥鱼」，机器人弹出了精灵查询菜单。
查名会走拼音索引，「肥鱼」(feiyu) 命中「费约克 / 绯云 / 绯玉 / 陌羽飞月 / 焰·绯云」一串，
而裸名自动识别当时是「**有结果就算命中**」。

现在 `_lookup_name` 只认同名：
- 完全相同（去空格/标点后）→ 命中；
- 「漫游者」→「漫游者-AH」这种**短英文/数字后缀**前缀 → 也算命中（部件常见）；
- 拼音模糊、跨字匹配一律不认，想按拼音查请用 `精灵 肥鱼` 这种显式指令。

**问题二**：菜单弹出后，**全群**任何人说话都会被当成在选序号，回「序号无效」直到有人发 0。
根因是 `multi_select_query` 的 `session_waiter` 用了默认过滤器（只按会话/群区分）。
现在换成 `_SenderOnlyFilter`（会话 + 发送者）——只有发起查询的人能继续选，别人正常聊天。

真机测试 [25] 覆盖：「肥鱼」/「今天吃什么」不触发、真正名字仍识别、不同用户的等待器 key 不同。

### 不回应「@ 别人」的消息

AstrBot 的唤醒判定有三条：@ 机器人、@全体成员、**引用/回复机器人的消息**，
另外还有唤醒前缀 `/`。所以「引用机器人的话 + 同时 @ 群友」会被判成在跟机器人说话。

本插件在宽松层 / AI 兜底层 / 能力注入三处都加了守卫：
**消息里 @ 了别人（且没 @ 机器人）就不接管、也不注入能力说明**，
让这条消息回到普通聊天流程，不去抢别人的对话。

**根因（QQ 官方适配器）**：`qqofficial_platform_adapter.py` 里群消息固定用
`force_group_mention=True` 转换，于是

```python
abm.self_id = bot_mention_ids[0] if bot_mention_ids else "qq_official"
if group_mentioned:                     # 恒为 True
    msg.append(At(qq=abm.self_id))
```

没被 @ 时 `self_id` 被填成占位值 `"qq_official"`，再塞一个同 id 的 `At`，
waking_check 一比对 `At.qq == self_id` 就成立——**群里每条消息都被判成 @ 了机器人**。
所以不能只靠 id 比较，要看平台原始消息里的 `mention.is_you`；
没有该信息时，`self_id` 是占位值就按「没 @ 我」处理（私聊除外）。

只拦「真的 @ 了别人」这一种：QQ 官方消息 @ 别人时会留下 `<@OPENID>` 文本标记，
普通闲聊没有；占位 At（qq=`qq_official`）不算 @ 别人。
这样普通群聊照常有活人感，只有「@群友」的对话不插嘴。

另外在 `on_decorating_result` 里也加了兜底：判断为「@ 别人」时把回复清空，
这样即使核心 LLM 被误唤醒，消息也不会发到群里。

### 奶龙图鉴（`奶龙精灵查询` / `上传奶龙图`）

恶搞图：一个「奶龙名」对应一只精灵，比如「奶政」对应「始皇帝·嬴政」。

```
奶龙精灵查询            列出：1 奶政（始皇帝·嬴政） 2 奶霜（不破帝·南霜）…
奶龙精灵查询 奶政       直接出图
上传奶龙图 奶政 嬴政    图片 + 奶龙名 + 精灵名/ID，同一条消息
```

- 存储：`plugin_data/<插件>/milk_dragon/<精灵ID>_<奶龙名>.png` + `index.json`
  （顺序按加入先后，菜单就按这个顺序编号）
- 同名再传会覆盖旧图但**保留原位置**；文件名里的 `/ : * ?` 等字符会被清掉
- 出图流程跟配置图一样：先发菜单、再挂会话等待器（先注册再 yield，
  避免生成器被关闭后收不到序号），60 秒超时，`0` 退出，可以连着看多张
- 权限复用配置图那套：AstrBot 管理员 / 平台上报的群管理 / 白名单
- 图片上限 8 MB，支持 png/webp/jpg/jpeg

真机测试 [24]：上传两张 → 菜单按顺序列出 → 按名字直接出图。

### 白名单管理（`白名单` 命令 + 昵称支持）

手打 UID 太费劲，所以做了三件事：

1. **配置支持「昵称=UID」**：`pet_config_uploaders` 里既能写裸 UID，也能写
   `佑栖=F81DAEAC…`，匹配只看 UID，昵称只是给人看。分隔符支持换行/中英文逗号/分号。
2. **被动记录候选**：插件在宽松指令过滤器里顺手记下「谁说了话、说了多少条」
   （过滤器返回 False 不影响唤醒），落在 `plugin_data/<插件>/uploaders.json`，
   最多保留 500 人，每 20 条消息落一次盘。
3. **`白名单` 命令**：
   ```
   白名单              查看白名单 + 常聊的人（✅已加 / ☐未加，带昵称和 UID）
   白名单 添加 1 3 5   按序号加
   白名单 添加 佑栖     按昵称加
   白名单 添加 <UID>   直接加
   白名单 移除 佑栖     按昵称或 UID 移除
   ```
   权限：AstrBot 管理员，或已经在白名单里的人。

历史数据也能一把灌进去：从 AstrBot 的 `platform_message_history` 表按发送者聚合，
发言 ≥5 条的自动进白名单（实测导入了 16 人 / 31 个候选）。

### 魂印分段 + 技能 ID + 限时折扣

拿上游渲染截图对比时发现的三个差距，一并补上：

1. **魂印富文本**：`soulmark.desc_formatting_adjustment` 里带 `<indent=N><sprite=K>` 排版标记，
   之前只用 `desc`/`analyze_desc` 渲染成一整段。现在 `core/soulmark_format.py` 把它解析成
   「小节 + 分层级行」：`indent//16` 算层级，行尾带 `：` 且层级 ≤1 的当作小节标题，
   `<color=#xxx>`/`<b>` 转成 HTML 并在行内闭合。渲染出「登场效果 / 战阶结束效果 / 不破诛罚」
   三段卡片，带 `▸`/`·` 缩进；没有富文本标记的魂印自动回退到原来的纯文本。
2. **技能 ID**：技能卡右上角显示 `ID: 36628`（对齐上游），方便对着游戏核对。
3. **限时折扣**：`item_exchange_price.end_time` 落在当前时间窗内时，价格后面补「限时折扣」。

`南霜（4511）` 实拍对比：魂印从「一整段文字」变成三段分节，`星纪` 卡片出现
「激活道具: 银弧-补充装」和「开启消耗：共振晶体 ×350（限购 1 次）」，
每个技能右上角都有 ID。

---

### 图标：魂印 / 激活道具 / 兑换货币

上游的图里，魂印左上角、技能「激活道具」标签、开启消耗价格前面都带小图标。补齐：

| 图标 | 资源路径 | 位置 |
| --- | --- | --- |
| 魂印 | `effecticon/{icon_id}.png`（icon_id 来自 `soulmark_icon` 映射） | 魂印卡片左上角，46px 圆角，标签在右侧 |
| 激活道具 | `item/{子目录}/icon/{道具ID}.png` | 「激活道具: 银弧-补充装」标签内 |
| 兑换货币 | 同上 | 「开启消耗：共振晶体 ×350」前面 |
| 专属效果 | `battleeffect/signbuff/{icon_key 或 status_id}.png` | 效果卡片左侧，38px 圆角；两者都没有就不显示 |

魂印**也不能**直接拿 `soulmark.id` 当图标 id：萨特的魂印 1578 对应图标 1554，
`effecticon/1578.png` 那张是别的技能图标（会显示成「啼」而不是「尘」）。
正确做法是查 `soulmark_icon` 表拿 `icon_id`（同表还有 `pet_id`，优先取本精灵那条）。

专属效果**不能用** `glossary_id` 当图标 id——那样会把「术语表图标」错贴到效果上
（例如黯天之尘被贴上「刻」的字形图标）。正确规则与上游一致：优先 `icon_key`，
其次 `status_id`，都没有就不显示图标（黯天之尘就是这种）。刻印图标同样「没有就不渲染」，
避免出现白色占位图。

道具图标分散在 `doodle / petitem / skillstone / throw / userinfo / glasses` 六个子目录，
`ItemImageGetter` 按这个顺序依次尝试（`GetImage` 自带多 URL 兜底），全部失败就用占位图。

魂印块改成 flex 布局（图标 + 右侧内容），避免浮动把标签挤到奇怪的位置；
图标下载失败时 `sm.icon` 为空，模板直接不渲染 `<img>`，不会出现破图。

---

## 五、误触发防护

无分隔符拼接天然有歧义（`精灵雷伊` 与 `精灵王` 无法从字面区分），所以做了四层收敛：

1. **必须被唤醒**：群聊仍需 `@bot` 或唤醒前缀，私聊才直接生效——和改造前一致；
2. **严格层优先**：`matches_strict_command()` 命中的消息完全不进后两层；
3. **宽松层要求「像名字」**：长度 ≤ 16、不含空格换行、不以
   `这只/那个/什么/怎么/我的/今天/好/真/太/很/是/有/都/怎/咋` 等词开头，
   不以 `吗/呢/啊/吧/呀/了/啥` 结尾，且不含 `怎么/什么/哪些/多少/是不是` 等疑问词；
4. **AI 层默认只认关键词**：`ai_fallback=keyword` 时，不含赛尔号相关词的消息根本不会发给模型。

实在有误判，把 `fuzzy_command` 或 `ai_fallback` 关掉即可立刻回到更保守的行为，代码不用回滚。

---

## 六、配置项

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `fuzzy_command` | bool | `true` | 宽松指令识别总开关 |
| `bare_name_search` | bool | `false` | 允许「整条消息就是名字」直接查询 |
| `auto_dispatch` | bool | `true` | 裸名自动识别：直接发套装/部件/精灵名，自动查表判断类型 |
| `ai_fallback` | string | `keyword` | `off` / `keyword` / `always` |
| `ai_fallback_provider_id` | string | `""` | 专用模型 ID，留空沿用会话模型；建议填便宜的小模型 |
| `ai_fallback_max_length` | int | `120` | 超过该长度不送 AI |
| `llm_awareness` | bool | `true` | 聊天时把能力说明注入系统提示词 |
| `llm_capability_prompt` | string | `""` | 自定义能力说明，留空用内置文本 |

旧配置文件缺少这些字段会自动用默认值（`config.py` 声明为可选，`main.py` 用 `_cfg_flag()` 兜底），无需迁移。

成本参考：`keyword` 模式下只有「被 @ / 私聊」且「带领域词」且「规则解析不了」的消息才会多一次小请求；
正常查询、闲聊都不会产生额外调用。

---

## 七、怎么应用

覆盖到 `data/plugins/astrbot_plugin_seer_info/` 对应位置即可：

```text
main.py                    ← 覆盖
config.py                  ← 覆盖
data.py                    ← 覆盖（只多了一段本地库预加载）
_conf_schema.json          ← 覆盖
core/__init__.py           ← 覆盖
core/misc_handler.py       ← 覆盖
core/command_parser.py     ← 新增
core/ai_fallback.py        ← 新增
tests/                     ← 可选，只用于本地验证
```

然后重载插件（或重启 AstrBot）。模板、数据库、渲染逻辑都没动，不需要清理 `plugin_data`。

两个小提醒：

* `config.py` 里对 `get_astrbot_plugin_path` 的导入做了 `try/except` 双路径兼容
  （4.29.x 是 `astrbot_path`，上游 main 分支是 `astrbot_plugin_path`），直接覆盖即可；
* 如果插件目录里还留着 `seer_info_natural_language.patch` 这类早先的草稿补丁，
  建议一并删掉——它没有应用过，留着容易和现在的实现混淆。

建议同时把 `metadata.yaml` 的 `version` 推到 `v1.0.9`、`CHANGELOG.md` 补一条（纯文案，未包含在本目录里）。

---

## 八、验证情况

### 8.1 离线测试（不需要 AstrBot / 数据库 / 网络）

```bash
$ python tests/test_command_parser.py
OK: 94 条解析用例 + 16 条严格匹配用例全部通过
指令一致性检查通过，共 27 个严格指令名

$ python tests/test_ai_fallback.py
OK: 12 条 JSON 解析用例 + 10 条空结果用例 + 16 条触发判断用例全部通过

$ python tests/test_wiring.py
OK: 接线测试通过（严格/宽松/AI 三层互斥、分发与容错均正常）
```

* `test_command_parser.py`：解析用例（含负例）、严格匹配语义、以及**AST 一致性检查**——
  直接解析 `main.py` 里所有 `@filter.command(...)` 的指令名和别名与解析器指令表比对；
* `test_ai_fallback.py`：模型输出的各种畸形格式、`none` 判定、触发条件、提示词完整性；
* `test_wiring.py`：用桩模块把 `main.py` **真正导入并实例化插件**，验证
  「严格指令不被后两层抢走」「宽松层能解析的不进 AI 层」「AI 返回 none 时零输出」
  「模型报错时静默」「无参指令不带参数」「AI 只调用一次模型」。

### 8.2 真机集成测试（AstrBot 4.29.1 + 真实数据库 + 真实渲染）

已在一个**隔离副本**上跑过真 AstrBot：把线上实例的插件目录与
`seerapi.sqlite` / `aliases.sqlite` 复制到独立的 `ASTRBOT_ROOT`，
用实例自带的 venv 解释器执行 `tests/test_real_astrbot.py`，全程不触碰线上进程与线上数据。

```bash
$ $env:ASTRBOT_ROOT = '<隔离目录>'
$ '<实例>\venv\Scripts\python.exe' tests\test_real_astrbot.py
astrbot : ...\core\astrbot
[1] 真实 CommandFilter 语义对照
  ok   matches_strict_command 与真实 CommandFilter 一致（13 条）
...
  ok   渲染文件存在: ...\image_cache\ecd125960017e5fe.jpeg
  ok   图片大小 1529 KB
  ok   内容是 JPEG
...
OK: 真实 AstrBot 环境下全部通过        # exit 0
```

20 项检查全部通过，其中最有价值的是：

| 检查 | 说明 |
| --- | --- |
| 真实 `CommandFilter` 语义对照 | 我复刻的 `matches_strict_command` 与 AstrBot 4.29.1 的实现逐条一致 |
| `EventMessageTypeFilter(ALL)` 恒真 | 证实「只挂 ALL 会唤醒每条消息」的判断 |
| 过滤器是真正的 `CustomFilter` 子类 | 三层互斥在真机上成立 |
| 插件真实实例化 + 加载 4941 条精灵 | 内存库导入、模块导入链正常 |
| `pet_info(id=70)` 真渲染 | 产出 1529 KB 的 JPEG，Playwright 无头浏览器可用 |
| 宽松层 / AI 层出图 | 两条新链路都走到了真实 handler |
| AI 只调一次、`none` 零输出、模型报错静默 | 容错与成本控制符合设计 |
| 真实 `ProviderRequest` 注入能力说明 | 含防编造约束，重复调用不叠加 |

**这一步抓到并修掉了一个真实 bug**：上游 master 的 `config.py` 从
`astrbot.core.utils.astrbot_plugin_path` 导入，而 4.29.1 里该模块叫
`astrbot.core.utils.astrbot_path`——照上游原样覆盖会在 4.29.1 上直接 `ImportError`，
插件根本加载不起来。现在改成 `try/except` 双路径兼容，两个版本都能用。

### 8.2.1 已经部署到线上实例并复测

8 个文件（`main.py`、`config.py`、`data.py`、`_conf_schema.json`、`core/__init__.py`、
`core/misc_handler.py` + 新增 `core/command_parser.py`、`core/ai_fallback.py`）
已覆盖到线上插件目录，覆盖前整目录备份为
`astrbot_plugin_seer_info.bak-<时间戳>`（35 个文件）。

随后以 `ASTRBOT_ROOT=<实例>\core` 对**线上插件目录 + 线上数据库**再跑了一遍
`tests/test_real_astrbot.py`：20 项全部通过、exit 0，渲染结果落在线上
`image_cache`（1529 KB JPEG）。**但运行中的 AstrBot 进程仍是旧代码，需要在面板里重载插件后生效。**

回滚：

```powershell
$p='<实例>\core\data\plugins'
Rename-Item "$p\astrbot_plugin_seer_info" "$p\astrbot_plugin_seer_info.failed"
Rename-Item "$p\astrbot_plugin_seer_info.bak-<时间戳>" "$p\astrbot_plugin_seer_info"
```

### 8.2.2 线上反馈：重载后提示「数据库未加载」

重载后的第一条消息返回 `❌数据库未加载，请稍后再试`。排查结论：

* 本地库文件完好：`pragma quick_check = ok`，4941 行；
* `aliases` 指纹一致，会秒加载；但 **`seerapi` 的远端指纹已更新**
  （本地 `36962831…` vs 远端 `07f0ae51…`），插件会去 GitHub 下 25.7 MB 的新库；
* 实测下载 **约 122 KB/s → 25.7 MB 需要约 3.6 分钟**，这期间
  `db_manager.is_database_loaded()` 一直是 `False`，所有查询都回「数据库未加载」。

这是插件**原有的启动顺序问题**，不是本次改动的回归：`register_database()` 只挂一个后台
任务，要等下载完才 `load_from_file()`；`_sync_database_locked()` 里下载失败的分支
甚至直接 `return "failed"`，永远等不到数据。

修复（`data.py`，+13 行）：注册时就先用本地文件加载，再让后台任务检查更新。

```python
_sync_specs[name] = (sync_url, get_fingerprint)
if not db_manager.is_database_loaded(name):
    local_path = get_plugin_db_path(name)
    if Path(local_path).exists():
        db_manager.load_from_file(name, local_path)   # 立刻可用
async def sync_task(): ...                            # 照旧后台更新
```

效果：重载后**立即**可用（旧数据），后台更新完成后自动替换；下载慢或失败都不影响使用。

验证（`tests/test_db_preload.py`，把后台同步换成空操作，断言 `register_database()` 返回时库已就绪）：

```text
[1] register_database（配置了非空 sync_url，模拟线上）
  ok   register_database 返回时数据库已就绪
  ok   本地库可用（pet 表 4941 行）
[2] 后台同步任务仍然在跑
  ok   后台仍会检查更新（调用 ['seerapi']）
  ok   同步配置（URL）仍然保留
OK: 本地库预加载生效，启动/重载不再出现「数据库未加载」
```

### 8.2.3 线上反馈：「给我发一下南霜的立绘」把修饰词当成了名字

用户实测返回 `❌未找到匹配的立绘: 发一下南霜`。原因是宽松层只在指令词前后取名字，
`给我发一下南霜的立绘` 里 `给我` 被剥掉后，`发一下` 不在修饰词表中，被当成了名字的一部分。

修复两处：

* `_FILLER_PREFIXES` 补 `看下`、`查下`、`瞧下`、`瞅瞅`、`发我`；
* 新增请求动词规则：`(发|来|整|弄|给|上|搞) + 量词`，量词必需，
  避免 `发条鸟`、`来古`、`上古炎兽` 被吃掉开头；
* `_clean_arg` 的修饰词剥离层数从 2 提到 3（`麻烦帮我发一下…` 这类多层修饰）。

对线上库跑真实链路（本地渲染 + 真实图片下载）：

```text
解析结果: ('pet_image', '南霜')
返回结果: 1 条
  chain/Image: ...png
  chain/ Plain : 💎【寂刃孤锋·南霜】
结论: OK 出图
```

另外把 `资料 / 信息 / 介绍 / 详情` 加进 `SEER_KEYWORDS`：
`南霜的资料`、`南霜介绍` 这类**不含指令词**的说法规则层判不出来，现在会触发 AI 兜底
（实测 `parse=None, ask_ai=True`），由模型给出 `pet_info/南霜`。

### 8.2.4 「魂印」类说法

反馈希望「魂印」也能触发精灵查询。排查后发现：`魂印 雷伊`、`魂印雷伊`、`雷伊魂印`、`雷伊的魂印`
本来就能触发（`魂印` 既是 `精灵` 的 AstrBot 别名，也在宽松层的指令词里）；
真正漏的是**句子形式**和**名字前多出来的说明词**：

| 输入 | 修复前 | 修复后 |
| --- | --- | --- |
| `雷伊的魂印是什么` | None（落到 AI 层） | pet_info / 雷伊 |
| `雷伊魂印效果` | None | pet_info / 雷伊 |
| `雷伊的魂印有哪些` | None | pet_info / 雷伊 |
| `魂印效果雷伊` | pet_info / **效果雷伊** | pet_info / 雷伊 |
| `魂印技能雷伊` | pet_info / **技能雷伊** | pet_info / 雷伊 |

修复：剥句尾疑问成分 + 剥名字前的说明词（见第三节），并加了一条防误伤规则——
剥完只剩裸指令词时按闲聊处理，避免 `精灵怎么样` 被当成查询。

对线上库真实跑通：`魂印南霜` / `南霜魂印` → `('pet_info','南霜')` → 正常出图。

### 8.3 仍未覆盖

* **真实平台收发**（QQ / NapCat）与**真实大模型**：测试里平台事件和 LLM 都是桩。
  建议把插件装到线上实例后，在群里按第三节的例子点几条，确认唤醒行为与出图正常。
* 若某个平台唤醒行为特殊（比如 Lark 保留空白），`_event_text()` / `_event_is_woken()`
  都做了 `getattr` 兼容，只改这两个函数即可。

### 8.4 在别的机器上复跑

```bash
# 用 AstrBot 的 venv，ASTRBOT_ROOT 指向实例的 core 目录
set ASTRBOT_ROOT=<实例>\core
<实例>\venv\Scripts\python.exe tests\test_real_astrbot.py
```

前置条件不满足（没装 astrbot / 没有数据库）时脚本会打印 `SKIP` 并返回 0。


---

## 九、已知限制

* **拼接歧义**：`精灵王` 会被解析成「查询精灵『王』」。词表已尽量挡住闲聊句式，
  剩下的属于概率问题；查询不中只会回一句「未找到」。
* **`keyword` 模式覆盖不全**：像 `雷伊长什么样` 这种不含领域词、又没用标准格式的句子不会触发 AI，
  需要覆盖更全就切到 `always`（代价是每条被唤醒的消息都多一次小请求）。
* **参数只取第一个词**：与原版一致（`精灵 雷伊 超进化` → 查 `雷伊`）。想改成整段传入，
  把 `arg` 改成 `GreedyStr` 即可，但会影响原有严格指令的行为，故未动。
* **AI 兜底依赖模型可用性**：模型不可用/超时/返回畸形内容时，这条消息会被当作「不是查询」静默放过，
  不会降级成报错。
* **AI 兜底不参与对话**：它只把句子翻译成指令；闲聊仍由 AstrBot 原有聊天流程回答。
* 指令表是手写的，靠 `tests/` 的 AST 检查防止与 `main.py` 脱节；以后加新指令时
  **两处都要加**（`main.py` 的装饰器 + `core/command_parser.py` 的 `INTENTS`），否则测试会报错提醒。
