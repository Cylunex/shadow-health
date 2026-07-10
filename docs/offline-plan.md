# 手机离线记录与自动补同步——设计方案（2026-07-11 定稿加强版）

> 需求：手机应用不连服务器也能用，数据先本地缓存，连上之后自动同步。
> 结论：**可行，全程不依赖 PWA/Service Worker**（用户 2026-07-11 确认走加强版）。
> 架构：「壳本地启动页（秒开）+ 离线记录队列 + ingest 补发通道 + 原生页面快照」，
> 复用体脂秤队列与 import_raw 幂等基建。约 3 天：服务端半天 + 壳启动页与队列
> 1-1.5 天 + 快照缓存 1 天；页面内写队列为可选阶段四。
>
> **落地状态（2026-07-11）：阶段一/二/三已全部完成**（代码清单见各阶段标题），
> APK 已本机构建通过；待真机回归（清单见 §5 第 4 条）。阶段四暂缓，视手感决定。

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
| A 壳内离线层 + ingest 通道（**加强版，已选定**） | 壳启动秒开本地页，离线可记录（本地队列 + WorkManager 补发），在线无缝进网页；原生快照缓存让常看页面离线可读 | ✅ 复用全部现有基建，幂等天然成立，零 PWA 依赖 |
| B 页面 JS 写队列 | 页面已打开时拦 htmx:sendError 暂存、online 事件重放 | ✅ 作为可选阶段四（覆盖「快照页上突然断网时的写操作」） |
| C PWA SW 离线 | SW 缓存页面快照 + Background Sync | ❌ HTTP 局域网 SW 不可用，搁置（上 HTTPS 后再议） |
| D 全原生 App 重写 | 原生 UI + 本地 SQLite + 双向同步引擎 | ❌ 几周级重写、之后每个功能写两遍、双库同步复杂度高——单人局域网场景不值（用户 2026-07-11 确认不走） |

## 3. 定稿架构（方案 A 加强版，四阶段）

### ✅ 阶段一：服务端补发通道（已落地：迁移 11 + app/routers/offline.py + tests/test_offline_ingest.py 24 测）

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

### ✅ 阶段二：壳本地启动页（秒开）+ 离线记录队列（已落地：assets/offline.html + OfflineStore/OfflineFlushWorker + MainActivity 改造）

- **本地启动页取代直连加载**：壳启动先 loadDataWithBaseURL 内置本地页
  （毫秒级出屏，永不白屏），同时后台探测 `/healthz`：
  - 在线（局域网内 ~100-300ms）→ 自动跳转服务器页面，体感只是闪一下启动屏
  - 离线 → 停留本地页直接可用；顶部显示「离线中 · 已暂存 N 条」+「重试连接」，
    并每 30s 自动探测，连上即跳转
  - 原 showErrorPage（加载中途失败）也回落到同一本地页
- **本地页内容**（暗色风格与 App 一致，内置 HTML+JS）：
  - 打卡清单：习惯列表用**上次在线时缓存的副本**（壳在每次成功加载首页后，
    经新增轻量 `GET /api/offline/bootstrap`（Bearer）拉 active 习惯清单存
    SharedPreferences；顺带缓存常用训练类型）
  - 快速记饮食（餐次 + 自由文本 + 热量/蛋白可选）、记训练（类型/时长/RPE）、
    记体重——一屏小表单，不求全，覆盖「出门在外随手记」
- **本地队列**：ShellBridge 加 `enqueueRecord(json)`；存 SharedPreferences
  JSON 数组（照 ScaleScanService 的 KEY_QUEUE/QUEUE_MAX 模式，上限 500 条），
  `client_id` 由壳生成 UUID，`date` 取壳本地日期（时钟即真相）
- **补发**：WorkManager 一次性任务，约束 `NetworkType.CONNECTED`，网络恢复即跑；
  另在 App 启动/回前台时尝试。保序批量 POST /api/ingest/offline，成功清队列，
  失败保留下轮再试；通知栏提示「已补同步 N 条离线记录」

### ✅ 阶段三：原生页面快照缓存——离线可读（已落地：SnapshotCache.java，MainActivity.shouldInterceptRequest 接入）

- WebViewClient.`shouldInterceptRequest` 把**成功的 GET 响应**写壳内磁盘缓存
  （URL 哈希做键，LRU 上限 ~20MB）：导航 HTML、`/fragments/*`（今日页的计划卡/
  习惯/饮食区块都是 hx-get 片段，不缓存片段则离线页面只剩骨架）、`/static/*`
- 离线时（探测失败/请求错误）同 URL 回放缓存，并对 HTML 注入
  「📴 离线快照 · 截至 HH:MM」顶部横幅；无缓存 → 回本地启动页
- 只缓存 GET；POST/PUT/DELETE 永不缓存。快照页上的写操作离线时会失败，
  由 base.html 全局 toast 报「网络错误」——引导回本地页记录（或做阶段四）
- 缓存按 URL 覆盖写（永远是最后一次成功响应），无 TTL，横幅时间戳即真相

### 阶段四：页面内写队列（可选，~半天）

- base.html 拦 `htmx:sendError`——把失败请求的 (method, url, body) 存
  localStorage，`online` 事件时重放并 toast「已补同步」。
  仅对**带显式日期参数**的写操作开启（打卡已带 d、饮食表单带 log_date；
  chips 一击/照片上传要先补上显式 d，否则次日重放会记错天）；
  toggle 类翻转语义不入队（重放两次会翻回来），只入 set 语义的操作
- 做完这条，快照页（阶段三）上的离线写操作也能暂存，闭环完整

## 4. 边界与不做的事

- 离线不支持：照片上传/AI 识别、AI 分析、报表生成、搜索联想（都要服务端）
- 冲突策略从简：habit 同日已有记录则跳过（先到先得）；diet/workout 直插
  （import_raw 挡重复补发，不做跨设备合并——单用户单手机，冲突面极小）
- 卸载 App 丢未同步队列：可接受（通知栏常提示积压条数）
- 秤/手表数据不受影响（已有独立队列/水位线）

## 5. 实施顺序与验收

1. ✅ 阶段一：迁移 11（source 词表 + 'offline'）+ `/api/ingest/offline` +
   `/api/offline/bootstrap` + pytest（payload 校验/幂等重放/单条失败隔离，
   全套 89 测通过）
2. ✅ 阶段二：bootstrap 缓存 → 本地启动页（秒开 + 探测跳转）→ 队列 →
   WorkManager 补发 → 通知
3. ✅ 阶段三：shouldInterceptRequest 快照缓存 + 离线横幅 + LRU 上限
4. ⏳ 真机回归清单：①冷启动秒出本地页、在线自动进网页 ②飞行模式：本地页记
   打卡+饮食+体重 → 恢复网络 → 通知补同步 → 服务端落库、重复补发不双写
   ③飞行模式下打开最近看过的页面 → 出快照 + 离线横幅 ④队列积压跨 App 重启不丢
5. 阶段四视手感决定要不要做

> 开发注意照 handover.md：改模板后重建 Tailwind + 升 SW_VERSION；
> 改数据逻辑跑 `uv run pytest`；Android 构建见 android/README.md。
