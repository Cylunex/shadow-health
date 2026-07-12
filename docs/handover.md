# 会话交接（自包含，新会话从这里继续）

> 最后更新：2026-07-12。配套阅读：README（功能全貌）· docs/deploy.md（NAS 部署照单）·
> docs/mobile-sync.md（三星直读背景）· gateway/README.md（体脂秤双端）·
> docs/audit-2026-07-10.md（全面审查清单，已全清、归档备忘）·
> docs/offline-plan.md（手机离线记录方案，阶段一~三已落地，待真机回归）·
> docs/subpath-agent-plan.md（V3 批次执行计划：/shealth 子路径 + 多 Agent MCP +
> personal_data 迁移——**三段已全部完成**，见下）。
> **当前无进行中批次**：剩余事项 = 真机回归（V3/V4 壳与 APK 改动）+
> offline-plan 阶段四（视真机手感决定）+ NAS 上线切换（§1.8 与 P3 runbook，用户做）。

## ✅ 已完成：V4 优化批次（2026-07-12，六项全落地）

1. **A1 Agent 写入流水页**：GET /agent-log + /fragments/agent-log/status（5s 轮询，
   照 /scale 范本；app/routers/agent_log.py）——import_raw source='agent' 最近
   30 条流水（类型徽标 + 摘要：diet 提 meal/free_text、workout 提 session_type、
   metric 列字段名、habit 反查名字），90s 内带「新」高亮；行内「撤销」（仅
   diet/workout 且 parsed）复用 agent.delete_record（与 /api/agent/delete 同一实现，
   已抽共享函数）——留档没存行 id，workout 按 external_id='agent-{client_id}' 唯一
   索引反查、diet 按解析后全字段内容匹配（多条命中取 id 最小），撤销成功往留档行
   blob 记 {revoked_row_id, revoked_at}（raw 不动），行没了（含 MCP 端删的）如实标
   「已撤销」；顶部 sync_state('agent') 健康度；更多页入口「Agent 记录」。
   验收：INGEST_TOKEN 实发 diet/workout/metric 三条 → 流水出现带「新」→ 页面撤销
   diet（内容匹配）与 workout（ext_id 反查）均真删行 → 测试留档按 external_id 精确
   删 + 当日 mood 还原
2. **A2 mood_score 全链路**：指标页图表加「心情」（1-10 折线，_CHART_METRICS/
   _COLORS/_chart_context）；日报「身体」卡显示心情（report._BODY_FIELDS）；
   周/月快照聚合纳入 avg_mood/mood_days（review._aggregate_week +
   report._aggregate_month），卡片「心情均分」**旧快照缺字段容错不显示**（快照
   惰性落库不可再生，只影响未来快照）；tests/test_mood_chain.py 9 个口径锁
3. **A3 build_context 升级**（services/llm.py）：mood 近 30 天全序列单行；饮水类
   习惯（名字含「水」）补近 14 天逐日计数明细；围度改近一年月度采样（每月最后
   一次测量）替代 30 天首末对比。实测总量 ~1.8k 字符，远低预算
4. **C1** .env.example 补 OPENAI_API_KEY/BASE_URL/MODEL 注释（设置页可配，.env 回退）
5. **C2 复用重构（行为不变）**：ingest.py 新增共享 `_mark_raw(db, source, rtype,
   ext_id, status, error, version)` 与 `_touch_sync_state(db, source, ok, error,
   now=)`——HC/秤/三星直读/offline 全部改调（原 _mark/_mark_miscale/_mark_sd/
   offline._mark 与 6 处 sync_state 更新块删除）。136 测全绿零行为变化
6. **C3 Android postJson 统一**：新增 HttpPost.java 静态助手（tag + 连接/读取超时
   参数化），ScaleScanService(8s/8s)、OfflineStore(8s/15s)、SamsungSyncWorker.kt
   (10s/20s) 三处改薄封装调用，各自超时不变；assembleDebug 构建通过，APK 已拷
   static/shadow-health.apk（**待真机回归**）

