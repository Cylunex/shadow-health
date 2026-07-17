# 会话交接（自包含，新会话从这里继续）

> 最后更新：2026-07-16（V8 批次落地：NAS 上线后首批使用反馈）。配套阅读：
> README（功能全貌）· docs/deploy.md（NAS 部署照单）· docs/mobile-sync.md
>（三星直读背景）· gateway/README.md（体脂秤双端）· docs/audit-2026-07-10.md
>（全面审查清单，已全清、归档备忘）· docs/offline-plan.md（手机离线记录方案，
> 阶段一~三已落地，待真机回归）· docs/subpath-agent-plan.md（V3 批次计划，
> 三段已完成）· mcp_server/README.md（MCP 16 工具 + 话术规则 + NAS 部署）。
> **NAS 已上线、用户日常使用中**。当前无进行中批次：剩余事项 = 真机回归
>（V3/V4/V8 壳与 APK 改动，V8 重点是多服务器切换）+ offline-plan 阶段四
>（视真机手感决定）+ NAS 侧 MCP 接入（supervisor/Hermes/OpenClaw 注册照
> mcp_server/README，用户做）。血压计 BLE 用户已明确**不做**。

## ✅ 已完成：V8.1 批次（2026-07-16，饮食两项，275 测全绿）

1. **饮食页补全三大营养素展示**：记录行 meta 从「克数·kcal·蛋白」补到
   「克数·kcal·蛋白·碳水·脂肪」（diet_log_row，非空才显示）；餐次卡头部 kcal
   下加三大营养素小计行（_day_ctx 餐组新增 protein/carb/fat 合计，任一非零
   才渲染）。日汇总卡四项 vs 目标本来就有，未动
2. **free_text 新食物自动进食物库**（diet._auto_catalog_food，三通道共用）：
   自由文本记录带**克数 + 热量**时按每 100g 折算自动建档（Food.notes 标
   「自动建档（来自饮食记录）」），下次搜索联想/常吃 chips 直接可选、营养
   按用量服务端重算。门槛：名字 ≤20 字（更长视为整句描述防污染搜索）、
   折算超生理上限（>900 kcal/100g 或单宏量 >100g/100g）不建、**重名跳过
   不覆盖**（手工维护值优先）。接线三处：UI POST /diet/logs 与 PUT 编辑
   free_text 路径（toast 附「新食物已入库」）、AI 餐照识别循环（提示附
   「N 个新食物已入库」）、offline._normalize_diet free_text 路径（离线
   本地页 + agent/MCP 通道同享）。整餐复制/组合模板不触发（复制的行当初
   已建过档）。测试 tests/test_diet_autocatalog.py 12 个（折算/浮点输入/
   重名不覆盖/五组门槛/整句名/UI 与 offline 通道链路——日期用 2020-04，
   食物按名精确清理）；SW v19

## ✅ 已完成：V8.3 批次（2026-07-17，frp Basic 验证支持，279 测全绿）

用户 frp 入口加了 HTTP Basic 验证（http_user/http_password）。方案 =
**地址内嵌凭据**：连接设置里外网地址写成 `http://用户:密码@frp域名`。

1. **服务端**：ingest._bearer_reject 加备用头 **X-Ingest-Token**（Authorization
   Bearer 合法时优先；被 frp Basic 占用时壳把 app token 挪到备用头）。全部
   Bearer 端点（ingest/agent/offline/reminders）共用该 helper 自动继承
2. **壳 ServerConfig**：bare()（去 user:pass@，WebView 加载/展示/rebase/同源
   比较全用裸地址）+ basicAuthHeader()/basicAuthHeaderForUrl()/
   credentialsForHost()；probe() 探活带 Basic 头；rebase 改按 bare 比较
