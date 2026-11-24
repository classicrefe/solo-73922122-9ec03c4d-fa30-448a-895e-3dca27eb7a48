# 月度价格指数 API（可回溯、可发布快照）

FastAPI + SQLite 实现的固定权重加权价格指数服务。统计员登记商品篮子、基期整数万分比权重和各月报价；服务按**加权价格相对数**计算月环比，并**链式累积**得到各月指数。发布是全有或全无的事务，发布后版本不可修改；更正是基于已发布版本新建草稿，只重算**最早受影响月份及其后**。

---

## 1. 快速开始

### Docker Compose

```bash
docker compose up --build
# API:      http://localhost:8000
# Swagger:  http://localhost:8000/docs
# 数据卷:   ppi-data（SQLite 文件位于容器内 /data/ppi.sqlite3）
```

### 本地运行

```bash
python3 -m pip install -r requirements-dev.txt
PPI_DB_PATH=./data/ppi.sqlite3 python3 -m uvicorn app.main:app --port 8000

# 另开终端跑端到端示例
python3 examples/smoke.py http://127.0.0.1:8000
```

运行测试：

```bash
python3 -m pytest          # 55 个用例
```

另附接续功能的独立演示（服务启动后）：

```bash
python3 examples/continuation_demo.py http://127.0.0.1:8000
```

---

## 2. 计算口径

- 权重为整数万分比 `weight_bp`，同一版本内各**权重槽位（slot）**之和必须恰为 `10000`。
- 基期指数固定为 `I₀ = 100`。
- 每月环比（固定权重加权相对数）：

  ```text
  R_t = Σ_i (w_i / 10000) · (p_i,t / p_i,t−1)
  I_t = I_t−1 · R_t
  ```

- **商品替换**：权重槽位与权重不变，只替换代表商品。旧商品在替换月 `m` 仍有效，新商品自 `m` 开始，二者在 `m` 月必须**同时有报价**（同月重叠）。
  - 替换月 `m` 的相对数仍用旧商品自身变动 `p_old,m / p_old,m−1`（新商品没有历史价）；
  - `m+1` 起改用新商品自身变动 `p_new,m+1 / p_new,m`。
  - 因此指数链不因换品产生水平跳变。
- 所有计算使用 `fractions.Fraction` 精确有理数；库中存储约分后的 `index_num/index_den`、`mom_num/mom_den` 字符串，避免链式累积的浮点误差。对外同时给 6 位小数的十进制表示。

---

## 3. 版本、发布与查询

- 一个篮子 = 一串**不可变已发布版本** + 至多一个**草稿**。
- `POST /baskets` 创建篮子并生成草稿 v1。
- `POST /baskets/{id}/publish` 在一个 `BEGIN IMMEDIATE` 事务中：校验 → 计算 → 写入定义与指数点 → 翻转状态 → 更新 `current_version_id`。任一步失败整体回滚，**旧版本保持当前且可查询**，草稿保留以便修正。
- `POST /baskets/{id}/revisions` 把当前已发布版本完整复制成新草稿（`parent_id` 记录来源）。
- 发布修订版时，按每月计算输入（槽位权重、开口商品、前后两月报价）生成指纹，与父版本比对，取**最早不同月份**；此前各月直接复用父版本的点（按原始分数精确复制，`origin=reused`），从该月起重新计算（`origin=computed`）。
- 查询接口（`/snapshot`、`/versions/{n}/snapshot`、`/versions/{n}/index`）在单个 `BEGIN DEFERRED` 读事务中读取，配合 WAL，**一次响应只来自一个已提交快照**，不会与发布过程混用版本。快照携带 `snapshot_sha256`（定义、报价与指数点的规范化哈希）便于回溯。
- 已发布数据由数据库触发器强制不可变（改/删报价、权重、商品、指数点一律 `ABORT`）。历史版本永久可查。

### 拒绝发布的规则

| 规则 | 错误码 |
| --- | --- |
| 槽位权重和 ≠ 10000 | `weight_sum_not_10000` |
| 缺报价（基期、环比前后月、替换重叠月） | `missing_quote`（重叠月附 `context=replacement_overlap`） |
| 替换无同月重叠报价 | `missing_quote` / 时间线不连续 |
| 时间线断裂、首项带前任、非开放结尾 | `timeline_*`、`first_item_has_predecessor`、`predecessor_mismatch` |
| 同一批次重复报价 | `duplicate_quote`（HTTP 409，写事务内拒绝） |
| 同一槽位同月重复替换 | `duplicate_replacement`（HTTP 409） |
| 修订删除已发布月份 / 改基期 | `cannot_remove_months`、`base_month_changed` |

失败的发布返回 `422`，响应体为 `{"error": {"code": "publish_rejected", "details": {"issues": [...]}}}`。

---

