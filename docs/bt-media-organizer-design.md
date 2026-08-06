# bt-media-organizer 详细设计文档 v1.1

> 对应需求:`bt-media-organizer-requirements.md`(决策已确认)。本文档为接口/数据/流程定稿,实现以此为据。
> v1.1(2026-08-06):按实现代码(`workspace/bt-media-organizer/`)校验补充——类别自动分类(category_folder/category_rules)、TMDB api_base/proxy、PUID/PGID 降权、模板上下文变量、接口签名与实际实现对齐、测试清单更新。

---

## 1. 技术选型

| 项 | 选择 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | 与参考实现同栈 |
| HTTP | 标准库 `http.server.ThreadingHTTPServer` | 零额外依赖,端点少 |
| 依赖 | `pyyaml` `jinja2` `httpx` | 仅 3 个 |
| 存储 | SQLite(WAL 模式) | 历史 + TMDB 缓存 |
| 下载器 | qBittorrent WebUI API v2(自研最小客户端) | 登录/列表/打标签/删种,约百行 |
| 部署 | Docker + 挂载 `/config`、`/data` | 见 Dockerfile |

## 2. 配置 Schema(定稿)

```yaml
server:
  host: 0.0.0.0
  port: 8900
  token: "change-me"            # API/webhook 鉴权(query token 或 X-Token 头)

engine:
  threads: 2                    # 单次整理内文件并发数
  rename:
    movie: "{{title}} ({{year}})/{{title}} ({{year}}){% if part %}-{{part}}{% endif %}{% if quality %} - {{quality}}{% endif %}{{ext}}"
    tv: "{{title}} ({{year}})/{{season_dir}}/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{{ext}}"
    s0_alias: ["Specials", "SPs"]   # 第 0 季目录名,取第一个
    subtitle_lang_tag: true     # 字幕文件追加语言标记
  default_overwrite: never      # never/always/size/latest
  min_filesize: 0               # MB,0=不限制
  exclude_words: []             # 全局屏蔽词(命中即跳过)
  media_exts: [".mkv", ".mp4", ".ts", ".iso", ".avi", ".wmv", ".rmvb", ".mov", ".m2ts"]
  subtitle_exts: [".srt", ".ass", ".ssa", ".sub", ".idx"]
  audio_exts: [".mka", ".flac", ".aac", ".dts", ".ac3", ".wav", ".eac3"]
  tmp_exts: [".part", ".download", ".!qb", ".torrent"]   # 下载中/临时后缀
  delete_empty_source_dirs: true  # move 模式下清理源空目录

directories:                    # 下载目录 → 媒体库映射,按顺序匹配,命中即止
  - name: "电影下载"
    download_path: "/data/downloads/movies"   # 源路径须是该目录或其子路径
    library_path: "/data/media"               # 目标根目录
    transfer_type: hardlink     # move/copy/hardlink/softlink
    media_type: movie           # movie/tv/all;all=不校验类型
    category: ~                 # 固定类别子目录(如"华语");留空不建
    category_folder: false      # true=按识别类别自动建子目录(未识别归"未分类")
    category_rules: {}          # MP 格式类别规则(如 {纪录片: {genre_ids: "99"}});留空用内置 MP 默认规则
    renaming: true              # false=保持原文件名直接转移
    monitor: true               # 参与事件/轮询匹配
    overwrite_mode: ~           # 覆盖全局
    min_filesize: ~             # 覆盖全局
    exclude_words: []           # 目录级屏蔽词,合并全局

downloaders:
  - name: qb
    type: qbittorrent
    url: "http://127.0.0.1:8080"
    username: "admin"
    password: "***"
    poll_interval: 60           # 秒;0=关闭轮询
    tag: "已整理"               # 完成标签(对账依据)
    webhook: true               # 启用 webhook 入口

recognize:
  tmdb:
    enabled: false
    api_key: ""
    language: "zh-CN"           # 标题语言(zh-CN 优先中文译名,translations 兜底)
    timeout: 10                 # 秒
    api_base: "https://api.themoviedb.org/3"  # 可换镜像/自建反代(如 DNS 污染场景)
    proxy: ""                   # 如 http://127.0.0.1:7890;留空则读系统 HTTPS_PROXY

history:
  db: "./data/organizer.db"
  keep_days: 365                # 0=永久

log:
  level: info
  file: "./data/organizer.log"  # 空=仅 stdout
```

