import asyncio
import html
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from seerapi_models import MintmarkORM, PetORM
from seerapi_models.mintmark import PetMintmarkLink, SkillMintmarkLink
from sqlalchemy import text
from sqlalchemy.orm import object_session
from sqlmodel import col, select

from ..service import (
    ElementTypeImageGetter,
    ItemImageGetter,
    MintmarkBodyImageGetter,
    PetBodyImageGetter,
    PetHeadImageGetter,
    MintmarkIconGetter,
    SignBuffImageGetter,
    SoulmarkIconGetter,
    flip_image_horizontal,
    to_data_uri,
)
from .analyzer import AnalyzeDescParser, parse_analyze_desc
from .renderer import get_template_content
from .soulmark_format import parse_rich_soulmark

PET_TEMPLATE = get_template_content("pet_info/template.html.j2")
_TERM_COLOR = "#f35555"
_TERM_COLORS = (
    "#ff9e6e",
    "#f5d84a",
    "#8ae878",
    "#6eebc0",
    "#ff8ab8",
    "#e8a0ff",
    "#ff7b7b",
    "#ffd4a0",
)


def _extract_skill(skill_in_pet) -> list[dict[str, Any]]:
    skill = skill_in_pet.skill
    if not skill or getattr(skill, "id", 0) == 19002:
        return []

    skill_type = getattr(skill, "type", None)
    skill_category = getattr(skill, "category", None)
    skill_hide_effect = getattr(skill, "hide_effect", None)
    skill_activation_item = getattr(skill_in_pet, "skill_activation_item", None)

    effects = []
    for effect in getattr(skill, "skill_effect", []):
        parser = AnalyzeDescParser.from_cache(getattr(effect, "analyze_info", "") or "")
        effects.append(
            {
                "id": getattr(effect, "effect_id", 0),
                "info": parser.to_html(),
                "_parser": parser,
            }
        )

    hide_effect_desc = (
        getattr(skill_hide_effect, "description", None) if skill_hide_effect else None
    )
    activation_item = (
        getattr(skill_activation_item, "name", None) if skill_activation_item else None
    )

    result = {
        "id": skill.id,
        "name": skill.name,
        "type_id": skill_type.id if skill_type else 0,
        "type_name": skill_type.name if skill_type else "",
        "category_id": skill_category.id if skill_category else 0,
        "category_name": skill_category.name if skill_category else "",
        "power": skill.power or 0,
        "max_pp": skill.max_pp or 0,
        "accuracy": "必中" if skill.must_hit else skill.accuracy,
        "crit_rate": skill.crit_rate,
        "priority": skill.priority or 0,
        "must_hit": skill.must_hit,
        "info": skill.info,
        "learning_level": skill_in_pet.learning_level,
        "is_special": skill_in_pet.is_special,
        "is_advanced": skill_in_pet.is_advanced,
        "is_fifth": skill_in_pet.is_fifth,
        "effects": effects,
        "glossaries": [],
        "activation_item": activation_item,
        "activation_item_id": getattr(skill_activation_item, "id", None),
        "activation_item_icon": "",
        "activation_price": "",
        "friend_bonus": False,
        "hide_effect_desc": hide_effect_desc,
    }

    if hasattr(skill, "friend_skill_effect") and len(skill.friend_skill_effect) > 0:
        friend_skill = {
            **result,
            "friend_bonus": True,
            "is_special": True,
            "effects": [
                {"id": getattr(e, "effect_id", 0), "info": getattr(e, "info", "")}
                for e in skill.friend_skill_effect
            ],
        }
        return [result, friend_skill]

    return [result]


def _extract_soulmark(soulmarks: list, pet: PetORM) -> list[dict[str, Any]]:
    results = []
    for sm in soulmarks:
        sm_desc = getattr(sm, "analyze_desc", "") or getattr(sm, "desc", "")
        rich = getattr(sm, "desc_formatting_adjustment", "") or ""
        results.append(
            {
                "id": getattr(sm, "id", None),
                "icon": "",
                "desc": parse_analyze_desc(sm_desc or ""),
                "sections": parse_rich_soulmark(rich),
                "intensified": getattr(sm, "intensified", False),
                "is_adv": getattr(sm, "is_adv", False),
                "pve_effective": getattr(sm, "pve_effective", None),
                "tags": [t.name for t in getattr(sm, "tag", []) or []],
                "glossaries": [],
                "_parser": AnalyzeDescParser.from_cache(sm_desc or ""),
            }
        )

    return results


