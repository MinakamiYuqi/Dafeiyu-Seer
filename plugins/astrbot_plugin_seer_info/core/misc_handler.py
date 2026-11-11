import re

import astrbot.api.message_components as Comp
import httpx
from astrbot.api.event import AstrMessageEvent


class MiscHandler:
    async def preview_cmd(self, event: AstrMessageEvent):
        yield event.chain_result(
            [
                Comp.Image.fromURL(
                    "https://cnb.cool/HurryWang/seer-unity-preview-img-dumper-cnb/-/git/raw/master/img/preview.png"
                ),
                Comp.Plain("预告图来自 https://github.com/WhY15w/seer-unity-preview-img-dumper"),
            ]
        )

    async def server_info_cmd(self, event: AstrMessageEvent):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("https://unity-notice.61.com/unity_notice/")
                resp.raise_for_status()
                data = resp.json()
            for item in data:
                if item.get("type") == 3:
                    text = re.sub(r"<[^>]*>", "", item.get("text", ""))
                    text = text.replace("\\n", "\n")
                    yield event.plain_result(text)
                    return
            yield event.plain_result("开服了哦~")
        except Exception:
            yield event.plain_result("开服了哦~")

    async def help_cmd(self, event: AstrMessageEvent, status: str = ""):
        help_text = """🤖 赛尔号数据查询插件
命令：
  🐱精灵相关：
    精灵/查询精灵信息/魂印/技能 <名称/ID> — 查询精灵基础信息
    立绘/皮肤/查询立绘 <名称/ID> — 查询精灵或皮肤立绘
    配置图/配置 <名称/ID> — 查询精灵配置图（本地收录）
    属性/属性表 <名称/ID> — 查询属性克制表
    异常/查询异常状态 <名称/ID> — 查询异常状态信息

  💮刻印相关：
    刻印 <名称/ID> — 查询刻印信息及数值
    宝石 <名称/ID> — 查询刻印宝石信息

  👚装备相关：
    套装/查询套装信息 <名称/ID> — 查询套装信息
    部件/查询部件信息 <名称/ID> — 查询装备部件信息

  🏅称号相关：
    称号/查询称号信息 <名称/ID> — 查询称号信息

  🔀其他功能：
    下周预告 — 获取下周预告图
    开服查询 — 查询服务器是否已开服
    收录配置图 <名称> [作者:某某] [备注:文字] — 管理员/白名单，图和指令同一条消息
    配置备注 <名称> [作者:某某] [备注:文字] — 管理员/白名单，只改文字不动图
    外号 <名称/ID> — 查精灵的外号/别称（不带参数看别名库概况）
    白名单 — 管理配置图收录白名单（按序号/昵称/UID 添加）
    奶龙精灵查询 — 恶搞奶龙图图鉴（回序号看图）
    上传奶龙图 <奶龙名> <精灵名/ID> — 收录奶龙图，图片和指令同一条消息

💡 指令写法很宽松，下面这些都能识别（以精灵为例）：
    精灵 雷伊 / 精灵雷伊 / 雷伊精灵 / 雷伊的精灵
    查一下精灵 雷伊 / 帮我查一下精灵雷伊 / 赛尔号精灵雷伊
    名称也支持拼音（leiyi）、别名和 ID
🤖 实在说不准的句子会交给 AI 兜底解析，比如「雷伊的技能都有啥」"""
        if status:
            help_text = f"{help_text}\n{status}"
        yield event.plain_result(help_text)
