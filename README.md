# 声景巡检事件归并 API（Soundscape Inspection Event Merge API）

基于 **FastAPI + SQLite** 的服务，接收来自不同巡检设备的噪声事件（带时区时间段、经纬度位置、置信度），按**位置网格 + 时间相邻关系**把事件实时归并为事件簇；时间重叠的冲突记录**保留置信度最高者为代表**，其余记录全部保留并写入**来源审计链**；支持查询簇详情、成员列表与完整审计/合并谱系。

- 启动即自动初始化数据库（建表 + WAL）
- 容器暴露 `8000` 端口，挂载独立数据卷
- 交互式文档：启动后访问 `http://localhost:8000/docs`（Swagger）与 `/redoc`

---

## 1. 归并规则

### 空间相邻（位置网格）

地球表面做等距矩形投影（equirectangular），按 `GRID_CELL_METERS`（默认 **50 米**）划分为正方形网格，每个事件落入一个 `(cell_x, cell_y)`。

- 两个事件位于**同一格或周围 8 格**（3×3 邻域）内，即视为空间相邻；
- 候选匹配基于**簇成员所在的格**而非簇锚点格，因此一个簇向四周扩展到任意大小后，新事件仍可通过它的任何远端成员找到该簇（与离线重建的并查集结果保持一致）。

### 时间相邻

两个时间区间满足以下任一条件即视为时间相邻：

1. 区间**重叠**（含端点相接）；
2. 两区间间隔 ≤ `GAP_TOLERANCE_SECONDS`（默认 **2 秒**）。

**同时**满足空间相邻与时间相邻的事件归入同一簇。一个新事件若同时邻接多个已有簇，则这些簇被**传递合并**（union-find 语义）为一个存活簇。

### 冲突处理（置信度优先 + 来源留痕）

同一簇内时间区间**严格重叠**（正长度重叠，仅端点相接不算）的记录被视为对同一声学事件的重复观测：

- 置信度最高者成为簇**代表记录**（`representative_event_id`，平局依次按最早开始、最早结束、最小 id 排序）；
- 其余记录**不删除**，继续作为簇成员保留，并各写入一条 `event_conflict` 审计，记录其 `device_id`（来源）以及输给了哪条事件、双方置信度；
- 被吸收的旧簇置为 `active=false` 并记录 `merged_into_id` 指向存活簇，吸收双方都写 `cluster_merged` 审计。

查询存活簇或已吸收簇时都能沿 `merged_into_id` 双向遍历出完整**谱系（lineage）**，聚合谱系内所有成员与审计行。

### 时区处理

- 入参时间**必须带显式时区**（如 `2026-09-22T08:00:00+08:00` 或 `2026-09-22T00:00:00Z`），无时区的“天真时间”返回 `422`；
- 不同时区表达的同一时刻会被正确归一化（内部统一存 UTC，原始字符串与 UTC 偏移分钟数一并留存：`start_time_raw` / `tz_offset_minutes`）；
- 所有响应时间均为 UTC（以 `Z` 结尾）。

---

## 2. 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker compose up -d --build
curl http://localhost:8000/api/v1/health
# {"status":"ok","database":"sqlite"}
```

- 数据库文件位于容器内 `/data/soundscape.db`，持久化到 Docker 卷 `soundscape-data`；
- 容器启动（uvicorn lifespan）时自动建表，无需手动初始化；
- 停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据卷
```

### 方式二：本地运行

```bash
pip install -r requirements.txt
mkdir -p data
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

SQLite 默认开启 `foreign_keys=ON`、`journal_mode=WAL`、`synchronous=NORMAL`。

---

## 3. API 一览（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查（同时探活数据库） |
| POST | `/events` | 上报单个事件，实时归并 |
| POST | `/events/batch` | 批量上报（按数组顺序依次归并） |
| GET  | `/events` | 查询事件（可按 `device_id` / `cluster_id` 过滤，分页） |
| GET  | `/events/{id}` | 查询单条事件（含原始时区串） |
| GET  | `/clusters` | 查询簇（默认仅存活簇，支持经纬度包围盒与时间窗过滤） |
| GET  | `/clusters/{id}` | 簇详情：成员 + 审计链 + 合并谱系 |
| POST | `/clusters/rebuild` | 从全部原始事件离线重算簇（并查集，保留重算审计） |

### 上报事件

```bash
curl -X POST http://localhost:8000/api/v1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "device_id": "sensor-A-07",
    "start_time": "2026-09-22T08:00:00+08:00",
    "end_time":   "2026-09-22T08:00:40+08:00",
    "latitude": 31.2304,
    "longitude": 121.4737,
    "confidence": 0.72,
    "sound_type": "construction"
  }'
# {"created":true,"cluster_id":1,"event_id":1,"merged_cluster_ids":[]}
```

随后另一台设备在同一网格、重叠时间段以更高置信度上报：

```bash
curl -X POST http://localhost:8000/api/v1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "device_id": "sensor-B-02",
    "start_time": "2026-09-22T08:00:30+08:00",
    "end_time":   "2026-09-22T08:01:10+08:00",
    "latitude": 31.2305,
    "longitude": 121.4738,
    "confidence": 0.91,
    "sound_type": "construction"
  }'