3. **壳各请求通道**：HttpPost.postJson 自动判 URL 凭据（有 → Authorization
   Basic + X-Ingest-Token，无 → Bearer 原语义），新增 applyAuth 给 GET 共用
   （OfflineStore.fetchBootstrap / Reminders.fetchDigest 改造）；WebView
   onReceivedHttpAuthRequest 按 host 自动应答 Basic 挑战（POST/登录/sw.js 等
   非代理请求靠它）；SnapshotCache.fetchAndStore 代理注入 Basic 头（不透传
   WebView 的 Authorization）；DownloadListener 下载请求补 Basic 头
   （DownloadManager 不走 WebView 认证缓存）
4. 测试 +4（Bearer 原语义/X-Ingest-Token 通过/错 token 401/Bearer 优先）；
   全量 279 绿；APK 已重构建拷 static/。**需服务端与 APK 同时升级**：旧服务端
   收到 X-Ingest-Token 会 401（只在 frp 带凭据地址时才用备用头，内网 Bearer
   不受影响）
5. 真机回归补充：frp 地址带凭据 → ①外网打开页面不弹认证框直接进 ②登录/
   htmx 片段/照片上传正常 ③上秤/离线补发/三星同步/每日提醒过 frp 成功
   ④CSV 导出下载成功 ⑤快照缓存离线回放正常

## ✅ 已完成：V8.2 批次（2026-07-16，训练日历两项，275 测全绿）

1. **训练日历加 1 个月选项**：HEATMAP_MONTHS_OPTIONS (3,6,12)→(1,3,6,12)，
   模板/端点零改动（chips 由 options 渲染、months 校验白名单自动继承）
2. **日历日详情行可展开**：workout_day_detail.html 行加与 workout_log_row
   同款详情面板（开始时间/时长/距离/配速/消耗/平均最大心率/原始标题/
   原始类型/备注，Alpine 折叠 + chevron），有隐藏字段才可点；SW v20
3. **静息心率结论（用户问）**：翻 AAR 确认 Samsung Health Data SDK 1.1.0
   只有 HeartRateType（HEART_RATE/MIN_HEART_RATE/MAX_HEART_RATE/SERIES_DATA），
   **没有独立静息心率类型**——应用现状（DailyActivity.hr_min 日最低当静息
   代理，心率图/RHR 基线/准备度共用）就是能拿到的最优解，不改。顺带发现
   SDK 有 EnergyScoreType（三星自家准备度分），记为后续候选

## ✅ 已完成：V8 批次（2026-07-16，NAS 上线后使用反馈四项，263 测全绿）

1. **壳多服务器地址**（android/ ServerConfig.java 新增）：连接设置对话框改多行
   （每行一个地址，靠前优先——内网在上、frp 外网在下，去重规范化）；prefs 存
   `server_urls`（换行分隔清单）+ `server_url`（活动地址，探测通了谁写谁，
   老安装单地址兼容）；resolve() 按序 GET /healthz（4s 超时，活动地址优先、
   零切换成本）；MainActivity 探测循环换 resolve + 断点深链 rebase（服务器切换
   时换源保路径，含子路径前缀）；SnapshotCache.intercept 改按请求 origin 匹配
   清单（sameOrigin 开放 package 级，各 origin 独立缓存条目）；后台四通道
   （OfflineStore.drain/fetchBootstrap、ScaleScanService 上报与补发、
   SamsungSyncWorker、ReminderWorker）全部改 resolveOrActive()（可达优先，
   全不通退回活动地址走各自原有失败重试；探测均已挪到 IO/Worker 线程）。
   **注意 cookie 按 origin 隔离，每个地址首次使用需各自登录一次**（对话框
   hint 已写明）。APK 已构建拷 static/shadow-health.apk，**待真机回归**
2. **运动类型中文化**（deps.session_label + 模板全局）：三星直读/zip/HC 写入的
   英文 session_type（walking/running/circuit_training/cycling…60+ 词表）展示层
   翻中文；手动录入的中文原样透传；未知英文只把下划线换空格；空值回退「训练」。
   **仅展示层，库里保持原值**——跑步图关键词（metrics._RUN_KEYWORDS）、跑步 PR
   （pr._is_run）、Keep 去重都依赖原文。覆盖点：workout 行/日历日详情/日报训练卡/
   年度回顾 top3/agent-log 摘要。tests/test_v8_features.py 有「导入词表全覆盖」锁，
   新增枚举映射缺失会先红
