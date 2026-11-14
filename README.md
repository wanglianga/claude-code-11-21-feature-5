# 试听分流与报名效果分析服务

面向课程运营的 A/B 分流与报名效果分析后端。从**首次分配**开始固定比较对象，在**观察时间相同**的前提下判断新试听安排是否改变整体报名表现；不连接真实投放或支付，行为数据由合成事件流提供，时钟可推进。在单实验分流之上，进一步提供**多实验共享流量的互斥层分配管理**：运营可声明哪些试验必须互斥、哪些允许共存，并能解释任意一次分流为什么没有入选。

- 技术栈：Python 3.12 · FastAPI · PostgreSQL（SQLAlchemy 2 + psycopg 3）
- 接口文档（Swagger UI）：启动后访问 `http://127.0.0.1:8000/docs`（OpenAPI 3.x：`/openapi.json`）
- 全部时间统一 **UTC**；观察窗口为 `[入组时刻, 入组时刻+观察天数)`，**左闭右开**

## 核心规则（与实现的对应）

### 单实验分流与分析

| 规则 | 实现 |
| --- | --- |
| 首次分配固定比较对象 | `assignments` 每实验每学员仅一行（首次事实），重复请求/换设备返回原组 |
| 稳定学员标识 | `student_key` 显式提供或系统生成；设备绑定解析匿名请求 |
| 按标识哈希分流 | SHA256(`seed:student_key`) → 当前生效比例配置；比例调整只影响后来学员 |
| ITT 全量分析 | 分析含未曝光/曝光失败者，不做任何事后筛选 |
| 统一截止点 + 成熟样本 | 窗口未结束 → `obs_insufficient`；窗口结束但上报水位未覆盖 → `report_incomplete`（数据等待） |
| 有效报名 | 窗口内创建且窗口结束前未被撤销的 ≥1 笔订单；同学员多笔仅计一次 |
| 事件不可变 | 按 `(source, biz_id, seq)` 去重；取消/退款为独立事件；按 `occurred_at` 评估，撤销先到等待原单 |
| 分析快照 | 每次运行冻结截止点、事件序号、水位线、比例配置与**分层声明**；晚报只属于新运行；中断可按原快照续算 |
| 证据门槛 | 两组成功/未成功均 ≥10 才给正态近似 95% 区间；跨零只说“未区分方向”，绝不说“等效” |
| 分流卡方检查 | 按各比例配置生效期间分别算期望再合并；p 值超阈值暂停效果判断并列出异常来源 |
| 受众规则不可变 | 规则变化需新开实验；停招只停新分配，既有观察继续 |

### 互斥层与共享流量（多实验）

| 规则 | 实现 |
| --- | --- |
| 互斥 vs 共存 | 实验绑定互斥层（`experiments.layer_id`）；同一学员在一层内至多进入一个实验，不同层允许共存 |
| 两段式分流 | 先按层种子对稳定标识哈希出**桶位**（与修订版本无关，永不挪动），命中层内实验区间后再按该实验自己的种子/比例分组 |
| 容量区间 | 层内桶位空间切给各实验的 `[bucket_from, bucket_to)`，区间两两不重叠、总额不超 `total_buckets`；申请时强校验 |
| 扩流不洗牌 | 扩流新区间必须完整包含旧区间，只吃未分配桶位；新边界不驱逐任何已分配学员，旧决定不重判 |
| 停招释放 | 停招实验的区间标记释放、推进层 revision；**旧成员保留原分配完成观察，释放范围仅新来者可用**（老成员黏性） |
| 并发修订不超卖 | 层行锁 + `revision` 乐观锁（条件更新）；两个管理员争抢最后容量只有一个生效，另一个收到 409，需重新读取剩余容量 |
| 三类未入组原因 | 受众不合格 `ineligible`、层内未命中 `layer_not_selected`、实验停止 `stopped`，分别落库，不用一条失败代替 |
| 决策可解释 | `GET /layers/{id}/explain` 给出学员桶位、在招区间与命中/未命中原因；分配记录自带当时 `route_snapshot` |
| 历史重放 | 每次修订写入 `layer_revisions` 全量快照；重放请求返回原决定，解释旧入组只能用当时路由，不能拿新边界重判 |
| 跨层共同曝光 | 曝光回执登记 `co_exposures`；`/reports/co-exposures` 按实验对报告共同分组及曝光人数，只陈述共存事实、**不承诺互不影响** |
| 禁止事后剔除 | 分析运行冻结 `layer_snapshot` 分层声明；系统不提供看完效果再剔除跨层重叠学员的入口，ITT 保持全量 |

