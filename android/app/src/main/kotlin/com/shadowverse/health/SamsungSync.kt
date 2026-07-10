package com.shadowverse.health

import android.app.Activity
import android.content.Context
import android.util.Log
import android.widget.Toast
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.samsung.android.sdk.health.data.HealthDataService
import com.samsung.android.sdk.health.data.permission.AccessType
import com.samsung.android.sdk.health.data.permission.Permission
import com.samsung.android.sdk.health.data.request.DataTypes
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.util.concurrent.TimeUnit

/**
 * 三星健康 Data SDK 直读同步的开关与调度（docs/mobile-sync.md）。
 *
 * 前置：手机三星健康「开发者模式（Data Read）」已开（版本号连点 10 次解锁；
 * App 更新后可能自动关闭，需偶尔重开）。授权页由三星健康自己弹出。
 */
object SamsungSync {

    private const val TAG = "SamsungSync"
    private const val WORK_NAME = "samsung-health-sync"
    const val PREF_ENABLED = "samsung_sync_enabled"

    @JvmStatic
    val PERMISSIONS: Set<Permission> = setOf(
        Permission.of(DataTypes.STEPS, AccessType.READ),
        Permission.of(DataTypes.SLEEP, AccessType.READ),
        Permission.of(DataTypes.HEART_RATE, AccessType.READ),
        Permission.of(DataTypes.EXERCISE, AccessType.READ),
        Permission.of(DataTypes.BODY_COMPOSITION, AccessType.READ),
    )

    private val scope = CoroutineScope(Dispatchers.Main)

    /** 开启：确保 SDK 读权限（缺则弹三星健康授权页）→ 注册周期任务 → 立即同步一次。 */
    @JvmStatic
    fun enable(activity: Activity) {
        scope.launch {
            try {
                val store = HealthDataService.getStore(activity.applicationContext)
                var granted = store.getGrantedPermissions(PERMISSIONS)
                if (!granted.containsAll(PERMISSIONS)) {
                    granted = store.requestPermissions(PERMISSIONS, activity)
                }
                if (!granted.containsAll(PERMISSIONS)) {
                    activity.getSharedPreferences("shell", Context.MODE_PRIVATE)
                        .edit().putBoolean(PREF_ENABLED, false).apply()
                    Toast.makeText(activity, "未授予三星健康读权限，同步未开启", Toast.LENGTH_LONG).show()
                    return@launch
                }
                schedule(activity.applicationContext)
                syncNow(activity.applicationContext)
                Toast.makeText(activity, "三星健康同步已开启", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                // 常见原因：三星健康未装/版本低/开发者模式 Data Read 未开
                Log.w(TAG, "开启三星健康同步失败", e)
                activity.getSharedPreferences("shell", Context.MODE_PRIVATE)
                    .edit().putBoolean(PREF_ENABLED, false).apply()
                Toast.makeText(
                    activity,
                    "三星健康连接失败（确认已开启开发者模式 Data Read）：" + e.message,
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }

    @JvmStatic
    fun disable(ctx: Context) {
        WorkManager.getInstance(ctx).cancelUniqueWork(WORK_NAME)
    }

    /** 注册每小时周期同步（应用启动时重申，UPDATE 策略幂等）。 */
    @JvmStatic
    fun schedule(ctx: Context) {
        val req = PeriodicWorkRequestBuilder<SamsungSyncWorker>(1, TimeUnit.HOURS)
            .setConstraints(
                Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
            )
            .build()
        WorkManager.getInstance(ctx)
            .enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.UPDATE, req)
    }

    @JvmStatic
    fun syncNow(ctx: Context) {
        WorkManager.getInstance(ctx).enqueue(OneTimeWorkRequestBuilder<SamsungSyncWorker>().build())
    }
}
