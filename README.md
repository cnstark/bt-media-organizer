# ptpilot

轻量级媒体管理服务，三个独立功能模块（各自独立开关，全部轮询驱动，无 webhook/事件依赖）：

1. **整理 organize**：下载完成后自动**识别 → 规范化重命名 → 转移**进媒体库（硬链接/移动等）
2. **转移 transfer**：把种子从 A 下载器搬到 B 下载器继续做种（路径映射/删源/标记可选）
3. **辅种 reseed**：用已有做种数据去 Jackett 索引的 PT 站匹配同源种子并注入做种（白名单/流控/严格同名匹配/记录管理）

参考 MoviePilot v2.15.4 与 IYUUPlus 实现思路精简而成。支持下载器：**qBittorrent / Transmission**。

## 特性

- ✅ YAML 配置整理规则(目录映射 / 整理方式 / 命名模板 / 过滤 / 覆盖策略)
- ✅ 文件名正则解析(标题/年份/季集/Part/版本/资源组),可选 TMDB 识别增强
- ✅ 四种转移方式:move / copy / hardlink / softlink
- ✅ 蓝光原盘整体整理、字幕/音频跟随主视频 + 语言标记(`.zh-cn` 等)
- ✅ 覆盖策略:never / always / size / latest;类别自动分类(可选)
- ✅ 整理:定期轮询下载器对账(「已整理」标签幂等,失败自愈)
- ✅ 转移:轮询扫描来源下载器做种 → 路径过滤/选择/转换(IYUU 语义 eq/sub/add/replace)
  → 种子文件导出或读盘 → 注入目标下载器(可配置删源/自动开始/标记);无记录表,幂等靠目标下载器状态
- ✅ 辅种:Jackett Torznab 匹配(白名单/严格同名/大小容差/文件级比对/流控防管控/跳过已覆盖站点)
  → 发布组匹配完立即注入;SQLite 记录管理(查看/删除/重试)
- ✅ SQLite 历史记录 + 幂等去重 + 失败自动重试(下轮轮询)
- ✅ 常驻服务 + Docker,HTTP API 无 Web UI(三模块状态/手动触发/记录管理)
- ✅ 配置热重载:监听 config.yaml 变更自动生效,也可 API 手动触发,无需重启

## 快速开始

```bash
cp config.example.yaml config.yaml   # 修改配置(详见下文「配置文件详解」)
pip install -r requirements.txt
python -m src.main --config config.yaml
# 环境变量: PTPILOT_CONFIG(配置路径) / PTPILOT_TOKEN(覆盖 server.token)
#           PTPILOT_WATCH_INTERVAL(配置热重载监听间隔秒数,默认 3;0=关闭监听)
```

或 Docker(部署版,参考 docker-compose.example.yml):
- 镜像: `ghcr.io/cnstark/ptpilot:latest`(GitHub Actions 打 tag 自动构建,多架构 amd64/arm64)
- 挂载 `./config.yaml:/config/config.yaml:ro`、`./data:/data`(数据库/日志持久化)
- 挂载媒体库目录(与下载器相同路径,见「路径一致性」)
- `PUID`/`PGID` 环境变量降权(默认 root)
- 本地开发调试(修改代码后先构建再启动): `./scripts/debug.sh`

### 镜像发布(GitHub Actions)

打 tag 自动构建并推送镜像到 GHCR(无需本地构建):

```bash
git tag v1.2.3          # 或 git tag -a v1.2.3 -m "..."
git push origin v1.2.3
```

- 触发条件: 推送 `v*` 标签(如 `v1.2.3`),也可在 Actions 页面手动运行
- 产物: `ghcr.io/cnstark/ptpilot` 的 `1.2.3` / `1.2` / `latest` 三个标签
- 部署机升级: `docker compose pull && docker compose up -d`;本地调试用 `./scripts/debug.sh`
- 工作流文件: `.github/workflows/docker-image.yml`

## 配置文件详解

> 完整示例见 `config.example.yaml`;所有模块 `enabled: false` 时功能关闭。

### server(服务)

```yaml
server:
  host: "0.0.0.0"
  port: 8900
  token: "change-me"      # API 鉴权 token,必填且务必修改(所有接口除 /health 外均需)
```

