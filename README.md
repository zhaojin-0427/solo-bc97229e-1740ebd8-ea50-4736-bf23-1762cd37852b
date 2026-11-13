# Citation Corpus Collector

沿论文站的参考文献构建引用语料的采集系统：**FastAPI + SQLite** 采集 API、双采集槽并发、
全量状态持久化、中断自动续采，附带一个含循环引用 / 迟到短路径 / 一次请求失败的本地论文站。

## 快速开始

```bash
docker compose up --build
```

| 服务 | 地址 | 说明 |
|---|---|---|
| crawler | http://localhost:8000/docs | 交互式 API 文档（Swagger UI），全部演示在此完成 |
| papersite | http://localhost:8080/papers/p0.html | 本地论文站（静态 HTML） |

采集状态保存在命名卷 `crawler_data` 的 SQLite 中，`docker compose restart crawler`
甚至 `down`/`up` 之后仍可续采。

> Docker 不可用时可直接本地启动（见文末「本地开发」），API 与演示流程完全相同；
> 容器内 `papersite` 主机名对应本地 `127.0.0.1:8080`。

## 演示流程（在 Swagger UI 中操作，或用 curl）

**1. 提交采集作业** — `POST /jobs`：

```json
{"seed_url": "http://papersite:8080/papers/p0.html", "max_depth": 4}
```

种子深度为 0，`max_depth` 取值 0～4。

**2. 观察采集** — `GET /jobs/1`。论文站里 `slow.html` 延迟 2.5s、`X.html` 延迟 4s：

- 两个采集槽不会等同层慢页，先把长链 `c1→c2→c3→X` 抓完，X 先以**深度 4** 登记；
- 慢页 `slow.html` 在 X 的响应**仍在途**时到达，揭示短路径 `slow→X`，X 的深度被下调为 2；
- X 的在途响应按**最新最短深度 2** 登记其引用：`Y` 记为深度 3（而不是旧深度推出的 5）；
- 深度下调沿已存引用边传播，使原本超深的 `Y`（5→3）、`Z`（4）进入深度范围被**补抓**，
  已抓页面不重复抓取。

这正是「深度下降与在途响应」的竞态：若在途响应沿用认领时的旧深度，`Y` 会被记成
`out_of_range` 而漏采。最终：

```
status: failed          # broken.html 首次请求返回 500，仍有失败 -> 不得报完成
counts: {done: 13, failed: 1, external: 1, non_html: 1}
by_depth: {0:1, 1:3, 2:6, 3:3, 4:3}     # X 已在深度 2，Y 在 3，Z 在 4
```

**3. 查看失败** — `GET /jobs/1/failures` → `broken.html / HTTP 500`。

**4. 重试续采** — `POST /jobs/1/retry`：失败页重新入队（已采内容保留、不重复登记），
`broken.html` 第二次请求成功，作业转为 `completed`。

**5. 中断续采** — 再建一个作业（`POST /jobs`），在慢页窗口内执行
`docker compose restart crawler`（X 的响应仍在途）：进程重启后自动把 `fetching`
重置为 `pending` 并恢复全部 running 作业；X 重新抓取后仍按其最新最短深度 2 登记，
不漏抓、不重复登记（`papers` 表 `UNIQUE(job_id,url)`、`citations` 表主键去重）。

**6. 查询语料**：

- `GET /jobs/1/papers`（可按 `status`、`depth` 过滤）— 论文 URL、深度、题名、状态
- `GET /jobs/1/citations`（可按 `src` 过滤）— 引用边

## 演示语料站结构

```
p0 ─┬─> p1 ─┬─> p2 ─┬─> p3 ─> p1          循环引用（登记一次，不重复展开）
    │       │       └─> p4 ─> p5 ─> mailto:（非 http，忽略）
    │       │              └─> slides.pdf  非 HTML，记 non_html
    │       ├─> p3 (p2.html#related 片段已去掉)
    │       ├─> broken.html                首次 500，重试成功（一次请求失败）
    │       └─> http://example.org/...     非同源，记 external 不抓
    ├─> slow ──────────────> X             迟到短路径（延迟 2.5s）：X 深度 4→2
    └─> c1 ─> c2 ─> c3 ─> X ─> Y ─> Z      长链；X 响应延迟 4s（在途期间深度被下调）
                                             Y 按 X 的新深度记为 3（5→3 补抓），Z 深度 4 补抓
```

