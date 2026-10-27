"""CommandCode（Go 套餐）上游协议常量。

`/alpha/generate` 没有公开文档，字段形状来自官方 CLI `command-code@1.88.0` 的实测
（见 `docs/research/commandcode/PROTOCOL-EVIDENCE.md`）。**所有上游假设集中在本模块**：
网关一旦改版，只改这里，不要散落到调用点。
"""

from typing import Final

# ─── 端点 ───────────────────────────────────────────────────────────────
DEFAULT_BASE_URL: Final[str] = "https://api.commandcode.ai"
PATH_GENERATE: Final[str] = "/alpha/generate"
PATH_WHOAMI: Final[str] = "/alpha/whoami"
PATH_CREDITS: Final[str] = "/alpha/billing/credits"
PATH_SUBSCRIPTIONS: Final[str] = "/alpha/billing/subscriptions"
PATH_USAGE_SUMMARY: Final[str] = "/alpha/usage/summary"
PATH_MODELS: Final[str] = "/provider/v1/models"

# ─── 请求头（官方 CLI buildCommandAuthHeaders 的形状）────────────────────
HEADER_AUTHORIZATION: Final[str] = "Authorization"
HEADER_CONTENT_TYPE: Final[str] = "Content-Type"
HEADER_USER_AGENT: Final[str] = "User-Agent"
HEADER_CLI_VERSION: Final[str] = "x-command-code-version"
HEADER_CLI_ENVIRONMENT: Final[str] = "x-cli-environment"
HEADER_PROJECT_SLUG: Final[str] = "x-project-slug"
HEADER_TASTE_LEARNING: Final[str] = "x-taste-learning"
HEADER_SESSION_ID: Final[str] = "x-session-id"

USER_AGENT_VALUE: Final[str] = "cli"
CLI_ENVIRONMENT_VALUE: Final[str] = "production"
DEFAULT_CLI_VERSION: Final[str] = "1.88.0"
DEFAULT_PROJECT_SLUG: Final[str] = "maibot"

# ─── 请求信封 ───────────────────────────────────────────────────────────
PERMISSION_MODE_STANDARD: Final[str] = "standard"
DEFAULT_MAX_TOKENS: Final[int] = 8192
DEFAULT_TEMPERATURE: Final[float] = 0.7
MAX_OUTPUT_TOKENS: Final[int] = 64000

# ─── 流式响应 ───────────────────────────────────────────────────────────
#: 单行（一个事件）上限；超过按协议错误处理，避免把内存吃穿。
MAX_EVENT_LINE_BYTES: Final[int] = 8 * 1024 * 1024
#: 一整次响应的事件总字节上限。
MAX_STREAM_BYTES: Final[int] = 64 * 1024 * 1024
#: `pause_turn` 续跑次数上限（CLI 内部也有上限，这里取保守值）。
MAX_PAUSE_TURN_ROUNDS: Final[int] = 2

# ─── 用量 / 结束原因 ────────────────────────────────────────────────────
#: 这些 finishReason 视为"继续跑"，不是结束（已按 normalize_finish_reason 归一）。
PAUSE_FINISH_REASONS: Final[frozenset[str]] = frozenset({"pause-turn"})
#: 这些 finishReason 说明输出被截断，工具参数可能不完整。
TRUNCATED_FINISH_REASONS: Final[frozenset[str]] = frozenset(
    {"length", "max_tokens", "max-output-tokens"}
)

# ─── 错误识别（大小写不敏感的子串）──────────────────────────────────────
MODEL_NOT_IN_PLAN_PREFIX: Final[str] = "MODEL_NOT_IN_PLAN"
UPGRADE_REQUIRED_MARKER: Final[str] = "upgrade_required"
BUDGET_MARKERS: Final[tuple[str, ...]] = (
    "premium_credits_exhausted",
    "insufficient credits",
    "insufficient_credits",
    "credit balance",
    "quota exceeded",
)
RENEWAL_MARKERS: Final[tuple[str, ...]] = ("renewal_pending",)
CONTEXT_LIMIT_MARKERS: Final[tuple[str, ...]] = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "token limit",
)
REQUEST_TOO_LARGE_MARKERS: Final[tuple[str, ...]] = (
    "json node count",
    "too large",
    "payload too large",
)
CLOUDFLARE_MARKERS: Final[tuple[str, ...]] = (
    "cf-mitigated",
    "error 1020",
    "attention required",
    "<!doctype html",
    "<html",
)

# ─── 本地默认值 ─────────────────────────────────────────────────────────
DEFAULT_REQUEST_TIMEOUT_SEC: Final[int] = 285
DEFAULT_MAX_HISTORY_CHARS: Final[int] = 60000
DEFAULT_MAX_TOOL_OUTPUT_CHARS: Final[int] = 20000
DEFAULT_MAX_IMAGE_BYTES: Final[int] = 4 * 1024 * 1024
DEFAULT_THREAD_REUSE_CALLS: Final[int] = 50
DEFAULT_FIVE_HOUR_BUDGET_USD: Final[float] = 3.0
DEFAULT_WEEKLY_BUDGET_USD: Final[float] = 6.0
DEFAULT_MONTHLY_BUDGET_USD: Final[float] = 9.0
DEFAULT_BUDGET_RESERVE_USD: Final[float] = 0.5
DEFAULT_ENVIRONMENT: Final[str] = "win32-x64"