其他：Tailwind 重建 + SW v14；全量 pytest 136 绿（127 + 9 新增）

## ✅ 已完成：V3 批次（照 docs/subpath-agent-plan.md，任务已拆三段）

1. ~~**P1 子路径适配**~~ ✅ **已完成（2026-07-12，含 SnapshotCache 计划外项）**：
   X-Forwarded-Prefix 中间件（存 scope 私有键——**不是 root_path**，那会让
   Mount(StaticFiles) 静态解析错位 404，缘由见计划 §1 落地记录）；模板全局
   u() ≈190 处替换 + tests/test_template_urls.py 裸 URL lint 防返工；重定向/
   HX-Redirect 走 deps.prefixed/redirect；登录 cookie path 收窄到前缀；
   manifest 相对化 + sw.js 自推前缀（SW v13）；SnapshotCache 按 serverUrl
   剥前缀再匹配、前缀外同域不代理；壳/网关全部拼接点核对为 baseUrl+"/api/…"。
   验收：125 测全绿 + 本地前缀代理全页面走查（htmx/图表/hx-push-url 均正常）+
   APK 构建拷至 static/shadow-health.apk。剩余：真机回归 + §1.8 上线切换（用户做）
2. ~~**P2 REST+MCP**~~ ✅ **已完成（2026-07-12，含全部增补）**：迁移 12
   （'agent'/'legacy' 词表 + mood_score 加列，metrics 页三处 + 白名单自动继承）；
   /api/ingest/agent（offline 管线 ingest_batch(source=...) 薄别名 + per-record
   明细 results[{client_id,status,row_id}]）；/api/agent/summary、
   /api/agent/report/weekly、/api/agent/foods（计划外，search_food 需要）、
   /api/agent/delete（仅 diet/workout，外部来源 403）；mcp_server/ FastMCP
   双模式（127.0.0.1:8180/mcp + --stdio）9 工具 + 60s 同参数短窗去重（理由与
   话术规则在 mcp_server/README）+ record_* 附当日累计。验收：125 测全绿
   （新增 test_agent_channel.py 22 个）+ stdio 实调 9 工具 18 项全过。
   剩余（NAS 侧）：supervisor [program:shealth-mcp]、Hermes/OpenClaw 注册、
   skill v2 与 cron 迁移——照 mcp_server/README 执行
3. ~~**P3 迁移脚本**~~ ✅ **已交付（2026-07-12，脚本就绪待 NAS 执行）**：
   scripts/migrate_personal_data.py（幂等可重跑：import_raw source='legacy'
   留档 + parse_status 门控 + 单行 begin_nested 隔离；--dry-run 整体回滚；
   源列名运行时解析；映射定案见计划 §3 落地记录）+ scripts/
   freeze_personal_data.sql（REVOKE 为主、*_frozen 改名备用）+
   docs/legacy-migration-runbook.md（NAS 一页手册：备份→dry-run→迁移→核对→冻结）。
   验收：tests/test_migrate_legacy.py 端到端（模拟源重跑四遍幂等），127 测全绿。
   **NAS 执行由用户照 runbook 做，本机未连生产**
4. 环境边界：这台 Mac 的 .env 是本地临时 PG（≠生产）；真机回归与 §1.8 上线切换由用户做

## ✅ 已完成：手机离线记录 + 自动补同步（2026-07-11，阶段一~三全落地）

照 docs/offline-plan.md（方案 A 加强版，零 PWA 依赖）实施完毕：

- **阶段一（服务端）**：迁移 11（`ck_import_source` 词表加 'offline'）；
  `POST /api/ingest/offline` + `GET /api/offline/bootstrap`（app/routers/offline.py，
  Bearer 同秤/手表；import_raw 留档 + parse_status 门控幂等 + begin_nested 单条
  隔离；归一化语义：habit→ON CONFLICT DO NOTHING、diet→直插、
  workout→manual+`offline-{client_id}`、metric→视同手动保存后写覆盖 + mark_manual）；
  tests/test_offline_ingest.py 27 测（DB 不可达自动 skip），全套 92 测绿