模板变量(渲染上下文):

| 变量 | 来源 | 说明 |
|---|---|---|
| `title` | TMDB 命中→规范标题;否则文件名解析 | 已清洗非法字符 |
| `year` | 同上 | 缺失为空 |
| `season` / `season_dir` | 文件名解析 | season=数字;season_dir=S0 时取 s0_alias[0] 否则 `Season {n}` |
| `episode` / `episode_end` | 文件名解析 | `{:02d}` 补零 |
| `season_episode` | 解析 | `S01E02` / `S00E02` |
| `part` | 解析 | `Part1`/`CD2`/`Disc 1` 原文 |
| `quality` | 解析 | 分辨率.来源.编码,如 `1080p.BluRay.x264` |
| `resolution`/`source`/`video_codec`/`audio_codec`/`group` | 解析 | 独立字段 |
| `original_title` | TMDB | 原始标题(原名),未命中为空 |
| `media_type` | TMDB/解析 | `movie` / `tv` |
| `tmdb_id` | TMDB | 命中时的 TMDB ID,未命中为空 |
| `ext` | 源文件 | 含点,如 `.mkv` |

> 对齐 MP:`E01` 这类无季号剧集命名在渲染时默认按第 1 季处理(`season=1`);`season_dir` 在 S0 时取 `s0_alias[0]`,否则 `Season {n}`。

## 3. 数据模型(SQLite)

```sql
CREATE TABLE IF NOT EXISTS transfer_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_path TEXT NOT NULL,
  download_hash TEXT,
  downloader TEXT,
  target_path TEXT,
  meta_json TEXT,               -- {title, year, season, episode...}
  transfer_type TEXT,
  status TEXT NOT NULL,         -- success / failed
  message TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_hash ON transfer_history(download_hash, status);
CREATE INDEX IF NOT EXISTS idx_history_src  ON transfer_history(source_path);
CREATE INDEX IF NOT EXISTS idx_history_status ON transfer_history(status, created_at);

CREATE TABLE IF NOT EXISTS media_cache (     -- TMDB 识别缓存
  key TEXT PRIMARY KEY,          -- "movie|zh-CN|觉醒年代|2021"(键含语言,切换语言不命中旧缓存)
  json TEXT NOT NULL,            -- MediaInfo 序列化;未命中缓存空对象 {},避免反复请求
  created_at TEXT NOT NULL
);
```

幂等规则:
- 跳过条件:该 `source_path` 存在 `status=success` 记录(force 除外);无唯一索引,靠应用层检查 + 普通索引(失败记录会随重试多次写入)
- 批次完成判定:某 torrent 全部规划文件均 success(按 `download_hash` 统计失败数=0)→ 打标签;
  全部文件因幂等命中而跳过时同样打标签(避免轮询反复扫描同一任务)
- webhook/轮询前置检查:`success_count_by_hash>0 且 fail_count_by_hash==0` → 直接跳过
- 重整理(redo):删除旧记录 → force 重新执行

## 4. 模块接口(定稿)

### 4.1 `src/parse/filename.py`

