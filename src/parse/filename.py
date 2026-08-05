"""文件名解析(纯正则,参考 MoviePilot app/core/meta/metavideo.py 精简实现)。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------- 正则

# 年份:独立成词,如 (2026) / .2026. / 2026
_YEAR_RE = re.compile(r"(?:^|[.\[(\s-])(19\d{2}|20\d{2})(?:$|[.\])\s-])")
# 季:Season 1 / S01 / 第2季(英文 S 后必须是数字边界,避免 S01E02 里重复匹配)
_SEASON_RE = re.compile(r"[Ss]eason[\s._-]*(\d{1,2})|(?:^|[.\s_-])S(\d{1,2})(?!\d)|第\s*(\d{1,3})\s*季|第\s*([零一二两三四五六七八九十百]+)\s*季")
# 集:S01E02 / S01E02-E03 / E02 / EP02 / 第2集
_EPISODE_RE = re.compile(
    r"(?:^|[.\s_-])S\d{1,2}E(\d{1,3})(?:-?E?(\d{1,3}))?"
    r"|(?:^|[.\s_-])EP?0*(\d{1,3})(?!\d)"
    r"|第\s*(\d{1,3})\s*[集话]"
    r"|第\s*([零一二两三四五六七八九十]+)\s*[集话]"
)
# 分辨率
_RES_RE = re.compile(r"\b(2160p|1080p|720p|480p|4k|uhd)\b", re.I)
# 视频编码
_VIDEO_RE = re.compile(r"\b(x264|h264|avc|hevc|x265|h265|mpeg4|mpeg2|vc-?1)\b", re.I)
# 音频编码
_AUDIO_RE = re.compile(
    r"\b(truehd|atmos|dts-?hd(?:ma)?|dts|ac3|eac3|aac|flac|pcm|lpcm|dd5|ddp5\.1|dd5\.1|5\.1|7\.1)\b", re.I
)
# 来源
_SOURCE_RE = re.compile(
    r"\b(blu-?ray|remux|web-?dl|webrip|hdtv|uhdtv|dvdrip|bdrip|bdiso|uhdbluray|h265)\b", re.I
)
# Part/CD/Disc
_PART_RE = re.compile(r"(?:^|[.\s_-])(part|pt|cd|disc|disk)[.\s_-]?(\d{1,2})(?:$|[.\s_-])", re.I)
# 资源组:末尾 -XXX(至少 3 字符,排除纯年份与 Part/CD 类、WEB-DL 等来源后缀)
_GROUP_RE = re.compile(r"-(?!\d{4}$)([A-Za-z0-9]{3,})$")
# 不可能是资源组的来源类后缀
_NON_GROUP_SUFFIX = {"dl", "web", "rip", "hdtv", "remux"}
# Part/CD/Disc 类后缀(用于排除在资源组之外)
_PART_LIKE_RE = re.compile(r"(?i)(part|pt|cd|disc|disk)\d*$")
# 末尾 Part/CD/Disc:如 x264-PART1 / Movie.CD1 / Movie.Disc 1
_PART_TAIL_RE = re.compile(r"[.\s_-]((?:part|pt|cd|disc|disk)[.\s_-]?)(\d{1,2})$", re.I)
# 噪声词(版本/画质说明,不进入标题)
_NOISE_RE = re.compile(r"\b(dv|hdr10\+?|hdr|sdr|dolby\s?vision|10bit|8bit|dovi|hlg)\b|\b\d{2,3}fps\b", re.I)
# 季/集单词(单独成 token 时丢弃)
_SE_EXTRA_WORDS = {"season", "ep", "episode", "episodes", "e", "s", "集", "季", "话"}
# 内嵌 tmdbid 等媒体 ID:{tmdbid=12345} / {mediaid=123}
_MEDIAID_RE = re.compile(r"\{[a-zA-Z]+=\d+\}")
# 括号处理(对齐 MoviePilot metavideo.py):
# - 首个 [中文] 或 [发布组] 括号:非英文发布名格式则整个剥掉
# - 括号内为英文发布名(含年份+资源类型)时保留内容去括号
_FIRST_BRACKET_RE = re.compile(r"^[\[【](.+?)[\]】]")
_BRACKET_DOT_TITLE_RE = re.compile(r"[A-Za-z]+\..+(?:19|20)\d{2}")
_BRACKET_RESOURCE_RE = re.compile(r"(?:2160|1080|720|480)[PIpi]|4K|UHD|Blu[\-. ]?ray|REMUX|WEB[\-. ]?DL|HDTV")

# 分隔符(把 . _ 空格 [] () 全部归一为空格)
_SPLIT_RE = re.compile(r"[._\s\[\](){}]+")

# ---------------------------------------------------------------- 语言标记(字幕)

_ZHCN_RES = [
    re.compile(r"(?:^|[.\[(\s-])(zh[-_]?cn|chs|chi|简中|简体|中字|国语)(?:$|[.\])\s-])", re.I),
    re.compile(r"简体中[文字]|中[文字]简体|简中字", re.I),
]
_ZHTW_RES = [
    re.compile(r"(?:^|[.\[(\s-])(zh[-_]?(tw|hk|hant)|cht|tc|繁中|繁体)(?:$|[.\])\s-])", re.I),
    re.compile(r"繁体中[文字]|中[文字]繁体|繁中字", re.I),
]
_JA_RES = [
    re.compile(r"(?:^|[.\[(\s-])(ja[-_]?jp|jpn|ja|日语|日語)(?:$|[.\])\s-])", re.I),
    re.compile(r"日本語|日語", re.I),
]
_ENG_RES = [re.compile(r"(?:^|[.\[(\s-])eng(?:$|[.\])\s-])", re.I)]


# ---------------------------------------------------------------- 数据结构

@dataclass
class ParsedMeta:
    """文件名解析结果。"""

    title: str = ""
    year: Optional[int] = None
    season: Optional[int] = None
    begin_episode: Optional[int] = None
    end_episode: Optional[int] = None
    part: Optional[str] = None
    resolution: Optional[str] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    source: Optional[str] = None
    group: Optional[str] = None
    raw_name: str = ""
    ext: str = ""
    tokens: List[str] = field(default_factory=list)

    @property
    def is_tv(self) -> bool:
        return self.season is not None or self.begin_episode is not None

    @property
    def season_episode(self) -> str:
        if self.season is None:
            return ""
        ep = f"E{self.begin_episode:02d}" if self.begin_episode is not None else ""
        return f"S{self.season:02d}{ep}"

    @property
    def quality(self) -> str:
        parts = [p for p in (self.resolution, self.source, self.video_codec) if p]
        return ".".join(parts) if parts else ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "year": self.year,
            "season": self.season,
            "begin_episode": self.begin_episode,
            "end_episode": self.end_episode,
            "part": self.part,
            "resolution": self.resolution,
            "source": self.source,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "group": self.group,
        }


# ---------------------------------------------------------------- 工具

def normalize_stem(stem: str) -> str:
    """归一化:去分隔符、小写、去资源组,用于主视频/附加文件归属匹配。"""
    s = _MEDIAID_RE.sub("", stem)
    s = _SPLIT_RE.sub("", s).lower()
    s = _GROUP_RE.sub("", s)
    return s


def strip_lang_tag(stem: str) -> str:
    """去掉字幕文件名中的语言标记(用于归属匹配)。"""
    for res in (*_ZHCN_RES, *_ZHTW_RES, *_JA_RES, *_ENG_RES):
        stem = res.sub("", stem)
    return stem


def subtitle_lang_tag(name: str) -> str:
    """识别字幕语言,返回后缀标记(如 .zh-cn),无识别结果返回空串。"""
    for res in _ZHTW_RES:
        if res.search(name):
            return ".zh-tw"
    for res in _ZHCN_RES:
        if res.search(name):
            return ".zh-cn"
    for res in _JA_RES:
        if res.search(name):
            return ".ja"
    for res in _ENG_RES:
        if res.search(name):
            return ".eng"
    return ""


def _find_year(text: str) -> Optional[int]:
    """取最后一个年份(标题内年份如《2001太空漫游》不干扰发行年份)。"""
    years = [int(y) for y in _YEAR_RE.findall(text)]
    return years[-1] if years else None


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(text: str) -> Optional[int]:
    """中文数字转整数,支持 一~九十九(如 四→4, 十二→12, 二十一→21)。"""
    if not text or any(c not in _CN_NUM for c in text):
        return None
    if text == "十":
        return 10
    if "十" in text:
        a, _, b = text.partition("十")
        tens = _CN_NUM[a] if a else 1
        ones = _CN_NUM[b] if b else 0
        return tens * 10 + ones
    return _CN_NUM[text]


def _find_season(text: str) -> Optional[int]:
    m = _SEASON_RE.search(text)
    if not m:
        return None
    for g in m.groups():
        if g is None:
            continue
        if g.isdigit():
            return int(g)
        cn = _cn_to_int(g)
        if cn is not None:
            return cn
    return None


def _find_episodes(text: str) -> tuple[Optional[int], Optional[int]]:
    m = _EPISODE_RE.search(text)
    if not m:
        return None, None
    groups = [g for g in m.groups() if g is not None]
    begin = int(groups[0]) if groups[0].isdigit() else _cn_to_int(groups[0])
    end = None
    if len(groups) > 1:
        end = int(groups[1]) if groups[1].isdigit() else _cn_to_int(groups[1])
    return begin, end


# ---------------------------------------------------------------- 主解析

def parse_filename(name: str) -> ParsedMeta:
    """
    解析文件名(含扩展名),如:
      Movie.2026.1080p.BluRay.x264-GROUP.mkv
      觉醒年代.2021.S01E01-E02.1080p.x265.mkv
      [SubGroup] Title [1080p][x265].mp4
    """
    path = Path(name)
    stem = _MEDIAID_RE.sub("", path.stem)
    ext = path.suffix

    meta = ParsedMeta(raw_name=name, ext=ext)

    # 首个括号处理(对齐 MP):括号内容非英文发布名格式时剥掉
    bracket = _FIRST_BRACKET_RE.match(stem)
    if bracket:
        bracket_content = bracket.group(1)
        if _BRACKET_DOT_TITLE_RE.search(bracket_content) \
                and _BRACKET_RESOURCE_RE.search(bracket_content):
            stem = bracket_content + stem[bracket.end():]
        else:
            stem = stem[bracket.end():]

    # 先提取末尾资源组/Part(避免与后续 token 分类粘连,如 x264-GROUP)
    gm = _GROUP_RE.search(stem)
    if gm and not _PART_LIKE_RE.match(gm.group(1)) \
            and gm.group(1).lower() not in _NON_GROUP_SUFFIX:
        meta.group = gm.group(1)
        stem = stem[: gm.start()]
    pm = _PART_TAIL_RE.search(stem)
    if pm:
        p1, p2 = pm.group(1).strip(". _-\t").lower(), pm.group(2)
        if p1 in ("part", "pt"):
            meta.part = f"Part{p2}"
        elif p1 == "cd":
            meta.part = f"CD{p2}"
        else:
            meta.part = f"{p1.capitalize()} {p2}"
        stem = stem[: pm.start()]

    meta.year = _find_year(stem)
    meta.season = _find_season(stem)
    meta.begin_episode, meta.end_episode = _find_episodes(stem)

    # 逐 token 分类
    tokens = [t for t in _SPLIT_RE.split(stem) if t]
    title_tokens: List[str] = []
    for token in tokens:
        low = token.lower()
        if _RES_RE.fullmatch(token):
            meta.resolution = "2160p" if low in ("4k", "uhd") else low
            continue
        if _VIDEO_RE.fullmatch(token):
            meta.video_codec = low
            continue
        if _AUDIO_RE.fullmatch(token):
            # 保留首个音频编码(如 TrueHD.7.1.Atmos 取 TrueHD)
            if meta.audio_codec is None:
                meta.audio_codec = low
            continue
        if _SOURCE_RE.fullmatch(token):
            src = low.replace("-", "")
            meta.source = {"bluray": "BluRay", "webdl": "Web-DL", "webrip": "WebRip",
                           "hdtv": "HDTV", "dvdrip": "DVDRip", "bdrip": "BDRip",
                           "bdiso": "BDISO", "uhdbluray": "UHDBluRay", "remux": "Remux",
                           "h265": "H265"}.get(src, token)
            continue
        m = _PART_RE.fullmatch(token)
        if m:
            p1, p2 = m.group(1).lower(), m.group(2)
            if p1 in ("part", "pt"):
                meta.part = f"Part{p2}"
            elif p1 == "cd":
                meta.part = f"CD{p2}"
            else:
                meta.part = f"{p1.capitalize()} {p2}"
            continue
        # 季/集 token(如 S01 / S01E02 / 第2集)已在正则阶段消耗,这里丢弃
        if _SEASON_RE.search(token) or _EPISODE_RE.search(token):
            continue
        # 语言标记 token(如 chs/cht/jpn/简中)丢弃
        if any(res.fullmatch(token) for res in (*_ZHCN_RES, *_ZHTW_RES, *_JA_RES, *_ENG_RES)):
            continue
        # 噪声词/季集单词丢弃
        if low in _SE_EXTRA_WORDS or _NOISE_RE.fullmatch(token):
            continue
        # 数字:单数字(声道 7.1 等)或等于检测年份的丢弃;其余保留(如片名 2001/1917/300)
        if token.isdigit():
            if len(token) == 1 or (meta.year is not None and int(token) == meta.year):
                continue
        title_tokens.append(token)

    meta.title = " ".join(title_tokens).strip() if title_tokens else stem
    meta.tokens = title_tokens
    return meta