3. **运动记录详情展开**（workout_log_row.html 重构）：行上有隐藏字段
   （started_at/calories/avg_hr/max_hr/detail.title——手表同步数据全在这）时
   可点开详情面板（开始时间/时长/距离/配速/消耗/平均与最大心率/原始标题/
   原始类型/备注，Alpine 折叠）；外部来源行右侧「只读」锁换成「详情/收起」钮
   （没有隐藏字段的仍显示锁）；manual 行编辑/删除不变，点正文也可展开。
   新增 deps.local_hm（tz-aware → 本地 HH:MM）
4. **指标自定义显示**（app_settings['metrics_hidden_fields']）：指标页「记录指标」
   卡头 ⚙ 自定义显示面板（21 个字段勾选，POST /metrics/display 存隐藏清单，
   HX-Trigger metrics-changed 让表单/历史/图表一起刷新）；录入表单隐藏未勾字段
   （「更多指标」折叠区全隐藏时整块不渲染）；历史表整列隐藏（血压两字段都藏才
   去列）；**只依赖 BodyMetrics 手录字段的图表选项**（体重/体脂/体成分/血压/
   血氧/心情/围度）在依赖字段全隐藏时从 chips 去掉，手表驱动图（心率/步数/
   睡眠/负荷/跑步/入睡时刻）不受影响；当前图表被隐藏时退默认（_default_metric）；
   今日页 quick 面板同步遵守。**只影响展示**——隐藏字段既有数据不动、秤/手表/
   Agent 自动写入照常
5. 测试：tests/test_v8_features.py 25 个（session_label 词表与透传/local_hm/
   图表选项过滤/隐藏配置 roundtrip 与词表外键过滤/表单与历史列联动/默认图表
   回退/display 端点/页面渲染——不占测试错峰日期，display 配置 fixture 收尾还原）；
   全量 263 绿；Tailwind 重建 + SW v18
6. 本机预览已走查：workout 行详情展开/中文化/指标 ⚙ 面板保存后表单+历史列+
   图表 chips 三处联动（血压隐藏后血压图 chip 消失）

## ✅ 已完成：V7 批次（2026-07-12，roadmap 顺延清单，238 测全绿）

1. **B4 跑步进阶包**：services/pr.py 加 cardio_prs（最长单次/最快均配速仅认
   ≥3km/单月最大跑量/累计），workout 页「跑步 PR」卡（无带距离跑步记录不显示，
   dev 数据全是 walking 故当前隐藏——正确门控）；跑步图加第三条线有氧效率
   EF=速度(m/min)÷心率（时长加权均心率，仅 avg_hr 非空日）——同配速心率变低
   = 有氧基础变好
2. **H2 年度回顾** GET /report/annual?y=（更多页入口）：年聚合卡片墙——训练
   次数/分钟/负荷、跑量+类型 top3、体重与体脂年变化（_first_last_change 复用）、
   饮食天数+最长连击、习惯达标天次、步数、睡眠均值、今年解锁的成就徽章；
   年份 chips 从最早数据年到今年；进行中年份标注
3. **B5 体测协议**：迁移 14 fitness_tests（(test_date,item) 唯一，重测覆盖）；
   /fitness 页（更多页入口）——四项协议（俯卧撑力竭/平板支撑秒/坐位体前屈
   ±cm/1 分钟心率恢复），0-100 分线性锚（40 次/180s/+15cm/40bpm，体前屈从
   -10 起算量程）+ 优秀/良好/待提高分档 + 较上次差值 + Chart.js 雷达图
   （最新 vs 上次，__ensureChartJs 同款按需加载）；digest 距上次体测 ≥6 周
   提醒（从未测过不催）
4. **B1 计时器语音口令**：workout_timer speechSynthesis 播报（开始+动作名/
   休息+下一动作预告/新一组/完成），🔊 开关存 localStorage，'speechSynthesis'
   feature-detect 不支持即隐藏——**WebView 内 TTS 可用性待真机回归**
