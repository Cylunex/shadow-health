"""单用户认证（设计文档 §7.2）。

- .env 存 scrypt 哈希，不存明文：python -m app.auth hash <密码> 生成
- 登录成功签发随机 session token，服务端内存保存
- 登录限速：同 IP 连续失败 5 次锁 60 秒
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import sys
import time

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

SESSION_COOKIE = "sh_session"
SESSION_MAX_AGE = 30 * 24 * 3600

# token -> 签发时间戳
_sessions: dict[str, float] = {}
# ip -> (连续失败次数, 最近失败时间)
_login_failures: dict[str, tuple[int, float]] = {}

LOCK_THRESHOLD = 5
LOCK_SECONDS = 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
        )
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def is_locked(ip: str) -> bool:
    count, last = _login_failures.get(ip, (0, 0.0))
    return count >= LOCK_THRESHOLD and (time.time() - last) < LOCK_SECONDS


def record_failure(ip: str) -> None:
    count, _ = _login_failures.get(ip, (0, 0.0))
    _login_failures[ip] = (count + 1, time.time())


def clear_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time()
    return token


def session_valid(token: str | None) -> bool:
    if not token or token not in _sessions:
        return False
    if time.time() - _sessions[token] > SESSION_MAX_AGE:
        _sessions.pop(token, None)
        return False
    return True


def destroy_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    else:
        print("用法: python -m app.auth hash <密码>", file=sys.stderr)
        sys.exit(1)
