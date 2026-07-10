# 会话交接（自包含，新会话从这里继续）

> 最后更新：2026-07-10。配套阅读：README（功能全貌）· docs/deploy.md（NAS 部署照单）·
> docs/mobile-sync.md（三星直读背景）· gateway/README.md（体脂秤双端）·
> **docs/audit-2026-07-10.md（全面审查：24 缺陷 + 18 建议，兼修复工作清单）**。

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

1. **真机回归**（Android 壳与网关改动后必做）：重新构建 APK 装机——验证壳内
   hx-confirm 删除弹系统确认框、拍照记餐/导入 zip 文件选择器、CSV 导出下载、
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
- Android 构建见 android/README.md；AAR 在 android/app/libs/（不入仓）
- 提交身份：仓库 local 配置已设 Cylunex
