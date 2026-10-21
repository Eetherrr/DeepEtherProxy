"""DeepSeek 网页版上游协议常量。

网页版没有公开契约，接口路径、请求头与请求体字段都可能随前端发版漂移。
**所有上游假设集中在本文件**：上游一旦改版，只需要改这里，不要散落到各调用点。
"""

from typing import Final

# ─── 上游地址 ───────────────────────────────────────────────────────────
UPSTREAM_BASE_URL: Final[str] = "https://chat.deepseek.com"

PATH_CHAT_SESSION_CREATE: Final[str] = "/api/v0/chat_session/create"
PATH_POW_CHALLENGE: Final[str] = "/api/v0/chat/create_pow_challenge"
PATH_CHAT_COMPLETION: Final[str] = "/api/v0/chat/completion"
PATH_USER_CURRENT: Final[str] = "/api/v0/users/current"

# ─── 请求头（名字与取值分开，别混用）──────────────────────────────────────
HEADER_NAME_AUTHORIZATION: Final[str] = "Authorization"
HEADER_NAME_POW_RESPONSE: Final[str] = "x-ds-pow-response"
HEADER_NAME_USER_AGENT: Final[str] = "User-Agent"
HEADER_NAME_ORIGIN: Final[str] = "Origin"
HEADER_NAME_REFERER: Final[str] = "Referer"

USER_AGENT_VALUE: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ORIGIN_VALUE: Final[str] = "https://chat.deepseek.com"
REFERER_VALUE: Final[str] = "https://chat.deepseek.com/"

# ─── 浏览器指纹头（反封控之一：别让请求"不像浏览器"）────────────────────
# 网页版前端每发一个 XHR 都会带下面这套头。只带 Authorization/UA/origin 的请求
# 在服务端看就是"裸脚本"，是风控最容易一眼识别的特征之一。
# 这里只放**与上面 UA 字符串自洽**的标准浏览器头（Chrome 131 + Windows）；
# 不伪造 x-client-* 这类私有头——取值随前端发版变化，写错了比不带更可疑。
HEADER_NAME_ACCEPT_LANGUAGE: Final[str] = "Accept-Language"
HEADER_NAME_SEC_CH_UA: Final[str] = "sec-ch-ua"
HEADER_NAME_SEC_CH_UA_MOBILE: Final[str] = "sec-ch-ua-mobile"
HEADER_NAME_SEC_CH_UA_PLATFORM: Final[str] = "sec-ch-ua-platform"
HEADER_NAME_SEC_FETCH_DEST: Final[str] = "sec-fetch-dest"
HEADER_NAME_SEC_FETCH_MODE: Final[str] = "sec-fetch-mode"
HEADER_NAME_SEC_FETCH_SITE: Final[str] = "sec-fetch-site"

ACCEPT_LANGUAGE_VALUE: Final[str] = "zh-CN,zh;q=0.9,en;q=0.8"
SEC_CH_UA_VALUE: Final[str] = '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
SEC_CH_UA_MOBILE_VALUE: Final[str] = "?0"
SEC_CH_UA_PLATFORM_VALUE: Final[str] = '"Windows"'

#: XHR（application/json）场景下浏览器会发的全套指纹头。
BROWSER_XHR_HEADERS: Final[dict[str, str]] = {
    HEADER_NAME_ACCEPT_LANGUAGE: ACCEPT_LANGUAGE_VALUE,
    HEADER_NAME_SEC_CH_UA: SEC_CH_UA_VALUE,
    HEADER_NAME_SEC_CH_UA_MOBILE: SEC_CH_UA_MOBILE_VALUE,
    HEADER_NAME_SEC_CH_UA_PLATFORM: SEC_CH_UA_PLATFORM_VALUE,
    HEADER_NAME_SEC_FETCH_DEST: "empty",
    HEADER_NAME_SEC_FETCH_MODE: "cors",
    HEADER_NAME_SEC_FETCH_SITE: "same-origin",
}

#: SSE 流（text/event-stream）场景下 sec-fetch-* 与 accept 的取值。
BROWSER_STREAM_ACCEPT: Final[str] = "text/event-stream"
BROWSER_STREAM_SEC_FETCH_DEST: Final[str] = "empty"

# ─── 响应封装（HTTP 200 也可能是业务失败）────────────────────────────────
# 实测（2026-10-21）：token 失效时上游返回 **HTTP 200** + {"code": 40003, "msg": "INVALID_TOKEN"}，
# 必须解析 body 里的 code，否则会被误判成"协议异常/建会话接口变更"，
# 既误导用户，也不会把凭证标记成失效（报告 §6.6.1 凭证层）。
FIELD_CODE: Final[str] = "code"
FIELD_MESSAGE: Final[str] = "msg"
UPSTREAM_AUTH_ERROR_CODES: Final[tuple[int, ...]] = (40003,)
UPSTREAM_AUTH_ERROR_MARKERS: Final[tuple[str, ...]] = ("INVALID_TOKEN", "AUTHORIZATION FAILED")

# ─── 请求体字段 ─────────────────────────────────────────────────────────
DEFAULT_MODEL_TYPE: Final[str] = "default"
DEFAULT_POW_ALGORITHM: Final[str] = "DeepSeekHashV1"
FIELD_TARGET_PATH: Final[str] = "/api/v0/chat/completion"

# ─── 凭证回环服务 ───────────────────────────────────────────────────────
CREDENTIAL_CLAIM_PATH: Final[str] = "/credential/claim"
CREDENTIAL_HEALTH_PATH: Final[str] = "/health"
DEFAULT_CREDENTIAL_PORT: Final[int] = 18765
DEFAULT_ALLOWED_ORIGIN: Final[str] = "https://chat.deepseek.com"
LOOPBACK_HOST: Final[str] = "127.0.0.1"

# ─── 其它 ───────────────────────────────────────────────────────────────
MASK_KEEP_CHARS: Final[int] = 4
CREDENTIAL_FILE_NAME: Final[str] = "credential.json"
STATUS_RECORD_JSON_LIMIT: Final[int] = 200_000