5. **D3 条码 + OFF 离线库**：迁移 14 foods.barcode（唯一）+ off_products 缓存表；
   scripts/import_off_products.py（官方全量 CSV 流式导入，69 前缀或
   countries=china，幂等 upsert，--dry-run；NAS 上跑，见脚本 docstring）；
   GET /diet/barcode/{code}（foods 优先 → off_products「建档并可记录」一键
   进自建库挂条码 → 没查到提示手动建档）；饮食页「扫码记录」卡
   （BarcodeDetector+getUserMedia 扫码，feature-detect 隐藏相机钮，手输条码
   兜底永远可用）——**壳内相机权限需 WebChromeClient onPermissionRequest
   放行，真机回归项**
6. **G6 化验档案**：迁移 14 lab_results（(report_date,item_key) 唯一）；/labs 页
   （更多页入口）——常见 10 项词表（血脂四项/血糖/糖化/尿酸/肝肾）带默认单位
   与参考范围（化验单原范围优先可改）+ 自定义项目；按项目分组多年序列，
   超范围 ↑↓ 标黄（就医确认提示，不做诊断）；AI 拍化验单结构化（vision →
   可编辑预览 → 确认入库 /labs/bulk，同名与词表对齐保趋势连续；无 Key 隐藏
   入口，手动录入永远可用）
7. **G3 BLE 血压计——本批不做（有意）**：omblepy/hass-omron 协议按具体型号
   （HEM-7361T 等）逐款适配，手上没有设备盲写协议必然不可验证；等用户确认
   血压计型号后照小米秤模式（gateway/ 或壳内 BLE）单独立项。血压手输通道
   与趋势图（V6 E7）已可用
8. 测试：tests/test_v7_features.py 8 个（体测锚点分/化验超范围方向/条码归一化/
   OFF 解析口径/跑步关键词/体测与化验 DB 链路——错峰日期 2020-03-01）；
   全量 238 绿；Tailwind 重建 + SW v17
9. 真机回归新增项：计时器 TTS（WebView speechSynthesis）、扫码相机权限
   （getUserMedia + BarcodeDetector，壳需 onPermissionRequest 放行 CAMERA）、
   /fitness /labs /report/annual /achievements 四个新页面走查 + SnapshotCache
   排除名单核对
10. **Agent skills（skill v2 实体）**：mcp_server/skills/ 四个技能文档——
    recorder（记录与纠错，food_id 优先/反假确认/update 与 delete 流程）、
    morning-briefing（晨间简报，digest 一次拿全+体征预警优先）、
    weekly-review（周月复盘播报+能量账本对账话术+run_analysis 轮询规矩）、
    analyst（按问题粒度选工具，get_health_context 优先，三板斧套路）。
    NAS 注册 Hermes/OpenClaw 时把文件内容挂进各自技能库（README 有挂载说明），
    cron 只发触发语

## ✅ 已完成：V6 批次「面向未来」（2026-07-12，照 docs/future-roadmap.md 推荐组合 P1-P8，230 测全绿）

1. **P1 睡眠洞察**：services/sleep.py 加 night_rows/stage_stats/fmt_bedtime（效率=
   总睡眠÷在床钳 100%、深睡/REM 占比、入睡时刻按「距中午分钟数」跨午夜连续可比、
   就寝规律性=标准差）；指标页「入睡时刻」图（24=午夜口径）；日报睡眠卡加
   效率/深睡占比/入睡时刻；_aggregate_week/_aggregate_month 加 avg_sleep_h/
   sleep_ok_days(≥7h)/avg_bedtime/bedtime_std_min/deep_pct + 周/月快照「睡眠」卡
   （**旧快照缺字段容错不显示**，照 avg_mood 模式）
