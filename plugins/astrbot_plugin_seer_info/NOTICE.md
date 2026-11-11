# NOTICE —— 关于本目录的代码来源

## 这个插件的来龙去脉

```
MengNianxiaoyao/astrbot_plugin_seer_info   (GPL v3，原项目)
    └─ 我们基于 v1.0.8（main 分支 e5c3bb7）做了改造
```

`MengNianxiaoyao` 的原项目本身参考了 `Nattsu39/IronsBot`，完整链条见仓库根目录的
[`CREDITS.md`](../../CREDITS.md)。

## 原项目信息

```
名称:   astrbot_plugin_seer_info
作者:   MengNianxiaoyao
许可:   GNU General Public License v3.0
仓库:   https://github.com/MengNianxiaoyao/astrbot_plugin_seer_info
基线:   v1.0.8 / main 分支 e5c3bb7
```

## 我们改了什么

主要改动集中在**指令识别层**和**几个附加功能**，详细说明见同目录 `CHANGES.md`：

```
指令识别     严格匹配 → 宽松匹配 → AI 兜底
附加功能     奶龙图库
            群友配置图上传（含白名单）
            宽松指令识别 / 裸名搜索 / 自动派发
数据同步     仍由本插件维护 seerapi.sqlite / aliases.sqlite
            （seer_expert 里的 data_sync.py 是同一份数据的另一条通道，
              两个插件互不依赖，装哪个都能用）
```

## 许可

本目录下的代码**整体遵循 GNU GPL v3**，见同目录 [`LICENSE`](LICENSE)。

原作者代码的版权归 `MengNianxiaoyao`；
我们新增与修改部分的版权归本项目作者。两者都以 GPL v3 分发。

**如果你要再分发这个目录**：

```
① 保留本 NOTICE 和 LICENSE
② 保留 CHANGES.md（它记录了相对上游改了什么）
③ 你的版本也用 GPL v3
```

GPL 保护的是代码不是名字 —— **改个插件名并不能免除上面这些义务**。
