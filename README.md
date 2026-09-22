# 声景巡检事件归并 API（Soundscape Inspection Event Merge API）

基于 **FastAPI + SQLite** 的服务，接收来自不同巡检设备的噪声事件（带时区时间段、经纬度位置、置信度），按**位置网格 + 时间相邻关系**把事件实时归并为事件簇；时间重叠的冲突记录**保留置信度最高者为代表**，其余记录全部保留并写入**来源审计链**；支持查询簇详情、成员列表与完整审计/合并谱系。

此外内置**巡检地理围栏告警**：可保存带外环与可选洞的经纬度多边形围栏（支持跨 ±180° 经线），为围栏绑定巡检设备；事件写入时按**事件时间顺序**计算设备的进入/离开状态，仅在状态真正翻转时生成 `enter` / `exit` 告警；边界点规则固定（`boundary_inclusive`）并随围栏全生命周期写入审计。

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
| POST | `/events` | 上报单个事件，实时归并（响应附带触发的围栏告警） |
| POST | `/events/batch` | 批量上报（按数组顺序依次归并） |
| GET  | `/events` | 查询事件（可按 `device_id` / `cluster_id` 过滤，分页） |
| GET  | `/events/{id}` | 查询单条事件（含原始时区串） |
| GET  | `/clusters` | 查询簇（默认仅存活簇，支持经纬度包围盒与时间窗过滤） |
| GET  | `/clusters/{id}` | 簇详情：成员 + 审计链 + 合并谱系 |
| POST | `/clusters/rebuild` | 从全部原始事件离线重算簇（并查集，保留重算审计） |
| POST | `/geofences` | 创建围栏（外环 + 可选洞 + 绑定设备） |
| GET  | `/geofences` | 查询围栏（可按 `device_id` 过滤，分页） |
| GET  | `/geofences/{id}` | 围栏详情（含绑定设备与告警计数） |
| PUT  | `/geofences/{id}` | 整体替换围栏（几何/名称/设备），自动重算告警 |
| DELETE | `/geofences/{id}` | 删除围栏（级联清除绑定/状态/告警，审计保留） |
| POST | `/geofences/{id}/devices` | 为围栏绑定设备（幂等，补算该设备历史事件） |
| DELETE | `/geofences/{id}/devices/{device_id}` | 解绑设备（同时清除该设备的告警与状态） |
| GET  | `/geofences/{id}/audit` | 围栏审计链（删除围栏后仍可查询） |
| GET  | `/geofence-alerts` | 按时间分页查询告警（围栏/设备/方向/时间窗过滤） |
| POST | `/geofences/replay` | 从存量事件重放告警（可选 `fence_id` / `device_id` 限定范围） |

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

## 4. 巡检地理围栏告警

### 围栏几何

- 多边形按 **GeoJSON 顺序** `[longitude, latitude]` 提供：`outer` 为外环（≥3 个顶点），`holes` 为可选洞数组（每个洞 ≥3 个顶点）；首尾点相同的闭合环会自动去重存储；
- 顶点经度 ∈ [-180, 180]、纬度 ∈ [-90, 90]，环展开后经度跨度不得超过 360°，违反返回 `422`；
- **跨 ±180° 经线**：存储前把每个环的经度"展开"为连续坐标（相邻顶点经度差保持在 180° 内），判定点再把测试点经度平移 ±360° 的整数倍到同一坐标系，因此跨越太平洋的围栏（如外环 `[[170,40],[-170,40],[-170,50],[170,50]]`）无需任何特殊处理。

### 边界点规则（固定）

服务统一采用 **`boundary_inclusive`**：恰好落在多边形边或顶点上的点（容差 1e-9°）判定为**在围栏内**；洞排除其内部，但洞的边缘同样算在围栏内。该规则不可配置——它写在每条围栏记录的 `boundary_rule` 列，并随 `geofence_created` / `geofence_updated` / `geofence_deleted` / `alerts_replayed` 等审计条目永久留痕，保证任何一条告警的判定依据事后可查。

### 进入/离开状态机

- 每个 **(围栏, 设备)** 对维护一个状态机：设备初始视为 `outside`，按该设备事件的 **`(start_time, id)` 时间序**逐条判定在围栏内/外；
- **仅在状态真正翻转时**写一条告警（`enter` / `exit`），持续处于同侧的重复观测不产生告警；首次观测就在围栏内会产生一条 `enter`；
- 事件乱序到达（如补传）时，自动按事件时间**整体重算**该 (围栏, 设备) 时间线，告警流始终反映事件时间顺序而非到达顺序；
- 告警的 `occurred_at` 取触发事件的 `start_time`（UTC）；上报事件的响应体附带本次触发的告警 `geofence_alerts`（通常为空或一条）。

### 示例

