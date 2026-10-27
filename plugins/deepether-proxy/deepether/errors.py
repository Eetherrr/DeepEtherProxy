"""统一错误类型与中文错误文案。

插件 Provider 抛出的异常会被 Host 转成 `success=false`（见报告 §2.5 C5），
Host 据此把本次模型尝试判为失败、按任务 `model_list` 切到下一个模型。
所以这里的每条错误都必须带**人类可读的简体中文说明**，方便直接定位问题。
"""

from typing import Any, Final

MASK_KEEP_CHARS: Final[int] = 4


def mask_secret(value: str, keep: int = MASK_KEEP_CHARS) -> str:
    """把敏感字符串脱敏成可安全写进日志的形式。

    Args:
        value: 原始敏感字符串（例如网页版 token）。
        keep: 保留的明文前缀字符数。

    Returns:
        str: 形如 `abcd…(128 字符)` 的脱敏文本；空值返回 `(空)`。
    """

    if not value:
        return "(空)"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}…({len(value)} 字符)"


class ErrorCode:
    """插件内部错误码。

    这些字符串会出现在日志与状态命令里，保持稳定，便于排查时按码检索。
    """

    CREDENTIAL_MISSING: Final[str] = "credential_missing"
    CREDENTIAL_INVALID: Final[str] = "credential_invalid"
    PROTOCOL_ERROR: Final[str] = "protocol_error"
    POW_FAILED: Final[str] = "pow_failed"
    UPSTREAM_HTTP: Final[str] = "upstream_http"
    UPSTREAM_TIMEOUT: Final[str] = "upstream_timeout"
    UPSTREAM_EMPTY: Final[str] = "upstream_empty"
    CONCURRENCY_REJECTED: Final[str] = "concurrency_rejected"
    CIRCUIT_OPEN: Final[str] = "circuit_open"
    PACING_COOLDOWN: Final[str] = "pacing_cooldown"
    PACING_QUOTA: Final[str] = "pacing_quota"
    ACCOUNT_RISK: Final[str] = "account_risk"
    CHANNEL_DISABLED: Final[str] = "channel_disabled"
    NOT_SUPPORTED: Final[str] = "not_supported"
    INTERNAL: Final[str] = "internal"
    # ─── CommandCode 通道 ───────────────────────────────────────────────
    MODEL_NOT_ALLOWED: Final[str] = "model_not_allowed"
    PLAN_NOT_SUPPORTED: Final[str] = "plan_not_supported"
    UPSTREAM_BLOCKED: Final[str] = "upstream_blocked"
    CONTEXT_TOO_LONG: Final[str] = "context_too_long"
    REQUEST_TOO_LARGE: Final[str] = "request_too_large"
    BUDGET_EXHAUSTED: Final[str] = "budget_exhausted"
    BILLING_PENDING: Final[str] = "billing_pending"


class DeepEtherError(Exception):
    """插件统一异常。

    Attributes:
        code: 稳定的错误码，取自 :class:`ErrorCode`。
        message: 面向用户的中文错误说明。
        retryable: 本次失败是否值得重试（用于熔断判定）。
        detail: 供日志使用的补充信息，不直接回给用户。
        status: 上游 HTTP 状态码（有响应时填写）；反封禁分类要按它区分
            "403 封禁 / 403 软限流 / 401 凭证过期"，只靠错误码分不出来。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        detail: str = "",
        status: int | None = None,
    ) -> None:
        """构造插件异常。

        Args:
            code: 稳定的错误码。
            message: 面向用户的中文错误说明。
            retryable: 本次失败是否值得重试。
            detail: 供日志使用的补充信息。
            status: 上游 HTTP 状态码（可空）。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail
        self.status = status

    def __str__(self) -> str:
        """返回带错误码的中文描述。"""

        if self.detail:
            return f"[{self.code}] {self.message}（{self.detail}）"
        return f"[{self.code}] {self.message}"

    def to_payload(self) -> dict[str, Any]:
        """转换为可序列化的错误字典。"""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }
