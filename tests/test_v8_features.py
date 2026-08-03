"""V8/V9 批次口径锁：运动类型中文化 + 指标展示/录入独立配置。

前半纯函数直测；后半 DB 集成（Mac 临时 PG 55433），不可达自动跳过。
展示配置存 metrics_hidden_fields，录入配置存 metrics_entry_hidden_fields；
fixture 收尾分别还原，不碰真实配置以外的行。
"""
from __future__ import annotations

import pytest

from app.deps import local_hm, session_label
from app.routers.metrics import (
    _CHART_METRICS,
    _DISPLAY_HIDDEN_SETTING_KEY,
    _ENTRY_HIDDEN_SETTING_KEY,
    _TOGGLE_KEYS,
    _visible_chart_options,
)


# ---------- session_label：外源英文翻中文 / 手动中文透传 ----------

@pytest.mark.parametrize("raw,expected", [
    ("walking", "健走"),               # 三星直读/zip 最常见
    ("running", "跑步"),
    ("circuit_training", "循环训练"),   # zip ACTIVITY_TYPE_MAP 10007
    ("cycling", "骑行"),
    ("elliptical", "椭圆机"),
    ("Running", "跑步"),               # 大小写不敏感
    (" walking ", "健走"),             # 前后空白
    ("力量", "力量"),                   # 手动中文原样
    ("早·盆底", "早·盆底"),
    ("", "训练"),                      # 空值回退（模板原 or '训练' 口径）
    (None, "训练"),
    ("lat_pull_downs", "lat pull downs"),  # 未知英文只把下划线换空格
    ("晨跑_快", "晨跑_快"),             # 非 ASCII 不动下划线
])
def test_session_label(raw, expected):
    assert session_label(raw) == expected


def test_session_label_covers_all_import_vocabularies():
    """三个导入源的词表值必须全部有中文映射（新增枚举时这里先红）。"""
    from app.importers.samsung_zip import ACTIVITY_TYPE_MAP
    from app.routers.ingest import _HC_EXERCISE_TYPE

    for vocab in (ACTIVITY_TYPE_MAP.values(), _HC_EXERCISE_TYPE.values()):
        for t in vocab:
            assert not session_label(t).isascii() or session_label(t) == "HIIT", (
                f"导入词表值 {t!r} 缺中文映射"
            )


def test_local_hm():
    from datetime import datetime, timezone

    # UTC 04:05 = 北京 12:05
    assert local_hm(datetime(2020, 3, 1, 4, 5, tzinfo=timezone.utc)) == "12:05"
    assert local_hm(None) == ""


# ---------- 自定义显示：图表选项过滤（纯函数） ----------

def test_visible_chart_options_default_all():
    assert _visible_chart_options(set()) == _CHART_METRICS


def test_visible_chart_options_hides_fully_hidden_deps():
    hidden = {"bp_systolic", "bp_diastolic", "mood_score"}
    keys = {k for k, _ in _visible_chart_options(hidden)}
    assert "bp" not in keys
    assert "mood" not in keys
    assert "weight" in keys  # 未隐藏的照常


def test_visible_chart_options_partial_deps_stay():
    # 围度五字段只藏一个：图表还在（其余部位仍可画）
    keys = {k for k, _ in _visible_chart_options({"waist_cm"})}
    assert "girth" in keys


def test_watch_driven_charts_never_hidden():
    # 手表/日活驱动的图不受手录字段隐藏影响
    keys = {k for k, _ in _visible_chart_options(set(_TOGGLE_KEYS))}
    for k in ("hr", "steps", "sleep", "load", "running", "bedtime", "sleep_stages"):
        assert k in keys


# ---------- DB 集成（不可达自动跳过） ----------

def _db_ready() -> bool:
    try:
        from sqlalchemy import text

        from app.db import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    if not _db_ready():
        pytest.skip("临时 PG 不可达")
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def restore_display_setting(db):
    """记住展示/录入设置原值，测试结束分别还原（含原本不存在 → 删行）。"""
    from app.models import AppSetting

    keys = (_DISPLAY_HIDDEN_SETTING_KEY, _ENTRY_HIDDEN_SETTING_KEY)
    originals = {}
    for key in keys:
        row = db.get(AppSetting, key)
        originals[key] = None if row is None else row.value
    yield
    db.rollback()
    for key, original in originals.items():
        row = db.get(AppSetting, key)
        if original is None:
            if row is not None:
                db.delete(row)
        else:
            if row is None:
                db.add(AppSetting(key=key, value=original))
            else:
                row.value = original
    db.commit()