# {"created":true,"cluster_id":1,"event_id":2,"merged_cluster_ids":[]}
```

### 查询簇详情（成员 + 审计链 + 谱系）

```bash
curl http://localhost:8000/api/v1/clusters/1
```

响应关键字段：

```jsonc
{
  "id": 1,
  "cell_x": 230998, "cell_y": 69453,
  "device_ids": ["sensor-A-07", "sensor-B-02"],
  "representative_event_id": 2,   // 置信度 0.91 的记录胜出
  "confidence": 0.91,
  "event_count": 2,
  "started_at": "2026-09-22T00:00:00Z",
  "ended_at":   "2026-09-22T00:01:10Z",
  "active": true,
  "merged_into_id": null,
  "lineage": [1],
  "members": [ /* 两条事件，含 start_time_raw、tz_offset_minutes */ ],
  "audit_chain": [
    {"seq": 1, "action": "cluster_created", "event_id": 1, "device_id": "sensor-A-07"},
    {"seq": 2, "action": "event_added",     "event_id": 1, "device_id": "sensor-A-07"},
    {"seq": 3, "action": "event_added",     "event_id": 2, "device_id": "sensor-B-02"},
    {"seq": 4, "action": "event_conflict",  "event_id": 1, "device_id": "sensor-A-07",
     "detail": "overlaps event_id=2 from device_id=sensor-B-02; kept lower-confidence record confidence=0.720 vs winner confidence=0.910"}
  ]
}
```

### 审计动作（`audit_chain[*].action`）

| action | 含义 |
| --- | --- |
| `cluster_created` | 簇由首条事件创建 |
| `event_added` | 有事件加入簇（含来源 `device_id`） |
| `event_conflict` | 该事件与更高置信度事件严格重叠，落选但保留 |
| `cluster_merged` | 簇被吸收（吸收方与被吸收方各写一条，`other_cluster_id` 互指） |
| `cluster_rebuilt` | 离线重算产生的簇 |

每个簇内审计 `seq` 从 1 单调递增，形成可追溯的审计链。

### 过滤查询示例

```bash
# 某设备的全部事件
curl 'http://localhost:8000/api/v1/events?device_id=sensor-A-07&limit=50'

# 经纬度包围盒 + 时间窗内的存活簇
curl 'http://localhost:8000/api/v1/clusters?min_lat=31.20&max_lat=31.30&min_lon=121.40&max_lon=121.50&start_after=2026-09-22T00:00:00Z'

# 包含已被吸收的历史簇
curl 'http://localhost:8000/api/v1/clusters?include_merged=true'
```

### 离线重建

调参数（网格大小/时间容差）后，可用原始事件全量重算（并查集），结果写入新簇并补审计：

```bash
curl -X POST http://localhost:8000/api/v1/clusters/rebuild
# {"cluster_count":1,"event_count":2}
```

---

## 4. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/soundscape.db`（容器内为 `sqlite:////data/soundscape.db`） | SQLAlchemy 数据库连接串 |
| `GRID_CELL_METERS` | `50` | 位置网格边长（米） |
| `GAP_TOLERANCE_SECONDS` | `2` | 判定时间相邻允许的最大间隔（秒） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

> 网格边长越小、容差越短，归并越严格。修改后可调用 `/clusters/rebuild` 用新参数重算历史数据。

---

## 5. 数据模型

- **`clusters`** — 簇：锚点网格、起止时间、代表事件、置信度、事件数、设备列表、`active`、`merged_into_id`
- **`noise_events`** — 原始事件：设备、UTC 起止时间、**原始时区串与偏移分钟**、经纬度、所属网格、置信度、`cluster_id`
- **`audit_entries`** — 仅追加审计：`(cluster_id, seq)` 唯一，动作、关联事件/簇、来源设备与说明

写入侧使用进程级锁串行化“查候选 → 合并 → 重挂成员”序列；部署形态为单 uvicorn worker（SQLite 单机场景）。需要多 worker 水平扩展时，可切换为 PostgreSQL（仅需更换 `DATABASE_URL` 驱动）并以行锁替代进程锁。

---

## 6. 测试

```bash
pip install -r requirements.txt
pytest -q
```

覆盖：时区校验与归一化、UTC 输出、网格分簇、时间间隔边界、置信度冲突留痕、三簇传递合并、长链远端成员归并、批量上报、包围盒过滤、合并谱系双向回溯、离线重建与 404 等 16 个用例。

---

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 应用 + lifespan 启动建表
│   ├── config.py        # 环境变量配置
│   ├── database.py      # 引擎/Session/ORM 模型/SQLite PRAGMA
│   ├── schemas.py       # Pydantic 入参/出参（含时区校验）
│   ├── clustering.py    # 网格、相邻判定、实时归并、冲突审计、并查集重建
│   └── routers.py       # HTTP 路由与序列化
├── tests/               # pytest 端到端测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
