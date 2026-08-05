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
    category: Optional[str] = None
    renaming: bool = True
    monitor: bool = True
    overwrite_mode: Optional[str] = None
    min_filesize: Optional[int] = None
    exclude_words: List[str] = field(default_factory=list)


@dataclass
class DownloaderConf:
    name: str = "qb"
    type: str = "qbittorrent"
    url: str = "http://127.0.0.1:8080"
    username: str = ""
    password: str = ""
    poll_interval: int = 60
    tag: str = "已整理"
    webhook: bool = True


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
class Config:
    server: ServerConf = field(default_factory=ServerConf)
    engine: EngineConf = field(default_factory=EngineConf)
    directories: List[TransferDirConf] = field(default_factory=list)
    downloaders: List[DownloaderConf] = field(default_factory=list)
    recognize: RecognizeConf = field(default_factory=RecognizeConf)
    history: HistoryConf = field(default_factory=HistoryConf)
    log: LogConf = field(default_factory=LogConf)


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
    """加载 YAML 配置;环境变量 LITE_TOKEN 可覆盖 server.token。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    cfg = Config()
    cfg.server = _from_dict(ServerConf, raw.get("server"))
    if os.getenv("LITE_TOKEN"):
        cfg.server.token = os.getenv("LITE_TOKEN")

    engine_raw = raw.get("engine") or {}
    cfg.engine = _from_dict(EngineConf, engine_raw)
    cfg.engine.rename = _load_rename(engine_raw.get("rename"))

    cfg.directories = [_from_dict(TransferDirConf, d) for d in raw.get("directories") or []]
    cfg.downloaders = [_from_dict(DownloaderConf, d) for d in raw.get("downloaders") or []]
    cfg.recognize = _from_dict(RecognizeConf, raw.get("recognize"))
    cfg.recognize.tmdb = _from_dict(TmdbConf, (raw.get("recognize") or {}).get("tmdb"))
    cfg.history = _from_dict(HistoryConf, raw.get("history"))
    cfg.log = _from_dict(LogConf, raw.get("log"))

    _validate(cfg)
    return cfg


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
    for dl in cfg.downloaders:
        if dl.type != "qbittorrent":
            raise ValueError(f"暂不支持的下载器类型: {dl.type}(目前仅 qbittorrent)")
        if not dl.url:
            raise ValueError(f"下载器 [{dl.name}] 缺少 url")
    if cfg.recognize.tmdb.enabled and not cfg.recognize.tmdb.api_key:
        raise ValueError("recognize.tmdb.enabled=true 但未配置 api_key")