2. **P2 图表欠账**：指标页新图——体成分（脂肪量=体重×体脂% 与肌肉量双线）、
   心率（hr_min 静息代理 + hr_avg）、血氧、训练负荷（见 P3）；bp 图无个人目标时
   画 130 固定参考线；历史表 bp≥130/85、SpO2<94 标黄（琥珀非红）；
   _weight_trend_hint 泛化为 _metric_trend_hint（体重/体脂/腰围三图共用达标预测，
   _TARGET_KEYS 加 body_fat/girth）；设置页目标值加 目标体脂率/目标腰围；
   计数型习惯（target>1 daily）管理行加近 30 天 done_count 纯 div 条形图（绿=达标）
3. **P3 负荷与恢复（services/readiness.py）**：ACWR（7/28 天均比，<0.8 低/
   0.8-1.3 适中/1.3-1.5 偏高/>1.5 预警）+ Foster 单调性（>2 提示，恒定负荷封顶
   9.9）——训练页负荷卡显示，周报快照存 acwr（容错显示）；CTL/ATL/TSB 42/7 天
   EWMA 曲线（指标页「训练负荷」图，**取数带 3×42 天预热**，不足会系统性偏低）；
   RHR 基线（hr_min 7 日均 vs 28 日均 +5bpm 告警）；**每日准备度 0-100**（昨夜
   睡眠/前日负荷/RHR/主观 各对 28 天基线 z 分→50+20z 钳 0-100→加权 .35/.25/.25/.15
   缺项摊权，全缺不出分）今日页卡片（分数环+建议，恢复优先措辞中性）
4. **P4 自适应能量引擎（services/energy.py，MacroFactor 思路）**：体重趋势线
   （时间感知 EMA，日 α=0.10，断档按 (1-α)^n 补偿）叠加在体重图；TDEE 反推
   （28 天窗，门槛：饮食≥14 天+体重≥8 点跨度≥14 天，TDEE=均摄入−趋势Δkg×7700÷天）；
   周报 tab 顶「每周 Check-in」卡（实测代谢+下周建议热量=TDEE+速率×7700÷7 钳
   TDEE±1000 且 ≥1200，差 <75 kcal 不折腾；「应用为新目标」写 target_kcal）；
   周/月报「能量账本」卡（累计缺口→理论 kg vs 实际趋势 kg 对账，只累计「有饮食
   记录且 BMR 可知」的天）；设置加 energy_rate_kgpw（缺省 -0.35）/
   energy_train_day_offset（>0 且当日有训练 → 饮食页目标上浮 N% 带「训练日」标）
5. **P5 组合菜谱**：迁移 13 meal_templates（name 唯一 + items JSONB）+
   achievements 两表；饮食页每餐「☆ 存组合」（prompt 组合名走表单值防 HX-Prompt
   头非 Latin-1 报错，同名覆盖）→ 组合 chips 一键整组记录（food_id 行按食物库
   现值重算、食物已删跳过）；「复制上次」与餐次频次 chips 本就有（批次四）
6. **P6 洞察引擎（services/insights.py）**：只做 6 组预设配对分桶（防伪相关），
   双桶各 ≥8 样本 + 差异过阈值才输出：睡眠→次日精力/摄入/步数、高负荷→次日心情、
   23 点后入睡→次日晨勃率、训练日→当日心情；月报详情「数据洞察」卡（滚动 90 天
   实时算，不进冻结快照）；llm.build_context 注入晨勃序列+晨勃率、主观睡眠质量
   均值、洞察结论行（E4：此前 AI 完全看不见这两个作息信号）
7. **P7 主动预警**：vitals_alert（睡眠时长/hr_min/SpO2 各 28 天滚动中位数±3×MAD
   个人典型区间，只看坏方向，**≥2 项同时越界才报**）→ 今日页琥珀横幅 +
   digest；digest 升级晨间简报：体征预警 > 准备度一句话（分数+band+建议）>
   新成就 > agent 告警 > 缺口清单（全离线规则模板，无 AI 依赖）