## 快速开始（Docker Compose，一键）

```bash
docker compose up --build
# 服务就绪后：
#   Swagger UI  http://127.0.0.1:8000/docs
#   健康检查    curl http://127.0.0.1:8000/health
```

Compose 含 `db`（postgres:18-alpine，库/用户/密码 `ccdev`）与 `app` 两个服务；建表由应用启动时自动完成。

## 本地开发

```bash
# 1) 准备数据库（容器已带 PostgreSQL 18：127.0.0.1:5432，库/用户/密码 ccdev）
# 2) 安装依赖
/opt/python-3.12/bin/python3 -m venv venv
./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
# 3) 启动（默认连接 postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev，可用 DATABASE_URL 覆盖）
./venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 演示（合成行为流 + 可推进时钟）

服务运行后执行：

```bash
./venv/bin/python scripts/demo.py        # DEMO_BASE_URL 可覆盖目标地址
```

五个场景（脚本内置 35 项断言，全部通过才退出 0）：

1. **选择偏差** — 800 名学员、新方案真实效果为 0。系统 ITT 结果区间跨 0（“未区分方向”）；对照的错误做法（把看完介绍的人当作新组）算出约 +21pp 的虚假提升。
2. **观察不足 / 上报未齐** — 入组当天、第 4 天、第 8 天（水位未覆盖）、补齐水位后四种状态依次呈现，分清“现在还不能判断”与“没有效果”。
3. **重复入组与重复报名** — 幂等键重放、换设备返回原组、多笔订单仅计一次、恰在结束时刻的订单不计入、窗口内取消不计入、结束后退款仍计入、事件整批重发被去重、资格不足与停招各自返回原因。
4. **晚报** — 运行 A 冻结快照；迟到订单只进入运行 B；A 结果不变且可回查。
5. **互斥层与共享流量** — 同层互斥与桶位稳定、三类未入组原因分别落库、扩流只吃空闲桶位且老成员不换组、两个管理员争抢最后容量只有一个修订生效（陈旧 revision 409）、停招释放仅新来者可用、历史路由快照冻结、跨层共同曝光报告。

> 演示依赖可推进的虚拟时钟（只进不退），对同一数据库重复运行前需重置库（见下）。

## 测试

```bash
./venv/bin/python -m pytest            # 43 个用例；使用独立测试库 ccdev_test（自动创建）
```

测试库地址可用 `TEST_DATABASE_URL` 覆盖；维护连接用 `TEST_MAINTENANCE_DSN` 覆盖。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/clock`、`/clock/advance` | 可推进虚拟时钟（只前进） |
| POST/GET | `/experiments`、`/experiments/{id}` | 登记实验：受众规则、固定种子、比例、报名定义、观察天数、可选 `layer_id` |
| POST | `/experiments/{id}/ratio` | 调整比例（仅作用于后来学员） |
| POST | `/experiments/{id}/stop` | 停招（只停新分配；分层实验同时释放层内容量） |
| POST | `/experiments/{id}/assignments` | 申请分配（幂等键 + 稳定学员标识 + 设备）；返回三类未入组原因与当时路由快照 |
| POST | `/experiments/{id}/exposures` | 曝光回执（失败仍保留分配事实；分层实验同时登记共同曝光） |
| POST/GET | `/layers`、`/layers/{id}` | 建立/查看互斥层（桶位空间、revision、已用/空闲容量、各实验区间） |
| POST | `/layers/{id}/experiments/{eid}/slot` | 初次申请层内容量区间（带 `expected_revision` 乐观锁） |
| PUT | `/layers/{id}/experiments/{eid}/capacity` | 扩流（新区间必须包含旧区间，否则 409） |
| GET | `/layers/{id}/explain?student_key=` | 解释某学员为何命中/未命中（桶位与在招区间，不落库） |
| GET | `/layers/{id}/revisions[/{rev}]` | 修订历史与当时全量路由快照（历史重放依据） |
| GET | `/reports/co-exposures` | 跨层碰撞报告：实验对的共同分组及曝光人数（不提供剔除入口） |
| POST | `/events` | 合成行为流批量摄取（去重 + 水位线） |
| GET/POST | `/sources`、`/sources/{name}/watermark` | 各来源“已完整上报”时间界限 |
| POST | `/experiments/{id}/analysis` | 以当前时钟为统一截止点运行分析（冻结分层声明） |
| POST | `/analysis/{run_id}/resume` | 断点续算同一批次（截止点/快照不变） |
| GET | `/analysis/{run_id}`、`/analysis/{run_id}/students` | 结果与学员分段明细（对账） |
| GET | `/experiments/{id}/students/{key}` | 按学员回查资格、分组、成熟条件、报名依据与跨层曝光 |

