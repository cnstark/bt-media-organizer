"""配置加载与校验(YAML → dataclass)。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


# ---------------------------------------------------------------- 配置类

@dataclass
class ServerConf:
    host: str = "0.0.0.0"
    port: int = 8900
    token: str = ""


@dataclass
class RenameConf:
    movie: str = (
        "{{title}}{% if year %} ({{year}}){% endif %}"
        "/{{title}}{% if year %} ({{year}}){% endif %}"
        "{% if part %}-{{part}}{% endif %}"
        "{% if quality %} - {{quality}}{% endif %}{{ext}}"
    )
    tv: str = (
        "{{title}}{% if year %} ({{year}}){% endif %}"
        "/{{season_dir}}/{{title}} - {{season_episode}}"
        "{% if part %}-{{part}}{% endif %}{{ext}}"
    )
    s0_alias: List[str] = field(default_factory=lambda: ["Specials", "SPs"])
    subtitle_lang_tag: bool = True


@dataclass
class EngineConf:
    threads: int = 2
    rename: RenameConf = field(default_factory=RenameConf)
    default_overwrite: str = "never"
    min_filesize: int = 0
    exclude_words: List[str] = field(default_factory=list)
    media_exts: List[str] = field(default_factory=lambda: [
        ".mkv", ".mp4", ".ts", ".iso", ".avi", ".wmv", ".rmvb", ".mov", ".m2ts"])
    subtitle_exts: List[str] = field(default_factory=lambda: [
        ".srt", ".ass", ".ssa", ".sub", ".idx"])
    audio_exts: List[str] = field(default_factory=lambda: [
        ".mka", ".flac", ".aac", ".dts", ".ac3", ".wav", ".eac3"])
    tmp_exts: List[str] = field(default_factory=lambda: [
        ".part", ".download", ".!qb", ".torrent"])
    delete_empty_source_dirs: bool = True

    @property
    def all_exts(self) -> List[str]:
        return self.media_exts + self.subtitle_exts + self.audio_exts

    def is_tmp(self, path: Path) -> bool:
        return path.suffix.lower() in self.tmp_exts


@dataclass
class TransferDirConf:
    name: str = ""
    download_path: str = ""
    library_path: str = ""
    transfer_type: str = "hardlink"
    media_type: str = "all"          # movie / tv / all
    category: Optional[str] = None   # 固定类别子目录(如"华语");留空不建
    category_folder: bool = False    # 按识别类别自动建子目录(未识别归"未分类")
    category_rules: dict = field(default_factory=dict)  # MP 格式类别规则,留空用内置 MP 默认
    renaming: bool = True
    monitor: bool = True
    overwrite_mode: Optional[str] = None
    min_filesize: Optional[int] = None
    exclude_words: List[str] = field(default_factory=list)


@dataclass
class DownloaderConf:
    name: str = "qb"
    type: str = "qbittorrent"     # qbittorrent / transmission
    url: str = "http://127.0.0.1:8080"
    username: str = ""
    password: str = ""
    poll_interval: int = 60
    tag: str = "已整理"
    torrent_path: str = ""        # 种子目录(qB: BT_backup;TR: 兜底读取),转移功能需要


@dataclass
class TmdbConf:
    enabled: bool = False
    api_key: str = ""
    language: str = "zh-CN"
    timeout: int = 10
    api_base: str = "https://api.themoviedb.org/3"  # 可换镜像/代理,如自建反代
    proxy: str = ""  # 如 http://127.0.0.1:7890,留空则不设(仍会读系统 HTTPS_PROXY)


@dataclass
class RecognizeConf:
    tmdb: TmdbConf = field(default_factory=TmdbConf)


@dataclass
class HistoryConf:
    db: str = "./data/organizer.db"
    keep_days: int = 365


@dataclass
class LogConf:
    level: str = "info"
    file: str = "./data/organizer.log"


@dataclass
class PathRuleConf:
    """路径过滤/选择/转换(IYUU 语义)。"""
    convert_type: str = "eq"      # eq / sub / add / replace
    rules: List[tuple] = field(default_factory=list)        # [(源前缀, 目标前缀)]
    filter_paths: List[str] = field(default_factory=list)  # 排除(前缀匹配)
    selector_paths: List[str] = field(default_factory=list)  # 仅包含(前缀匹配)


@dataclass
class TransferConf:
    enabled: bool = False
    poll_interval: int = 300
    from_client: str = ""
    to_client: str = ""
    delete_source: bool = False
    auto_start: bool = True
    marker: str = "empty"         # empty / tag / category
    path: PathRuleConf = field(default_factory=PathRuleConf)


@dataclass
class JackettConf:
    url: str = ""
    api_key: str = ""
    indexers: List[str] = field(default_factory=list)      # 白名单,必须显式配置
    max_candidates: int = 20
    size_tolerance: float = 0.02
    per_indexer_delay: float = 2.0


@dataclass
class ReseedConf:
    enabled: bool = False
    poll_interval: int = 3600
    target_client: str = ""        # 注入目标下载器(必填,可选任意已配置下载器)
    auto_start: bool = False        # 默认暂停(校验后由用户开始)
    check_on_add: bool = False
    marker: str = "category"       # empty / tag / category
    matcher: str = "jackett"
    jackett: JackettConf = field(default_factory=JackettConf)
    exclude_paths: List[str] = field(default_factory=list)


@dataclass
class Config:
    server: ServerConf = field(default_factory=ServerConf)
    engine: EngineConf = field(default_factory=EngineConf)
    directories: List[TransferDirConf] = field(default_factory=list)
    downloaders: List[DownloaderConf] = field(default_factory=list)
    recognize: RecognizeConf = field(default_factory=RecognizeConf)
    history: HistoryConf = field(default_factory=HistoryConf)
    log: LogConf = field(default_factory=LogConf)
    transfer: TransferConf = field(default_factory=TransferConf)
    reseed: ReseedConf = field(default_factory=ReseedConf)


# ---------------------------------------------------------------- 加载

def _from_dict(cls, data: dict):
    """按 dataclass 字段过滤字典,缺省字段用默认值。"""
    if data is None:
        return cls()
    import dataclasses
    fields = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in fields})


def _load_rename(data: dict) -> RenameConf:
    conf = _from_dict(RenameConf, data)
    if data and "s0_alias" in data and not data.get("s0_alias"):
        conf.s0_alias = ["Specials", "SPs"]
    return conf


def load_config(path: str) -> Config:
    """加载 YAML 配置;环境变量 BT_MEDIA_TOKEN 可覆盖 server.token。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    cfg = Config()
    cfg.server = _from_dict(ServerConf, raw.get("server"))
    if os.getenv("BT_MEDIA_TOKEN"):
        cfg.server.token = os.getenv("BT_MEDIA_TOKEN")

    engine_raw = raw.get("engine") or {}
    cfg.engine = _from_dict(EngineConf, engine_raw)
    cfg.engine.rename = _load_rename(engine_raw.get("rename"))

    cfg.directories = [_from_dict(TransferDirConf, d) for d in raw.get("directories") or []]
    cfg.downloaders = [_from_dict(DownloaderConf, d) for d in raw.get("downloaders") or []]
    cfg.transfer = _load_transfer(raw.get("transfer"))
    cfg.reseed = _load_reseed(raw.get("reseed"))
    cfg.recognize = _from_dict(RecognizeConf, raw.get("recognize"))
    cfg.recognize.tmdb = _from_dict(TmdbConf, (raw.get("recognize") or {}).get("tmdb"))
    cfg.history = _from_dict(HistoryConf, raw.get("history"))
    cfg.log = _from_dict(LogConf, raw.get("log"))

    _validate(cfg)
    return cfg