8. **P8 成就 + 进食窗口**：services/achievements.py 13 枚长期主义徽章（饮食连击
   7/30/100、跑量 100/500/1000km、训练 1k/5k/10k 分钟、习惯达标 100/500 天次、
   连续 7 夜睡够、力量档案 5 动作）——本体实时计算，achievements 表只记首达日期，
   digest 播报新达成一次；/achievements 徽章墙（更多页入口）；日报饮食卡
   「进食窗口 HH:MM–HH:MM（Nh）」（仅当天记录的时间戳，补记不算）；
   备份导出白名单补 meal_templates/achievements
9. **H5 去羞辱化（横切）**：审查确认现有超标呈现已是琥珀非红；新卡片全部
   趋势语言（能量账本「理论 vs 实际」、Check-in「调整目标」、建议「恢复优先」）
10. 测试：tests/test_v6_engines.py 12 个纯函数锁（ACWR 分档/EWMA 收敛与 TSB
    方向/组件分方向与缺项摊权/EMA 滤噪断档/TDEE 能量守恒与门槛/建议钳制/
    入睡时刻跨午夜/最长连击/分桶门槛）；全量 230 绿；Tailwind 重建 + SW v16
11. 注意：EWMA 预热必须 ≥3×42 天（服务与图表取数都已带）；周/月快照新字段只
    出现在 V6 之后生成的快照（旧快照容错隐藏）；insights/checkin/readiness
    数据门槛不达一律不显示（宁缺毋滥）；准备度卡与 agent-fresh 同用
    「outerHTML 自替换 + 空态 hidden」模式

## ✅ 已完成：V5 批次「Agent 深度使用」（2026-07-12，五段全落地，218 测全绿）

1. **P1 读面扩展**：GET /api/agent/context?days=N（直出 llm.build_context，与内置
   AI 注入完全一致）、/api/agent/report/monthly?month=YYYY-MM（report._aggregate_month
   同一查询，complete 口径照 weekly）、/api/agent/metrics/series?field=&days=
   （metrics._bm_field_map 同一取数，白名单 _FIELD_DEFS 20 字段 + steps，带
   manual 标记）；summary 加 metrics 字段（当日全部非空指标——血压等此前写得进
   读不出）；MCP 新工具 get_health_context/query_monthly_report/query_metric_series/
   get_daily_digest（包装既有 /api/reminders/digest）；record_weight 白名单 8→20
2. **P2 写面扩展**：diet payload 支持 food_id+amount_g（offline._normalize_diet
   存在性校验 + diet._food_macros 服务端重算营养，agent 自报 kcal 被忽略防漂移；
   free_text 与 food_id 二选一）；habit payload mode='increment'（done_count 累加，
   照 habits increment upsert 语义；防重放靠 client_id+parse_status 门控）；
   归一化 row_id 写 import_raw.blob（ingest._mark_raw 加 blob_patch 参数，JSONB
   || 合并）——/agent-log 撤销改为 blob.row_id 优先直查，内容匹配退化为老留档
   兜底（food_id 留档无 blob.row_id 视为已撤销）；POST /api/agent/update
   {type,row_id,fields}（部分字段修正，diet food 关联行仅 meal/amount_g 可改并
   重算营养；workout merge 后整体过 parse_workout_payload 重校验，外部来源 403）
   + MCP update_record（带短窗去重）
3. **P3 身份与 UI**：/api/ingest/agent 顶层可选 agent_name（≤50 字）落留档 blob，
   MCP 自动带 clientInfo.name（Hermes/OpenClaw 共用实例也分得清谁写的，兜底
   'mcp'，内置 AI 记 '内置AI'），/agent-log 流水行显「来自 X」；来源徽标三处
   （workout 行按 external_id 'agent-' 前缀，**保持可编辑**；metric 改登记
   autofilled='agent' 而非 mark_manual——徽标零改动显示，代价=同日秤实测可
   覆盖 agent 转述值，offline 通道语义不变；settings._SOURCE_LABELS 补
   agent/offline）；today 页「Agent 刚记了 N 条」迷你卡（60s 轮询、近 10 分钟
   窗口、空态 hidden 不占 space-y 间距）；/agent-log 类型筛选 + 加载更多
   （t/n 进 URL query 兼容 5s 轮询，revoke 透传保持视图）+ habit/metric 行
   「去改」链接；digest 附 agent 通道连续失败 ≥3 次告警（不计入 all_done）
