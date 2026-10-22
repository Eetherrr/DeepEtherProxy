"""自动注册 model_config.toml 的单测。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepether.provision import (
    DEFAULT_PLAN,
    ProvisionSettings,
    candidate_paths,
    detect_model_config,
    parse_wire_tasks,
    provision_model_config,
    settings_from_config,
)

BASE_TOML = """[inner]
version = 1

[[models]]
model_type = "llm"
model_identifier = "deepseek-v4-flash"
name = "deepseek-v4-flash"
api_provider = "DeepSeek"

[model_task_config.replyer]
model_list = ["deepseek-v4-flash"]
selection_strategy = "random"

[model_task_config.utils]
model_list = []
selection_strategy = "random"

[[api_providers]]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
client_type = "openai"
auth_type = "bearer"
"""


def _write_root(tmp_path: Path, text: str = BASE_TOML) -> Path:
    """在 tmp_path 下造一个实例根：config/model_config.toml + plugins/deepether-proxy。"""

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugins" / "deepether-proxy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "model_config.toml").write_text(text, encoding="utf-8")
    return tmp_path


def _provision(tmp_path: Path, settings: ProvisionSettings | None = None):
    """在实例根上跑一次自动注册。"""

    return provision_model_config(
        settings=settings or ProvisionSettings(),
        plugin_dir=tmp_path / "plugins" / "deepether-proxy",
    )


def _parsed(tmp_path: Path) -> dict:
    """读取写入后的配置。"""

    return tomllib.loads((tmp_path / "config" / "model_config.toml").read_text(encoding="utf-8"))


def test_registers_provider_models_and_wires_tasks(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    result = _provision(root)

    assert result.ok and result.changed
    assert result.path == root / "config" / "model_config.toml"

    parsed = _parsed(root)
    providers = [item["name"] for item in parsed["api_providers"]]
    assert providers == ["DeepSeek", "deepether"]
    assert parsed["api_providers"][1]["client_type"] == "com.ether.deepether"
    assert parsed["api_providers"][1]["auth_type"] == "none"
    assert parsed["api_providers"][1]["timeout"] == 300
    # max_retry=1：Host 默认 3 会把一次失败放大成 3 次尝试（每次还要等反封禁间隔）。
    assert parsed["api_providers"][1]["max_retry"] == 1

    models = {item["name"]: item for item in parsed["models"]}
    assert set(models) == {"deepseek-v4-flash"} | {plan.name for plan in DEFAULT_PLAN}
    assert models["deepether-replyer"]["extra_params"] == {
        "deepether_style": "replyer",
        "deepether_thinking": False,
    }
    assert models["deepether-utils"]["api_provider"] == "deepether"
    # planner 需要 function calling，网页版做不到：默认不注册它的模型。
    assert "deepether-planner" not in models

    replyer = parsed["model_task_config"]["replyer"]
    assert replyer["model_list"] == ["deepether-replyer", "deepseek-v4-flash"]
    assert replyer["selection_strategy"] == "sequential"
    assert parsed["model_task_config"]["utils"]["model_list"] == ["deepether-utils"]


def test_second_run_is_noop_and_keeps_file_bytes(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    assert _provision(root).changed
    target = root / "config" / "model_config.toml"
    before = target.read_text(encoding="utf-8")

    second = _provision(root)

    assert second.ok and not second.changed
    assert target.read_text(encoding="utf-8") == before
    assert not list((root / "config").glob("*.tmp"))


def test_wire_tasks_can_be_empty(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    result = _provision(root, ProvisionSettings(wire_tasks=()))

    assert result.ok and result.changed
    parsed = _parsed(root)
    assert parsed["model_task_config"]["replyer"]["model_list"] == ["deepseek-v4-flash"]
    assert parsed["model_task_config"]["replyer"]["selection_strategy"] == "random"


def test_existing_provider_fields_are_corrected(tmp_path: Path) -> None:
    text = BASE_TOML + """
