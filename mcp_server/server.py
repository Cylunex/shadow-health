"""Thin machine-v1 MCP adapter. Read or draft only; never holds a write credential.

Each installation supplies HEALTH_AGENT_TOKEN and HEALTH_PROFILE_ID. INGEST_TOKEN
is intentionally ignored. Stable caller-supplied idempotency survives MCP restarts.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("HEALTH_API_BASE", "http://127.0.0.1:8080").rstrip("/")
MCP_HOST = "127.0.0.1"
MCP_PORT = int(os.environ.get("MCP_PORT", "8180"))
mcp = FastMCP("shadow-health", host=MCP_HOST, port=MCP_PORT,
              instructions="健康工具只读或创建草案。草案不是入库成功；用户在 Health/Nexus 审核。不得猜测日期和份量。")


class ApiError(RuntimeError):
    pass


def _request(method: str, suffix: str, *, params=None, body=None, key=None):
    endpoint = urlsplit(API_BASE)
    if not endpoint.netloc or (endpoint.scheme != "https" and not (
            endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"})):
        raise ApiError("Health 远程 API 必须使用 HTTPS；仅回环地址允许 HTTP")
    token = os.environ.get("HEALTH_AGENT_TOKEN", "")
    profile = os.environ.get("HEALTH_PROFILE_ID", "primary")
    if not token:
        raise ApiError("HEALTH_AGENT_TOKEN 未配置；不再使用 INGEST_TOKEN")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", profile):
        raise ApiError("HEALTH_PROFILE_ID 无效")
    headers = {"Authorization": "Bearer " + token}
    if key is not None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", key):
            raise ApiError("必须提供稳定的 16–128 字符 idempotency_key；重试原样复用")
        headers["Idempotency-Key"] = key
    with httpx.Client(base_url=API_BASE, timeout=15.0, follow_redirects=False) as client:
        response = client.request(method, f"/api/machine/v1/agent/profiles/{profile}/{suffix}",
                                  params=params, json=body, headers=headers)
    if not response.is_success:
        raise ApiError(f"Health 请求失败（HTTP {response.status_code}），不要声称记录成功")
    return response.json()


@mcp.tool()
def query_today_summary(date: str) -> dict:
    """读取指定日期的最小健康摘要，不返回私密习惯、化验与原始序列。"""
    return _request("GET", "summary", params={"date": date})


@mcp.tool()
def query_metric_series(field: str, days: int = 30) -> dict:
    """读取聚合趋势，不返回原始逐点健康数据。"""
    if field not in {"weight_kg", "sleep_hours", "steps"} or not 7 <= days <= 90:
        raise ApiError("指标或时间范围不在白名单")
    return _request("GET", "trends", params={"metric": field, "days": days})


@mcp.tool()
def draft_record(record_type: str, effective_date: str, fields: dict[str, Any], idempotency_key: str) -> dict:
    """创建 metric/meal/workout 草案。meal 支持 items 一餐多项。尚未入库，须用户审核。"""
    return _request("POST", "drafts", key=idempotency_key,
                    body={"record_type": record_type, "effective_date": effective_date, "fields": fields})


@mcp.tool()
def draft_meal_update(row_id: int, effective_date: str, fields: dict[str, Any], idempotency_key: str) -> dict:
    """修正一条饮食的差异草案；不删除重建、不直接写。"""
    return _request("POST", "drafts", key=idempotency_key, body={"record_type": "meal",
        "effective_date": effective_date, "fields": fields, "operation": "update", "target_id": row_id})


@mcp.tool()
def query_weekly_evidence(end: str) -> dict:
    """读取两个独立周窗口的确定性比较与来源证据，不生成医疗处方。"""
    return _request("GET", "weekly-evidence", params={"end": end})


@mcp.tool()
def query_data_status() -> dict:
    """解释服务端已知同步状态；不凭未收到消息断言手机蓝牙故障。"""
    return _request("GET", "data-status")