def _load_transfer(data: dict) -> TransferConf:
    conf = _from_dict(TransferConf, data)
    if data:
        path_raw = data.get("path") or {}
        conf.path = _from_dict(PathRuleConf, path_raw)
        rules = path_raw.get("rules") or []
        if isinstance(rules, str):
            conf.path.rules = _parse_rule_text(rules)
        elif isinstance(rules, list):
            conf.path.rules = [(str(r[0]), str(r[1])) for r in rules if isinstance(r, (list, tuple)) and len(r) == 2]
    return conf


def _load_reseed(data: dict) -> ReseedConf:
    conf = _from_dict(ReseedConf, data)
    if data:
        conf.jackett = _from_dict(JackettConf, data.get("jackett"))
    return conf


def _parse_rule_text(text: str) -> List[tuple]:
    """多行规则文本 → [(源前缀, 目标前缀)];'#' 注释,空行跳过,分隔符 '{#**#}'。"""
    rules: List[tuple] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{#**#}" in line:
            parts = [p.strip() for p in line.split("{#**#}")]
            if len(parts) == 2 and parts[0]:
                rules.append((parts[0], parts[1]))
        elif line:
            rules.append((line, ""))  # 仅源前缀(用于 sub)
    return rules


def _validate(cfg: Config) -> None:
    if not cfg.server.token:
        raise ValueError("server.token 不能为空,请配置鉴权 token")
    for d in cfg.directories:
        if not d.download_path or not d.library_path:
            raise ValueError(f"目录配置 [{d.name}] 缺少 download_path 或 library_path")
        if d.transfer_type not in ("move", "copy", "hardlink", "softlink"):
            raise ValueError(f"目录 [{d.name}] transfer_type 非法: {d.transfer_type}")
        if d.media_type not in ("movie", "tv", "all"):
            raise ValueError(f"目录 [{d.name}] media_type 非法: {d.media_type}")
        if d.overwrite_mode and d.overwrite_mode not in ("never", "always", "size", "latest"):
            raise ValueError(f"目录 [{d.name}] overwrite_mode 非法: {d.overwrite_mode}")
    names = set()
    for dl in cfg.downloaders:
        if dl.type not in ("qbittorrent", "transmission"):
            raise ValueError(f"不支持的下载器类型: {dl.type}(支持 qbittorrent/transmission)")
        if not dl.url:
            raise ValueError(f"下载器 [{dl.name}] 缺少 url")
        if dl.name in names:
            raise ValueError(f"下载器名称重复: {dl.name}")
        names.add(dl.name)
    # 转移配置校验
    t = cfg.transfer
    if t.enabled:
        if not t.from_client or not t.to_client:
            raise ValueError("transfer.enabled=true 但未配置 from_client/to_client")
        if t.from_client == t.to_client:
            raise ValueError("transfer 的来源下载器和目标下载器不能相等")
        for key in (t.from_client, t.to_client):
            if key not in names:
                raise ValueError(f"transfer 引用了未配置的下载器: {key}")
        if t.marker not in ("empty", "tag", "category"):
            raise ValueError(f"transfer.marker 非法: {t.marker}")
        if t.path.convert_type not in ("eq", "sub", "add", "replace"):
            raise ValueError(f"transfer.path.convert_type 非法: {t.path.convert_type}")
        inter = set(t.path.filter_paths) & set(t.path.selector_paths)
        if inter:
            raise ValueError(f"transfer 过滤器与选择器存在交集: {','.join(sorted(inter))}")
    # 辅种配置校验
    r = cfg.reseed
    if r.enabled:
        if not r.target_client:
            raise ValueError("reseed.enabled=true 但未配置 target_client")
        if r.target_client not in names:
            raise ValueError(f"reseed.target_client 未配置的下载器: {r.target_client}")
        if r.matcher != "jackett":
            raise ValueError(f"reseed.matcher 暂不支持: {r.matcher}")
        if not r.jackett.url or not r.jackett.api_key:
            raise ValueError("reseed.enabled=true 但 jackett.url/api_key 未配置")
        if not r.jackett.indexers:
            raise ValueError("reseed 索引器白名单为空: 必须显式配置 jackett.indexers")
        if r.marker not in ("empty", "tag", "category"):
            raise ValueError(f"reseed.marker 非法: {r.marker}")
    if cfg.recognize.tmdb.enabled and not cfg.recognize.tmdb.api_key:
        raise ValueError("recognize.tmdb.enabled=true 但未配置 api_key")