[[api_providers]]
name = "deepether"
base_url = "plugin://wrong"
client_type = "com.ether.deepether"
auth_type = "bearer"
timeout = 60
max_retry = 3
"""
    root = _write_root(tmp_path, text)
    result = _provision(root)

    assert result.ok
    parsed = _parsed(root)
    ours = [item for item in parsed["api_providers"] if item["name"] == "deepether"]
    assert len(ours) == 1
    assert ours[0]["base_url"] == "plugin://com.ether.deepether"
    assert ours[0]["auth_type"] == "none"
    assert ours[0]["timeout"] == 300
    # 旧配置里的 max_retry=3 必须被纠正成 1，否则网页版失败后会在同一模型上重试。
    assert ours[0]["max_retry"] == 1


def test_official_only_detaches_wired_tasks(tmp_path: Path) -> None:
    """切到 official_only 后必须把 deepether 模型移出任务 model_list，让 Host 直接走官方模型。"""

    root = _write_root(tmp_path)
    assert _provision(root, ProvisionSettings(mode="web_first")).changed
    before = _parsed(root)
    assert before["model_task_config"]["replyer"]["model_list"] == ["deepether-replyer", "deepseek-v4-flash"]
    assert before["model_task_config"]["replyer"]["selection_strategy"] == "sequential"

    # selection_strategy 故意给一个与接线要求不同的值：detach 分支不应再写选择策略。
    result = _provision(root, ProvisionSettings(mode="official_only", selection_strategy="random"))

    assert result.ok and result.changed
    parsed = _parsed(root)
    assert parsed["model_task_config"]["replyer"]["model_list"] == ["deepseek-v4-flash"]
    assert parsed["model_task_config"]["replyer"]["selection_strategy"] == "sequential"
    assert parsed["model_task_config"]["utils"]["model_list"] == []
    # 移除后 model_list 为空时必须告警，而不是让用户以为还在用网页版。
    assert any("为空" in item and "utils" in item for item in result.warnings)


def test_warns_when_task_has_no_fallback_model(tmp_path: Path) -> None:
    """web_first 接线后如果任务只剩 deepether 模型，该模型失败时任务没有其它可选模型，必须告警。"""

    # BASE_TOML 里 utils 的 model_list 为空：接线后只剩 deepether-utils。
    root = _write_root(tmp_path)
    result = _provision(root)

    assert result.ok
    parsed = _parsed(root)
    assert parsed["model_task_config"]["utils"]["model_list"] == ["deepether-utils"]
    assert any("没有其它可选模型" in item for item in result.warnings)
    assert any("utils" in item for item in result.warnings if "没有其它可选模型" in item)


def test_web_only_is_treated_as_web_first(tmp_path: Path) -> None:
    """web_only 已废弃：接线行为与 web_first 完全一致。"""

    root_web_first = _write_root(tmp_path / "a")
    root_web_only = _write_root(tmp_path / "b")
    first = _provision(root_web_first, ProvisionSettings(mode="web_first"))
    second = _provision(root_web_only, ProvisionSettings(mode="web_only"))

    assert first.ok and second.ok
    text_first = (root_web_first / "config" / "model_config.toml").read_text(encoding="utf-8")
    text_web_only = (root_web_only / "config" / "model_config.toml").read_text(encoding="utf-8")
    assert text_first == text_web_only


def test_model_name_conflict_skips_only_that_model(tmp_path: Path) -> None:
    text = BASE_TOML + """