## 4. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/baskets` | 建篮子（草稿 v1） |
| GET | `/baskets` | 列出篮子及版本 |
| GET | `/baskets/{id}` | 篮子详情、当前/草稿版本 |
| POST | `/baskets/{id}/revisions` | 从当前已发布版本派生新草稿 |
| GET | `/baskets/{id}/draft` | 查看草稿定义与报价 |
| PUT | `/baskets/{id}/draft/slots/{slot_key}/weight` | 改槽位权重 |
| PUT | `/baskets/{id}/draft/prices` | 批量新增/更正报价 |
| DELETE | `/baskets/{id}/draft/prices/{item_code}/{month}` | 删除草稿报价 |
| POST | `/baskets/{id}/draft/replacements` | 商品替换（写同月新旧报价） |
| POST | `/baskets/{id}/draft/continuation` | 停报接续：求换品次数最少的链并一次写回草稿 |
| POST | `/baskets/{id}/publish` | 校验并发布草稿 |
| GET | `/baskets/{id}/snapshot` | 当前已发布快照（定义+报价+指数） |
| GET | `/baskets/{id}/versions/{n}/snapshot` | 指定版本快照 |
| GET | `/baskets/{id}/versions/{n}/index?from=&to=` | 指定版本指数（可筛选月份） |

### 建篮子请求

```json
{
  "name": "城市居民消费篮子 2025",
  "base_month": "2025-01",
  "slots": [
    {"slot_key": "food", "name": "食品", "weight_bp": 6000},
    {"slot_key": "transport", "name": "交通", "weight_bp": 4000}
  ],
  "items": [
    {"code": "rice", "name": "大米", "slot_key": "food",
     "active_from": "2025-01", "prices": {"2025-01": 1000}},
    {"code": "bus", "name": "公交", "slot_key": "transport",
     "active_from": "2025-01", "prices": {"2025-01": 200}}
  ]
}
```

### 商品替换请求

```json
{
  "slot_key": "food",
  "month": "2025-04",
  "new_code": "noodles",
  "new_name": "面条",
  "old_price_cents": 1250,
  "new_price_cents": 500
}
```

`month` 必须晚于当前商品起始月；请求会原子写入旧商品在 `month` 的新价与新商品同月报价。

### 停报接续请求（最少换品链）

代表商品停报后，统计员把同一槽位的报价接续到更晚的目标月。请求给出槽位、目标月和至多 10 个备选商品（代码、名称、逐月报价）；备选代码必须尚未在草稿中使用。

```json
{
  "slot_key": "food",
  "target_month": "2025-07",
  "candidates": [
    {"code": "noodles", "name": "面条",
     "prices": {"2025-03": 500, "2025-04": 550, "2025-05": 600}},
    {"code": "corn", "name": "玉米",
     "prices": {"2025-03": 800, "2025-04": 820, "2025-05": 840,
                "2025-06": 860, "2025-07": 880}}
  ]
}
```

求解与写回规则（实现见 `app/continuation.py` 与 `app/store.py`）：

- 现用商品的草稿报价必须从其 `active_from` 起逐月连续；接续从**最后一个报价月**开始（该月即首个换品月，新旧两份报价同时存在，当月环比仍用旧商品，新商品次月接棒）。
- 换品月必须严格晚于被换商品自身的起用月；每个备选在整条链中至多使用一次，**换下后不能重新接用**。
- 备选自身报价必须逐月无断点，且不能晚于目标月。服务用 BFS 在“商品 × 已用集合”状态图上找出能逐月接到目标月且**换品次数最少**的链（同深度按备选提交顺序决胜）。
- 整条链（各商品时间线 + 全部报价）在**一个 `BEGIN IMMEDIATE` 事务**里写回草稿，任一写入失败整体回滚。
- 求解在读事务中针对一个已提交快照进行；写事务会重新校验草稿身份、当前发布版本指针与该槽位报价/时间线的 SHA-256 指纹。期间草稿被发布或换稿、或该槽报价/商品时间线已变化，一律拒绝写回（HTTP 409）。求解期间其它槽位仍可正常录价、其它草稿仍可发版。
- 找不到能接到目标月的链时返回 422 `no_continuation_chain`，草稿保持原样。

响应返回换品月、每段采用的商品（起讫月、前任、逐月报价）以及写回后的完整草稿：

```json
{
  "basket_id": 1,
  "draft_version_no": 2,
  "plan": {
    "slot_key": "food",
    "target_month": "2025-07",
    "replacement_count": 1,
    "replacement_months": ["2025-03"],
    "segments": [
      {"item_code": "rice", "active_from": "2025-01", "active_through": "2025-03",
       "switch_month": null, "prices": [{"month": "2025-01", "price_cents": 1000}]},
      {"item_code": "corn", "active_from": "2025-03", "active_through": null,
       "switch_month": "2025-03", "prices": [{"month": "2025-03", "price_cents": 800}]}
    ]
  },
  "draft": { "...": "写回后的完整草稿定义与报价" }
}
```