def _load_special_effects(session, pet_id: int) -> list[dict[str, Any]]:
    """专属效果。

    数据在 `pet_special_effect` / `pet_special_effect_source` 两张表里，
    属于新数据源才有的表，这里用原生 SQL 查，避免依赖 seerapi_models 的新版本。
    """
    if session is None:
        return []
    try:
        rows = session.connection().execute(
            text(
                "select effect_key, name, description, glossary_id, status_id, icon_key "
                "from pet_special_effect where pet_id = :pid order by sort_id"
            ),
            {"pid": pet_id},
        ).fetchall()
    except Exception as e:
        logger.debug(f"专属效果查询失败（数据源可能不含该表）: {e}")
        return []
    if not rows:
        return []

    sources: dict[str, list[tuple[str, int, str]]] = {}
    try:
        for key, kind, source_id, detail in session.connection().execute(
            text(
                "select effect_key, source_kind, source_id, source_detail "
                "from pet_special_effect_source where pet_id = :pid"
            ),
            {"pid": pet_id},
        ).fetchall():
            sources.setdefault(str(key), []).append(
                (str(kind or ""), int(source_id or 0), str(detail or "").strip())
            )
    except Exception:
        sources = {}

    effects: list[dict[str, Any]] = []
    for key, name, desc, glossary_id, status_id, icon_key in rows:
        raw_sources = sources.get(str(key), [])
        meta_parts: list[str] = []
        for kind, source_id, detail in raw_sources:
            if kind in {"pet_glossary", "glossary"}:
                label = "官方关联词条"
            elif kind == "skill" and detail:
                label = f"技能 · {detail}"
            elif kind == "soulmark" and source_id:
                label = f"魂印状态{source_id}"
            else:
                label = detail
            if label and label not in meta_parts:
                meta_parts.append(label)
        effects.append(
            {
                "name": str(name or "").strip() or "专属效果",
                "desc": str(desc or "").strip(),
                "sources": [detail for _k, _i, detail in raw_sources if detail],
                "meta": "、".join(meta_parts),
                "status_id": int(status_id) if status_id else 0,
                # 图标沿用上游规则：icon_key 优先，其次 status_id；都没有就不显示
                "icon_key": str(icon_key).strip() if icon_key else "",
                "icon": "",
            }
        )
    return effects


def _load_exchange_prices(session, item_ids) -> dict[int, dict[str, Any]]:
    """技能激活道具的兑换价/限购，数据在 `item_exchange_price` 表。"""
    ids = {int(i) for i in item_ids if i}
    if session is None or not ids:
        return {}
    placeholders = ",".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": value for i, value in enumerate(sorted(ids))}
    try:
        rows = session.connection().execute(
            text(
                "select item_id, currency_name, amount, purchase_limit, "
                "start_time, end_time, currency_item_id "
                f"from item_exchange_price where item_id in ({placeholders})"
            ),
            params,
        ).fetchall()
    except Exception as e:
        logger.debug(f"激活道具价格查询失败: {e}")
        return {}

    now = int(time.time())
    prices: dict[int, dict[str, Any]] = {}
    for item_id, currency, amount, limit, start_time, end_time, currency_id in rows:
        parts = []
        if currency:
            parts.append(f"{currency} ×{amount}")
        elif amount:
            parts.append(str(amount))
        if limit:
            parts.append(f"限购 {limit} 次")
        if end_time and start_time and int(start_time) <= now <= int(end_time):
            parts.append("限时折扣")
        if not parts:
            continue
        value = parts[0]
        if len(parts) > 1:
            value += "（" + "，".join(parts[1:]) + "）"
        prices[int(item_id)] = {
            "text": value,
            "currency_id": int(currency_id) if currency_id else 0,
            "currency_name": currency or "",
            "amount": amount,
        }
    return prices


async def _fetch_icons(ids, getter) -> dict[int, str]:
    """批量下载图标并转成 data URI；失败的条目直接跳过。"""
    wanted = {int(i) for i in ids if i}
    if not wanted:
        return {}
    fetched = await asyncio.gather(
        *(getter.get_bytes(str(iid)) for iid in wanted), return_exceptions=True
    )
    out: dict[int, str] = {}
    for iid, data in zip(wanted, fetched):
        if not isinstance(data, Exception) and data:
            out[int(iid)] = to_data_uri(data)
    return out


