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

## ▶ 下一任务：日报 + 月报（用户 2026-07-10 提出，未开工）

周报已存在（`weekly_reviews` 惰性生成 + 快照卡 + 手写复盘，app/routers/review.py），
照它的模式扩两端：

**日报**：
- 无需新表——聚合某日即可：三环、饮食四项 vs 目标、训练明细+负荷、打卡清单、
  体重/体成分、睡眠（跨源去重走 services/sleep.py！）
- 路由建议 `GET /report/daily?d=`；今日页与提醒 digest（app/routers/reminders.py）
  已有大半聚合逻辑可抽公共函数复用
- 入口：今日页顶部日期点击 → 当日日报；饮食页同款翻天导航

**月报**：
- 建 `monthly_reviews` 表（month_start 唯一，CHECK 每月 1 号；照 WeeklyReview 抄）+
  迁移 08；惰性生成上一完整月（照 review.py 的 `_ensure_last_week` 模式）
- 快照维度：体重/体脂/围度首末变化、总训练次数/分钟/负荷、有氧达标周数、
  打卡率、饮食记录天数/日均四项、步数日均与达标天数、连击最高
- 手写复盘 + 供 LLM 快照引用（llm.build_context 已读 weekly_reviews，同样接入月报）

**报告中心**：更多页「周报」入口升级为「报告」，页内 日/周/月 三个 tab
（HTMX 片段切换，样式参考 metrics 图表的 chips）。

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
