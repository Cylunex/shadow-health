"""DietLog 人类可读备注的统一边界。"""

from __future__ import annotations

DIET_NOTES_MAX_LENGTH = 1000


def parse_diet_notes(value: object) -> str | None:
    """规范化饮食备注；空白视为未填写，超长明确拒绝而非静默截断。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("饮食备注必须是文本")
    notes = value.strip()
    if len(notes) > DIET_NOTES_MAX_LENGTH:
        raise ValueError(f"饮食备注不能超过 {DIET_NOTES_MAX_LENGTH} 个字符")
    return notes or None