def _set_hidden(
    db,
    fields: list[str],
    key: str = _DISPLAY_HIDDEN_SETTING_KEY,
) -> None:
    from app.models import AppSetting

    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=fields))
    else:
        row.value = fields
    db.commit()


def test_hidden_fields_roundtrip(db, restore_display_setting):
    from app.routers.metrics import _hidden_fields

    _set_hidden(db, ["bp_systolic", "bp_diastolic", "bogus_field"])
    hidden = _hidden_fields(db)
    assert hidden == {"bp_systolic", "bp_diastolic"}  # 词表外的键被过滤


def test_form_context_respects_hidden(db, restore_display_setting):
    from app.routers.metrics import _MORE_FIELDS, _form_context
    from app.timeutil import today_local

    _set_hidden(db, list(_MORE_FIELDS), _ENTRY_HIDDEN_SETTING_KEY)
    ctx = _form_context(db, today_local())
    assert ctx["show_more"] is False  # 更多指标全隐藏 → 折叠区不渲染
    assert set(_MORE_FIELDS) <= ctx["hidden"]


def test_previous_weight_is_hint_not_form_value(db):
    """旧体重只能做 placeholder，补其他指标时不能被带成当天手动体重。"""
    from datetime import date
    from decimal import Decimal

    from app.models import BodyMetrics
    from app.routers.metrics import _form_context

    old_day, new_day = date(2020, 6, 6), date(2020, 6, 7)
    db.query(BodyMetrics).filter(BodyMetrics.log_date.in_([old_day, new_day])).delete(
        synchronize_session=False
    )
    db.add(BodyMetrics(
        log_date=old_day,
        weight_kg=Decimal("84.50"),
        autofilled={"weight_kg": "miscale"},
    ))
    db.flush()

    ctx = _form_context(db, new_day)
    assert ctx["v"]["weight_kg"] == ""
    assert ctx["weight_entry"]["previous"] == "84.5"
    assert ctx["weight_entry"]["current"] == ""


def test_history_cols_respect_hidden(db, restore_display_setting):
    from app.routers.metrics import _history_context

    # 血压两字段都藏 → bp 列消失；只藏收缩压 → 列还在
    _set_hidden(db, ["bp_systolic", "bp_diastolic"])
    cols = _history_context(db)["history_cols"]
    assert "bp" not in cols
    assert _history_context(db)["history_colspan"] == len(cols) + 1

    _set_hidden(db, ["bp_systolic"])
    assert "bp" in _history_context(db)["history_cols"]


def test_default_metric_falls_back_when_weight_hidden(db, restore_display_setting):
    from app.routers.metrics import _default_metric

    _set_hidden(db, [])
    assert _default_metric(db) == "weight"
    _set_hidden(db, ["weight_kg"])
    assert _default_metric(db) == "body_fat"  # 选项序里 weight 之后第一个可见


@pytest.fixture()
def page(db):
    """带 session cookie 的页面客户端（照 test_agent_log 模式，进程内签发 token）。"""
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import app

    token = auth.create_session()
    with TestClient(app) as c:
        c.cookies.set(auth.SESSION_COOKIE, token)
        yield c


def test_metrics_display_endpoint(db, page, restore_display_setting):
    from app.routers.metrics import _display_hidden_fields, _entry_hidden_fields

    # 展示保存不能改变已经独立的录入设置。
    _set_hidden(db, ["mood_score"], _ENTRY_HIDDEN_SETTING_KEY)
    resp = page.post("/metrics/display", data={"visible": ["weight_kg", "sleep_hours"]})
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "metrics-display-changed"
    assert "展示设置已保存" in resp.text
    db.expire_all()
    assert _display_hidden_fields(db) == _TOGGLE_KEYS - {"weight_kg", "sleep_hours"}
    assert _entry_hidden_fields(db) == {"mood_score"}