```python
@dataclass
class ParsedMeta:
    title: str; year: Optional[int]; season: Optional[int]
    begin_episode: Optional[int]; end_episode: Optional[int]
    part: Optional[str]; resolution: Optional[str]
    video_codec: Optional[str]; audio_codec: Optional[str]
    source: Optional[str]; group: Optional[str]
    raw_name: str; ext: str
    tokens: List[str]            # 标题 token(供 TMDB 英文标题兜底搜索)
    # 属性: is_tv / season_episode / quality;方法: to_dict()(写历史 meta_json 用)

def parse_filename(name: str) -> ParsedMeta
def normalize_stem(stem: str) -> str          # 去分隔符小写,用于归属匹配
def strip_lang_tag(stem: str) -> str          # 去掉 chs/cht/jpn/简中 等语言标记
def subtitle_lang_tag(name: str) -> str       # 返回 .zh-cn/.zh-tw/.ja/.eng 或 ""
```

### 4.2 `src/storage/local.py`

```python
def ensure_dir(path: Path) -> None
def copy_file(src: Path, dest: Path) -> bool        # copyfile+保留mtime
def move_file(src: Path, dest: Path) -> bool        # 同盘rename,跨盘复制+删源
def hardlink(src: Path, dest: Path) -> bool         # .mp 临时名中转(原子)
def softlink(src: Path, dest: Path) -> bool
def delete_file(path: Path) -> None
def cleanup_empty_dirs(src_root: Path, stop_at: Path) -> None  # 从下往上删空目录,到 stop_at 为止
def is_same_disk(a: Path, b: Path) -> bool
```

### 4.3 `src/downloaders/base.py` + `qbittorrent.py`

```python
@dataclass
class TorrentInfo:
    hash: str; name: str; save_path: Path; content_path: Path
    category: str; tags: list[str]; size: int; state: str

@dataclass
class WebhookEvent:
    event: str; hash: str; name: str; save_path: Path; content_path: Path
    downloader: str = ""        # 适配器名(解析时由适配器填充)

class DownloaderAdapter(ABC):
    name: str
    def list_finished(self) -> list[TorrentInfo]       # 已完成任务(未过滤标签,由调用方过滤)
    def add_tag(self, hash: str) -> bool               # 打完成标签(对账依据)
    def delete_torrent(self, hash: str, delete_files: bool = True) -> bool  # 预留,默认不使用
    def parse_webhook(self, payload: dict) -> Optional[WebhookEvent]
    @staticmethod
    def has_tag(torrent: TorrentInfo, tag: str) -> bool  # tag in torrent.tags
```

`qbittorrent.py`:httpx 客户端,登录拿 SID cookie;`torrents/info?filter=completed`;`addTags`。webhook 归一化:`event==torrent_finished` 且 hash 非空。

### 4.4 `src/engine/namer.py`

```python
def render_path(meta: ParsedMeta, media: Optional[MediaInfo],
                template: str, s0_alias: list[str]) -> str   # 返回相对路径(含文件名)
def build_context(...) -> dict   # 上下文含 §2 全部变量 + original_title/media_type/tmdb_id
                               # 渲染后逐段 sanitize 并合并连续分隔符
                               # E01 无季号 → season 默认 1(对齐 MP season 属性)
def sanitize(name: str) -> str   # 替换 \/:*?"<>| 与连续空格
```

### 4.5 `src/engine/planner.py`

```python
@dataclass
class PlanItem:
    source: Path; kind: str        # main/subtitle/audio/bluray
    meta: Optional[ParsedMeta]     # main/bluray 有值
    related: Optional["PlanItem"]  # 附加文件归属的主视频

def is_bluray_dir(path: Path) -> bool
def plan(source: Path, conf: EngineConf) -> list[PlanItem]
```

规划顺序:主视频(含蓝光)→ 其同名附加文件 → 剩余附加文件;过滤(临时后缀/隐藏/屏蔽词/大小/扩展名)在收集时执行。

### 4.6 `src/engine/executor.py`

```python
@dataclass
class FileResult:
    success: bool; message: str; source: Path; target: Optional[Path]

def transfer_file(src: Path, dest: Path, transfer_type: str,
                  overwrite_mode: str, is_extra: bool) -> FileResult
def transfer_dir(src: Path, dest: Path, transfer_type: str,
                 overwrite_mode: str) -> FileResult      # 蓝光/目录递归
def delete_version_files(dest: Path) -> None             # latest 模式
```

