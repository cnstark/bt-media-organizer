"""重命名模板渲染(Jinja2,参照 MoviePilot RENAME_FORMAT + TemplateHelper)。

模板变量见设计文档 §2;目录结构由模板中的 "/" 生成相对路径。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from jinja2 import Template

from ..parse.filename import ParsedMeta
from ..recognize.tmdb import MediaInfo

# 非法字符(Windows/Linux 文件名均不允许)
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")


def sanitize(name: str) -> str:
    """清洗标题中的非法字符与多余空格(参照 MP __convert_invalid_characters)。"""
    name = _INVALID_CHARS.sub(" ", name)
    return _MULTI_SPACE.sub(" ", name).strip()


def build_context(
    meta: ParsedMeta,
    media: Optional[MediaInfo],
    s0_alias: List[str],
) -> dict:
    """构建 Jinja2 渲染上下文。"""
    title = (media.title if media and media.title else meta.title) or ""
    year = media.year if media and media.year else meta.year
    season = meta.season
    episode = meta.begin_episode

    # 第 0 季目录名:取别名
    season_dir = ""
    if season is not None:
        if season == 0:
            season_dir = s0_alias[0] if s0_alias else "Specials"
        else:
            season_dir = f"Season {season}"

    season_episode = ""
    if season is not None:
        ep = f"E{episode:02d}" if episode is not None else ""
        season_episode = f"S{season:02d}{ep}"

    return {
        # 标题类
        "title": sanitize(title),
        "original_title": sanitize((media.original_title if media else "") or ""),
        "year": year,
        "media_type": media.media_type if media else ("tv" if meta.is_tv else "movie"),
        "tmdb_id": media.tmdb_id if media else None,
        "category": (media.category if media else None) or "",
        # 季集
        "season": season,
        "season_dir": season_dir,
        "episode": episode,
        "episode_end": meta.end_episode,
        "season_episode": season_episode,
        # 版本
        "part": meta.part or "",
        "quality": meta.quality,
        "resolution": meta.resolution or "",
        "source": meta.source or "",
        "video_codec": meta.video_codec or "",
        "audio_codec": meta.audio_codec or "",
        "group": meta.group or "",
        # 文件
        "ext": meta.ext,
    }


def render_path(
    meta: ParsedMeta,
    media: Optional[MediaInfo],
    template: str,
    s0_alias: List[str],
) -> str:
    """渲染相对路径(如 'Movie (2026)/Movie (2026).mkv')。"""
    tpl = Template(template)
    rendered = tpl.render(build_context(meta, media, s0_alias))
    # 清洗路径中可能的非法字符,并合并连续分隔符
    parts = [sanitize(p) for p in Path(rendered).parts if p]
    return "/".join(parts)