## 互斥层手工验证示例（本地启动后）

```bash
# 1) 建层（1000 桶位）与两个同层实验
LID=$(curl -s -X POST http://127.0.0.1:8000/layers -H 'Content-Type: application/json' \
  -d '{"name":"layer-a","seed":"salt-a","total_buckets":1000}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
A=$(curl -s -X POST http://127.0.0.1:8000/experiments -H 'Content-Type: application/json' -d "{
  \"name\":\"exp-a\",\"seed\":\"sa\",\"observation_days\":7,
  \"ratios\":{\"control\":0.5,\"treatment\":0.5},\"layer_id\":$LID}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
# 2) A 申请 [0,300)，expected_revision 从 GET /layers/{id} 读取
curl -s -X POST http://127.0.0.1:8000/layers/$LID/experiments/$A/slot -H 'Content-Type: application/json' \
  -d '{"bucket_from":0,"bucket_to":300,"expected_revision":0}'
# 3) 分流并解释；再 PUT .../capacity 扩流（区间只能扩大），观察老成员不换组
curl -s -X POST http://127.0.0.1:8000/experiments/$A/assignments -H 'Content-Type: application/json' \
  -d '{"request_id":"r1","student_key":"stu-1"}'
curl -s "http://127.0.0.1:8000/layers/$LID/explain?student_key=stu-1"
```

## 测试账号 / 标识

本服务不设登录鉴权（内部运营工具）。可用以下标识验证：

- 演示脚本生成的实验：`demo-selection-*`、`demo-maturity-*`、`demo-dup-*`、`demo-late-*`、`demo-layer-*`、`demo-coexist-*`（`GET /experiments`、`GET /layers` 可查）
- 演示学员：`stu-dup`（多笔订单）、`stu-boundary`（结束时刻下单）、`stu-cancel`（窗口内取消）、`stu-refund-late`（结束后退款）、`stu-young`（资格不足）、`s5-*`（互斥层与跨层共同曝光）
- 手工验证示例见上

## 已执行的验证（本仓库交付前实际运行）

- `./venv/bin/python -m pytest`：**43 passed**（原 29 个单实验/事件/分析用例 + 新增 14 个互斥层用例：区间重叠与总额校验、revision 乐观锁、同层互斥与稳定桶位、三类未入组原因、扩流不驱逐老成员、停招释放仅新来者可用、并发争抢最后容量单赢家、混合操作容量恒成立、历史路由重放、跨层共同曝光报告、分析分层声明冻结、无层实验不受影响、层绑定边界）
- 并发争抢用例连续独立运行 5 次均通过（行锁 + 条件更新稳定线性化）
- `./venv/bin/python scripts/demo.py`：**35/35 项校验通过、退出码 0**（含场景 5 互斥层全流程）
- `curl /health`、`/openapi.json`、`/docs`：正常，8 个分层相关端点全部出现在 OpenAPI 3.x 文档
- 说明：本开发容器内无 Docker，Dockerfile/Compose 未实际构建；配置已经静态检查，本地开发与验证均按上文“本地开发”路径完成。重复运行 demo 前需重置开发库（虚拟时钟只进不退）：
  `psql` 连接后执行 `DROP SCHEMA public CASCADE; CREATE SCHEMA public;`，重启服务即自动建表。