### organize(整理模块)

> 整理相关配置(引擎/目录映射/识别)统一放在 `organize` 层级下,
> 这是唯一写法(旧版平铺在顶层的布局不再支持)。

```yaml
organize:
  engine:
    threads: 2                        # 单次整理内文件并发数
    rename:                           # 命名模板(Jinja2)
      movie: "{{title}} ({{year}})/{{title}} ({{year}}){{ext}}"
      tv: "{{title}} ({{year}})/{{season_dir}}/{{title}} - {{season_episode}}{{ext}}"
      s0_alias: ["Specials", "SPs"]   # 第 0 季目录别名
      subtitle_lang_tag: true         # 字幕文件追加语言标记(.zh-cn)
    default_overwrite: never          # never / always / size / latest
    min_filesize: 0                   # MB,0=不限制
    exclude_words: []                 # 全局整理屏蔽词(路径命中即跳过)
    media_exts / subtitle_exts / audio_exts / tmp_exts:  # 文件类型表
    delete_empty_source_dirs: true    # move 模式整理完清理源空目录

  directories:                        # 下载目录→媒体库映射,按顺序匹配,命中即止
    - name: "电视剧"
      download_path: "/data/downloads/tv"   # 下载器保存路径(必须与下载器看到的路径一致)
      library_path: "/data/media"           # 媒体库目标路径
      transfer_type: hardlink               # move / copy / hardlink / softlink
      media_type: tv                        # movie / tv / all
      category: ~                           # 固定类别子目录(如 "华语");留空不建
      category_folder: true                 # 按识别类别自动建子目录(未识别归"未分类")
      category_rules: {}                    # 可选,MP 格式(如 {纪录片: {genre_ids: "99"}})
      renaming: true                        # false = 保持原文件名直接转移
      monitor: true                         # 参与轮询匹配
      overwrite_mode: ~ / min_filesize: ~ / exclude_words: []   # 目录级覆盖,留空继承全局

  recognize:
    tmdb:
      enabled: false            # 需 TMDB API key
      api_key: ""
      language: "zh-CN"
      timeout: 10
      api_base: "https://api.themoviedb.org/3"   # 可换镜像/自建反代
      proxy: ""                 # 如 http://127.0.0.1:7890;留空读系统 HTTPS_PROXY
```

### downloaders(下载器)

```yaml
downloaders:
  - name: qb                  # 下载器标识(转移/辅种按此引用)
    type: qbittorrent         # qbittorrent / transmission
    url: "http://127.0.0.1:8080"
    username: ""
    password: ""
    poll_interval: 60         # 整理轮询间隔(秒);0 = 不参与整理
    tag: "已整理"             # 整理完成标签(对账依据)
    torrent_path: ""          # 种子目录:qB=BT_backup 绝对路径;TR=种子文件兜底目录。
                              # 转移时 qB 优先走 API torrents/export(跨主机可用),读盘仅兜底;
                              # TR 作转移【源】时需该路径可读
```

### transfer(转移模块)

```yaml
transfer:
  enabled: true               # 模块开关
  poll_interval: 300          # 轮询间隔(秒);0 = 关闭
  from_client: qb             # 来源下载器(做种所在)
  to_client: tr               # 目标下载器;必须 ≠ from_client
  delete_source: false        # 转移成功后删除来源种子(只删种子不删数据);
                              # ⚠️ 风险项:仅在注入成功后删除,但首次使用请勿开启
  auto_start: true            # 转移后自动开始做种;false = 添加后暂停
  marker: tag                 # 标记规则: empty / tag / category(如 tag=已转移,TR 用标签,qB 用分类或标签)
  path:
    convert_type: eq          # 路径转换: eq(相等)/sub(减前缀)/add(加前缀)/replace(替换)
    rules: ""                 # 多行文本,每行 "源前缀{#**#}目标前缀";为空=eq
                              # 例: replace + "/downloads{#**#}/volume1/downloads"
    filter_paths: []          # 排除目录(前缀匹配),命中不转移
    selector_paths: []        # 仅转移命中目录;与 filter_paths 不得有交集
```

