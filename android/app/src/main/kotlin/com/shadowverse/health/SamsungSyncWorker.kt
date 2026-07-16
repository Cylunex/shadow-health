package com.shadowverse.health

import android.content.Context
import android.util.Log
import android.widget.Toast
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import com.samsung.android.sdk.health.data.HealthDataService
import com.samsung.android.sdk.health.data.HealthDataStore
import com.samsung.android.sdk.health.data.request.DataType
import com.samsung.android.sdk.health.data.request.DataTypes
import com.samsung.android.sdk.health.data.request.LocalDateFilter
import com.samsung.android.sdk.health.data.request.LocalDateGroup
import com.samsung.android.sdk.health.data.request.LocalDateGroupUnit
import com.samsung.android.sdk.health.data.request.LocalTimeFilter
import com.samsung.android.sdk.health.data.request.LocalTimeGroup
import com.samsung.android.sdk.health.data.request.LocalTimeGroupUnit
import org.json.JSONArray
import org.json.JSONObject
import java.time.Duration
import java.time.LocalDate

/**
 * 三星健康 Data SDK 直读 → POST /api/ingest/samsung_direct。
 *
 * 读取窗口：首次回溯 7 天；此后按上次成功同步时间 +1 天重叠（服务端幂等 upsert，
 * 重叠只会刷新同一行）。所有记录按服务端契约发已归一化的紧凑 JSON——
 * daily 是当日总量（服务端 SET 语义），睡眠/运动带 uid 作 external_id 去重。
 */
class SamsungSyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val prefs = applicationContext.getSharedPreferences("shell", Context.MODE_PRIVATE)
        // 多服务器：探测可达地址（Worker 线程）；全不通退回活动地址走 Result.retry
        val server = ServerConfig.resolveOrActive(applicationContext).trimEnd('/')
        val token = prefs.getString("ingest_token", "") ?: ""
        if (server.isEmpty() || token.isEmpty()) {
            Log.w(TAG, "未配置服务器/Token，跳过同步")
            withContext(Dispatchers.Main) {
                Toast.makeText(
                    applicationContext, "三星同步未配置 INGEST_TOKEN（三指长按填写）", Toast.LENGTH_LONG
                ).show()
            }
            return Result.failure()
        }
        return try {
            val store = HealthDataService.getStore(applicationContext)
            val granted = store.getGrantedPermissions(SamsungSync.PERMISSIONS)
            if (!granted.containsAll(SamsungSync.PERMISSIONS)) {
                Log.w(TAG, "缺少三星健康读权限（开发者模式 Data Read 可能被关闭）")
                return Result.failure()
            }
            val today = LocalDate.now()
            val lastSync = prefs.getLong(KEY_LAST_SYNC, 0L)
            val days = if (lastSync == 0L) {
                DEFAULT_BACKFILL_DAYS
            } else {
                (((System.currentTimeMillis() - lastSync) / 86_400_000L).toInt() + 2)
                    .coerceIn(2, MAX_BACKFILL_DAYS)
            }
            val start = today.minusDays((days - 1).toLong())
            val payload = buildPayload(store, start, today)
            Log.i(TAG, "同步窗口 $start..$today，payload ${payload.toString().length}B")
            if (postJson("$server/api/ingest/samsung_direct", token, payload.toString())) {
                prefs.edit().putLong(KEY_LAST_SYNC, System.currentTimeMillis()).apply()
                Result.success()
            } else {
                Result.retry()
            }
        } catch (e: Exception) {
            Log.w(TAG, "三星健康同步失败", e)
            Result.retry()
        }
    }

    private suspend fun buildPayload(
        store: HealthDataStore, start: LocalDate, end: LocalDate,
    ): JSONObject {
        val timeFilter = LocalTimeFilter.of(start.atStartOfDay(), end.plusDays(1).atStartOfDay())

        // 每类数据独立容错：单类读取失败只丢那一类（记日志），不拖累整体上报
        // ---- 每日步数：一次请求分日聚合 ----
        val stepsByDay = HashMap<LocalDate, Long>()
        try {
            val stepsReq = DataType.StepsType.TOTAL.requestBuilder
                .setLocalTimeFilterWithGroup(
                    timeFilter, LocalTimeGroup.of(LocalTimeGroupUnit.DAILY, 1)
                )
                .build()
            for (agg in store.aggregateData(stepsReq).dataList) {
                val v = agg.value ?: continue
                // 注：AggregatedData 的 getStartLocalDateTime 在 Kotlin 元数据里是函数而非属性
                if (v > 0) stepsByDay[agg.getStartLocalDateTime().toLocalDate()] = v
            }
        } catch (e: Exception) {
            Log.w(TAG, "读取步数失败", e)
        }

        // ---- 每日心率 min/max：分日分组聚合，各一次请求（SDK 无 AVG 聚合） ----
        // 显式 inclusive 两端（4 参 of），绕开单日过滤的边界语义问题
        val hrByDay = HashMap<LocalDate, Pair<Float?, Float?>>()
        try {
            val dateFilter = LocalDateFilter.of(start, end, true, true)
            val daily1 = LocalDateGroup.of(LocalDateGroupUnit.DAILY, 1)
            val minByDay = HashMap<LocalDate, Float>()
            for (agg in store.aggregateData(
                DataType.HeartRateType.MIN.requestBuilder
                    .setLocalDateFilterWithGroup(dateFilter, daily1).build()
            ).dataList) {
                agg.value?.let { minByDay[agg.getStartLocalDateTime().toLocalDate()] = it }
            }
            for (agg in store.aggregateData(
                DataType.HeartRateType.MAX.requestBuilder
                    .setLocalDateFilterWithGroup(dateFilter, daily1).build()
            ).dataList) {
                val day = agg.getStartLocalDateTime().toLocalDate()
                val max = agg.value
                if (max != null || minByDay.containsKey(day)) {
                    hrByDay[day] = Pair(minByDay[day], max)
                }
            }
            for ((day, min) in minByDay) {
                if (day !in hrByDay) hrByDay[day] = Pair(min, null)
            }
        } catch (e: Exception) {
            Log.w(TAG, "读取心率失败", e)
        }

        val daily = JSONArray()
        for (day in (stepsByDay.keys + hrByDay.keys).toSortedSet()) {
            val o = JSONObject().put("date", day.toString())
            stepsByDay[day]?.let { o.put("steps", it) }
            hrByDay[day]?.let { (min, max) ->
                min?.let { o.put("hr_min", it.toDouble()) }
                max?.let { o.put("hr_max", it.toDouble()) }
            }
            daily.put(o)
        }

        // ---- 睡眠会话（含分期） ----
        val sleeps = JSONArray()
        try {
        val sleepReq = DataTypes.SLEEP.readDataRequestBuilder
            .setLocalTimeFilter(timeFilter).build()
        for (p in store.readData(sleepReq).dataList) {
            val uid = p.uid ?: continue
            val endTime = p.endTime ?: continue
            val o = JSONObject()
                .put("external_id", uid)
                .put("start", p.startTime.toString())
                .put("end", endTime.toString())
            var light = 0L; var deep = 0L; var rem = 0L; var awake = 0L
            var hasStage = false
            p.getValue(DataType.SleepType.SESSIONS)?.forEach { session ->
                session.stages?.forEach { st ->
                    val m = Duration.between(st.startTime, st.endTime).toMinutes()
                    when (st.stage) {
                        DataType.SleepType.StageType.LIGHT -> { light += m; hasStage = true }
                        DataType.SleepType.StageType.DEEP -> { deep += m; hasStage = true }
                        DataType.SleepType.StageType.REM -> { rem += m; hasStage = true }
                        DataType.SleepType.StageType.AWAKE -> { awake += m; hasStage = true }
                        else -> {}
                    }
                }
            }
            if (hasStage) {
                o.put("light_min", light).put("deep_min", deep)
                    .put("rem_min", rem).put("awake_min", awake)
            }
            p.getValue(DataType.SleepType.DURATION)?.let { o.put("total_sleep_min", it.toMinutes()) }
            sleeps.put(o)
        }
        } catch (e: Exception) {
            Log.w(TAG, "读取睡眠失败", e)
        }

        // ---- 运动会话 ----
        val exercises = JSONArray()
        try {
        val exReq = DataTypes.EXERCISE.readDataRequestBuilder
            .setLocalTimeFilter(timeFilter).build()
        for (p in store.readData(exReq).dataList) {
            val uid = p.uid ?: continue
            val o = JSONObject()
                .put("external_id", uid)
                .put("start", p.startTime.toString())
            p.endTime?.let { o.put("end", it.toString()) }
            val predefined = p.getValue(DataType.ExerciseType.EXERCISE_TYPE)
            var type = predefined?.name?.lowercase() ?: "other"
            if (type == "undefined" || type == "other") {
                val title = p.getValue(DataType.ExerciseType.CUSTOM_TITLE)
                if (!title.isNullOrBlank()) type = title.trim().lowercase()
            }
            o.put("type", type)
            p.getValue(DataType.ExerciseType.SESSIONS)?.firstOrNull()?.let { s ->
                runCatching { s.duration.toMinutes() }.getOrNull()?.let { o.put("duration_min", it) }
                s.distance?.let { o.put("distance_km", it.toDouble() / 1000.0) }
                if (s.calories > 0f) o.put("calories", s.calories.toDouble())
                s.meanHeartRate?.let { o.put("avg_hr", it.toDouble()) }
                s.maxHeartRate?.let { o.put("max_hr", it.toDouble()) }
            }
            exercises.put(o)
        }
        } catch (e: Exception) {
            Log.w(TAG, "读取运动失败", e)
        }

        // ---- 体成分（手表 BIA） ----
        val body = JSONArray()
        try {
        val bcReq = DataTypes.BODY_COMPOSITION.readDataRequestBuilder
            .setLocalTimeFilter(timeFilter).build()
        for (p in store.readData(bcReq).dataList) {
            val o = JSONObject().put("ts", p.startTime.toString())
            p.getValue(DataType.BodyCompositionType.WEIGHT)
                ?.let { o.put("weight_kg", it.toDouble()) }
            p.getValue(DataType.BodyCompositionType.BODY_FAT)
                ?.let { o.put("body_fat_pct", it.toDouble()) }
            (p.getValue(DataType.BodyCompositionType.SKELETAL_MUSCLE_MASS)
                ?: p.getValue(DataType.BodyCompositionType.SKELETAL_MUSCLE))
                ?.let { o.put("skeletal_muscle_kg", it.toDouble()) }
            p.getValue(DataType.BodyCompositionType.MUSCLE_MASS)
                ?.let { o.put("muscle_mass_kg", it.toDouble()) }
            p.getValue(DataType.BodyCompositionType.TOTAL_BODY_WATER)
                ?.let { o.put("body_water_kg", it.toDouble()) }
            p.getValue(DataType.BodyCompositionType.BASAL_METABOLIC_RATE)
                ?.let { o.put("bmr_kcal", it) }
            if (o.length() > 1) body.put(o)
        }
        } catch (e: Exception) {
            Log.w(TAG, "读取体成分失败", e)
        }

        Log.i(
            TAG,
            "读取结果：steps=${stepsByDay.size}天 hr=${hrByDay.size}天 " +
                "sleep=${sleeps.length()}条 exercise=${exercises.length()}条 body=${body.length()}条"
        )
        return JSONObject()
            .put("daily", daily)
            .put("sleep_sessions", sleeps)
            .put("exercises", exercises)
            .put("body", body)
    }

    // 整包（步数/睡眠/训练/体成分数天）体积大，超时给最宽
    private fun postJson(url: String, token: String, json: String): Boolean =
        HttpPost.postJson(TAG, url, token, json, 10_000, 20_000)

    companion object {
        private const val TAG = "SamsungSyncWorker"
        private const val KEY_LAST_SYNC = "samsung_last_sync"
        private const val DEFAULT_BACKFILL_DAYS = 7
        private const val MAX_BACKFILL_DAYS = 30
    }
}
