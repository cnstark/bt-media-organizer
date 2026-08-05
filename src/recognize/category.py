"""类别规则引擎:完全对齐 MoviePilot `config/category.yaml`。

规则语义(与 MP 一致):
- 一级 movie/tv 固定;二级名称即目录名,按先后顺序匹配,命中即止
- 一个规则内多个条件需同时满足(AND)
- 一个条件多个值用 `,` 分隔(OR);`!` 前缀表示排除该值
- release_year 支持范围,如 `2010-2020`
- 无条件的规则作为兜底(如 外语电影 / 未分类)
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .tmdb import MediaInfo

# 条件字段 → (取值函数, 比较方式)
def _int_list(value):
    return [int(v) for v in value.split(",") if v.strip().lstrip("!").strip().isdigit()]


def _str_set(value):
    return {v.strip().lstrip("!").strip().lower() for v in value.split(",") if v.strip()}


def _match_condition(media: MediaInfo, field: str, raw_value: str) -> bool:
    """单条件匹配:多值 OR,`!` 排除;条件不满足返回 False。"""
    negate = raw_value.lstrip().startswith("!")
    value = raw_value.lstrip().lstrip("!")
    vals = [v.strip() for v in value.split(",") if v.strip()]

    if field == "genre_ids":
        media_vals = set(media.genre_ids or [])
        cond_vals = set()
        for v in vals:
            if v.isdigit():
                cond_vals.add(int(v))
        hit = bool(media_vals & cond_vals)
    elif field in ("origin_country", "production_countries"):
        media_vals = {c.upper() for c in (media.origin_country or media.production_countries or [])}
        cond_vals = {v.upper() for v in vals}
        hit = bool(media_vals & cond_vals)
    elif field == "original_language":
        media_vals = {media.original_language.lower()} if media.original_language else set()
        cond_vals = {v.lower() for v in vals}
        hit = bool(media_vals & cond_vals)
    elif field == "release_year":
        year = media.year
        if year is None:
            hit = False
        else:
            hit = False
            for v in vals:
                if "-" in v:
                    try:
                        lo, hi = v.split("-", 1)
                        if int(lo) <= year <= int(hi):
                            hit = True
                            break
                    except ValueError:
                        continue
                elif v.isdigit() and int(v) == year:
                    hit = True
                    break
    else:
        # TMDB 详情其它一级字段:整型/字符串比较,列表取交集
        mv = getattr(media, field, None)
        if isinstance(mv, (list, tuple)):
            mv = {str(x).lower() for x in mv}
            hit = bool(mv & {v.lower() for v in vals})
        elif mv is not None:
            hit = str(mv).lower() in {v.lower() for v in vals}
        else:
            hit = False

    return (not hit) if negate else hit


def _match_rule(media: MediaInfo, rule: Tuple[str, dict]) -> bool:
    """规则匹配:所有条件同时满足;无条件规则恒真(兜底)。"""
    name, conditions = rule
    if not conditions:
        return True
    return all(_match_condition(media, field, value) for field, value in conditions.items())


def match_category(media: MediaInfo, rules: List[Tuple[str, dict]]) -> Optional[str]:
    """按规则顺序匹配,命中即返回目录名;全部未命中返回 None。"""
    if not media:
        return None
    for rule in rules:
        if _match_rule(media, rule):
            return rule[0]
    return None


def parse_rules(raw: dict) -> List[Tuple[str, dict]]:
    """把 MP 格式的 {分类名: {条件: 值}} 转为有序规则列表。"""
    rules: List[Tuple[str, dict]] = []
    for name, conditions in (raw or {}).items():
        if isinstance(conditions, dict):
            rules.append((str(name), {str(k): str(v) for k, v in conditions.items()}))
    return rules


# ---------------------------------------------------------------- 内置默认(MP 官方 category.yaml)

DEFAULT_MOVIE_RULES: List[Tuple[str, dict]] = [
    ("动画电影", {"genre_ids": "16"}),
    ("华语电影", {"original_language": "zh,cn,bo,za"}),
    ("外语电影", {}),
]

DEFAULT_TV_RULES: List[Tuple[str, dict]] = [
    ("国漫", {"genre_ids": "16", "origin_country": "CN,TW,HK"}),
    ("日番", {"genre_ids": "16", "origin_country": "JP"}),
    ("纪录片", {"genre_ids": "99"}),
    ("儿童", {"genre_ids": "10762"}),
    ("综艺", {"genre_ids": "10764,10767"}),
    ("国产剧", {"origin_country": "CN,TW,HK"}),
    ("欧美剧", {"origin_country": "US,FR,GB,DE,ES,IT,NL,PT,RU,UK"}),
    ("日韩剧", {"origin_country": "JP,KP,KR,TH,IN,SG"}),
    ("未分类", {}),
]
