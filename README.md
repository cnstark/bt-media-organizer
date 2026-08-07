# bt-media-organizer

轻量级媒体整理服务:下载完成后自动**识别 → 规范化重命名 → 转移**进媒体库。
不依赖 MoviePilot,事件触发 + 轮询对账双通道。参考 MoviePilot v2.15.4 实现思路精简而成。

## 特性

- ✅ YAML 配置整理规则(目录映射 / 整理方式 / 命名模板 / 过滤 / 覆盖策略)
- ✅ 文件名正则解析(标题/年份/季集/Part/版本/资源组),可选 TMDB 识别增强
- ✅ 四种转移方式:move / copy / hardlink / softlink
- ✅ 蓝光原盘整体整理、字幕/音频跟随主视频 + 语言标记(`.zh-cn` 等)
- ✅ 覆盖策略:never / always / size / latest
- ✅ 类别自动分类(可选):按识别类别建子目录,规则对齐 MoviePilot category.yaml(可自定义 category_rules)
- ✅ qBittorrent 下载完成事件触发(外部程序回调,秒级)
- ✅ 定期轮询下载器对账兜底(事件丢失自愈)
- ✅ SQLite 历史记录 + 幂等去重 + 失败自动重试(下轮轮询)
- ✅ 常驻服务 + Docker,HTTP API 无 Web UI

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

## qBittorrent 接入(事件触发)

> ⚠️ **路径一致性要求**:qBittorrent 与 bt-media-organizer 必须挂载相同的目录路径(例如两者都把宿主 `/vol1/1004/media1` 挂载为 `/media/media1`)。回调报文/脚本中的路径会**原样透传**给整理引擎,不做任何宿主↔容器路径映射;路径不一致会导致「未匹配到下载目录配置」而跳过。

qB「下载完成后运行外部程序」调用 `scripts/qb-notify.sh "%F"`:
1. 将 `scripts/qb-notify.sh` 放入 qB 容器(如 `/config/qb-notify.sh`,保留可执行权限)
2. 给 qB 容器设置环境变量 `BT_MEDIA_TOKEN=<config 里的 token>`(或放置令牌文件 `/config/bt-media-token`,内容为 token 无换行);同时设置 `BT_MEDIA_API=http://<bt-media-organizer 主机>:8900`(或直接修改脚本顶部 `API` 变量,勿提交私有地址)
3. WebUI → 选项 → 下载 → **完成后运行外部程序** 填:`/config/qb-notify.sh "%F"`
4. 同时配置 `downloaders[].poll_interval`(如 60s)作为兜底对账

> 事件与轮询共用同一幂等检查:失败的文件不会打「已整理」标签,下轮轮询自动重试。

## 配置要点

- `directories[].category_folder: true` 开启按识别类别自动建子目录(未识别归"未分类");`category_rules` 用 MP 格式自定义规则(如 `{纪录片: {genre_ids: "99"}}`),留空用内置 MP 默认规则
- `recognize.tmdb`:需 api_key 启用;`api_base` 可换镜像/自建反代;`proxy` 可配代理(留空读系统 HTTPS_PROXY)
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