[[models]]
model_type = "llm"
model_identifier = "x"
name = "deepether-utils"
api_provider = "DeepSeek"
"""
    root = _write_root(tmp_path, text)
    result = _provision(root)

    assert result.ok
    assert any("deepether-utils" in item for item in result.warnings)
    parsed = _parsed(root)
    utils_models = [item for item in parsed["models"] if item["name"] == "deepether-utils"]
    assert len(utils_models) == 1
    assert utils_models[0]["api_provider"] == "DeepSeek"
    assert parsed["model_task_config"]["utils"]["model_list"] == []
    assert parsed["model_task_config"]["replyer"]["model_list"][0] == "deepether-replyer"


def test_missing_sections_are_created(tmp_path: Path) -> None:
    root = _write_root(tmp_path, "[inner]\nversion = 1\n")
    result = _provision(root)

    assert result.ok and result.changed
    parsed = _parsed(root)
    assert [item["name"] for item in parsed["api_providers"]] == ["deepether"]
    assert {item["name"] for item in parsed["models"]} == {plan.name for plan in DEFAULT_PLAN}
    assert any("model_task_config" in item for item in result.warnings)


def test_broken_toml_is_reported_and_untouched(tmp_path: Path) -> None:
    root = _write_root(tmp_path, "[[models]\nname = ")
    result = _provision(root)

    assert not result.ok
    assert "解析" in result.error
    assert (root / "config" / "model_config.toml").read_text(encoding="utf-8").startswith("[[models]")


def test_missing_file_reports_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPETHER_MODEL_CONFIG", raising=False)
    monkeypatch.delenv("MAIBOT_INSTANCE_DIR", raising=False)
    settings = ProvisionSettings(model_config_path=str(tmp_path / "nope.toml"))

    result = provision_model_config(settings=settings, plugin_dir=tmp_path)

    assert not result.ok
    assert "没找到" in result.error
    assert result.warnings


def test_detect_prefers_explicit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _write_root(tmp_path)
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(tmp_path / "elsewhere"))
    settings = ProvisionSettings(model_config_path=str(root / "config" / "model_config.toml"))

    hit, candidates = detect_model_config(settings=settings, plugin_dir=tmp_path)

    assert hit == root / "config" / "model_config.toml"
    assert candidates[0] == hit


def test_detect_uses_instance_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _write_root(tmp_path)
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(root))
    monkeypatch.delenv("DEEPETHER_MODEL_CONFIG", raising=False)

    hit, _ = detect_model_config(settings=ProvisionSettings(), plugin_dir=tmp_path / "unrelated")

    assert hit == root / "config" / "model_config.toml"


def test_backup_written_and_pruned(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    target = root / "config" / "model_config.toml"

    for index in range(7):
        text = BASE_TOML + f"\n# noise {index}\n"
        target.write_text(text, encoding="utf-8")
        _provision(root)

    backups = sorted((root / "config").glob("model_config.toml.deepether.bak-*"))
    assert 1 <= len(backups) <= 5


def test_keep_backup_false_writes_nothing_extra(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    _provision(root, ProvisionSettings(keep_backup=False))

    assert not list((root / "config").glob("*deepether.bak*"))


def test_parse_wire_tasks_accepts_separators() -> None:
    # planner 会被跳过（网页版没有工具调用能力），其余任务正常解析。
    assert parse_wire_tasks("replyer, planner；utils") == ("replyer", "utils")
    assert parse_wire_tasks("") == ()
    assert parse_wire_tasks(None) == ()
    assert parse_wire_tasks(("replyer", "replyer")) == ("replyer",)


def test_parse_wire_tasks_skips_planner() -> None:
    """planner（工具调用）与 emoji（视觉）网页版做不到：不报错、但绝不接线。"""

    assert parse_wire_tasks("planner") == ()
    assert parse_wire_tasks(("planner", "utils")) == ("utils",)
    assert parse_wire_tasks("emoji") == ()
    assert parse_wire_tasks("emoji,memory") == ("memory",)


def test_settings_from_config_reports_ignored_planner() -> None:
    section = SimpleNamespace(
        provider_name="deepether",
        model_config_path="",
        wire_tasks="replyer,planner",
        keep_backup=True,
        selection_strategy="sequential",
    )
    settings = settings_from_config(
        SimpleNamespace(provision=section, behavior=SimpleNamespace(mode="web_first"))
    )

    assert settings.wire_tasks == ("replyer",)
    assert "planner" in settings.wire_note


def test_planner_wire_warning_and_no_wiring(tmp_path: Path) -> None:
    """即使直接构造 wire_tasks 带 planner/emoji，也只告警、不接线。"""

    root = _write_root(tmp_path)
    result = _provision(root, ProvisionSettings(wire_tasks=("replyer", "planner", "emoji")))

    assert result.ok
    assert any("planner" in item and "工具调用" in item for item in result.warnings)
    assert any("emoji" in item and "视觉" in item for item in result.warnings)
    assert _parsed(root)["model_task_config"]["replyer"]["model_list"][0] == "deepether-replyer"


def test_opt_in_task_caveat_warning(tmp_path: Path) -> None:
    """接线可选用任务（如 mid_memory）时，要给出 JSON 输出风险提示。"""

    text = BASE_TOML + """
