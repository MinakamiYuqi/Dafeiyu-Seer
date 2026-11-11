# seer-cli — 赛尔号知识库查询命令行

把 `astrbot_plugin_seer_expert` 的 `SeerKnowledge` 包成 CLI，
**任何 agent（DSH / Claude / 脚本 / AstrBot 插件）都能直接查库和算伤害。**

不用写 DSH 插件、不用起服务、不用 API——**一条 shell 命令就完事。**

---

## 为什么要有它

原来的 AstrBot 插件里有 20 个 `llm_tool`，问题是**模型不一定会调**——
实测过它凭记忆把「自然/圣灵打电」答成 2 倍（正确 4 倍）。

CLI 把这条路改成「必须动手」：

```
agent 想回答数值问题 → 必须先跑 seer.py → 结果直接可读
```

---

## 安装

**无依赖**（除了 Python 3.10+ 和已有的 `db.py` / `formula.py`）。

它靠 `sys.path` 找插件目录，默认候选：

```
<seer-cli>/../astrbot_plugin_seer_expert
G:\dsh workspace\astrbot_plugin_seer_expert
```

数据目录默认：

```
G:\temp\instances\eb74352a-...\core\data\plugin_data
```

**换路径的方式：**

```powershell
$env:SEER_DATA_DIR = 'D:\path\to\plugin_data'
# 或
python seer.py --data 'D:\path\to\plugin_data' pet 灵巢
```

---

## 命令

| 命令 | 作用 |
|---|---|
| `pet <名字>` | 精灵全景：属性/种族/魂印/技能/图鉴 |
| `skill <技能名>` | 技能详情（带〔固有〕〔命中〕标记） |
| `type <攻> <守>` | 克制计算（带拆解） |
| `dmg ...` | 伤害计算（走 `formula.combo_damage`） |
| `facts <关键词>` | 玩家口述规则检索 |
| `topics` | 列出全部 facts 主题 |
| `glossary <关键词>` | 官方词条 |
| `alias <俗称>` | 俗称 → 精灵 id |
| `tags <精灵>` | 功能标签覆盖（恢复/强化/先制…） |
| `pool <精灵>` | 巅峰池 |
| `status [分类]` | 异常状态列表 |
| `raw "<SQL>"` | 直接跑只读 SQL |

**全部支持 `--json`。**

---

## 常用例子

```powershell
# 精灵
python seer.py pet 灵巢
python seer.py pet 3524 --json

# 克制（双系防守会自动拆解）
python seer.py type 轮回 自然/超能          # ×0.5
python seer.py type 次元 自然/超能          # ×4.0  ← 双2特例
python seer.py type 战斗/龙 冰/暗影         # ×2.375

# 技能（看它是不是命中效果）
python seer.py skill 千魂之结
# → 〔命中〕3回合内将对手的攻击、特攻能力降为0点

# 伤害：威170 攻500 防450 4倍克制 本系 通用+150% 减伤10%
python seer.py dmg --power 170 --atk 500 --defense 450 --type 4.0 `
                   --stab --general-add 1.5 --reduce 0.10 --hp 1140

# 连击：冰王 180 段
python seer.py dmg --power 1 --atk 650 --defense 500 --type 1.0 --stab --hits 180

# 规则检索
python seer.py facts 转移
python seer.py glossary 最优系别
```

---

## ★ 两个必须知道的坑（都是做这个 CLI 时挖出来的）

### ① `type_multiplier_detail` 默认 mode 会算错双双

```python
k.type_multiplier_detail(["轮回"], ["自然", "超能"])
# → {"mult": 0.25}   ← 错了！默认 mode="multiply" 是 0.5×0.5

k.type_multiplier_detail(["轮回"], ["自然", "超能"], mode="seer")
# → {"mult": 0.5}    ← 正确，赛尔规则是「拆防守方再平均」
```

**所以任何调用都必须显式传 `mode="seer"`。**
**CLI 已经写死了，但你要是绕过 CLI 直接调 `db.py`，一定会踩。**

**验证过的 5 个用例（全对）：**

| 攻 | 守 | 期望 | mode=seer |
|---|---|---:|---:|
| 轮回 | 自然/超能 | 0.5 | ✅ |
| 次元 | 自然/超能 | 4.0 | ✅ |
| 自然/圣灵 | 电 | 4.0 | ✅ |
| 战斗/龙 | 冰/暗影 | 2.375 | ✅ |
| 虚空 | 神秘/战斗 | 4.0 | ✅ |

### ② 手算 base 容易错在乘法顺序

**正确：**

```
base = (等级×0.4 + 2) × 威力 × 攻击 ÷ 防御 ÷ 50 + 2
     = (42 × 威力 × 攻击 ÷ 防御) ÷ 50 + 2
```

**我用历史对话里手算的「威160 / 攻500 / 防450」举例：**

```
正确：42×160×500÷450÷50+2 = 151.33
我曾算成：146.00   ← 错
```

**差 5，会连锁到所有后续乘区的取整。**

**所以：伤害一律用 `seer.py dmg`，不要手推。**

---

## 已知局限

**① `dmg` 是简化入口**

**它用 `formula.combo_damage`，覆盖：**
```
base → 本系×克制 → 浮动 → 暴击(过抗) → 连击次数 → 通用增伤 → 额外增伤 → 减伤
```

**不覆盖：`formula.settle` 里的固伤/百分比伤害/真实伤害/属性伤害分项。**
**要那套，直接调 `formula.settle()`。**

**② 取整位置会影响结果**

**分部 floor 与一次算完可能差 1~数点。CLI 按 `combo_damage` 的分部取整走。**

**③ `pet_skills` 只有名字/威力/系别/先制**

**要完整效果文本，用 `skill <技能名>`。**

---

## 目录

```
seer-cli/
  seer.py      ← 就这一个文件
  README.md
```
