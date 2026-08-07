# bt-media-organizer

轻量级媒体管理服务,三个独立模块(各自可独立开关):

1. **整理**:下载完成后自动**识别 → 规范化重命名 → 转移**进媒体库(硬链接/移动等)
2. **转移**:把种子从 A 下载器搬到 B 下载器继续做种(路径映射/删源/标记可选)
3. **辅种**:用已有做种数据去 Jackett 索引的 PT 站匹配同源种子并注入做种(白名单/限速/记录管理)

参考 MoviePilot v2.15.4 与 IYUUPlus 实现思路精简而成。不依赖 MoviePilot。

## 特性

- ✅ YAML 配置整理规则(目录映射 / 整理方式 / 命名模板 / 过滤 / 覆盖策略)
- ✅ 文件名正则解析(标题/年份/季集/Part/版本/资源组),可选 TMDB 识别增强
- ✅ 四种转移方式:move / copy / hardlink / softlink
- ✅ 蓝光原盘整体整理、字幕/音频跟随主视频 + 语言标记(`.zh-cn` 等)
- ✅ 覆盖策略:never / always / size / latest
- ✅ 类别自动分类(可选):按识别类别建子目录,规则对齐 MoviePilot category.yaml(可自定义 category_rules)
- ✅ 支持下载器:qBittorrent / Transmission(适配器统一接口)
- ✅ 整理:定期轮询下载器对账(「已整理」标签幂等,失败自愈)
- ✅ 转移:轮询扫描来源下载器做种 → 路径过滤/选择/转换(IYUU 语义 eq/sub/add/replace)→ 种子文件导出或读盘 → 注入目标下载器(可配置删源/自动开始/标记);无记录表,幂等靠目标下载器状态
- ✅ 辅种:Jackett Torznab 匹配(白名单索引器/大小容差/infohash 直取或下载比对/限速)→ 注入默认暂停 + 标记;SQLite 记录管理(查看/删除/失败重试)
- ✅ SQLite 历史记录 + 幂等去重 + 失败自动重试(下轮轮询)
- ✅ 常驻服务 + Docker,HTTP API 无 Web UI(三模块状态/手动触发/记录管理)

## 快速开始

```bash
cp config.example.yaml config.yaml   # 修改配置
pip install -r requirements.txt
python -m src.main --config config.yaml   # 或设 BT_MEDIA_CONFIG 指定路径、BT_MEDIA_TOKEN 覆盖 token
```

或 Docker:

```bash
docker compose -f docker-compose.example.yml up -d --build
```

> Docker 部署支持 `PUID`/`PGID` 环境变量降权运行(默认 root,参照 MoviePilot);TMDB 网络不通时可配置 `recognize.tmdb.api_base` 换镜像/自建反代(见 config.example.yaml 注释)。

## qBittorrent / Transmission 接入

> ⚠️ **路径一致性要求**:整理模块要求 qBittorrent 与 bt-media-organizer 挂载相同的目录路径(例如两者都把宿主 `/vol1/1004/media1` 挂载为 `/media/media1`);路径不一致会导致「未匹配到下载目录配置」而跳过。
> 转移/辅种模块通过下载器 API 工作:qB 种子文件走 `torrents/export` 接口(跨主机可用),无需共享存储;TR 的种子文件读取依赖 `torrentFile` 路径或 `torrent_path` 配置(跨主机时需可读路径)。

三个模块全部为**轮询**驱动(无事件/webhook):

- 整理:`downloaders[].poll_interval`(如 60s)对账「已完成未打标签」的任务,失败自愈
- 转移:`transfer.poll_interval` 扫描来源下载器做种列表(幂等靠目标下载器状态)
- 辅种:`reseed.poll_interval` 匹配 + 注入(记录幂等)

## 配置要点

