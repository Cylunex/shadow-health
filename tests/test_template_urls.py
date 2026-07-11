"""模板 URL lint（V3 P1 子路径适配防返工锁）+ 前缀中间件回归。

子路径部署（/shealth）要求模板里所有站内 URL 经全局 u() 补前缀。
lint 扫描 templates/ 全部 *.html：URL 属性（href/src/action/hx-get/hx-post/
hx-put/hx-delete/hx-push-url）不允许再出现裸绝对路径值。
白名单：// 开头（协议相对）、http 开头（外链）、{{ 开头（已模板化）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.config import BASE_DIR

TEMPLATES_DIR = BASE_DIR / "templates"

URL_ATTRS = (
    "href", "src", "action",
    "hx-get", "hx-post", "hx-put", "hx-delete", "hx-push-url",
)
# 属性名前要求空白：排除内联 JS 的 location.href='/...'（那类由 u() 模板化，单独人工核）
BARE_ABS = re.compile(
    r"(?<=\s)(" + "|".join(re.escape(a) for a in URL_ATTRS) + r""")=["'](/[^"']*)["']"""
)


def _bare_urls(text: str) -> list[str]:
    hits = []
    for m in BARE_ABS.finditer(text):
        value = m.group(2)
        if value.startswith("//"):  # 协议相对，不属于站内路径
            continue
        hits.append(m.group(0))
    return hits


def test_no_bare_absolute_urls_in_templates():
    assert TEMPLATES_DIR.is_dir()
    offenders: list[str] = []
    for f in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = f.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for hit in _bare_urls(line):
                offenders.append(f"{f.relative_to(BASE_DIR)}:{lineno}: {hit}")
    assert not offenders, (
        "模板出现裸绝对路径属性（子路径部署会丢前缀），改成 {{ u('/...') }}：\n"
        + "\n".join(offenders)
    )


# ---------- 前缀中间件 + URL 生成（不依赖 DB 的路径） ----------

@pytest.fixture(scope="module")
def client():
    from app.main import app

    return TestClient(app, follow_redirects=False)


PREFIX = {"X-Forwarded-Prefix": "/shealth"}


def test_login_page_urls_prefixed(client):
    html = client.get("/login", headers=PREFIX).text
    assert '"/shealth/static/app.css"' in html
    assert 'action="/shealth/login"' in html
    # 不允许再冒出裸的 /static 引用
    assert 'href="/static/' not in html


def test_login_page_urls_bare_without_header(client):
    html = client.get("/login").text
    assert 'href="/static/app.css"' in html
    assert "/shealth" not in html


def test_unauth_redirect_carries_prefix(client):
    r = client.get("/metrics", headers=PREFIX)
    assert r.status_code == 303
    assert r.headers["location"] == "/shealth/login"
    # htmx 片段走 HX-Redirect
    r = client.get("/fragments/today/rings", headers={**PREFIX, "HX-Request": "true"})
    assert r.headers["HX-Redirect"] == "/shealth/login"


def test_unauth_redirect_bare_without_header(client):
    r = client.get("/metrics")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_prefix_header_sanitized(client):
    # 非 / 开头的脏头不生效；尾斜杠会被剥掉
    r = client.get("/metrics", headers={"X-Forwarded-Prefix": "shealth"})
    assert r.headers["location"] == "/login"
    r = client.get("/metrics", headers={"X-Forwarded-Prefix": "/shealth/"})
    assert r.headers["location"] == "/shealth/login"