注：`X.html` 的 4s 延迟保证它自己的响应在短路径到达时仍在途，使演示必然命中
本次修复的竞态；`slow.html` 的 2.5s 延迟则保证长链先完成。

## 采集规则实现

- **仅同源、无重定向、静态 HTML**：`httpx` 关闭重定向跟随，3xx 记为失败；
  非 `text/html` 记 `non_html`；非同源 URL 登记为 `external` 不抓取。
- **解析**：`<title>` 为题名，`a.citation` 的 `href` 为引用；相对地址按当前页
  `urljoin` 解析，`urldefrag` 去掉片段。
- **双采集槽**：每作业 2 个 worker 各自认领下一待抓页（按深度、登记序），
  槽空且有待抓页即发起请求，无同层等待。
- **深度即最短距离**：发现更短路径时下调深度并沿已存引用边传播（严格递减、
  有下界，引用环安全），新进入深度范围的论文自动补抓，已抓页面不重复抓取。
- **在途响应按最新深度落库**：登记响应时在同一事务内重新读取该页当前的最短深度，
  而不是用认领请求时的旧深度。因此「X 先以深度 4 认领、在途期间被慢页下调到 2」
  时，X 的引用仍按深度 2 展开（Y→3），不会按旧深度推出 5 而漏采。
- **共同持久化**：页面状态、引用边、待抓进度在同一 SQLite 事务中提交；
  重启时 `fetching → pending` 并恢复所有 running 作业。
- **失败语义**：请求失败仅标记该页 `failed`，已采内容保留；作业存在失败时
  状态为 `failed` 而非 `completed`；`POST /jobs/{id}/retry` 重新入队失败页续采。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/jobs` | 创建作业 `{seed_url, max_depth(0-4)}` |
| GET | `/jobs` / `/jobs/{id}` | 作业列表 / 状态与进度（按状态、深度统计） |
| POST | `/jobs/{id}/retry` | 失败页重新入队并续采 |
| GET | `/jobs/{id}/failures` | 失败 URL 及错误 |
| GET | `/jobs/{id}/papers` | 论文查询（`status`、`depth` 过滤） |
| GET | `/jobs/{id}/citations` | 引用边查询（`src` 过滤） |

## 回归测试

```bash
docker compose run --rm tests          # 容器内跑全部回归测试
```

或本地：

```bash
pip install -r crawler/requirements.txt -r crawler/requirements-dev.txt
( cd crawler && python -m pytest -q )
```

覆盖点：

- `test_shortened_in_flight_page_expands_from_current_depth` — 用 `Database` 精确复现
  双槽交错：X 以深度 4 认领 → 慢页将其下调到 2 → X 的在途响应必须把 Y 登记为 3；
  修复前该用例得到 Y=5/`out_of_range`。
- `test_already_fetched_children_follow_depth_decrease` — 深度下调沿已存引用边传播、
  `out_of_range` 重新入队、已抓页不重抓、引用环终止。
- `test_late_short_path_backfills_y_and_z` — 端到端跑真实 API + 论文站，校验
  `X=2 / Y=3 / Z=4`、按状态/深度统计、失败语义与 retry 转 `completed`。
- `test_two_slots_fetch_concurrently` — 慢页不阻塞另一槽。
- `test_restart_resumes_running_job` — X 仍在途时「重启」，恢复后仍按深度 2 落库，
  无重复论文/引用边。
- `test_api_docs_and_validation` — `/docs`、OpenAPI 路径与参数校验。

## 本地开发（不用 Docker）

```bash
pip install -r crawler/requirements.txt
( cd papersite && python server.py )                       # :8080
( cd crawler && DATABASE_PATH=./crawler.db \
    uvicorn app.main:app --port 8000 )                     # :8000/docs
```
