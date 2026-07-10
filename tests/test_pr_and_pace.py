"""力量组次明细（services/pr）与配速格式（deps.pace_str）的口径锁。"""
from app.deps import pace_str
from app.services.pr import normalize_strength, strength_lines


# ---------- 配速 ----------
def test_pace_str_basic():
    assert pace_str(30, 5) == "6'00\""       # 30 分 5 km
    assert pace_str(33, 5) == "6'36\""


def test_pace_str_rounds_seconds_carry():
    assert pace_str(29.99, 5) == "6'00\""    # 359.88s/km → 进位不出现 5'60"


def test_pace_str_rejects_missing_or_absurd():
    assert pace_str(None, 5) == ""
    assert pace_str(30, None) == ""
    assert pace_str(30, 0.1) == ""           # 距离太短
    assert pace_str(5, 5) == ""              # 1'00"/km 不是人力跑步
    assert pace_str(300, 5) == ""            # 60'00"/km 不是跑走


# ---------- 组次明细校验 ----------
def test_normalize_strength_happy_path():
    raw = [{"exercise": "标准俯卧撑", "sets": [{"reps": 15}, {"reps": 12, "weight_kg": "16"}]}]
    out = normalize_strength(raw)
    assert out == [{"exercise": "标准俯卧撑", "sets": [{"reps": 15}, {"reps": 12, "weight_kg": 16.0}]}]


def test_normalize_strength_drops_garbage():
    raw = [
        {"exercise": "", "sets": [{"reps": 10}]},          # 无名
        {"exercise": "深蹲", "sets": [{"reps": 0}, {"reps": "x"}]},  # 无有效组
        "not-a-dict",
        {"exercise": "壶铃摇摆", "sets": [{"reps": 15, "weight_kg": 9999}]},  # 超重截掉重量保留次数
    ]
    out = normalize_strength(raw)
    assert out == [{"exercise": "壶铃摇摆", "sets": [{"reps": 15}]}]


def test_normalize_strength_empty_returns_none():
    assert normalize_strength([]) is None
    assert normalize_strength("nope") is None


# ---------- 行摘要 ----------
def test_strength_lines_bodyweight_and_weighted():
    detail = {"strength": [
        {"exercise": "标准俯卧撑", "sets": [{"reps": 15}, {"reps": 12}, {"reps": 10}]},
        {"exercise": "壶铃摇摆", "sets": [{"reps": 15, "weight_kg": 16.0}] * 3},
    ]}
    lines = strength_lines(detail)
    assert lines == ["标准俯卧撑 15/12/10", "壶铃摇摆 16kg×15×3"]


def test_strength_lines_tolerates_non_dict():
    assert strength_lines(None) == []
    assert strength_lines({"from_template": True}) == []