转移流程:轮询来源做种列表 → 目标已有则跳过(幂等,无记录表) → 路径过滤/转换 →
读取种子文件(qB: torrents/export 优先,BT_backup 兜底;TR: RPC torrentFile 或配置目录)→
注入目标(强制 qB autoTMM=false)→ 成功且 delete_source → 删源(不删数据)。
失败自动下轮重试;重转移 = 手动删除目标种子即可。

### reseed(辅种模块)

```yaml
reseed:
  enabled: true               # 模块开关
  poll_interval: 3600         # 轮询间隔(秒);0 = 关闭
  target_client: tr           # 注入目标下载器(可选任意已配置下载器)
  auto_start: false           # 注入后自动开始做种;默认 false = 暂停(校验后手动开始)
  check_on_add: false         # 注入后是否发 recheck 校验命令(qB)
  marker: category            # 标记规则: empty / tag / category(如 category=辅种)
  matcher: jackett            # 匹配源(当前仅 jackett)
  jackett:
    url: "http://127.0.0.1:9117"
    api_key: ""               # Jackett API key
    indexers: [...]           # 🔒 索引器白名单(参与辅种的站点,必须显式配置;空=不辅种)
    max_candidates: 20        # 每发布组最多候选数
    size_tolerance: 0.02      # 候选大小容差比例(±2%)
    # ---- 流控(防 PT 站管控) ----
    per_indexer_delay: 3.0    # 同站任意请求最小间隔(秒)
    per_minute: 8             # 同站每分钟请求上限(滑动窗口)
    global_interval: 1.0      # 全局(所有站合计)最小间隔(秒)
    cooldown_seconds: 120.0   # 站点失败(429/超时)后冷却(秒),冷却期跳过该站
    # ---- tracker 站点识别(跳过已覆盖站点) ----
    tracker_map:              # tracker 域名关键词 → 索引器 id
      btschool: btschool
      cspt: cspt
      carpt: carpt
      hddolby: hddolby
      hdarea: hdarea
      hdfans: hdfans
      "m-team": mteamtp
      pandapt: panda
      pttime: pttime
      tjupt: tjupt
      ubits: ubits
      zmpt: zmpt
  exclude_paths: []           # 这些目录内的做种不参与辅种(前缀匹配)
```

**辅种匹配流程(严格四关)**:
1. **发布组去重**:同名同大小的做种归为一个发布组(跨站副本),整组只匹配一次;
   组内副本 tracker 识别已覆盖站点 → 跳过这些站只搜未覆盖站
2. **严格同名**:候选种子名称必须与本地种子名**完全一致**(字符串相等),否则跳过
3. **大小容差**:候选大小 ±2% 内
4. **文件级比对**:下载候选种子解析文件列表,与本地做种文件(basename+大小)比对 ≥ 0.9

命中 → 入队(pending,记录真实 infohash)→ **立即注入**(不等整轮匹配结束)→
TR 校验共存做种(不同 infohash 的同源种子可共存,即 IYUU 式辅种)。
记录管理:查看/删除/失败重试(见 API);失败下轮自动重试;删除记录后可重新匹配。

> 性能提示:搜索式辅种较慢(单发布组约 1-3 分钟),单轮预算 10 组、轮询错峰覆盖;
> 已被 IYUU 辅全的发布组(白名单站全覆盖)零搜索直接跳过。

### history + log

```yaml
history:
  db: "/data/organizer.db"    # SQLite 路径;Docker 务必用挂载卷内绝对路径(/data/...),否则重建容器丢历史
  keep_days: 365              # 历史保留天数,0=永久

log:
  level: info                 # debug / info / warning / error
  file: "/data/logs/organizer.log"   # 日志文件(父目录自动创建);空=仅 stdout
```

## API 参考

