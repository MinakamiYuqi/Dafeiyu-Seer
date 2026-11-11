# 赛尔知识库（astrbot_plugin_seer_expert）

让 bot 的赛尔号回答**基于本地事实库**，而不是模型记忆。

## 数据来源

直接只读你已有的 `plugin_data/astrbot_plugin_seer_info/seerapi.sqlite`（61 MB / 160 张表）：

| 用途 | 表 |
|---|---|
| 精灵 | `pet` / `pet_base_stats` / `element_type` |
| 技能 | `skill` / `skillinpetorm` |
| 魂印 | `soulmark` / `petsoulmarklink` |
| 术语/异常状态 | `glossary_entry` |
| 属性克制 | `elementtyperelationorm` |
| 图鉴简介 | `pet_encyclopedia_entry` |

不复制、不重新下载，只读打开。

## 八个 LLM 工具

| 工具 | 作用 |
|---|---|
| `seer_pet(name)` | 精灵属性 / 种族值 / 魂印 / 技能 / 简介 |
| `seer_skill(name)` | 技能威力、PP、命中、先制、效果文本 |
| `seer_soulmark(keyword)` | 按关键词搜魂印 |
| `seer_term(term)` | 术语、异常状态、机制说明 |
| `seer_type_chart(a, d)` | 属性克制倍率 |
| `seer_damage(...)` | 伤害估算 + 拆解 |
| `seer_recall(topic)` | 检索玩家补充过的知识 |
| `seer_remember(topic, content)` | 把玩家确认的判定记进库 |

## 伤害怎么算

只算可计算的部分，其余显式列为系数，避免假装精确：

```
基础值 = (((等级×0.4+2) × 威力 × 攻击/防御) / 50) + 2
最终值 = 基础值 × 克制 × 本系 × 暴击 × 随机(0.85~1.0) × 其他
```

魂印、特性、抗性、状态这些**不进公式**，用 `other` 系数表达，或在回复里单独说明。

## 不懂就问的闭环

```
工具查不到 → 返回「未收录」+ 提示模型去问人
        → 模型在群里问「这只精灵的正式名字是？」
        → 玩家回答
        → 模型调用 seer_remember 落库
        → 下次 seer_recall 命中，优先级高于静态库
```

补充的知识存在 `plugin_data/astrbot_plugin_seer_expert/kb.sqlite` 的 `facts` 表里，
可以随时用 SQLite 工具查看、修改、删除。

## 成本

全部是本地 SQL 查询，**不额外调用模型**。只在主回复里多几次 tool call，
每次返回一两百字，token 增量很小。
