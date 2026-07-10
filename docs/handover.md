# 会话交接（自包含，新会话从这里继续）

> 最后更新：2026-07-10。配套阅读：README（功能全貌）· docs/deploy.md（NAS 部署照单）·
> docs/mobile-sync.md（三星直读背景）· gateway/README.md（体脂秤双端）。

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

## 其余待办（优先级序）

1. **NAS 部署**（用户明确"这里部署不了生产"，等到 NAS 环境照 deploy.md 执行；
   生产库角色未建是第一步）
2. 心率同步真机回归：HR 分日分组聚合修复后未验证（手机装新包同步一次，
   查 daily_activity.hr_min/hr_max）
3. 候选池：力量组次明细+PR（Hevy 式，detail JSONB 就绪）、GPS 轨迹地图
  （依赖外网瓦片，违背断网原则，做成可选）、Keep API（搁置）

## 开发注意（换机器/新会话易踩）

- 改模板后：`npx -y tailwindcss@3.4.17 -c tailwind.config.js -i static/src/input.css
  -o static/app.css --minify`，并升 `static/sw.js` 的 `SW_VERSION`
- 改动数据逻辑后跑 `uv run pytest`；uvicorn --reload 会清内存 session（重新登录）
- Android 构建见 android/README.md；AAR 在 android/app/libs/（不入仓）
- 提交身份：仓库 local 配置已设 Cylunex
