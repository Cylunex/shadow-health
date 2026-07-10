"""确保项目根在 sys.path（uv run pytest 从仓库根运行）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