鉴权:`?token=` 或 `X-Token` 头(除 `/health`)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查(免鉴权) |
| POST | `/api/v1/download` | 添加下载任务(统一注入入口),body:`{downloader?, save_path?, url 或 torrent, category?, tags?, paused?, skip_checking?}`;`url` 为 magnet/http(s) 种子链接,`torrent` 为 .torrent 字节的 base64,二选一;下载器配置只此一份,找片侧无需持有下载器凭据 |
| POST | `/api/v1/transfer` | 手动整理,body:`{path 或 hash, downloader?, preview, force, transfer_type, target_path}` |
| GET | `/api/v1/history?status=&limit=&offset=` | 整理历史查询 |
| POST | `/api/v1/history/{id}/redo` | 按历史重新整理 |
| POST | `/api/v1/history/{id}/delete` | 删除历史记录(不删文件) |
| POST | `/api/v1/history/{id}/files/delete` | 删除该记录整理出的文件,body `{delete_source?, delete_history?}` |
| GET | `/api/v1/queue` | 整理运行状态 |
| GET | `/api/v1/status` | 三模块状态(开关/最近运行/统计/辅种记录计数) |
| GET | `/api/v1/config/status` | 配置热重载状态(路径/监听开关/最近重载结果) |
| POST | `/api/v1/config/reload` | 手动触发热重载(重新加载 config.yaml 并应用;失败保留旧配置) |
| POST | `/api/v1/transfer/run` | 手动触发一次转移扫描 |
| POST | `/api/v1/reseed/run` | 手动触发一次辅种匹配+执行 |
| GET | `/api/v1/reseed/records?status=&limit=&offset=` | 辅种记录查询(pending/success/failed/skipped) |
| DELETE | `/api/v1/reseed/records/{id}` | 删除辅种记录(删除后可重新匹配) |
| POST | `/api/v1/reseed/records/{id}/redo` | 失败/跳过记录立即重试 |

## 配置热重载

服务默认监听 `config.yaml`(间隔 `PTPILOT_WATCH_INTERVAL` 秒,默认 3;设 0 关闭),
检测到变更即重新加载并应用到运行组件;也可随时 `POST /api/v1/config/reload` 手动触发。

生效范围(无需重启):

- `organize` 全部(引擎参数/目录映射/命名模板/识别 TMDB 配置);
- `downloaders` 增删与凭据/路径变化(适配器重建,旧连接释放);
- `transfer` / `reseed` 开关、参数、`jackett` 白名单与 `tracker_map`(matcher 重建);
- `server.token`(下一请求生效)、`log.level`、`history` 存储路径;
- 轮询间隔(poll_interval)原地调整,线程不重启。

校验失败(语法/缺项)或应用异常时**保留旧配置**,错误记录在 `config/status` 的 `last_error`。

**需要重启进程**:`server.host/port`(监听端口启动时绑定)。

## 运维要点

- **路径一致性**:整理/转移要求 ptpilot 与下载器看到**相同的目录路径**
  (如宿主 `/vol6/1004/media2` 双方都挂载为 `/media/media2`)。路径不一致 → 整理不匹配、
  转移后目标校验失败。跨路径场景用 `transfer.path` 前缀转换。
- **数据库持久化**:`history.db` 必须指向挂载卷内绝对路径(见上),否则重建容器丢
  整理历史与辅种记录。
- **重建容器**:部署版 `docker compose pull && docker compose up -d` 拉取最新镜像;
  本地调试 `./scripts/debug.sh`(先 build 再 up)。config.yaml 为 bind 挂载,
  配置改动热重载生效(除 host/port);代码改动需先打 tag 发布新镜像再 pull(或本地 debug.sh)。
- **日志**:转移/辅种均有完整过程日志(发布组进度、搜索条数、命中/跳过原因、注入结果);
  辅种失败记录可查 `reseed_records`。

## 目录结构

```
src/
├── main.py                  # 入口:轮询线程 + HTTP 服务 + 热重载管理
├── config.py                # YAML 配置加载与校验
├── reload.py                # 配置热重载(文件监听/手动触发/组件重建)
├── api/server.py            # HTTP API(标准库)
├── engine/                  # 整理引擎(organizer/namer/executor/planner)
├── downloaders/             # 下载器抽象(qB/TR 适配器 + bencode 工具)
├── transfer/                # 转移引擎 + 路径转换(pathrule)
└── reseed/                  # 辅种引擎 + Jackett 匹配器(matcher) + 记录存储(store)
tests/                       # 单元测试(pytest/unittest)
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -q    # 140 个测试
```

## 文档

- 需求评审稿:`docs/bt-organizer-v2-需求评审稿.md`
- 详细设计:`docs/bt-organizer-v2-详细设计.md`
- IYUUPlus 调研(转移/辅种原理):`research/iyuuplus-转移与辅种调研.md`
