# 赛尔号查询插件（AstrBot）

给 QQ 群用的赛尔号（Seer）查询工具：精灵数据、官方词条、玩家结论、阵容战绩。

## 目录

| 路径 | 作用 |
|---|---|
| `plugins/astrbot_plugin_seer_expert` | 查询主力。32 个工具，覆盖精灵 / 技能 / 魂印 / 套装 / 刻印 / 伤害计算 / 先手判定，自带三个数据源的更新逻辑 |
| `plugins/astrbot_plugin_seer_info` | 数据层 + 群友互动（奶龙图库、配置图上传）。改造自 `MengNianxiaoyao` 的 GPL v3 项目，详见其 `NOTICE.md` |
| `seer-cli/seer.py` | 独立命令行查询器，人和 agent 都能用，不依赖 AstrBot |

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

1. `_xm/login_tail.txt`（492 字节的设备指纹，**每台机器不同，仓库不提供**）
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

## 来源与致谢

这个项目的设计与数据来源不止一处，完整链条写在 **[CREDITS.md](CREDITS.md)**：

```
AstrBot 插件市场的赛尔号插件（MengNianxiaoyao，GPL v3）
  → 参考 Nattsu39/IronsBot
  → 我们的改造版（不在本仓库）
  → 后续功能参考 Murmansk5000/IronsBot
  → 无头客户端线索来自其鸣谢名单（SeerRadar / oldml）
```

**如果你也参考了这些项目，请把致谢一样写清楚。**

## 许可

本项目为自用工具，未声明许可证。

引用的数据版权归原出处：游戏数据 © 淘米，seerapi 数据包来自 Murmansk-Seer，
别名库来自 Murmansk5000/IronsBot，手册词条来自 SeerFire，玩家结论来自本群群友。
