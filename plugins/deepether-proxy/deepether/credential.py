"""网页版凭证的落盘、读取与状态管理。

凭证只有一个：浏览器里 `localStorage.userToken` 的 `.value`。
落盘位置是插件专属 `data_dir`（已被 .gitignore 覆盖，不进版本库），
**日志里只允许出现脱敏形式**（前 4 位 + 长度）。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import json
import logging
import os

from .constants import CREDENTIAL_FILE_NAME
from .errors import mask_secret

CredentialState = Literal["missing", "present", "invalid"]

SOURCE_CONFIG: Final[str] = "config"
SOURCE_BOOKMARKLET: Final[str] = "bookmarklet"


@dataclass(frozen=True)
class CredentialRecord:
    """一条已保存的凭证。"""

    token: str
    source: str
    updated_at: str

    @property
    def masked(self) -> str:
        """返回脱敏后的 token，可安全写日志。"""

        return mask_secret(self.token)


class CredentialStore:
    """凭证文件读写与状态机。

    状态含义：
    - `missing`：没有任何 token；
    - `present`：有 token，且最近一次使用/探活没有判定失效；
    - `invalid`：有 token，但上游明确返回了 401/403，需要用户重新授权。
    """

    def __init__(self, data_dir: Path, logger: logging.Logger) -> None:
        """初始化凭证存储。

        Args:
            data_dir: 插件专属可写目录。
            logger: 插件日志器。
        """

        self._data_dir = data_dir
        self._path = data_dir / CREDENTIAL_FILE_NAME
        self._logger = logger
        self._record: CredentialRecord | None = None
        self._invalid_reason: str = ""

    # ─── 读 ─────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """返回凭证文件路径。"""

        return self._path

    @property
    def record(self) -> CredentialRecord | None:
        """返回当前凭证记录（不含配置里的 token）。"""

        return self._record

    @property
    def invalid_reason(self) -> str:
        """返回最近一次判定失效的原因。"""

        return self._invalid_reason

    @property
    def state(self) -> CredentialState:
        """返回当前凭证状态。"""

        if self._record is None or not self._record.token:
            return "missing"
        if self._invalid_reason:
            return "invalid"
        return "present"

    def load(self) -> CredentialRecord | None:
        """从磁盘加载凭证；文件不存在或损坏时返回 `None`。

        Returns:
            CredentialRecord | None: 加载到的凭证记录。
        """

        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._logger.warning("凭证文件无法解析，将按“未配置”处理：%s", exc)
            return None
        if not isinstance(raw, dict):
            self._logger.warning("凭证文件格式不正确（不是 JSON 对象），已忽略")
            return None

        token = str(raw.get("token") or "").strip()
        if not token:
            return None

        self._record = CredentialRecord(
            token=token,
            source=str(raw.get("source") or SOURCE_BOOKMARKLET),
            updated_at=str(raw.get("updated_at") or ""),
        )
        self._invalid_reason = str(raw.get("invalid_reason") or "")
        self._logger.info(
            "已加载网页版凭证：%s，来源=%s，更新于 %s",
            self._record.masked,
            self._record.source,
            self._record.updated_at or "未知",
        )
        return self._record

    # ─── 写 ─────────────────────────────────────────────────────────────

    def save(self, token: str, source: str) -> CredentialRecord:
        """保存一条新 token，并清除“已失效”标记。

        Args:
            token: 网页版 Bearer token。
            source: 来源标记，例如 `bookmarklet` / `config`。

        Returns:
            CredentialRecord: 写入后的凭证记录。

        Raises:
            ValueError: token 为空时抛出。
        """

        normalized = (token or "").strip()
        if not normalized:
            raise ValueError("凭证 token 不能为空")

        record = CredentialRecord(
            token=normalized,
            source=source or SOURCE_BOOKMARKLET,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._invalid_reason = ""
        self._record = record
        self._persist()
        self._logger.info("网页版凭证已更新：%s，来源=%s", record.masked, record.source)
        return record

    def mark_invalid(self, reason: str) -> None:
        """标记当前凭证已失效（上游返回 401/403 时调用）。

        Args:
            reason: 中文失效原因。
        """

        self._invalid_reason = reason or "凭证已失效"
        if self._record is not None:
            self._persist()
        self._logger.warning(
            "网页版凭证已失效（%s）：%s。请重新获取 token 并更新到插件配置的 credential.web_tokens。",
            self._invalid_reason,
            self._record.masked if self._record else "(空)",
        )

    def clear(self) -> None:
        """删除本地凭证文件并清空内存状态。"""

        self._record = None
        self._invalid_reason = ""
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            self._logger.warning("删除凭证文件失败：%s", exc)
            return
        self._logger.info("网页版凭证已清除")

    # ─── 内部 ───────────────────────────────────────────────────────────

    def _persist(self) -> None:
        """把当前状态原子写入凭证文件。"""

        payload = {
            "token": self._record.token if self._record else "",
            "source": self._record.source if self._record else "",
            "updated_at": self._record.updated_at if self._record else "",
            "invalid_reason": self._invalid_reason,
        }
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, self._path)
        except OSError as exc:
            self._logger.error("写入凭证文件失败（本次仅在内存生效）：%s", exc)