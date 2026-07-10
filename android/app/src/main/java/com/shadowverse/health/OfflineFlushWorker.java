package com.shadowverse.health;

import android.content.Context;

import androidx.annotation.NonNull;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

/**
 * 离线队列补发任务（docs/offline-plan.md 阶段二）：约束 NetworkType.CONNECTED，
 * 网络恢复即跑；失败按 WorkManager 默认指数退避重试（上限 5 小时间隔），
 * App 启动/回前台还会重新入队兜底。成功补发后通知栏提示条数。
 */
public class OfflineFlushWorker extends Worker {

    public OfflineFlushWorker(@NonNull Context context, @NonNull WorkerParameters params) {
        super(context, params);
    }

    @NonNull
    @Override
    public Result doWork() {
        int sent = OfflineStore.drain(getApplicationContext());
        if (sent == -2) {
            // 服务器/Token 没配：重试无意义，提醒用户去连接设置里补齐
            OfflineStore.notifyConfigMissing(
                    getApplicationContext(), OfflineStore.queueSize(getApplicationContext()));
            return Result.failure();
        }
        if (sent < 0) {
            return Result.retry();  // 联网 ≠ 能到局域网服务器：退避后再试
        }
        if (sent > 0) {
            OfflineStore.notifySynced(getApplicationContext(), sent);
        }
        return Result.success();
    }
}