### 快照响应（节选）

```json
{
  "version_no": 4,
  "base_month": "2025-01",
  "recomputed_from": "2025-03",
  "reused_point_count": 2,
  "computed_point_count": 3,
  "snapshot_sha256": "…",
  "slots": [{"slot_key": "food", "weight_bp": 6000}],
  "items": [{"code": "rice", "active_from": "2025-01", "active_through": "2025-03"}],
  "prices": [{"item_code": "rice", "month": "2025-03", "price_cents": 1200}],
  "index": [
    {"month": "2025-01", "index": "100.000000", "mom": null, "origin": "reused",
     "index_num": "100", "index_den": "1"}
  ]
}
```

---

## 5. 数据模型

- `baskets`：篮子与当前发布版本指针。
- `basket_versions`：版本号、父版本、`draft/published`、快照哈希、重算起点与复用/新算点数。
- `basket_slots`：权重槽位（版本内权重固定）。
- `basket_items`：槽位内的商品时间线（`active_from`/`active_through`/`predecessor_code`）；替换 = 在同一槽位新增一条时间线记录。
- `prices`：按（版本, 商品, 月）唯一。
- `index_points`：按（版本, 月）唯一，存精确分数与 `origin`。

索引与约束保证：每版本至多一个草稿；同槽位同月不能有两个起点；同槽位不能有两个商品指向同一前任。

---

## 6. 已实现

- 篮子/槽位/商品时间线/报价的登记与编辑，权重整数万分比校验。
- 固定权重加权相对数 + 链式累积，精确有理数存储与输出。
- 商品替换（权重槽位不变、同月新旧重叠报价、替换月用旧商品变动、次月起用新商品变动）。
- **停报接续 API**：纯函数 BFS 求换品次数最少的接续链，强制现用商品报价连续、换品月晚于起用月、旧品不复用；整条链单事务写回草稿，并用草稿身份 + 槽位 SHA-256 指纹做乐观并发控制，求解期间可继续录价/发版，冲突即拒绝且整次回滚。
- 发布全有或全无；缺报价、权重和不为 10000、替换无重叠、同月重复、时间线断裂等均拒绝且保留旧版本。
- 修订派生 + 最早受影响月份起的部分重算，之前各月精确复用；权重变更/基期报价变更/新增月份/中间月份更正均正确判定起点。
- 版本隔离查询：单读事务 + WAL；快照哈希并在读取时重算校验（`integrity_ok`）；历史版本永久可查；已发布数据由触发器保护。
- Dockerfile、docker-compose.yml、可运行的 `examples/smoke.py`。

## 7. 验证结果

在本地实际执行：

- `python3 -m pytest` → **55 passed**（原 30 个用例 + 接续求解器 8 个纯函数用例 + 接续 API 17 个端到端用例，含真实线程并发与回滚校验）。
- 真实 uvicorn 进程（容器同款启动命令 `uvicorn app.main:app`）+ `examples/smoke.py` → 全部检查通过，既有功能无回归。
- 真实 uvicorn 进程 + `examples/continuation_demo.py` → 全部检查通过，包括：
  - 三个备选里直达目标的 1 次换品链胜出，淘汰需要 2 次换品的路径；
  - 换品月即旧商品最后报价月，新旧同月报价齐备，写回后的草稿可直接发布，指数逐月连续到目标月；
  - 无解请求返回 422 `no_continuation_chain` 且草稿无任何残留；
  - OpenAPI `/docs` 含新接口，请求体对备选数量（1–10）、月份格式、正整数报价做结构校验。
- 并发：真实双线程（接续请求与普通录价同时提交）→ 二者之一成功，另一请求 409 拒绝，提交后的草稿始终自洽、无半链残留。

## 8. 未完成 / 已知限制

- **未在本环境执行 `docker compose up`**：当前环境没有 Docker/Podman，仅校验了 Compose YAML 可解析、固定版本依赖可解析、以及容器启动命令 `uvicorn app.main:app` 在本机可运行（已用该命令起真实服务并跑通两个示例脚本与 `/docs`）。镜像构建本身未实测。
- 接续 API 只处理**单个槽位**的接续，不支持一次请求同时接续多个槽位；多槽位需要分别调用。
- 接续写回后草稿未必可立即发布（其它槽位仍可能缺报价），发布校验与拒绝口径不变。
- 没有认证/授权、多租户与审计日志（谁改了什么）；版本本身可回溯，但操作者身份未记录。
- 单写者 SQLite（WAL + `BEGIN IMMEDIATE`）适合单实例；多实例横向扩展需换成 Postgres 等。
- 未实现篮子删除/归档、版本回滚（只能基于旧版本重新派生）。
- 指数精度对外固定 6 位小数；如需其它位数需改 `DECIMAL_PLACES`。
- 缺少 OpenAPI 契约测试与压力测试；并发验证为小规模功能验证。
