# 来源与致谢

这个项目的代码不是凭空出现的。把链条写清楚，既是尊重上游，也方便后来的人判断哪些能改、哪些要慎重。

## 一、这条线是怎么来的

```
AstrBot 插件市场的赛尔号插件
    └─ MengNianxiaoyao/astrbot_plugin_seer_info  (GPL v3)
         └─ 参考了 Nattsu39/IronsBot
              https://github.com/Nattsu39/IronsBot

我们自己改造的版本（未公开分发）
    ├─ 指令识别层（严格 → 宽松 → AI 兜底）
    ├─ 奶龙图库、群友配置图上传
    └─ 后续功能参考 Murmansk5000/IronsBot
         https://github.com/Murmansk5000/IronsBot
```

**`astrbot_plugin_seer_info` 已包含在本仓库里**，作为对原项目的 GPL v3 改造版，
按 GPL v3 要求保留版权、同协议分发、说明改动（见该目录的 `NOTICE.md` 与 `LICENSE`）。

## 二、本仓库的原创部分

| 目录 | 原创性 |
|---|---|
| `plugins/astrbot_plugin_seer_expert` | 从零写起。32 个查询工具、伤害口径、必先判定、facts 检索、数据同步逻辑 |
| `seer-cli/seer.py` | 从零写起 |

（早先还有一个 `seer_dsh`，实测与直接问 bot 拉不开差距，已从仓库移除。）

但**其中若干设计与数据来源来自下面这些人**。

## 三、具体致谢

### Nattsu39 — IronsBot

```
https://github.com/Nattsu39/IronsBot
```

赛尔号机器人这条线上比较早的项目。数据组织方式和部分字段理解受它影响。

### 摩尔曼斯克（Murmansk5000）— IronsBot 及其数据源

```
https://github.com/Murmansk5000/IronsBot
https://github.com/Murmansk-Seer/seerapi
```

- **数据源**：`seerapi.sqlite`（游戏数据镜像）和 `aliases.sqlite`（别名库）直接来自他的发布
- **图片渲染方式**：部分优化参考他的实现
- **胜率与场次查询**：接口理解上受过他指点
- **无头客户端的线索**：来自他项目的鸣谢名单

### 火火（Yogurt114514）— 火の辞典手册

```
GitHub:   https://github.com/Yogurt114514
数据源:   https://cnb.cool/SeerFire/update_cnb
```

`manual_db/` 里的 **15 个手册库全部来自他**：

```
effectDes      官方词条（640+ 条，按 kind 分 7 类）
skill_effect   技能效果
mintmark       刻印数值（含隐藏加成）
pets / moves   精灵 / 技能基础表
effectIcon / effectInfo / effectag / effectbuff / petEffectIcon / rich_text_tree
buff / pvp / skillTypes / te_xing
```

**没有这套手册，「官方词条怎么定义」这个问题就答不了** —— 游戏客户端里查不到，
seerapi 那套镜像也只有数值没有词条正文。感谢他整理并持续维护。

### SeerRadar / oldml

摩尔曼斯克项目的鸣谢名单里写着：

```
SeerRadar：Sequ 数据参考
oldml：saixiaoxi 无头登录参考
```

无头客户端（`_xm/headless.py`）的登录流程是在这个线索上做出来的。
**协议本身是我们自己实测推出来的**（见 `_xm/headless.py` 头部注释），但那两个项目提供了起点。

### 关于 12 只阵容查询

`cmdId 41635 GET_USER_PET_INFO_BY_ID` 的字段结构，早期一直没啃下来。
最终是靠对 `GameLogic.dll` 的反编译才拿到完整布局的（`_re/lineup_followup/`）。

## 四、我们希望别人怎么用这个仓库

```
✅ 拿去自己跑、自己改、自己部署
✅ 指出我们的 bug 或口径错误
⚠️ 如果你的项目也参考了上面这些人和项目，请同样把致谢写清楚
```

**不是法律要求，是这条线上大家都这么做的。** 上游把线索留在鸣谢名单里，
我们才少走了一些弯路；那我们也该把线索留给下一个人。

## 五、数据版权

```
游戏数据（精灵 / 技能 / 魂印 / 数值）    © 上海淘米网络
seerapi 数据包                          Murmansk-Seer
别名库                                  Murmansk5000/IronsBot
手册词条（manual_db）                    火火（Yogurt114514）
玩家结论（kb.sqlite）                    本群群友的实测与整理
```

图片资源（精灵头像 / 立绘 / 刻印图标）来自官方 CDN，本项目只做拉取缓存，不重新分发。