覆盖决策:附加文件强制覆盖;主文件按策略;目标存在且状态可查时严格处理。

### 4.7 `src/engine/organizer.py`(核心)

```python
@dataclass
class OrganizeResult:
    total: int; success: int; failed: int; skipped: int
    all_success: bool; preview: bool; message: str
    items: list[dict]    # {source, target, success, message, kind}

class TransferEngine:
    def __init__(self, conf, store: HistoryStore)   # TMDB 识别器在内部构建
    def organize(self, source: Path, download_hash: str = None,
                 downloader: str = None, preview: bool = False,
                 force: bool = False, transfer_type: str = None,
                 target_path: Path = None) -> OrganizeResult   # 不抛异常,失败以 message 返回
    def on_webhook(self, payload: dict, downloader: str = None) -> OrganizeResult | None
    def poll_once(self, downloader: str = None) -> dict   # 对账一轮:{scanned, organized, skipped, failed}
    def redo(self, history_id: int) -> tuple[bool, str, OrganizeResult | None]
    def status(self) -> dict            # {processing, recent(最近20条), downloaders}
    def match_dir(self, source: Path) -> Optional[TransferDirConf]
    def close(self)                    # 关闭 TMDB/下载器 httpx 客户端
```

`organize()` 流程:目录匹配 → 规划 → 识别(文件名→可选TMDB,缓存)→ 幂等检查 → 计算全部目标路径 → 并发执行 → 写历史 → 全部成功则打标签/清理空目录。

失败自愈:失败文件不写 success → torrent 不打标签 → 下轮轮询重试(已有 success 的自动跳过)。

### 4.8 `src/api/server.py`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 免鉴权 |
| POST | `/api/v1/webhook?downloader=` | qB webhook 直入(需 token);非完成事件/已处理返回 `{"code":0,"message":"ignored"}` |
| POST | `/api/v1/transfer` | body `{path 或 hash, downloader?, preview?, force?, transfer_type?, target_path?}`;hash 经下载器解析为 content_path |
| GET | `/api/v1/history?status=&limit=&offset=` | 历史查询(limit 默认 50,上限 500) |
| POST | `/api/v1/history/{id}/redo` | 重整理(校验源仍存在,删旧记录后 force 重做) |
| GET | `/api/v1/queue` | 运行状态:`{processing, recent(最近20条), downloaders}` |
| POST | `/api/v1/poll` | 立即触发对账,body `{downloader?}`(缺省用第一个下载器) |

鉴权:token 匹配(`hmac.compare_digest`),来源 `?token=` 或 `X-Token` 头。

### 4.9 `src/main.py`

加载配置 → 初始化日志/历史/引擎 → 启动每下载器轮询线程(daemon)→ 启动 HTTP → 信号优雅退出(SIGINT/SIGTERM)。

- 环境变量:`BT_MEDIA_CONFIG`(配置文件路径,默认 `config.yaml`)、`BT_MEDIA_TOKEN`(覆盖 `server.token`)
- `PUID`/`PGID`(Docker 部署):root 启动时降权运行(参照 MP),值 ≤0 或非 root 启动则保持当前用户;Dockerfile 另预留 `SUPERUSER`(当前仅记录,通知未启用)
- 启动时执行历史 `purge()`(按 `history.keep_days` 清理过期记录)

## 5. 关键流程时序

### 5.1 触发(两条入口,共用同一幂等检查)

**入口 A:qB「下载完成后运行外部程序」→ `/api/v1/transfer`(推荐,秒级)**
```
qB 完成下载 → 外部程序 scripts/qb-notify.sh "%F"(sleep 3 等落盘)
  → POST /api/v1/transfer {path: content_path}(token 校验,幂等)
  → engine.organize(content_path) → 全部成功 → add_tag(hash) → 结束
```