[model_task_config.mid_memory]
model_list = []
selection_strategy = "random"
"""
    root = _write_root(tmp_path, text)
    result = _provision(root, ProvisionSettings(wire_tasks=("mid_memory",)))

    assert result.ok
    assert any("mid_memory" in item and "JSON" in item for item in result.warnings)
    assert _parsed(root)["model_task_config"]["mid_memory"]["model_list"] == ["deepether-utils"]


def test_legacy_planner_model_is_removed(tmp_path: Path) -> None:
    """0.3.0 及更早会注册 deepether-planner；升级后必须清掉模型定义与所有任务接线。"""

    text = BASE_TOML + """
[[models]]
model_type = "llm"
model_identifier = "deepseek-web"
name = "deepether-planner"
api_provider = "deepether"

[model_task_config.planner]
model_list = ["deepether-planner", "deepseek-v4-flash"]
selection_strategy = "sequential"
"""
    root = _write_root(tmp_path, text)
    result = _provision(root)

    assert result.ok
    parsed = _parsed(root)
    assert "deepether-planner" not in {item["name"] for item in parsed["models"]}
    assert parsed["model_task_config"]["planner"]["model_list"] == ["deepseek-v4-flash"]
    assert any("deepether-planner" in item for item in result.applied)
    # 幂等：清理过一次之后不应再有改动
    assert _provision(root).changed is False


def test_parse_wire_tasks_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        parse_wire_tasks("replyer,nonsense")


def test_settings_from_config_defaults_when_section_missing() -> None:
    settings = settings_from_config(SimpleNamespace())

    assert settings.provider_name == "deepether"
    assert settings.wire_tasks == ("replyer", "utils")
    assert settings.keep_backup is True
    assert settings.mode == "web_first"


def test_settings_from_config_reads_section() -> None:
    section = SimpleNamespace(
        provider_name="custom",
        model_config_path="/tmp/model.toml",
        wire_tasks="replyer",
        keep_backup=False,
        selection_strategy="balance",
    )
    settings = settings_from_config(SimpleNamespace(provision=section))

    assert settings.provider_name == "custom"
    assert settings.wire_tasks == ("replyer",)
    assert settings.keep_backup is False
    assert settings.selection_strategy == "balance"


def test_settings_from_config_reads_mode() -> None:
    """通道模式来自 behavior.mode：official_only 决定接线时是插入还是移除模型。"""

    section = SimpleNamespace(
        provider_name="deepether",
        model_config_path="",
        wire_tasks="replyer",
        keep_backup=True,
        selection_strategy="sequential",
    )
    settings = settings_from_config(
        SimpleNamespace(provision=section, behavior=SimpleNamespace(mode="official_only"))
    )

    assert settings.mode == "official_only"
    assert settings.wire_tasks == ("replyer",)


def test_candidate_paths_dedup_and_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = str(tmp_path / "a.toml")
    monkeypatch.setenv("DEEPETHER_MODEL_CONFIG", explicit)
    monkeypatch.delenv("MAIBOT_INSTANCE_DIR", raising=False)

    candidates = candidate_paths(explicit=explicit, plugin_dir=tmp_path)

    assert candidates[0] == Path(explicit)
    assert len(candidates) == len(set(candidates))