- `directories[].category_folder: true` 开启按识别类别自动建子目录(未识别归"未分类");`category_rules` 用 MP 格式自定义规则(如 `{纪录片: {genre_ids: "99"}}`),留空用内置 MP 默认规则
- `recognize.tmdb`:需 api_key 启用;`api_base` 可换镜像/自建反代;`proxy` 可配代理(留空读系统 HTTPS_PROXY)
- 转移:见 config.example.yaml 的 `transfer:` 段(来源/目标下载器、删源、自动开始、标记、路径过滤/选择/转换);开启时 `from_client`/`to_client` 必填且不能相等
- 辅种:见 `reseed:` 段(注入目标、默认暂停、Jackett 地址/密钥/**索引器白名单必填**、大小容差);
  **流控防站点管控**:`per_indexer_delay`(同站最小间隔)、`per_minute`(同站每分钟上限)、
  `global_interval`(全局节流)、`cooldown_seconds`(站点失败后冷却,冷却期跳过该站);
  qB 种子导出无需 `torrent_path`,TR 兜底读取需要
- 环境变量:`BT_MEDIA_CONFIG`(配置文件路径)、`BT_MEDIA_TOKEN`(覆盖 server.token)、`PUID`/`PGID`(Docker 降权)

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查(免鉴权) |
| POST | `/api/v1/transfer` | 手动整理,body:`{path 或 hash, downloader?, preview, force, transfer_type, target_path}` |
| GET | `/api/v1/history?status=&limit=&offset=` | 历史查询 |
| POST | `/api/v1/history/{id}/redo` | 按历史重新整理 |
| POST | `/api/v1/history/{id}/delete` | 删除历史记录(不删文件) |
| POST | `/api/v1/history/{id}/files/delete` | 删除该记录整理出的文件,body `{delete_source?, delete_history?}` |
| GET | `/api/v1/queue` | 运行状态 |
| GET | `/api/v1/status` | 三模块状态(整理/转移/辅种:开关、最近运行、统计、记录计数) |
| POST | `/api/v1/transfer/run` | 手动触发一次转移扫描 |
| POST | `/api/v1/reseed/run` | 手动触发一次辅种匹配+执行 |
| GET | `/api/v1/reseed/records?status=&limit=&offset=` | 辅种记录查询 |
| DELETE | `/api/v1/reseed/records/{id}` | 删除辅种记录(可重新匹配) |
| POST | `/api/v1/reseed/records/{id}/redo` | 失败/跳过记录立即重试 |
| POST | `/api/v1/poll` | 立即触发轮询 |

> 鉴权:除 `/health` 外均需 token,来源 `?token=` 或 `X-Token` 头。

示例:

```bash
# 预览(只算路径不落盘)
curl -X POST "http://127.0.0.1:8900/api/v1/transfer?token=xxx" \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/downloads/movies/xxx.mkv", "preview": true}'

# 手动整理
curl -X POST "http://127.0.0.1:8900/api/v1/transfer?token=xxx" \
  -H "Content-Type: application/json" \
  -d '{"hash": "8c212779b4ab12..."}'

# 历史
curl "http://127.0.0.1:8900/api/v1/history?status=failed&token=xxx"

# 删除历史记录(不删文件)
curl -X POST "http://127.0.0.1:8900/api/v1/history/12/delete?token=xxx"

# 删除整理后的文件(可选 delete_source 连源文件一起删、delete_history 连历史一起删)
curl -X POST "http://127.0.0.1:8900/api/v1/history/12/files/delete?token=xxx" \
  -H "Content-Type: application/json" \
  -d '{"delete_source": false, "delete_history": true}'
```

> 删除说明:`files/delete` 只删除该记录对应的目标文件/目录,删除后自动向上清理空目录(到库根目录为止);
> `delete_history: true` 时,该记录有 download_hash 的话会连同同一次下载的所有记录一起删除。
> 目标/源已不存在时记入 `missing`,不视为错误。

## 目录结构

```
src/
├── main.py               # 入口:轮询线程 + HTTP
├── config.py             # YAML 配置加载/校验
├── log.py                # 日志
├── history.py            # SQLite 历史 + TMDB 缓存
├── parse/filename.py     # 文件名解析
├── storage/local.py      # copy/move/hardlink/softlink
├── downloaders/          # qBittorrent 适配器(协议可扩展)
├── recognize/
│   ├── tmdb.py           # TMDB 识别(可选)
│   └── category.py       # 类别规则(对齐 MP category.yaml)
├── engine/
│   ├── namer.py          # 命名模板渲染
│   ├── planner.py        # 规划(收集/过滤/排序)
│   ├── executor.py       # 单文件/目录转移 + 覆盖策略
│   └── organizer.py      # 整理引擎(核心编排)
└── api/server.py         # HTTP API(标准库)
```

## 测试

```bash
python tests/test_filename.py
python tests/test_namer.py
python tests/test_planner.py
python tests/test_tmdb.py
python tests/test_category.py
python tests/test_category_integration.py
python tests/test_integration.py
```

## 文档

- 需求:见 `workspace/docs/bt-media-organizer-requirements.md`
- 设计:见 `docs/bt-media-organizer-design.md`(仓库内)
