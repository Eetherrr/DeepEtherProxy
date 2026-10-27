"""Go 模型目录：条数、命名、筛选与生成脚本一致性。"""

import importlib.util
import sys
from pathlib import Path

import pytest

from deepether.commandcode.catalog import CURATED_IDENTIFIERS, CcModel, load_catalog

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGIN_ROOT.parent.parent
CATALOG_JSON = PLUGIN_ROOT / "deepether" / "commandcode" / "models_go.json"
MODELS_MD = REPO_ROOT / "docs" / "research" / "commandcode" / "models-1.88.0.md"
GENERATOR = REPO_ROOT / "scripts" / "gen_commandcode_catalog.py"


def test_catalog_has_all_go_models_with_unique_names():
    catalog = load_catalog()
    assert len(catalog) == 56
    names = [model.name for model in catalog]
    identifiers = [model.model_identifier for model in catalog]
    assert len(set(names)) == 56
    assert len(set(identifiers)) == 56
    assert all(model.name.startswith("cc-") for model in catalog)
    assert all(model.min_plan == "Go and above" for model in catalog)
    assert catalog.cli_version == "1.88.0"


def test_catalog_lookup_is_case_sensitive_for_identifiers():
    catalog = load_catalog()
    assert catalog.by_identifier("Qwen/Qwen3.8-Max") is not None
    assert catalog.by_identifier("qwen/qwen3.8-max") is None
    assert catalog.by_name("CC-QWEN3.8-MAX") is not None


def test_curated_models_all_exist():
    catalog = load_catalog()
    curated = catalog.select(mode="curated")
    assert len(curated) == len(CURATED_IDENTIFIERS) == 10
    assert {model.model_identifier for model in curated} == set(CURATED_IDENTIFIERS)


def test_select_exclude_and_include():
    catalog = load_catalog()
    without = catalog.select(mode="all", exclude="cc-deepseek-v4-flash,zai-org/GLM-5.3")
    assert len(without) == 54
    extra = catalog.select(mode="curated", include="xiaomi/mimo-v2.6-pro")
    assert "xiaomi/mimo-v2.6-pro" in {model.model_identifier for model in extra}
    assert catalog.select(mode="none") == ()
    assert catalog.select(mode="none", include="cc-kimi-k3")


def test_cost_calculation_includes_cache_reads():
    model = CcModel(
        name="cc-x",
        model_identifier="x",
        price_in=1.0,
        price_out=2.0,
        price_cache_read=0.1,
    )
    cost = model.cost_usd(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=500_000)
    assert cost == pytest.approx(0.5 * 1.0 + 0.5 * 0.1)


def test_catalog_snapshot_matches_generator():
    """生成物必须与 models.md 一致；否则打包前忘了跑脚本就会发空目录。"""

    spec = importlib.util.spec_from_file_location("gen_catalog_under_test", str(GENERATOR))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    rows = module.parse_models_md(MODELS_MD.read_text(encoding="utf-8"))
    expected = module.render(module.build_catalog(rows))
    assert CATALOG_JSON.read_text(encoding="utf-8") == expected


def test_catalog_file_is_not_massive():
    """目录不该把价格表以外的东西塞进来（防止误把整份文档打包）。"""

    assert CATALOG_JSON.stat().st_size < 60_000
