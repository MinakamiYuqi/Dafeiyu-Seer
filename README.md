# 赛尔号查询插件（AstrBot）

给 QQ 群用的赛尔号（Seer）查询工具：精灵数据、官方词条、玩家结论、阵容战绩。

## 目录

| 路径 | 作用 |
|---|---|
| `plugins/astrbot_plugin_seer_expert` | 查询主力。32 个工具，覆盖精灵 / 技能 / 魂印 / 套装 / 刻印 / 伤害计算 / 先手判定，自带三个数据源的更新逻辑 |
| `plugins/astrbot_plugin_seer_info` | 数据层 + 群友互动（奶龙图库、配置图上传）。改造自 `MengNianxiaoyao` 的 GPL v3 项目，详见其 `NOTICE.md` |
| `seer-cli/seer.py` | 独立命令行查询器，人和 agent 都能用，不依赖 AstrBot |
| `plugins/astrbot_plugin_seer_expert/_xm/login_tail.txt` | 无头登录必需的协议尾巴，仓库自带；详见[文末说明](#关于-_xmlogin_tailtxt) |

**两个插件互不依赖，装哪个都能用**：

```
只想查数据        → 装 seer_expert 就够（它自己同步 seerapi / aliases / manual_db）
还想要奶龙和配置图 → 再装 seer_info
两个都装          → 数据文件共享同一份，不会重复下载
```

> 早先还有一个 `seer_dsh`（把难题转给 DSH agent）。实测下来和直接问 bot 拉不开差距，
> 还多一层 150 秒超时风险，已经移除。

## 三个数据源

```
官方词条  →  manual_db/effectDes.db    640+ 条，按 kind 分 7 类
游戏数据  →  seerapi.sqlite            160+ 表，约 62 MB
玩家结论  →  kb.sqlite                 facts 表，一条一个主题
```

**哪个源查什么不能混**：查「词条定义」走 effectDes，查「精灵数值」走 seerapi，查「实战结论」走 kb。
库里查不到 ≠ 游戏里没有，换源顺序是 官方词条 → 玩家结论 → 才说没有。

## 数据从哪来、怎么更新

### ① seerapi.sqlite — 游戏数据镜像

```
来源:  https://github.com/Murmansk-Seer/seerapi
       releases/download/seerapi-data-latest/seerapi-data.sqlite
指纹:  同目录 seerapi-data.sqlite.sha256
更新:  data_sync.py::sync_seerapi，每 60 分钟比对一次指纹
```

### ② aliases.sqlite — 精灵别名库

```
来源:  https://github.com/Murmansk5000/IronsBot
       releases/download/alias-db-latest/aliases-data.sqlite
指纹:  同目录 aliases-data.sqlite.sha256
更新:  data_sync.py::sync_aliases，每 60 分钟比对一次指纹
```

### ③ manual_db/ — 官方词条手册（15 个库）

```
来源:  https://cnb.cool/SeerFire/update_cnb/-/git/raw/main/generated_files/db
更新:  manual_sync.py::sync，默认每 24 小时一轮
```

### ④ kb.sqlite — 玩家知识库

**不自动更新**，是本项目的自用数据。破坏性写入前会自动快照（`kb_backups/`，保留最近若干份）。

本地的别名补充不要直接改 `aliases.sqlite`，写进 `aliases_local_overlay.sqlite`，更新后会自动合并回来。

### 更新流程里的硬性检查

两个同步库在覆盖前都会过一遍验货闸门：

```
取远端 .sha256
  ├─ 与本指纹一致 → 跳过（不发 DB 请求）
  └─ 不一致 → 下载 → 验货 → 备份旧库 → 原子替换
```

**验货不通过就整包丢弃、保留旧库。** 这不是洁癖：上游曾经漏发 34 张表（含 `pet_special_effect`），
老同步逻辑无校验直接覆盖，导致专属名词查询全部静默失效了好几天。

## 安装

1. 装 [AstrBot](https://github.com/AstrBotDevs/AstrBot)
2. 把 `plugins/` 下两个目录复制到 `<AstrBot>/data/plugins/`
3. 重启 AstrBot

首次启动后：

- `seerapi.sqlite` / `aliases.sqlite`：启动约 3 分钟后自动下载（约 62 MB，耐心等）
- `manual_db/`：启动约 10 分钟后自动同步 15 个手册库
- `kb.sqlite`：**不会自动生成**，空库也能跑，只是「玩家结论」那部分查不到

## 查战绩 / 查阵容的额外准备

`seer_pvp` / `seer_roster` 走无头客户端直连游戏服务器，需要：

1. `_xm/login_tail.txt` —— **仓库里已经带了**（984 字符 hex，解码 492 字节）。
   内容是 `taomee` + `unity_app_taomee` + `com.taomee.seer.mobile` + 设备型号，
   不含账号密码，直接用就行。Temp 目录被清时 `seer_pvp` 会自动从 `_xm/` 复制回去。
2. 一个专用小号的米米号密码，通过环境变量给：

```powershell
[Environment]::SetEnvironmentVariable("SEER_ACCOUNT", "你的米米号", "User")
[Environment]::SetEnvironmentVariable("SEER_PASSWORD", "密码", "User")
```

缺这些时这两个工具会报错提示，其余 30 个工具正常。

> **小号必须专号专用**：同一账号同一时刻只能一个在线，和你自己玩会互相踢下线。

## 命令行用法

```bash
python seer-cli/seer.py pet 灵巢             # 精灵全景
python seer-cli/seer.py glossary --kind 语法  # 官方判定用语
python seer-cli/seer.py suit 晨曦             # 套装战斗效果
python seer-cli/seer.py raw "select count(*) from facts"
```

## 已知的坑

- **`glossary_entry`（595 条）是 `effectDes`（640 条）的子集**，查官方词条要走后者
- **`pet_special_effect` 会子串误挂**（「失体」挂到 199 只精灵），用 `pet_effects()` 而非直查
- **套装效果在 `suit_bonus` 表，关联键是 `suit.id`**，不是 `equip.bonus_id`
- **Lv0 技能是道具开启的补强**（175 只精灵 / 264 个技能），常直接改机制，查精灵别漏
- **判「库重复」不能只看代码引用**，必须逐字段比对；删库前先比对内容
- **`raw` 命令默认只连 kb 库**，查游戏数据要加 `--db seerapi`
- **皮肤头像会缺**：上游对部分皮肤不单独出图（新机体复用老机体），
  取图时按 `皮肤 → 同名皮肤 → 同名精灵 → 本体` 四级回退
- **`_xm/login_tail.txt` 不能排除**：它曾被当成「机器指纹」写进 .gitignore 和
  build_repo.py 的排除表，结果分享包缺文件、别人装完 `seer_pvp` 直接跑不起来。
  实际内容只有协议常量 + 设备型号，公开无风险。

## 来源与致谢

这个项目的设计与数据来源不止一处，完整链条写在 **[CREDITS.md](CREDITS.md)**：

```
AstrBot 插件市场的赛尔号插件（MengNianxiaoyao，GPL v3）
  → 参考 Nattsu39/IronsBot
  → 我们的改造版（即本仓库的 seer_info）
  → 后续功能参考 Murmansk5000/IronsBot
  → 无头客户端线索来自其鸣谢名单（SeerRadar / oldml）
```

**如果你也参考了这些项目，请把致谢一样写清楚。**

## 许可

本仓库整体遵循 **GNU GPL v3**（见 [LICENSE](LICENSE)）——
因为包含 `astrbot_plugin_seer_info`，它是对 `MengNianxiaoyao` 项目的 GPL v3 改造版。

```
seer_info     GPL v3（原作者 MengNianxiaoyao + 我们的修改）
seer_expert   GPL v3（同一仓库，同协议分发）
seer-cli      GPL v3
```

**再分发时**：保留 `LICENSE` 与各目录的 `NOTICE.md`，你的版本也用 GPL v3，并说明相对上游改了什么。

引用的数据版权归原出处：游戏数据 © 淘米，seerapi 数据包来自 Murmansk-Seer，
别名库来自 Murmansk5000/IronsBot，手册词条来自火火（Yogurt114514），玩家结论来自本群群友。

---

## 关于 `_xm/login_tail.txt`

无头客户端登录时需要这个文件（`headless.py` 从 `%TEMP%\login_tail.txt` 读），
Temp 目录被系统清理后会登录失败。`seer_pvp.py` 里有自愈逻辑，会从
`_xm/login_tail.txt` 复制回去 —— **所以它必须随包分发，别删**。

内容解码后是 492 字节：`taomee` + `unity_app_taomee` + `com.taomee.seer.mobile`
+ 一个笔记本型号（`NitroAN515-55`），**没有账号、密码或唯一设备 ID**。
早期把它当成「机器指纹」排除过，导致别人 clone 下来缺文件跑不起来 —— 已修正。

如果登录仍失败（比如赛尔号客户端升级改了协议），把你自己 `%TEMP%\login_tail.txt`
替换进来即可：

```powershell
copy "$env:TEMP\login_tail.txt" plugins\astrbot_plugin_seer_expert\_xm\login_tail.txt
```

---

## 这是怎么来的

先说清楚：**这个仓库里的代码，几乎没有一行是鱼片亲手敲的。**

鱼片是个外行。他不会 Python，看见 `async` 和 `await` 就问「这是什么东西」，
连 `git commit` 都要先问一句「敲哪个」。他干的事只有两件——

**挑刺**，和**指方向**。

```
「这个精灵查不到」
「你这个答得不对，技能顺序不是这么判的」
「能不能也查一下刻印」
「慢死了，去翻日志看看到底卡在哪」
「你明明有工具，为什么不用」
```

然后我们这群鱼——一条接一条，换过好几拨——去翻数据库、扒官方词条、
试协议、拆客户端、写测试、改代码，把他的话变成能跑的东西。

所以这仓库其实是两样东西的合体：

```
一个外行的直觉          他不懂实现，但他知道什么该有、什么不对
    +
一群不会累的鱼          会去把「应该有的」挖出来、跑通、写下来
```

有趣的是，很多关键的设计决定都是这个外行拍板的：

- 「先把记忆和判定分了，别混在一起」→ 才有了 `kb.sqlite` 和 `manual_db` 的两套结构
- 「官方词条查不到就去查玩家结论，别跟人说没有」→ 才有了换源顺序那套逻辑
- 「删库之前先比对字段，别只看代码引用」→ 这条救回了三个差点被删的库

我们负责实现，他负责当那个不断说「这不对」的人。
说真的，没有他一直在旁边挑毛病，这个项目大概早就停在某个"能跑就行"的版本上了。

—— 神代·欧鲸鲸（大肥鱼），写于某天凌晨三点