def _load_soulmark_icon_ids(session, pet_id: int, soulmark_ids) -> dict[int, int]:
    """魂印图标映射：soulmark_icon 表里 (soulmark_id, pet_id) → icon_id。

    直接用魂印 id 当图标 id 会贴错（比如萨特的魂印 1578 对应图标 1554，
    1578 那张是别的技能图标）。
    """
    ids = {int(i) for i in soulmark_ids if i}
    if session is None or not ids:
        return {}
    placeholders = ",".join(f":s{i}" for i in range(len(ids)))
    params = {f"s{i}": value for i, value in enumerate(sorted(ids))}
    params["pid"] = pet_id
    try:
        rows = session.connection().execute(
            text(
                "select soulmark_id, icon_id, pet_id from soulmark_icon "
                f"where soulmark_id in ({placeholders}) "
                "and icon_id is not null and icon_id > 0"
            ),
            params,
        ).fetchall()
    except Exception as e:
        logger.debug(f"魂印图标映射查询失败（数据源可能不含该表）: {e}")
        return {}

    mapping: dict[int, int] = {}
    for soulmark_id, icon_id, row_pet_id in rows:
        key = int(soulmark_id)
        if key in mapping and int(row_pet_id or 0) != pet_id:
            continue
        mapping[key] = int(icon_id)
    return mapping


async def _fetch_icons_by_key(keys, getter) -> dict[str, str]:
    """按字符串 key 批量取图；取不到就跳过（不塞占位图）。"""
    wanted = {str(k).strip() for k in keys if str(k).strip()}
    if not wanted:
        return {}
    fetched = await asyncio.gather(
        *(getter.get_bytes(key) for key in wanted), return_exceptions=True
    )
    out: dict[str, str] = {}
    for key, data in zip(wanted, fetched):
        if not isinstance(data, Exception) and data:
            out[key] = to_data_uri(data)
    return out


def _apply_glossaries(
    pet: PetORM,
    soulmarks: list[dict[str, Any]],
    skills: list[dict[str, Any]],
) -> None:
    entries = {
        entry.name: entry
        for entry in getattr(pet, "glossary_entry", []) or []
        if getattr(entry, "name", None)
    }
    colors: dict[str, str] = {}
    seen: set[str] = set()

    def color_for(name: str) -> str:
        if name not in colors:
            colors[name] = _TERM_COLORS[len(colors) % len(_TERM_COLORS)]
        return colors[name]

    def terms_for(parser: AnalyzeDescParser) -> list[str]:
        return list(
            dict.fromkeys(
                segment.text
                for segment in parser.segments_by_color(_TERM_COLOR)
                if segment.text in entries
            )
        )

    def styles(text: str) -> str:
        name = html.unescape(text)
        return f'<b style="color:{color_for(name)}">{text}</b>'

    def process(item: dict[str, Any]) -> None:
        parser = item.pop("_parser", None)
        if parser is None:
            return
        names = terms_for(parser)
        item["desc"] = parser.to_html({"#f35555": styles})
        item["glossaries"] = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            entry = entries[name]
            item["glossaries"].append(
                {
                    "name": name,
                    "desc": getattr(entry, "desc", ""),
                    "color": color_for(name),
                }
            )

    for soulmark in soulmarks:
        process(soulmark)
    for skill in skills:
        for effect in skill.get("effects", []):
            parser = effect.pop("_parser", None)
            if parser is None:
                continue
            names = terms_for(parser)
            effect["info"] = parser.to_html({"#f35555": styles})
            skill.setdefault("glossaries", [])
            for name in names:
                if name in seen:
                    continue
                seen.add(name)
                entry = entries[name]
                skill["glossaries"].append(
                    {
                        "name": name,
                        "desc": getattr(entry, "desc", ""),
                        "color": color_for(name),
                    }
                )