```bash
# 创建跨 ±180° 的围栏并绑定设备
curl -X POST http://localhost:8000/api/v1/geofences \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "pacific-watch",
    "outer": [[170.0, 40.0], [-170.0, 40.0], [-170.0, 50.0], [170.0, 50.0]],
    "holes": [[[175.0, 43.0], [177.0, 43.0], [177.0, 45.0], [175.0, 45.0]]],
    "device_ids": ["ship-1"]
  }'

# 设备在围栏内上报 -> 响应附带一条 enter 告警
curl -X POST http://localhost:8000/api/v1/events \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"ship-1","start_time":"2026-09-22T08:00:00+08:00",
       "end_time":"2026-09-22T08:00:30+08:00","latitude":44.0,"longitude":179.0,
       "confidence":0.8}'

# 按时间分页查询告警（支持 fence_id / device_id / transition / 时间窗过滤）
curl 'http://localhost:8000/api/v1/geofence-alerts?fence_id=1&transition=enter&limit=50'

# 修改几何或批量补传后，从存量事件重放告警（幂等）
curl -X POST 'http://localhost:8000/api/v1/geofences/replay'
# {"fences_processed":1,"pairs_evaluated":1,"alert_count":2}

# 围栏审计链（含固定的边界点规则；围栏删除后仍可查）
curl http://localhost:8000/api/v1/geofences/1/audit
```

注意事项：

- 更新围栏几何或设备绑定会**立即重算**受影响设备的告警；解绑设备会清除该设备在此围栏上的告警与状态（重新绑定可从存量事件重新算出）；
- 删除围栏会级联清除其绑定、状态与告警，但**审计链保留**（`GET /geofences/{id}/audit` 仍返回完整生命周期记录）；
- 重放/乱序重算会重建告警行，因此**告警 id 不稳定**，请以 `(fence_id, device_id, event_id, occurred_at)` 识别告警。

---

## 5. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/soundscape.db`（容器内为 `sqlite:////data/soundscape.db`） | SQLAlchemy 数据库连接串 |
| `GRID_CELL_METERS` | `50` | 位置网格边长（米） |
| `GAP_TOLERANCE_SECONDS` | `2` | 判定时间相邻允许的最大间隔（秒） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

> 网格边长越小、容差越短，归并越严格。修改后可调用 `/clusters/rebuild` 用新参数重算历史数据。围栏无额外配置项，边界点规则固定为 `boundary_inclusive`。

---

## 6. 数据模型

- **`clusters`** — 簇：锚点网格、起止时间、代表事件、置信度、事件数、设备列表、`active`、`merged_into_id`
- **`noise_events`** — 原始事件：设备、UTC 起止时间、**原始时区串与偏移分钟**、经纬度、所属网格、置信度、`cluster_id`
- **`audit_entries`** — 仅追加审计：`(cluster_id, seq)` 唯一，动作、关联事件/簇、来源设备与说明
- **`geofences`** — 围栏：名称、外环/洞 JSON（`[lon, lat]`，未闭合存储）、固定的 `boundary_rule`、时间戳（id 用 AUTOINCREMENT 永不复用，保证审计可对应）
- **`geofence_devices`** — 围栏↔设备绑定：`(fence_id, device_id)` 唯一
- **`geofence_states`** — (围栏, 设备) 当前在内/外状态与最后处理的事件游标（支撑增量快路径）
- **`geofence_alerts`** — 进入/离开告警：`(fence_id, device_id)`、`event_id`、`transition`、`occurred_at`（事件时间）
- **`geofence_audit_entries`** — 围栏生命周期仅追加审计：`(fence_id, seq)` 唯一；不作外键，围栏删除后审计仍完整保留

写入侧使用进程级锁串行化“查候选 → 合并 → 重挂成员”序列；部署形态为单 uvicorn worker（SQLite 单机场景）。需要多 worker 水平扩展时，可切换为 PostgreSQL（仅需更换 `DATABASE_URL` 驱动）并以行锁替代进程锁。

---

## 7. 测试

```bash
pip install -r requirements.txt
pytest -q
```

共 32 个用例：归并侧覆盖时区校验与归一化、UTC 输出、网格分簇、时间间隔边界、置信度冲突留痕、三簇传递合并、长链远端成员归并、批量上报、包围盒过滤、合并谱系双向回溯、离线重建与 404；围栏侧覆盖 CRUD 与审计留痕、几何校验（顶点数/经纬度范围/绕环跨度）、闭合环归一化、进出告警仅在状态翻转时产生、乱序事件整体重算、边界点包含规则、洞排除与洞边缘包含、跨 ±180° 围栏、绑定/解绑语义、几何更新重算、重放幂等性与告警分页过滤。

---

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 应用 + lifespan 启动建表
│   ├── config.py        # 环境变量配置
│   ├── database.py      # 引擎/Session/ORM 模型/SQLite PRAGMA
│   ├── schemas.py       # Pydantic 入参/出参（含时区与围栏校验）
│   ├── geometry.py      # 经度解绕（±180°）、射线法、固定边界包含规则
│   ├── geofencing.py    # 围栏生命周期、进出状态机、告警重算/重放
│   ├── clustering.py    # 网格、相邻判定、实时归并、冲突审计、并查集重建
│   └── routers.py       # HTTP 路由与序列化
├── tests/               # pytest 端到端测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
