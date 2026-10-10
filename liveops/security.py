from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    """Encrypt settings at rest with a local Fernet key kept outside the database."""

    def __init__(self, key: str | None = None, key_path: str | Path = "var/master.key") -> None:
        configured = key or os.getenv("APP_MASTER_KEY", "").strip()
        path = Path(key_path)
        if configured:
            raw = configured.encode("ascii")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raw = path.read_bytes().strip()
            else:
                raw = Fernet.generate_key()
                path.write_bytes(raw + b"\n")
        self._fernet = Fernet(raw)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise ValueError("设置密钥与已保存密文不匹配；请恢复原 var/master.key。") from exc