async def _build_pet_render_data(pet: PetORM) -> dict[str, Any]:
    pet_name = getattr(pet, "name", "未知精灵")
    pet_id = getattr(pet, "id", 0)

    gender_id = 0
    try:
        if hasattr(pet, "gender") and pet.gender:
            if isinstance(pet.gender, int):
                gender_id = pet.gender
            elif hasattr(pet.gender, "id"):
                gender_id = pet.gender.id
            elif hasattr(pet.gender, "value"):
                gender_id = int(pet.gender.value)
    except Exception:
        gender_id = 0

    pet_gender_icon = ""
    try:
        plugin_dir = Path(__file__).parent.parent
        icon_path = plugin_dir / "templates" / "pet_info" / "images" / f"{gender_id}.png"
        if icon_path.exists():
            pet_gender_icon = to_data_uri(icon_path.read_bytes())
    except Exception as e:
        logger.error(f"获取性别图标失败: {e}")

    pet_type_id = (
        pet.type.id if hasattr(pet, "type") and getattr(pet.type, "id", None) is not None else 0
    )
    pet_type_name = pet.type.name if hasattr(pet, "type") else ""

    all_skills = []
    if hasattr(pet, "skill_links") and pet.skill_links:
        for sl in pet.skill_links:
            all_skills.extend(_extract_skill(sl))

    soulmarks = _extract_soulmark(getattr(pet, "soulmark", []) or [], pet)
    if pet_id == 2500:
        soulmarks.append(
            {
                "desc": "登场首回合所有攻击先制+1同时增加20%暴击率",
                "intensified": True,
                "is_adv": False,
                "pve_effective": None,
                "tags": [],
                "glossaries": [],
            }
        )

    _apply_glossaries(pet, soulmarks, all_skills)

    session = object_session(pet)
    special_effects = _load_special_effects(session, pet_id)
    activation_prices = _load_exchange_prices(
        session, [s.get("activation_item_id") for s in all_skills]
    )

    # 三类图标：道具（激活道具/货币）、魂印、专属效果（后两者共用 effecticon 资源）
    icon_ids = {
        int(entry["currency_id"])
        for entry in activation_prices.values()
        if entry.get("currency_id")
    } | {
        int(skill["activation_item_id"])
        for skill in all_skills
        if skill.get("activation_item_id")
    }
    icons = await _fetch_icons(icon_ids, ItemImageGetter)

    soulmark_icon_ids = _load_soulmark_icon_ids(
        session, pet_id, {sm.get("id") for sm in soulmarks}
    )
    soulmark_icons = await _fetch_icons(soulmark_icon_ids.values(), SoulmarkIconGetter)
    for sm in soulmarks:
        sid = sm.get("id")
        if sid:
            icon_id = soulmark_icon_ids.get(int(sid))
            sm["icon"] = soulmark_icons.get(int(icon_id), "") if icon_id else ""

    effect_keys = {
        fx["icon_key"] or str(fx["status_id"])
        for fx in special_effects
        if fx.get("icon_key") or fx.get("status_id")
    }
    effect_icons = await _fetch_icons_by_key(effect_keys, SignBuffImageGetter)
    for fx in special_effects:
        key = fx.get("icon_key") or (str(fx["status_id"]) if fx.get("status_id") else "")
        if key:
            fx["icon"] = effect_icons.get(key, "")

    for skill in all_skills:
        item_id = skill.get("activation_item_id")
        if item_id:
            skill["activation_item_icon"] = icons.get(int(item_id), "")
        entry = activation_prices.get(int(item_id)) if item_id else None
        if entry:
            skill["activation_price"] = entry["text"]
            skill["activation_price_icon"] = icons.get(
                int(entry.get("currency_id") or 0), ""
            )

    fifth_skills = [s for s in all_skills if s.get("is_fifth")][::-1]
    advanced_skills = [s for s in all_skills if s.get("is_advanced")][::-1]
    special_skills = [s for s in all_skills if s.get("is_special")][::-1]
    level_skills = sorted(
        [
            s
            for s in all_skills
            if not s.get("is_fifth") and not s.get("is_advanced") and not s.get("is_special")
        ],
        key=lambda x: x.get("learning_level") or 0,
        reverse=True,
    )

    skill_ids = [sl.skill_id for sl in pet.skill_links] if pet.skill_links else []
    pet_skill_names = {s["name"] for s in all_skills}
    type_ids = list(
        {skill["type_id"] for skill in all_skills if skill.get("type_id")} | {pet_type_id}
    )

    mintmarks = []
    if session:
        try:
            stmt = (
                select(MintmarkORM)
                .outerjoin(
                    SkillMintmarkLink,
                    col(SkillMintmarkLink.mintmark_id) == col(MintmarkORM.id),
                )
                .outerjoin(
                    PetMintmarkLink,
                    col(PetMintmarkLink.mintmark_id) == col(MintmarkORM.id),
                )
                .where(
                    col(SkillMintmarkLink.skill_id).in_(skill_ids)
                    | (col(PetMintmarkLink.pet_id) == pet_id)
                )
                .where(
                    col(PetMintmarkLink.pet_id).is_(None) | (col(PetMintmarkLink.pet_id) == pet_id)
                )
                .distinct()
            )
            mintmarks = session.execute(stmt).scalars().all()
        except Exception as e:
            logger.error(f"查询刻印数据失败: {e}")

    try:
        resource_id = getattr(pet, "resource_id", None)
        res_str = str(resource_id) if resource_id else None

        pet_head_task = (
            PetHeadImageGetter.get_bytes(res_str) if res_str else asyncio.sleep(0, result=b"")
        )
        pet_body_task = (
            PetBodyImageGetter.get_bytes(res_str) if res_str else asyncio.sleep(0, result=b"")
        )

        gather_results = await asyncio.gather(
            pet_head_task,
            pet_body_task,
            *(ElementTypeImageGetter.get_bytes(str(tid)) for tid in type_ids),
            ElementTypeImageGetter.get_bytes("prop"),
            *(MintmarkIconGetter.get_bytes(str(mm.id)) for mm in mintmarks),
            return_exceptions=True,
        )

        pet_head_bytes, pet_body_bytes = gather_results[0], gather_results[1]
        type_icon_count = len(type_ids) + 1
        type_icon_results = gather_results[2 : 2 + type_icon_count]
        mm_icon_results = gather_results[2 + type_icon_count :]

        pet_head_img = "" if isinstance(pet_head_bytes, Exception) else to_data_uri(pet_head_bytes)
        if isinstance(pet_body_bytes, Exception):
            pet_body_img = ""
        else:
            try:
                pet_body_img = to_data_uri(flip_image_horizontal(pet_body_bytes))
            except Exception as e:
                logger.warning(f"水平翻转精灵立绘失败，使用原图: {e}")
                pet_body_img = to_data_uri(pet_body_bytes)

        type_icons = {}
        for i, tid in enumerate(type_ids):
            if not isinstance(type_icon_results[i], Exception):
                type_icons[str(tid)] = to_data_uri(type_icon_results[i])
        if not isinstance(type_icon_results[-1], Exception):
            type_icons["prop"] = to_data_uri(type_icon_results[-1])

        skill_marks = []
        for mm, icon_result in zip(mintmarks, mm_icon_results, strict=True):
            icon_uri = "" if isinstance(icon_result, Exception) else to_data_uri(icon_result)
            skill_marks.append(
                {
                    "id": mm.id,
                    "name": mm.name,
                    "desc": getattr(mm, "desc", ""),
                    "icon": icon_uri,
                    "skills": list(
                        dict.fromkeys(s.name for s in mm.skill if s.name in pet_skill_names)
                    ),
                }
            )
    except Exception as e:
        logger.error(f"获取精灵图标失败: {e}")
        pet_head_img = ""
        pet_body_img = ""
        type_icons = {}
        skill_marks = []

    stats = {}
    if hasattr(pet, "base_stats") and pet.base_stats:
        if hasattr(pet.base_stats, "to_model"):
            base_stats_model = pet.base_stats.to_model()
            if hasattr(base_stats_model, "round"):
                base_stats_model = base_stats_model.round()
            stats = base_stats_model.model_dump()

    advance_stats = None
    if hasattr(pet, "advance") and pet.advance:
        if hasattr(pet.advance.base_stats, "to_model"):
            adv_model = pet.advance.base_stats.to_model()
            if hasattr(adv_model, "round"):
                adv_model = adv_model.round()
            advance_stats = adv_model.model_dump()

    encyclopedia = getattr(pet, "encyclopedia", None)
    pet_introduction = getattr(encyclopedia, "introduction", None)
    pet_height = (
        f"{encyclopedia.height:g}cm"
        if encyclopedia is not None and getattr(encyclopedia, "height", None) is not None
        else "未知"
        if encyclopedia is not None
        else None
    )
    pet_weight = (
        f"{encyclopedia.weight:g}kg"
        if encyclopedia is not None and getattr(encyclopedia, "weight", None) is not None
        else "未知"
        if encyclopedia is not None
        else None
    )

    render_data = {
        "pet_name": pet_name,
        "pet_id": pet_id,
        "pet_gender_id": gender_id,
        "pet_gender_icon": pet_gender_icon,
        "pet_type_id": str(pet_type_id) if pet_type_id else "0",
        "pet_type_name": pet_type_name,
        "pet_introduction": pet_introduction,
        "pet_height": pet_height,
        "pet_weight": pet_weight,
        "pet_food": getattr(encyclopedia, "food", None),
        "pet_head_img": pet_head_img,
        "pet_body_img": pet_body_img,
        "type_icons": type_icons,
        "stats": stats,
        "advance_stats": advance_stats,
        "soulmarks": soulmarks,
        "special_effects": special_effects,
        "fifth_skills": fifth_skills,
        "advanced_skills": advanced_skills,
        "special_skills": special_skills,
        "level_skills": level_skills,
        "skill_marks": skill_marks,
    }

    return render_data


async def render_pet_info_data(pet: PetORM) -> dict[str, Any]:
    return await _build_pet_render_data(pet)