- **阶段二（壳）**：assets/offline.html 本地启动页（秒开、暗色、打卡清单用
  bootstrap 缓存 + 快记饮食/训练/体重）；MainActivity 启动先本地页 + 后台探测
  /healthz（在线自动切网页、离线 30s 自动重试，加载失败也回落本地页）；
  OfflineStore.java 队列（SharedPreferences，上限 500，UUID client_id，
  drain 快照式补发不丢并发新记录）+ bootstrap 缓存（成功加载后 ≥1h 重拉）；
  OfflineFlushWorker（NetworkType.CONNECTED，成功通知「已补同步 N 条」）
- **阶段三（壳）**：SnapshotCache.java——shouldInterceptRequest 同源成功 GET
  （导航 HTML + /fragments/* + /static/*）写磁盘 LRU（sha1 键，20MB，单条 3MB）；
  离线回放 + 主文档注入「📴 离线快照 · 截至 HH:MM」横幅；无缓存回本地页；
  /api//login/sw.js/uploads 及 attachment 下载不拦；302/非 200 交回原生
- 阶段四（页面内写队列）**暂缓**，真机用过后视手感决定
- APK 已本机构建通过（见下「开发注意」），**待真机回归**（清单见 offline-plan §5 第 4 条）
- **落地后做了 11 角度全量审查，修了 14 处**（2026-07-11，测试 92 个全绿）。要点：
  批级失败改 503 + parse_status 门控自愈（原 200+xmax 门控会清壳端队列、数据永久
  搁浅在 raw）；metric 改「视同手动保存后写覆盖」（原 autofill 语义会静默丢同日
  第二条离线体重）；快照代理同源判定改 Uri 规范化（原字符串前缀比对遇 WebView
  规范化 URL 会整层静默失效）；本地页 DOM textContent 渲染（习惯名 XSS 可打
  ShellBridge）；连接/读取阶段区分 + 10s 负缓存（服务器关机不再逐请求干等超时、
  在线慢页不误回快照）；快照页保持 30s 探测、连上原地 reload，横幅带「去离线记录」；
  SnapshotCache 磁盘读写全挂锁（并发写同键会损坏快照）+ ETag/304 复验 + 大文件
  Content-Length 预检；NUL 落档前剔除（JSONB 拒收，毒记录会卡死队列）+ 留档
  失败 503；date 加一年下界（坏时钟）；离线页补「改地址」按钮与饮食界值前置校验；
  Token 未配置时通知提醒（原来无声不同步）；测试清理改按 external_id 精确删
  （原来整删 source='offline' 会毁真实留档）。接受的从简语义记在 offline-plan §4
  （habit 先到先得含否决行/计数冻结）

## ✅ 已完成：LLM 双供应商 + 设置页配置（2026-07-11）

- services/llm.py 抽象成 Claude / OpenAI 双通道（`_call` 统一入口，images 由各家
  适配器拼内容块；OpenAI 走 chat.completions，gpt-5/o 系用 max_completion_tokens、
  兼容端点用 max_tokens；错误映射同款中文口径）
- 配置存 `app_settings['llm_config']`（设置页「AI 模型」卡片维护，**改完即生效
  不用重启**）：provider 切换 + 两家各自的 模型/API Key/Base URL；字段留空回退
  .env（ANTHROPIC_API_KEY/LLM_MODEL、OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL）
  与内置默认（claude-opus-4-8 / gpt-5.1）。Key 掩码显示、留空不变、输「清除」删；
  「测试连接」按钮打真端点验证连通性（错误映射已实测 401 路径）
- OpenAI 兼容端点（DeepSeek/Ollama/oneapi 等）填 Base URL 即可用；
  AI 分析、问答、餐照识别三处共用这套配置（is_configured/analyze_meal_photo
  等签名都带 db 了）。tests/test_llm_meal_parse.py 增配置解析口径锁，97 测全绿

## 当前状态（截至 42883e8）

- 功能面已对标 Keep/薄荷/MFP 补齐：四大模块 + 餐次拍照 AI 识别（Claude Vision）+
  三大营养素 + 今日三环 + 饮食连击 + 达标预测 + 动作库(68)/内置计划(5) + 计时器 +
  周负荷(sRPE) + 成就卡 + 每日提醒（壳内通知）
- 数据通道：三星 zip 导入 / 三星 Data SDK 直读（Android 壳内置，真机已通）/
  小米体脂秤 2 BLE 双监听（手机+NAS 网关，服务端按秤时间戳去重）
- 三代理全面审查完成，18 处修复（睡眠跨源翻倍、水位线、修订传播、双端舍入等），
  关键数据逻辑有 28 个 pytest 回归锁（`uv run pytest`）
- 开发环境：Mac 临时 PG(55433) + `uv run uvicorn`（launch.json）；真实数据在生产 PG
  （172.22.169.180:55432，**角色未建**，见 deploy.md §1）

## ✅ 已完成：日报 + 月报 + 报告中心（2026-07-10）

- **报告中心** `GET /report?t=daily|weekly|monthly`（app/routers/report.py，
  tab 片段 `GET /fragments/report/tabs`）；更多页入口「周报」→「报告」，
  旧 `/review` 列表 303 到 `/report?t=weekly`（周报详情 `/review/{week_start}` 不变）
- **日报** `GET /report/daily?d=`：无表纯聚合——三环（复用 today._rings）、
  饮食四项 vs 目标（复用 diet._summary_ctx + diet_summary 片段）、训练明细+sRPE 负荷、
  打卡清单、体重/体成分、睡眠跨源去重（services/sleep）；入口：今日页日期行、
  报告中心日报 tab（近 14 天批量聚合）；饮食页同款翻天导航
- **月报** `monthly_reviews` 表（迁移 08，含 updated_at 触发器）+
  `GET/PUT /report/monthly/{month_start}`，惰性生成上一完整月（照周报模式）；
  快照口径见 report.py 模块 docstring（有氧达标周 = 周一落在本月的整周）；
  llm.build_context 已接入最近 3 份月报快照
- 回归：tests/test_monthly_report.py 锁月份边界/周归属/连击口径（42 个测试全绿）

## ✅ 批次四已全量落地（2026-07-10：F1-F8 全部功能 + U1-U10 全部优化）

audit 文档全清。要点（详情见 audit-2026-07-10.md 各条）：
- **饮食**：整餐一键复制（POST /diet/meals/copy）· 能量收支缺口行（diet_summary，
  BMR+活动消耗）· 今日页拍照直达（meal 按时间预选）· 全局绿色成功 toast
  （HX-Trigger JSON 带 toast 事件）· chips 按当前餐次排序 · 搜索按 90 天频次排序
  （带「常吃」徽标）· 日期胶囊点开日历跳转（饮食页/日报页）
- **训练**：计时器练完一键记入 + 跟练模式（/workout/timer?exercises=…，动作库
  每分类「跟练这组」入口）· 模板打卡自动带上次同类型时长/RPE（detail.auto_filled）·
  配速显示（deps.pace_str 模板全局）+ 指标页「跑步」趋势图（配速+距离双线）·
  力量组次明细（表单折叠区 → detail.strength，**services/pr.py** 算 PR 与
  3×15 进阶提示，动作库页展示）
- **习惯**：auto_rule 派生字段 workout_min/diet_count/sleep_start_clock
  （**迁移 10** 给存量「23点前睡/称重×2/量腰围×1」补规则；weekly 习惯 auto
  每天最多 +1）· 打卡三端点支持 d 补记日期（≤30 天），日报页翻到昨天可点补卡
- **提醒**：digest 先跑 auto_rule 再算缺口 + 「还没记饮食」+ 周后半 weekly 缺口
- **其他**：/settings/backup 一键全量备份（全表 CSV + 照片 zip，唯一带照片出口）·
  AI 分析改后台线程 + app_settings 任务态轮询（锁屏/切应用不再作废）·
  chip 全局 htmx-request 加载态。SW v10。测试 65 个全绿。

## ✅ 审查缺陷已全部修复（2026-07-10，24 缺陷 + 存疑 2）

docs/audit-2026-07-10.md 全部勾完。要点：月报冻结等末周结束（_month_frozen_after）+
周/月报列表回填缺口 + 详情翻页导航；samsung_direct daily 水位线按日期比较；zip 睡眠
回填走跨源去重；Keep 去重清单补 samsung_direct；auto_rule 撤销否决（**迁移 09**：
habit_logs.done_count 允许 0 = 否决行）；三环/计划卡/打卡分区 HTMX 被动刷新补齐；
base.html 全局错误 toast + 「更多」导航高亮扩展；SW v9（cache:reload 防固化旧资源、
跳过 .apk）；导入上传 500MB 上限；网关看门狗+本地队列（compose 挂 /data 卷）；
壳 WebChromeClient/文件选择器/DownloadListener + 秤本地补发队列。
新增 tests/test_review_habit_rules.py（15 个口径锁），57 测试全绿。

## 其余待办（优先级序）

1. **真机回归**（Android 壳与网关改动后必做；本次离线三阶段全是壳改动，装机必验）：
   - **离线四项**（offline-plan §5 第 4 条）：①冷启动秒出本地页、在线自动进网页
     ②飞行模式本地页记打卡+饮食+体重 → 恢复网络 → 通知「已补同步 N 条」→
     服务端落库、重复补发不双写 ③飞行模式打开最近看过的页面 → 快照 + 离线横幅
     ④队列积压跨 App 重启不丢
   - 快照拦截是全量 GET 代理，重点回归在线路径无回归：登录跳转、htmx 片段刷新
     （HX-Trigger 透传）、CSV 导出下载（attachment 放行）、拍照记餐上传
   - 秤「称重模式」：今日页/离线页/秤接收状态页点「⚖️ 开秤监听 3 分钟」→ 上秤 →
     通知「已记录 xx kg」→ 3 分钟后通知自动消失（常驻开关关闭时才走限时；
     普通浏览器里按钮应不可见）。**GET /scale「秤接收状态」页**（更多页入口）
     每 3s 轮询 import_raw/sync_state，上秤后几秒内出带「新」标的记录——
     排查秤问题先开这页
   - 历史项：壳内 hx-confirm 删除弹系统确认框、导入 zip 文件选择器、
     断服上秤后恢复补发；NAS 网关重建镜像验证看门狗与 /data 队列。
     顺带做心率同步回归（查 daily_activity.hr_min/hr_max）
2. **NAS 部署**（用户明确"这里部署不了生产"，等到 NAS 环境照 deploy.md 执行；
   生产库角色未建是第一步；注意迁移 08/09 会随容器启动自动应用）
3. 候选池：功能/UX 建议见 audit 文档 F1-F8/U1-U10（F8 即原「力量组次明细+PR」，
   detail JSONB 就绪）、GPS 轨迹地图（依赖外网瓦片，违背断网原则，做成可选）、
   Keep API（搁置）

## 开发注意（换机器/新会话易踩）

- 改模板后：`npx -y tailwindcss@3.4.17 -c tailwind.config.js -i static/src/input.css
  -o static/app.css --minify`，并升 `static/sw.js` 的 `SW_VERSION`
- 改动数据逻辑后跑 `uv run pytest`；uvicorn --reload 会清内存 session（重新登录）
- Android 构建见 android/README.md；AAR 在 android/app/libs/（不入仓）。
  **这台 Mac 可以直接构建**（此前交接说不能是错的）：SDK 在
  /opt/homebrew/share/android-commandlinetools（local.properties 已配），
  默认 JDK 是 11，要用 `JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
  ./gradlew assembleDebug` 构建；真机回归仍不可省（构建通过 ≠ 运行验证）
- 提交身份：仓库 local 配置已设 Cylunex