def test_metrics_entry_endpoint(db, page, restore_display_setting):
    from app.routers.metrics import _display_hidden_fields, _entry_hidden_fields

    # 录入保存不能改变展示设置。
    _set_hidden(db, ["bp_systolic", "bp_diastolic"])
    resp = page.post("/metrics/entry", data={"visible": ["weight_kg", "sleep_hours"]})
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "metrics-entry-changed"
    assert "录入设置已保存" in resp.text
    db.expire_all()
    assert _entry_hidden_fields(db) == _TOGGLE_KEYS - {"weight_kg", "sleep_hours"}
    assert _display_hidden_fields(db) == {"bp_systolic", "bp_diastolic"}


def test_old_shared_setting_is_copied_before_display_changes(
    db, page, restore_display_setting,
):
    from app.models import AppSetting
    from app.routers.metrics import _display_hidden_fields, _entry_hidden_fields

    entry = db.get(AppSetting, _ENTRY_HIDDEN_SETTING_KEY)
    if entry is not None:
        db.delete(entry)
        db.commit()
    _set_hidden(db, ["mood_score"])

    resp = page.post("/metrics/display", data={"visible": ["weight_kg"]})
    assert resp.status_code == 200
    db.expire_all()
    assert _entry_hidden_fields(db) == {"mood_score"}
    assert _display_hidden_fields(db) == _TOGGLE_KEYS - {"weight_kg"}


def test_new_install_entry_defaults_stay_visible_when_display_changes(
    db, page, restore_display_setting,
):
    from app.models import AppSetting
    from app.routers.metrics import _display_hidden_fields, _entry_hidden_fields

    for key in (_DISPLAY_HIDDEN_SETTING_KEY, _ENTRY_HIDDEN_SETTING_KEY):
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
    db.commit()

    resp = page.post("/metrics/display", data={"visible": ["weight_kg"]})
    assert resp.status_code == 200
    db.expire_all()
    assert _entry_hidden_fields(db) == set()
    assert _display_hidden_fields(db) == _TOGGLE_KEYS - {"weight_kg"}


def test_metrics_page_renders_with_hidden(db, page, restore_display_setting):
    _set_hidden(db, ["mood_score", "morning_erection"])
    resp = page.get("/metrics")
    assert resp.status_code == 200
    assert "展示和录入分别控制" in resp.text
    assert "保存展示设置" in resp.text
    assert "保存录入设置" in resp.text
    assert "当前状态" in resp.text
    assert "更多指标…" in resp.text
    assert 'href="/metrics?metric=sleep&days=30"' in resp.text


def test_metrics_entry_deep_link_and_mobile_parent_navigation(db, page):
    metrics = page.get("/metrics").text
    assert "if (target.tagName === 'DETAILS') target.open = true" in metrics
    assert 'href="#metrics-entry"' in metrics

    scale = page.get("/scale").text
    assert 'aria-label="返回身体趋势"' in scale
    assert 'href="/metrics"' in scale

    foods = page.get("/diet/foods").text
    assert 'aria-label="返回饮食记录"' in foods


def test_today_overview_fragment(db, page):
    resp = page.get("/fragments/today/overview")
    assert resp.status_code == 200
    assert "体重与体成分" in resp.text
    assert "今日活动" in resp.text
    assert "同步状态" in resp.text
    assert "需要时补录" not in resp.text


# ---------- Bearer 备用头 X-Ingest-Token（V8.3：frp Basic 验证占用 Authorization） ----------

@pytest.fixture()
def api(db):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    if not get_settings().ingest_token:
        pytest.skip("INGEST_TOKEN 未配置")
    with TestClient(app) as c:
        yield c, get_settings().ingest_token


def test_bearer_header_still_works(api):
    c, token = api
    assert c.get("/api/agent/summary", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_x_ingest_token_header_accepted(api):
    # frp Basic 验证场景：Authorization 被 Basic 占用，token 走备用头
    c, token = api
    resp = c.get("/api/agent/summary", headers={
        "Authorization": "Basic dXNlcjpwYXNz",  # frp 消费的 Basic 凭据，app 不认它
        "X-Ingest-Token": token,
    })
    assert resp.status_code == 200


def test_x_ingest_token_wrong_rejected(api):
    c, _ = api
    assert c.get("/api/agent/summary", headers={"X-Ingest-Token": "wrong"}).status_code == 401


def test_bearer_takes_priority_over_alt_header(api):
    # Authorization Bearer 合法时优先；备用头乱填不影响
    c, token = api
    resp = c.get("/api/agent/summary", headers={
        "Authorization": f"Bearer {token}", "X-Ingest-Token": "garbage",
    })
    assert resp.status_code == 200
