package com.shadowverse.health.shpoc

import android.os.Bundle
import android.util.Log
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import com.samsung.android.sdk.health.data.HealthDataService
import com.samsung.android.sdk.health.data.HealthDataStore
import com.samsung.android.sdk.health.data.permission.AccessType
import com.samsung.android.sdk.health.data.permission.Permission
import com.samsung.android.sdk.health.data.request.DataType
import com.samsung.android.sdk.health.data.request.DataTypes
import com.samsung.android.sdk.health.data.request.LocalTimeFilter
import kotlinx.coroutines.launch
import java.time.LocalDate
import java.time.LocalDateTime

/**
 * 技术验证 PoC：绕过 Health Connect，用 Samsung Health Data SDK **直接读**三星健康数据。
 * 前置：手机三星健康「开发者模式（Data Read）」已开（本机已确认）；SDK AAR 已放 app/libs/。
 * 跑通「读到今日步数」= 直读这条路对我们可行，正式集成即可把手表数据（步数/睡眠/心率）拉进 shadow-health。
 *
 * 注意：以下 SDK 调用的确切签名/导入路径以真实 AAR 编译为准，首次编译可能需按报错微调。
 */
class MainActivity : ComponentActivity() {

    private lateinit var status: TextView
    private lateinit var store: HealthDataStore

    private val stepsPermission = Permission.of(DataTypes.STEPS, AccessType.READ)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        status = TextView(this).apply {
            textSize = 18f
            setPadding(48, 120, 48, 48)
            text = "连接三星健康 Data SDK…"
        }
        setContentView(status)

        store = HealthDataService.getStore(applicationContext)
        ensurePermissionThenRead()
    }

    private fun ensurePermissionThenRead() {
        lifecycleScope.launch {
            try {
                val granted = store.getGrantedPermissions(setOf(stepsPermission))
                if (!granted.containsAll(setOf(stepsPermission))) {
                    // 弹三星健康的权限授权页
                    store.requestPermissions(setOf(stepsPermission), this@MainActivity)
                }
                readTodaySteps()
            } catch (e: Exception) {
                status.text = "连接/授权失败：${e.message}"
                Log.e(TAG, "permission failed", e)
            }
        }
    }

    private suspend fun readTodaySteps() {
        try {
            val start = LocalDate.now().atStartOfDay()
            val end = LocalDateTime.now()
            val filter = LocalTimeFilter.of(start, end)
            val request = DataType.StepsType.TOTAL.requestBuilder
                .setLocalTimeFilter(filter)
                .build()
            val result = store.aggregateData(request)
            var steps = 0L
            result.dataList.forEach { agg -> agg.value?.let { steps = it } }
            status.text = "✅ 直读三星健康成功\n\n今日步数：$steps"
            Log.i(TAG, "today steps = $steps")
        } catch (e: Exception) {
            status.text = "读取失败：${e.message}"
            Log.e(TAG, "read failed", e)
        }
    }

    private companion object {
        const val TAG = "SHPoc"
    }
}