**入口 B:qB WebUI Webhook → `/api/v1/webhook`(需 qB 配置 webhook,适配器字段大小写兼容)**
```
qB 完成下载 → POST {event:"torrent_finished"/"torrent_completed", hash, contentPath, ...}
  → /api/v1/webhook(token 校验,?downloader= 指定适配器)
  → engine.on_webhook():处理中集合+历史计数去重
  → organize(content_path, hash, downloader)
  → 全部成功 → add_tag(hash) → 结束;非完成事件/已处理 → {"code":0,"message":"ignored"}
```

> **路径一致性**:qB 与 bt-media-organizer 容器必须挂载相同路径,contentPath/脚本参数原样透传,不做宿主↔容器映射。`scripts/qb-notify.sh` 的 token 经环境变量 `BT_MEDIA_TOKEN` 或文件 `/config/bt-media-token` 注入,脚本不硬编码 token。

### 5.2 轮询对账
```
轮询线程每 N 秒:
  list_finished() → 过滤已带 tag / 处理中 / 已有成功历史
  → 逐个 organize(content_path)
  → 全部成功 → add_tag(hash)
```

### 5.3 organize() 内部
```
1. 目录匹配:源路径前缀命中 directories[].download_path(monitor=true),取首个
   (若指定 target_path 则跳过匹配,直接使用)
2. plan():展开/过滤/排序(蓝光原盘整体;单文件补齐同级附加文件)
3. 类型校验:配置 media_type=movie/tv 时,附加文件跟随其主视频判定;TMDB 识别类型与配置不一致 → 跳过
4. 识别:主视频 parse_filename → TMDB(可选,SQLite 缓存)→ MediaInfo
5. 幂等:history 有 success 记录 → 跳过(force 除外)
6. 目标路径:namer.render(模板) 挂在 library_path(+category 固定目录 / category_folder 识别类别目录)
   附加文件目标 = 其主视频目标同级 + 主视频新 stem + 语言标记 + 原后缀;
   孤儿附加文件(未匹配到主视频)独立解析按模板渲染,类别归"未分类"
7. 执行:ThreadPoolExecutor(engine.threads) 并发 transfer_file/transfer_dir
8. 历史:逐文件写 success/failed
9. 收尾:全部成功(或全部幂等跳过)→ add_tag;move 模式 → cleanup_empty_dirs
```

## 6. 边界与异常处理

- 识别失败:仍按文件名结果整理(TMDB 仅为增强,不阻断)
- 目标目录获取失败/源不存在:整批失败,记录 message,不打标签 → 轮询重试
- 并发安全:引擎级 `processing_hashes` 集合(带锁)防同 hash 并发;历史幂等为最终防线
- 软链接/硬链接目标已存在:同覆盖策略;附加文件强制覆盖
- 中文/特殊字符标题:命名 sanitize(替换 `\/:*?"<>|` 与连续空格)
- qB 未登录/超时:轮询异常捕获,记日志,下轮重试;qB 5.x 白名单/免登录来源 login 返回 204 视为登录成功
- 孤儿附加文件:未匹配到主视频时独立解析,仍无标题则丢弃
- 日志:RotatingFileHandler 10MB×5,文件目录自动创建;不可写时降级仅 stdout

## 7. 测试计划

- `tests/test_filename.py`:标题/年份/季集(含中文数字)/Part/版本/资源组/中文剧集/蓝光目录名/字幕语言标记
- `tests/test_planner.py`:单文件+同级附加文件、目录递归、蓝光原盘、过滤规则
- `tests/test_namer.py`:模板渲染、S0 别名、非法字符清洗
- `tests/test_tmdb.py`:TMDB 识别/打分选优/本地化标题(mock 请求)
- `tests/test_category.py` / `test_category_integration.py`:类别规则(对齐 MP category.yaml 语义)
- `tests/test_integration.py`:真实文件整理/幂等/redo 端到端(不依赖 qB 网络)
