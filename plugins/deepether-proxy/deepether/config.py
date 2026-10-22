"""插件配置模型。

配置分区对应 WebUI 配置页的分节。所有字段都必须有默认值，否则 SDK 无法生成默认配置。
"""

from typing import Literal

from maibot_sdk import Field, PluginConfigBase

from .constants import DEFAULT_ALLOWED_ORIGIN, DEFAULT_CREDENTIAL_PORT


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。

    该节名 `plugin` 与 `config_version` 字段名由宿主保留：缺少版本号会直接导致配置校验失败。
    """

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件；禁用后本 Provider 不再注册到 Host")
    config_version: str = Field(default="0.2.0", description="配置版本号，用于识别配置结构升级")


class ProvisionConfig(PluginConfigBase):
    """自动注册：把 Provider 与预设模型写进 Host 的 model_config.toml。"""

    __ui_label__ = "自动注册"
    __ui_icon__ = "wand"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description=(
            "插件加载时自动把 Provider 与三个预设模型写进 Host 的 config/model_config.toml，"
            "并把 deepether 模型接到任务 model_list 的首位。Host 会热重载该文件、无需重启；"
            "写入前自动备份，内容无变化时不落盘"
        ),
    )
    provider_name: str = Field(
        default="deepether",
        description="写入 model_config.toml 的服务商名称（api_providers 的 name），改这里也要同步改模型里的 api_provider",
    )
    model_config_path: str = Field(
        default="",
        description=(
            "model_config.toml 的绝对路径；留空自动探测：环境变量 DEEPETHER_MODEL_CONFIG、"
            "MAIBOT_INSTANCE_DIR/config/model_config.toml，再按插件目录向上找实例根"
        ),
    )
    wire_tasks: str = Field(
        default="replyer,utils",
        description=(
            "要接到预设模型上的纯文本任务名，逗号分隔；可选 replyer/utils/memory/mid_memory/fast_model/learner。"
            "planner 需要工具调用（function calling）、emoji 需要视觉，网页版做不到，写进去会被忽略并告警。"
            "memory/mid_memory/learner 输出要求 JSON、fast_model 在回复等待路径上，开启前请评估。"
            "留空表示只注册模型、不改任务配置"
        ),
    )
    selection_strategy: Literal["sequential", "balance", "random"] = Field(
        default="sequential",
        description="接线时写入的选择策略；必须 sequential，只有按顺序优先才会先走网页版",
    )
    keep_backup: bool = Field(default=True, description="写入前备份原 model_config.toml（最多保留 5 份）")


class CredentialConfig(PluginConfigBase):
    """凭证配置。"""

    __ui_label__ = "凭证"
    __ui_icon__ = "key"
    __ui_order__ = 2

    web_token: str = Field(
        default="",
        description=(
            "【必填】DeepSeek 网页版登录 token。获取方法：登录 chat.deepseek.com 后按 F12 打开控制台，"
            "执行 copy(JSON.parse(localStorage.getItem('userToken')).value) 复制，粘贴到本项。"
            "它是 Bearer token（不是 cookie），过期时重新获取一次即可"
        ),
    )
    loopback_enabled: bool = Field(
        default=False,
        description=(
            "是否启用本地授权接收服务（仅 127.0.0.1，用于书签小工具一键送达 token）。"
            "正式使用一般不需要，保持关闭即可；手动把 token 填到上面那一项更简单"
        ),
    )
    credential_port: int = Field(
        default=DEFAULT_CREDENTIAL_PORT,
        ge=1024,
        le=65535,
        description="本地授权接收服务的端口（仅在启用 loopback_enabled 时生效），只绑定 127.0.0.1",
    )
    allowed_origin: str = Field(
        default=DEFAULT_ALLOWED_ORIGIN,
        description="允许提交凭证的来源 Origin；只放行该来源，不要填 *",
    )
    health_check_interval_min: int = Field(
        default=30,
        ge=5,
        le=1440,
        description="凭证探活间隔（分钟）；失效会在日志与 /deepether-status 中中文提示",
    )


class BehaviorConfig(PluginConfigBase):
    """通道与生成行为。"""

    __ui_label__ = "行为"
    __ui_icon__ = "sliders-horizontal"
    __ui_order__ = 3

    mode: Literal["web_first", "web_only", "official_only"] = Field(
        default="web_first",
        description=(
            "通道策略：web_first 正常接管网页版（自动注册把 deepether 模型接到任务 model_list 首位）；"
            "official_only 不接管网页版（自动注册把 deepether 模型移出任务 model_list）；"
            "web_only 已废弃，等同 web_first"
        ),
    )
    thinking_enabled: bool = Field(default=True, description="是否让网页版开启深度思考模式")
    search_enabled: bool = Field(
        default=False,
        description=(
            "是否启用网页版内置联网搜索。实测可用（会带引用角标，插件已自动清洗），"
            "但每次调用会多抓一轮网页、明显变慢；一期省 token 场景建议保持关闭"
        ),
    )
    model_type: str = Field(default="default", description="网页版 model_type 参数，保持默认即可")
    request_timeout_sec: int = Field(default=180, ge=10, le=600, description="单次上游请求的软超时（秒）")
    max_history_chars: int = Field(
        default=60000,
        ge=2000,
        le=400000,
        description="渲染 prompt 时保留的最大字符数；超出时从最旧的历史开始丢弃",
    )
    response_protocol: Literal["legacy", "output_items"] = Field(
        default="legacy",
        description="返回给 Host 的响应协议：legacy 为旧标量协议（一期），output_items 为新协议（二期）",
    )
    prompt_style: Literal["plain", "replyer", "raw"] = Field(
        default="plain",
        description=(
            "提示词风格：plain 通用（默认）；replyer 强约束口语化纯文本（§4.5.3 细节 5）；raw 不追加任何指令。"
            "也可以在模型 extra_params 里用 deepether_style 按模型覆盖"
        ),
    )


class ConcurrencyConfig(PluginConfigBase):
    """并发闸门与熔断。"""

    __ui_label__ = "并发与熔断"
    __ui_icon__ = "gauge"
    __ui_order__ = 4

    max_concurrency: int = Field(
        default=2,
        ge=1,
        le=3,
        description="同时进行中的网页版请求上限；网页版账号级并发约 3，建议留一个余量给风控",
    )
    queue_wait_sec: int = Field(
        default=8,
        ge=0,
        le=120,
        description="排队等待上限（秒）；预计等不到就立刻返回失败，避免把请求挂到 hard_timeout",
    )
    circuit_fail_threshold: int = Field(default=3, ge=1, le=20, description="连续失败多少次后熔断")
    circuit_cooldown_sec: int = Field(default=60, ge=5, le=3600, description="熔断冷却时间（秒），冷却后放一个探针请求")


class AntibanConfig(PluginConfigBase):
    """反封禁节奏控制（pacing / 配额 / 会话复用）。

    这一节的每个字段都是降低账号被风控封禁概率的旋钮，取值依据见 docs/ANTIBAN.md。
    调低是安全的，调高之前请先确认上游行为没有变化。
    """

    __ui_label__ = "反封禁"
    __ui_icon__ = "timer"
    __ui_order__ = 5

    enabled: bool = Field(
        default=True,
        description=(
            "是否启用节奏控制。启用后同一账号绝不并发，两次请求之间有 8~20 秒随机间隔，"
            "并有分/时/日三级配额。只有排障时才建议关闭"
        ),
    )
    min_interval_sec: float = Field(default=8.0, ge=1.0, le=300.0, description="两次上游请求的最小间隔（秒）")
    max_interval_sec: float = Field(
        default=20.0,
        ge=1.0,
        le=600.0,
        description="间隔抖动上界（秒）；实际间隔在最小间隔与本值之间随机，固定间隔反而是可疑特征",
    )
    jitter_ratio: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="抖动比例；0 表示不加抖动（不建议），0.25 表示最多在区间上再放宽四分之一",
    )
    max_per_minute: int = Field(default=3, ge=1, le=60, description="每分钟最多调用几次网页版，超过则本次调用直接失败")
    max_per_hour: int = Field(default=30, ge=1, le=3600, description="每小时最多调用几次网页版")
    max_per_day: int = Field(
        default=200,
        ge=1,
        le=20000,
        description="每天最多调用几次网页版；真人重度使用约在 100~200 次，超过基本可判定为自动化",
    )
    cooldown_on_rate_limit_sec: int = Field(
        default=900,
        ge=0,
        le=86400,
        description="命中限流（429 或空正文）后的冷却时间（秒）；冷却期内网页版调用直接失败",
    )
    cooldown_on_ban_sec: int = Field(
        default=21600,
        ge=0,
        le=604800,
        description="疑似账号被封禁后的冷却时间（秒）；重试只会加长封禁，默认 6 小时",
    )
    adaptive_backoff: bool = Field(
        default=True,
        description="是否让冷却后的请求间隔自动翻倍（连续命中风控时逐步退避，成功一次后折半恢复）",
    )
    session_reuse_calls: int = Field(
        default=8,
        ge=1,
        le=100,
        description="同一个上游会话最多复用于多少次调用；每轮都新建会话是明显的机器行为",
    )
    persist_state: bool = Field(
        default=True,
        description="是否把配额与冷却状态落盘；关闭后重启插件等于配额清零（不建议）",
    )


class DebugConfig(PluginConfigBase):
    """调试与诊断。"""

    __ui_label__ = "调试"
    __ui_icon__ = "bug"
    __ui_order__ = 7

    dump_request: bool = Field(default=False, description="把 Host 发来的原始请求写入 runtime_dir，用于固化契约测试样本")
    dump_response: bool = Field(default=False, description="把上游原始 SSE 报文写入 runtime_dir，用于协议漂移排查")
    verbose_log: bool = Field(default=False, description="打印上游各阶段耗时与增量统计")


class DeepEtherProxyConfig(PluginConfigBase):
    """DeepEther Proxy 插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    provision: ProvisionConfig = Field(default_factory=ProvisionConfig)
    credential: CredentialConfig = Field(default_factory=CredentialConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    antiban: AntibanConfig = Field(default_factory=AntibanConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
