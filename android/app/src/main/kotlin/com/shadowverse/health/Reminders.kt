package com.shadowverse.health

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.time.Duration
import java.time.LocalDateTime
import java.time.LocalTime
import java.util.concurrent.TimeUnit

/**
 * 每日提醒（约 20:30）：拉服务端 /api/reminders/digest，有缺口才弹通知。
 *
 * 局域网 http 下 PWA 的 Web Push 不可用（Push API 要求 HTTPS），提醒走壳内
 * 本地通知；文案由服务端拼好（打卡缺口/蛋白/步数/周有氧）。
 */
object Reminders {

    const val PREF_ENABLED = "reminder_enabled"
    private const val WORK_NAME = "daily-reminder"
    private val REMIND_AT: LocalTime = LocalTime.of(20, 30)

    @JvmStatic
    fun schedule(ctx: Context) {
        val now = LocalDateTime.now()
        var next = now.toLocalDate().atTime(REMIND_AT)
        if (!next.isAfter(now)) {
            next = next.plusDays(1)
        }
        val req = PeriodicWorkRequestBuilder<ReminderWorker>(24, TimeUnit.HOURS)
            .setInitialDelay(Duration.between(now, next))
            .setConstraints(
                Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
            )
            .build()
        // 必须 CANCEL_AND_REENQUEUE：UPDATE 策略保留首次入队时刻作为周期起点，
        // 重算的 initialDelay 会相对旧起点解释，提醒时间随每次打开 App 漂移
        WorkManager.getInstance(ctx)
            .enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.CANCEL_AND_REENQUEUE, req)
    }

    @JvmStatic
    fun cancel(ctx: Context) {
        WorkManager.getInstance(ctx).cancelUniqueWork(WORK_NAME)
    }
}

class ReminderWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    override suspend fun doWork(): Result {
        val prefs = applicationContext.getSharedPreferences("shell", Context.MODE_PRIVATE)
        // 多服务器：探测可达地址（Worker 线程）；全不通退回活动地址走 Result.retry
        val server = ServerConfig.resolveOrActive(applicationContext).trimEnd('/')
        val token = prefs.getString("ingest_token", "") ?: ""
        if (server.isEmpty() || token.isEmpty()) {
            return Result.failure()
        }
        return try {
            val digest = withContext(Dispatchers.IO) { fetchDigest(server, token) }
                ?: return Result.retry()
            if (!digest.optBoolean("all_done", false)) {
                notify(digest.optString("message", "今天还有目标没完成"))
            }
            Result.success()
        } catch (e: Exception) {
            Log.w(TAG, "提醒拉取失败", e)
            Result.retry()
        }
    }

    private fun fetchDigest(server: String, token: String): JSONObject? {
        var conn: HttpURLConnection? = null
        return try {
            val target = "$server/api/reminders/digest"
            conn = URL(ServerConfig.bare(target)).openConnection() as HttpURLConnection
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            HttpPost.applyAuth(conn, target, token)  // frp Basic 时 token 走 X-Ingest-Token
            if (conn.responseCode != 200) {
                null
            } else {
                JSONObject(conn.inputStream.bufferedReader().readText())
            }
        } finally {
            conn?.disconnect()
        }
    }

    private fun notify(message: String) {
        val nm = applicationContext.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "每日提醒", NotificationManager.IMPORTANCE_DEFAULT)
                    .apply { description = "打卡与目标缺口提醒" }
            )
        }
        val open = Intent(applicationContext, MainActivity::class.java)
        val pi = PendingIntent.getActivity(
            applicationContext, 1, open, PendingIntent.FLAG_IMMUTABLE
        )
        val n: Notification = Notification.Builder(applicationContext, CHANNEL_ID)
            .setContentTitle("今日还有目标待完成")
            .setContentText(message)
            .setStyle(Notification.BigTextStyle().bigText(message))
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(pi)
            .setAutoCancel(true)
            .build()
        nm.notify(NOTIFICATION_ID, n)
    }

    companion object {
        private const val TAG = "ReminderWorker"
        private const val CHANNEL_ID = "reminder"
        private const val NOTIFICATION_ID = 2001
    }
}