4. **P4 内置 AI 工具化**：app/services/ai_tools.py 进程内执行器（7 工具：
   record_diet/workout/weight/habit + delete_record + query_summary + search_food；
   **不 import mcp**）——写入走 offline.ingest_records（从 ingest_batch 抽出的
   HTTP 无关核心，返回 (status, body)）source='agent'；llm._call 加 tools +
   tool_executor（Claude 原生工具循环——assistant 回合含 thinking 块必须原样回传；
   OpenAI function calling 循环；上限 8 轮），不传 tools 的 analyze/餐照路径零变化；
   llm.ask 升级为 (answer, actions) 带反假确认话术规则（new 计数/skipped 如实/
   补记先确认日期/删除先复述），ai_answer.html 显「本次执行的操作」+ 撤销入口，
   写操作成功后 HX-Trigger 广播刷新今日页；build_context 补：生命体征（血压/
   静息心率/血氧/内脏脂肪）、手记备注、进行中训练计划、力量 PR（pr.exercise_prs
   前 8）、周/月报手写复盘 summary；GET/POST /api/agent/analysis（读缓存报告 +
   触发后台分析，与 /ai/analyze 同 _JOB_KEY 互斥）+ MCP run_analysis/get_analysis
5. **P5 测试文档**：tests/test_mcp_tools.py 33 个（MockTransport 全 mock：
   record_habit 三态匹配、_ingest 503 同 client_id 重试、20 字段白名单锁、
   update_record 去重）+ tests/test_agent_log.py 27 个（blob.row_id 优先/内容
   匹配兜底/revoke 幂等与 blob 合并/筛选分页；登录用 auth.create_session 直发
   token）+ tests/test_agent_v5.py 22 个（新端点口径/food_id 重算/increment/
   update 边界/agent_name 落 blob/ai_tools 直测；错峰日期 today-360，agent_log
   用 today-340）；/api/agent/ 加入 CSRF Bearer 豁免前缀（main.py，显式化）；
   mcp_server/README 工具表 9→16 + 话术规则补 update/food_id；README 补多 Agent
   通道段落。Tailwind 重建 + SW v15
6. 环境注意：**开发机要 `uv sync --group mcp`**，否则 test_mcp_tools 整文件
   importorskip 静默跳过（假全绿）；测试错峰日期已占 today-300/-320/-340/-350/
   -360、2020-01/02，新测试另选
7. 剩余（NAS 侧，用户做）：supervisor [program:shealth-mcp]、Hermes/OpenClaw
   注册、skill v2（话术规则含 update_record）、cron 迁移——照 mcp_server/README；
   Agent 向 V6 候选：DietLog source 列（饮食行徽标）、餐照识别 agent 通道、
   训练计划操作面、周/月复盘写端点、重算冻结快照端点、per-agent token、
   pending 审阅流
8. **产品路线图**：docs/future-roadmap.md（2026-07-12 生成）——5 路竞品/趋势
   调研 + 本地数据利用盘点，40+ 机会点按主题分组（恢复与准备度/训练进阶/
   饮食智能化/降摩擦/主动洞察/睡眠/数据源/长期主义）+ 淘汰清单 + 推荐 V6
   组合（P1-P8，核心结论：采集面远大于利用面，优先在已有数据上补「基线引擎+
   分数合成+趋势巡检+主动推送」，全程零新增外网依赖）

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
   - **V8 多服务器**（2026-07-16 新增）：①连接设置填内网 + frp 两行地址 → 内网
     环境连内网、关 Wi-Fi 走流量自动切 frp（30s 探测周期内）②切到 frp 后首次
     需登录一次（cookie 按 origin 隔离）③外网环境下上秤/离线补发/三星同步/
     每日提醒能走 frp 地址上报 ④快照缓存在两个地址下都能离线回放 ⑤老安装
     升级（只有单地址）行为不变
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
