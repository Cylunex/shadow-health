"""设置 + 导入中心（设计文档 §3.6、§四 /settings 与 /settings/imports 行）。

端点：
- GET  /settings                          目标值表单 + 各源同步状态 + CSV 导出 + 导入中心入口
- POST /settings/targets                  保存 app_settings（jsonb 值），返回表单片段
- GET  /settings/export?table=...         CSV 导出（BOM utf-8-sig，Excel 可直接打开）
- GET  /settings/imports                  导入历史列表 + 各源状态
- GET  /settings/imports/new              导入向导（同页渲染，向导展开）
- POST /settings/imports                  multipart 上传；samsung_zip / keep_file 均走
                                          BackgroundTasks 调对应导入器（keep 密码透传不落库）
- GET  /settings/imports/{job_id}/status  进度片段；done/failed 时响应 286 停止轮询
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.deps import require_login, templates
from app.services import llm
from app.models import (
    AppSetting,
    BodyMetrics,
    DailyActivity,
    DietLog,
    Food,
    Habit,
    HabitLog,
    ImportJob,
    MonthlyReview,
    Recipe,
    SleepSession,
    SyncState,
    WeeklyReview,
    WorkoutLog,
)
from app.timeutil import LOCAL_TZ, now_local, today_local

router = APIRouter(prefix="/settings", dependencies=[Depends(require_login)])

# jsonb 的 JSON null（app_settings.value NOT NULL，"未设定"用 JSON null 表达）
_JSONB_NULL = text("'null'::jsonb")

# 目标值字段：(key, 中文名, 类型, 下限, 上限)
_TARGET_DEFS: list[tuple[str, str, str, float, float]] = [
    ("target_weight_kg", "目标体重", "decimal", 20, 500),
    ("target_kcal", "目标热量", "int", 500, 10000),
    ("target_protein_g", "目标蛋白质", "int", 10, 500),
    ("target_steps", "目标步数", "int", 500, 100000),
    ("target_weekly_cardio_min", "周有氧目标", "int", 10, 3000),
    ("height_cm", "身高", "decimal", 50, 250),
]

_SOURCE_LABELS = {
    "samsung_zip": "三星导出",
    "health_connect": "Health Connect",
    "samsung_direct": "三星直读",
    "keep_file": "Keep 文件",
    "keep_api": "Keep API",
    "miscale": "体脂秤",
}

# CSV 导出白名单：table 参数 -> (模型, 排序列)
_EXPORT_MODELS: dict[str, tuple[type, tuple[str, ...]]] = {
    "body_metrics": (BodyMetrics, ("log_date",)),
    "diet_logs": (DietLog, ("log_date", "id")),
    "workout_logs": (WorkoutLog, ("log_date", "id")),
    "habit_logs": (HabitLog, ("log_date", "id")),
    "daily_activity": (DailyActivity, ("log_date",)),
    "sleep_sessions": (SleepSession, ("wake_date", "id")),
    "weekly_reviews": (WeeklyReview, ("week_start",)),
    "monthly_reviews": (MonthlyReview, ("month_start",)),
    "habits": (Habit, ("sort", "id")),
    "foods": (Food, ("id",)),
    "recipes": (Recipe, ("id",)),
}
_EXPORT_LABELS = [
    ("body_metrics", "身体指标"),
    ("diet_logs", "饮食记录"),
    ("workout_logs", "训练记录"),
    ("habit_logs", "打卡记录"),
    ("daily_activity", "每日活动"),
    ("sleep_sessions", "睡眠会话"),
    ("weekly_reviews", "周报"),
    ("monthly_reviews", "月报"),
    ("habits", "习惯定义"),
    ("foods", "食物库"),
    ("recipes", "药膳库"),
]

_IMPORT_SOURCES = ("samsung_zip", "keep_file")
_IMPORT_SOURCE_LABELS = {"samsung_zip": "三星健康导出 zip", "keep_file": "Keep 导出文件"}
_JOB_STATUS_LABELS = {"pending": "等待中", "running": "进行中", "done": "已完成", "failed": "失败"}
_SAFE_NAME_RE = re.compile(r"[^\w.\-]+")
# 上传护栏：多年三星导出 zip 一般也就几百 MB；扩展名白名单（Keep 导出常见 zip/7z/csv）
_IMPORT_EXTS = {".zip", ".7z", ".csv"}
IMPORT_MAX_BYTES = 500 * 1024 * 1024


# ---------- 通用小工具 ----------
def _fmt_num(v: Any) -> str:
    """app_settings 数值 → 表单显示字符串；JSON null/缺失 → ''。"""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def _fmt_ts(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


# ---------- 目标值 ----------
def _targets_context(
    db: Session, saved: bool = False, errors: list[str] | None = None
) -> dict[str, Any]:
    stored = {r.key: r.value for r in db.execute(select(AppSetting)).scalars()}
    values = {key: _fmt_num(stored.get(key)) for key, *_ in _TARGET_DEFS}
    # 蛋白目标建议：最近体重 × 1.8（§5.6，录入体重后提示重算）
    last_weight = db.execute(
        select(BodyMetrics.weight_kg)
        .where(BodyMetrics.weight_kg.is_not(None))
        .order_by(BodyMetrics.log_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    protein_hint = (
        f"建议：最近体重 {_fmt_num(float(last_weight))} kg × 1.8 ≈ {round(float(last_weight) * 1.8)} g/日"
        if last_weight is not None
        else None
    )
    sex = stored.get("sex")
    birth = stored.get("birth_date")
    return {
        "t_values": values,
        "protein_hint": protein_hint,
        "t_sex": sex if sex in ("male", "female") else None,
        "t_birth_date": birth if isinstance(birth, str) else None,
        "t_saved": saved,
        "t_errors": errors or [],
    }


def _sync_rows(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(select(SyncState).order_by(SyncState.source)).scalars().all()
    return [
        {
            "label": _SOURCE_LABELS.get(r.source, r.source),
            "last_success": _fmt_ts(r.last_success_at),
            "last_error": r.last_error,
            "failures": r.consecutive_failures,
            "needs_reauth": r.needs_reauth,
            "watermark": _fmt_ts(r.watermark),
        }
        for r in rows
    ]


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db)):
    ctx: dict[str, Any] = {"sync_rows": _sync_rows(db), "export_tables": _EXPORT_LABELS}
    ctx.update(_targets_context(db))
    ctx.update(_llm_context(db))
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/targets")
async def targets_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    errors: list[str] = []
    parsed: dict[str, Any] = {}
    for key, label, kind, lo, hi in _TARGET_DEFS:
        raw = str(form.get(key) or "").strip()
        if raw == "":
            parsed[key] = None  # 留空 = 未设定，存 JSON null
            continue
        try:
            value: Any = int(raw) if kind == "int" else float(Decimal(raw))
        except (ValueError, InvalidOperation):
            errors.append(label)
            continue
        if not (lo <= float(value) <= hi):
            errors.append(f"{label}（{_fmt_num(lo)}~{_fmt_num(hi)}）")
            continue
        parsed[key] = round(value, 2) if isinstance(value, float) else value

    # 体成分档案（体脂秤计算用）：sex 受控词表，birth_date 合法日期
    raw_sex = str(form.get("sex") or "").strip()
    parsed["sex"] = raw_sex if raw_sex in ("male", "female") else None
    raw_birth = str(form.get("birth_date") or "").strip()
    if raw_birth:
        try:
            birth = date.fromisoformat(raw_birth)
            if date(1900, 1, 1) <= birth <= today_local():
                parsed["birth_date"] = birth.isoformat()
            else:
                errors.append("出生日期")
        except ValueError:
            errors.append("出生日期")
    else:
        parsed["birth_date"] = None

    saved = False
    if not errors:
        for key, value in parsed.items():
            stmt = pg_insert(AppSetting).values(
                key=key, value=_JSONB_NULL if value is None else value
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": stmt.excluded.value, "updated_at": text("now()")},
            )
            db.execute(stmt)
        saved = True
    return templates.TemplateResponse(
        request, "fragments/settings_targets.html", _targets_context(db, saved=saved, errors=errors)
    )


# ---------- AI 模型（LLM 供应商）配置 ----------
_LLM_ENV_KEYS = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
}


def _llm_raw(db: Session) -> dict:
    row = db.get(AppSetting, llm.CONFIG_KEY)
    return row.value if row is not None and isinstance(row.value, dict) else {}


def _llm_context(
    db: Session,
    saved: bool = False,
    errors: list[str] | None = None,
    test_result: str | None = None,
    test_error: str | None = None,
) -> dict[str, Any]:
    raw = _llm_raw(db)

    def sub(p: str) -> dict:
        v = raw.get(p)
        return v if isinstance(v, dict) else {}

    def key_hint(p: str) -> str:
        k = str(sub(p).get("api_key") or "")
        if k:
            masked = f"{k[:4]}……{k[-4:]}" if len(k) > 12 else "已配置"
            return f"已保存（{masked}）——留空保持不变，输入「清除」删除"
        env_names = "/".join(_LLM_ENV_KEYS[p])
        if any(os.environ.get(e) for e in _LLM_ENV_KEYS[p]):
            return f"未在此保存，当前回退 .env 的 {env_names}"
        return f"未配置（也可写 .env 的 {env_names}）"

    effective = llm.get_config(db)
    return {
        "llm_provider": raw.get("provider") if raw.get("provider") in llm.PROVIDERS else "claude",
        "llm_forms": {
            p: {
                "model": str(sub(p).get("model") or ""),
                "base_url": str(sub(p).get("base_url") or ""),
            }
            for p in llm.PROVIDERS
        },
        "llm_key_hints": {p: key_hint(p) for p in llm.PROVIDERS},
        "llm_defaults": llm.DEFAULT_MODELS,
        "llm_effective": effective,
        "llm_effective_label": llm.PROVIDER_LABELS[effective["provider"]],
        "llm_saved": saved,
        "llm_errors": errors or [],
        "llm_test_result": test_result,
        "llm_test_error": test_error,
    }


def _llm_fragment(request: Request, db: Session, **kwargs):
    return templates.TemplateResponse(
        request, "fragments/settings_llm.html", _llm_context(db, **kwargs)
    )


@router.post("/llm")
async def llm_save(request: Request, db: Session = Depends(get_db)):
    """保存 LLM 配置到 app_settings['llm_config']（Key 留空=不变、「清除」=删）。"""
    form = await request.form()
    prev = _llm_raw(db)
    errors: list[str] = []

    provider = str(form.get("provider") or "").strip()
    if provider not in llm.PROVIDERS:
        provider = "claude"
    new_cfg: dict[str, Any] = {"provider": provider}
    for p in llm.PROVIDERS:
        prev_sub = prev.get(p) if isinstance(prev.get(p), dict) else {}
        model = str(form.get(f"{p}_model") or "").strip()[:100]
        base_url = str(form.get(f"{p}_base_url") or "").strip()[:300]
        if base_url and not base_url.startswith(("http://", "https://")):
            errors.append(f"{llm.PROVIDER_LABELS[p]} 的 Base URL 须以 http(s):// 开头")
            base_url = str(prev_sub.get("base_url") or "")
        key_in = str(form.get(f"{p}_api_key") or "").strip()
        if key_in == "":
            api_key = str(prev_sub.get("api_key") or "")  # 留空 = 保持不变
        elif key_in == "清除":
            api_key = ""
        else:
            api_key = key_in[:400]
        new_cfg[p] = {"model": model, "base_url": base_url, "api_key": api_key}

    stmt = pg_insert(AppSetting).values(key=llm.CONFIG_KEY, value=new_cfg)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": stmt.excluded.value, "updated_at": text("now()")},
    )
    db.execute(stmt)
    db.flush()
    return _llm_fragment(request, db, saved=not errors, errors=errors)


@router.post("/llm/test")
async def llm_test(request: Request, db: Session = Depends(get_db)):
    """用当前已保存配置发一条最小请求验证连通性（同步短等待，photo 识别同款口径）。"""
    from starlette.concurrency import run_in_threadpool

    try:
        # 推理型模型会先烧思考预算，max_tokens 给足才不至于空回复
        reply = await run_in_threadpool(
            llm._call, db, "你是连通性测试助手。", "请只回复两个字：正常", None, 2000
        )
        cfg = llm.get_config(db)
        result = (
            f"连接正常：{llm.PROVIDER_LABELS[cfg['provider']]} · {cfg['model']}"
            f" → 「{reply[:40]}」"
        )
        return _llm_fragment(request, db, test_result=result)
    except llm.LLMError as e:
        return _llm_fragment(request, db, test_error=str(e))
    except Exception as e:  # SDK 之外的意外（配置怪值等）也要给出可读提示
        return _llm_fragment(request, db, test_error=f"测试失败：{str(e)[:200]}")


# ---------- CSV 导出 ----------
def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.astimezone(LOCAL_TZ).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


@router.get("/export")
def settings_export(table: str = "", db: Session = Depends(get_db)):
    entry = _EXPORT_MODELS.get(table)
    if entry is None:
        raise HTTPException(status_code=404, detail="不支持导出该表")
    model, order_cols = entry
    columns = [c.key for c in model.__table__.columns]
    rows = (
        db.execute(select(model).order_by(*(getattr(model, c) for c in order_cols)))
        .scalars()
        .all()
    )
    # 先物化再流式输出：get_db 的会话在响应发送前就会关闭（FastAPI >= 0.106）
    data = [[_csv_cell(getattr(r, c)) for c in columns] for r in rows]

    def _iter():
        buf = io.StringIO()
        writer = csv.writer(buf)
        yield "\ufeff"  # BOM（utf-8-sig）：Excel 直接打开不乱码
        writer.writerow(columns)
        yield buf.getvalue()
        for row in data:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow(row)
            yield buf.getvalue()

    filename = f"{table}_{today_local():%Y%m%d}.csv"
    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/backup")
def settings_backup(db: Session = Depends(get_db)):
    """一键全量备份 zip：全部表 CSV + 餐次/体态照片。

    照片不在 pg_dump 每日备份里，这是唯一带照片的全量出口。先写临时文件再回发
    （照片可能数百 MB，不占内存；照片已压缩用 STORED 免重压），下载完成后台清理。
    """
    import tempfile
    import zipfile

    from starlette.background import BackgroundTask

    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
            for table, (model, order_cols) in _EXPORT_MODELS.items():
                columns = [c.key for c in model.__table__.columns]
                rows = (
                    db.execute(
                        select(model).order_by(*(getattr(model, c) for c in order_cols))
                    ).scalars().all()
                )
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(columns)
                for r in rows:
                    writer.writerow([_csv_cell(getattr(r, c)) for c in columns])
                zf.writestr(f"csv/{table}.csv", "\ufeff" + buf.getvalue())
            photo_dir = get_settings().photo_dir
            if photo_dir.is_dir():
                for p in sorted(photo_dir.iterdir()):
                    if p.is_file():
                        zf.write(p, f"photos/{p.name}", compress_type=zipfile.ZIP_STORED)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    filename = f"shadow-health-backup_{today_local():%Y%m%d}.zip"
    return FileResponse(
        tmp_name,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: Path(tmp_name).unlink(missing_ok=True)),
    )


# ---------- 导入中心 ----------
def _job_polling(job: ImportJob) -> bool:
    """pending（后台任务即将接手）与 running 轮询；done/failed 由 286 停止。"""
    return job.status in ("pending", "running")


def _job_ctx(job: ImportJob) -> dict[str, Any]:
    total = job.total or 0
    processed = (job.inserted or 0) + (job.skipped or 0) + (job.failed or 0)
    pct = min(100, round(processed * 100 / total)) if total else None
    return {
        "job": job,
        "source_label": _IMPORT_SOURCE_LABELS.get(job.source, _SOURCE_LABELS.get(job.source, job.source)),
        "status_label": _JOB_STATUS_LABELS.get(job.status, job.status),
        "pct": pct,
        "polling": _job_polling(job),
        "started": _fmt_ts(job.started_at),
        "finished": _fmt_ts(job.finished_at),
        "report_json": json.dumps(job.report, ensure_ascii=False, indent=1) if job.report else None,
    }


def _raw_stats(db: Session) -> list[dict[str, Any]]:
    """import_raw 各源解析状态汇总（数据源明细：留档/已归一化/跳过/失败一目了然）。"""
    from app.models import ImportRaw

    rows = db.execute(
        select(
            ImportRaw.source,
            ImportRaw.record_type,
            ImportRaw.parse_status,
            func.count(),
        ).group_by(ImportRaw.source, ImportRaw.record_type, ImportRaw.parse_status)
    ).all()
    by_key: dict[tuple[str, str], dict[str, int]] = {}
    for source, rtype, status, n in rows:
        by_key.setdefault((source, rtype), {})[status] = n
    out = []
    for (source, rtype), st in sorted(by_key.items()):
        out.append({
            "label": _SOURCE_LABELS.get(source, source),
            "rtype": rtype,
            "parsed": st.get("parsed", 0),
            "skipped": st.get("skipped", 0),
            "failed": st.get("failed", 0),
            "pending": st.get("pending", 0),
            "total": sum(st.values()),
        })
    return out


def _imports_context(
    db: Session, wizard_open: bool = False, wizard_error: str | None = None
) -> dict[str, Any]:
    jobs = (
        db.execute(select(ImportJob).order_by(ImportJob.id.desc()).limit(50)).scalars().all()
    )
    return {
        "jobs": [_job_ctx(j) for j in jobs],
        "sync_rows": _sync_rows(db),
        "raw_stats": _raw_stats(db),
        "wizard_open": wizard_open,
        "wizard_error": wizard_error,
    }


@router.get("/imports")
def imports_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "imports.html", _imports_context(db))


@router.get("/imports/new")
def imports_new(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "imports.html", _imports_context(db, wizard_open=True)
    )


def _finish_job_failed(job_id: int, message: str) -> None:
    """后台任务兜底：导入器没接手/中途抛异常时把 job 置 failed（importer 已收尾则不动）。"""
    db = SessionLocal()
    try:
        job = db.get(ImportJob, job_id)
        if job is not None and job.status not in ("done", "failed"):
            job.status = "failed"
            job.error = message
            job.finished_at = now_local()
        db.commit()
    finally:
        db.close()


def _run_samsung_import(zip_path: str, job_id: int) -> None:
    """BackgroundTasks 入口：调并行开发的三星导入器，进度由它写 import_jobs。"""
    try:
        from app.importers.samsung_zip import import_zip
    except ImportError:
        _finish_job_failed(
            job_id,
            "三星导入器（app.importers.samsung_zip）尚未就绪；文件已保存在 uploads/，导入器上线后可重新发起导入",
        )
        return
    try:
        import_zip(zip_path, db=None, dry_run=False, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 后台任务兜底，避免静默失败
        _finish_job_failed(job_id, f"导入失败：{exc}")


def _run_keep_import(file_path: str, job_id: int, password: str | None) -> None:
    """BackgroundTasks 入口：Keep 导出文件导入器（M4b），进度由它写 import_jobs。"""
    try:
        from app.importers.keep_file import import_keep
    except ImportError:
        _finish_job_failed(
            job_id,
            "Keep 导入器（app.importers.keep_file）尚未就绪；文件已保存在 uploads/，导入器上线后可重新发起导入",
        )
        return
    try:
        import_keep(file_path, db=None, dry_run=False, job_id=job_id, password=password)
    except Exception as exc:  # noqa: BLE001 后台任务兜底，避免静默失败
        _finish_job_failed(job_id, f"导入失败：{exc}")


@router.post("/imports")
async def imports_create(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    source: str = Form(""),
    file: UploadFile | None = File(None),
    keep_password: str = Form(""),
):
    if source not in _IMPORT_SOURCES:
        return templates.TemplateResponse(
            request,
            "imports.html",
            _imports_context(db, wizard_open=True, wizard_error="请选择有效的数据来源"),
            status_code=400,
        )
    if file is None or not (file.filename or "").strip():
        return templates.TemplateResponse(
            request,
            "imports.html",
            _imports_context(db, wizard_open=True, wizard_error="请选择要上传的文件"),
            status_code=400,
        )

    original = Path(file.filename).name
    if Path(original).suffix.lower() not in _IMPORT_EXTS:
        return templates.TemplateResponse(
            request,
            "imports.html",
            _imports_context(
                db, wizard_open=True,
                wizard_error="仅支持 zip/7z/csv 导出文件",
            ),
            status_code=400,
        )

    upload_dir = get_settings().upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe = _SAFE_NAME_RE.sub("_", original) or "upload.bin"
    dest = upload_dir / f"{now_local():%Y%m%d_%H%M%S}_{source}_{safe}"
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > IMPORT_MAX_BYTES:
                    # 无上限会被误传的超大文件写满 NAS 磁盘，拖垮同机 PG
                    raise HTTPException(status_code=413, detail="too large")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        return templates.TemplateResponse(
            request,
            "imports.html",
            _imports_context(
                db, wizard_open=True,
                wizard_error=f"文件超过 {IMPORT_MAX_BYTES // (1024 * 1024)}MB 上限，"
                             "请确认选的是健康数据导出文件",
            ),
            status_code=413,
        )

    job = ImportJob(source=source, filename=original, status="pending")
    db.add(job)
    db.flush()
    # 显式提交：后台任务/导入器用独立会话按 job_id 读行，必须先落库
    # （get_db 的收尾 commit 不保证在 BackgroundTasks 之前执行）
    db.commit()
    if source == "samsung_zip":
        background_tasks.add_task(_run_samsung_import, str(dest), job.id)
    else:  # keep_file：密码只透传给导入器，不落库
        background_tasks.add_task(
            _run_keep_import, str(dest), job.id, keep_password.strip() or None
        )
    return RedirectResponse("/settings/imports", status_code=303)


@router.get("/imports/{job_id}/status")
def import_job_status(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    # 286：HTMX 收到后停止 every 2s 轮询（任务已结束）
    status_code = 286 if job.status in ("done", "failed") else 200
    return templates.TemplateResponse(
        request,
        "fragments/settings_import_job_status.html",
        _job_ctx(job),
        status_code=status_code,
    )
