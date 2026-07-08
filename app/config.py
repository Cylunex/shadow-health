"""应用配置：一切凭据来自 .env（设计文档 §7.1）。"""
import os
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class Settings:
    def __init__(self) -> None:
        _load_dotenv()
        self.database_url: str = os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://health_app:health_dev@127.0.0.1:55433/health_dev",
        )
        self.auth_password_hash: str = os.environ.get("AUTH_PASSWORD_HASH", "")
        self.session_secret: str = os.environ.get("SESSION_SECRET", "")
        self.ingest_token: str = os.environ.get("INGEST_TOKEN", "")
        self.keep_mobile: str = os.environ.get("KEEP_MOBILE", "")
        self.keep_password: str = os.environ.get("KEEP_PASSWORD", "")
        self.upload_dir: Path = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
