# 手机离线记录与自动补同步——设计方案（2026-07-10，未开工）

> 需求：手机应用不连服务器也能用，数据先本地缓存，连上之后自动同步。
> 结论：**可行**。推荐「壳内原生离线页 + ingest 补发通道」，复用体脂秤队列
> 与 import_raw 幂等基建，分三阶段落地（服务端半天 + 壳一天 + 增强半天）。

## 1. 关键约束（决定了技术路线）

- **局域网 HTTP 下 Service Worker 不可用**：浏览器/WebView 只在 HTTPS 或
  localhost 注册 SW。生产是 `http://NAS_IP`，**PWA 离线路线在手机上走不通**
  （现有 sw.js 只在开发 localhost 生效）。除非未来上自签 HTTPS，否则不押注 SW。
- **页面是服务端渲染**（Jinja2+HTMX）：离线时连页面都取不到，先解决「离线打得开」
  才谈「离线能记录」。
- 壳已有成熟范式可复用：ScaleScanService 的 SharedPreferences 队列 + 保序补发、
  SamsungSync 的 WorkManager 周期任务、INGEST_TOKEN Bearer 通道、
  import_raw 的 (source, record_type, external_id) 幂等去重。

## 2. 方案对比

| 方案 | 思路 | 结论 |
|---|---|---|
| A 壳内离线页 + ingest 通道 | 连不上时壳展示内置记录页，写本地队列，网络恢复 WorkManager 补发到 Bearer 接口 | ✅ **推荐**：复用全部现有基建，幂等天然成立 |
| B 页面 JS 写队列 | 页面已打开时拦 htmx:sendError 暂存、online 事件重放 | ✅ 作为阶段三增强（覆盖「页面开着突然断网」） |
| C PWA SW 离线 | SW 缓存页面快照 + Background Sync | ❌ HTTP 局域网 SW 不可用，搁置（上 HTTPS 后再议） |

## 3. 推荐架构（方案 A，三阶段）

### 阶段一：服务端补发通道（~半天）

**`POST /api/ingest/offline`**（Bearer 鉴权，与秤/手表同通道）：

```json
{"records": [
  {"type": "habit",   "client_id": "uuid", "date": "2026-07-11", "payload": {"habit_id": 3, "done_count": 1}},
  {"type": "diet",    "client_id": "uuid", "date": "2026-07-11", "payload": {"meal": "午餐", "free_text": "牛肉面", "kcal": 550, "protein_g": 25}},
  {"type": "workout", "client_id": "uuid", "date": "2026-07-11", "payload": {"session_type": "跑步", "duration_min": 30, "distance_km": 5.2, "rpe": 6}},
  {"type": "metric",  "client_id": "uuid", "date": "2026-07-11", "payload": {"weight_kg": 71.5, "sleep_hours": 7.5}}
]}
```

- **留档幂等**：全部先进 import_raw，`source='offline'`（迁移 11：ck_import_source
  词表加 'offline'），`external_id = f"{type}-{client_id}"`——重复补发自动去重，
  与秤/手表完全同构
- **归一化落库**（每类幂等语义）：
  - habit → habit_logs `ON CONFLICT (habit_id, log_date) DO NOTHING`
    （声明式「该日已做」，不用 toggle 翻转语义，重放安全；服务端校验 habit 存在且 active）
  - diet → DietLog 直插（import_raw 层已挡重复补发；payload 走现有
    `_parse_decimal` 校验口径）
  - workout → WorkoutLog `source='manual'` + `external_id='offline-{client_id}'`
    （部分唯一索引现成，天然幂等）
  - metric → `autofill_fields` 字段级回填（**标手动来源 mark_manual**，同步不可覆盖；
    只允许 metrics 页同一批数值字段白名单）
- 响应 `{received, new, skipped}`；单条失败不毒化整批（begin_nested，照 samsung_direct 通道抄）

### 阶段二：壳内离线记录页 + 队列补发（~1 天）

- **入口**：现有 showErrorPage（连不上服务器）升级为「离线模式」页
  （loadDataWithBaseURL 内置 HTML+JS，暗色风格与 App 一致）：
  - 打卡清单：习惯列表用**上次在线时缓存的副本**（壳在每次成功加载首页后，
    通过 `GET /api/reminders/digest` 或新增轻量 `GET /api/offline/bootstrap`
    拉习惯清单存 SharedPreferences）
  - 快速记饮食（餐次 + 自由文本 + 热量/蛋白可选）、记训练（类型/时长/RPE）、
    记体重——都是一屏小表单，不求全，覆盖「出门在外随手记」
- **本地队列**：ShellBridge 加 `enqueueRecord(json)`；存 SharedPreferences
  JSON 数组（照 ScaleScanService 的 KEY_QUEUE/QUEUE_MAX 模式，上限 500 条），
  `client_id` 由壳生成 UUID，`date` 取壳本地日期（时钟即真相）
- **补发**：WorkManager 一次性任务，约束 `NetworkType.CONNECTED`，网络恢复即跑；
  另在 App 启动/回前台时尝试。保序批量 POST /api/ingest/offline，成功清队列，
  失败保留下轮再试；通知栏提示「已补同步 N 条离线记录」
- **在线检测**：离线页顶部「重试连接」按钮 + 自动每 30s 探测 `/healthz`，
  连上即跳回正常页面

### 阶段三：增强（可选，~半天-1 天）

- **页面内写队列**（方案 B）：base.html 拦 `htmx:sendError`——把失败请求的
  (method, url, body) 存 localStorage，`online` 事件时重放并 toast「已补同步」。
  注意：仅对**带显式日期参数**的写操作开启（打卡已带 d、饮食表单带 log_date；
  chips 一击/照片上传要先补上显式 d，否则次日重放会记错天）；
  toggle 类翻转语义不入队（重放两次会翻回来），只入 set 语义的操作
- **壳内只读快照**：WebViewClient.shouldInterceptRequest 缓存最近成功的 GET 页面，
  离线时回放并注入「离线快照 · 截至 HH:MM」横幅（工程量中等，价值看使用习惯再定）

## 4. 边界与不做的事

- 离线不支持：照片上传/AI 识别、AI 分析、报表生成、搜索联想（都要服务端）
- 冲突策略从简：habit 同日已有记录则跳过（先到先得）；diet/workout 直插
  （import_raw 挡重复补发，不做跨设备合并——单用户单手机，冲突面极小）
- 卸载 App 丢未同步队列：可接受（通知栏常提示积压条数）
- 秤/手表数据不受影响（已有独立队列/水位线）

## 5. 实施顺序与验收

1. 迁移 11（source 词表 + 'offline'）+ `/api/ingest/offline` + pytest
   （payload 校验/幂等重放/单条失败隔离的用例）
2. 壳：bootstrap 缓存 → 离线页 UI → 队列 → WorkManager 补发 → 通知
3. 真机回归清单：飞行模式打开 App → 出离线页 → 记打卡+饮食+体重 →
   恢复网络 → 通知补同步 → 服务端各表落库且重复补发不双写
4. 阶段三视手感决定要不要做

> 开发注意照 handover.md：改模板后重建 Tailwind + 升 SW_VERSION；
> 改数据逻辑跑 `uv run pytest`；Android 构建见 android/README.md。
