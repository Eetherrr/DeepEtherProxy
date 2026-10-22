"""PoW 封装：挑战提取与错误映射。

说明：`deepseek-pow` 的求解器需要**服务端真实下发的挑战**才能验证。本机合成挑战
一律返回 NoSolutionError（见 P0.5 报告里的风险项 R-PoW），因此这里只断言
"挑战提取正确" 与 "求解失败被翻译成中文错误"，真实求解的用例用 skip 标记，
等拿到 token 后启用。
"""

from types import SimpleNamespace

import base64
import json
import threading
import time

import pytest

from deepether.errors import DeepEtherError
from deepether.upstream.pow import extract_challenge, solve_pow


def _synthetic_payload(difficulty: int = 144000) -> dict:
    return {
        "data": {
            "biz_data": {
                "challenge": {
                    "algorithm": "DeepSeekHashV1",
                    "challenge": "0123456789abcdef0123456789abcdef",
                    "salt": "0123456789abcdef",
                    "difficulty": difficulty,
                    "expire_at": 1783681627181,
                    "signature": "unit-test-signature",
                }
            }
        }
    }


def test_extract_challenge_from_nested_payload():
    challenge = extract_challenge(_synthetic_payload())
    assert challenge["challenge"] == "0123456789abcdef0123456789abcdef"
    assert challenge["salt"] == "0123456789abcdef"


def test_extract_challenge_missing_raises_protocol_error():
    with pytest.raises(DeepEtherError) as excinfo:
        extract_challenge({"code": 0, "data": {}})
    assert excinfo.value.code == "protocol_error"


def test_solver_failure_is_translated_to_chinese_error():
    """合成挑战求解必然失败，但必须转成带中文说明的 pow_failed。"""

    with pytest.raises(DeepEtherError) as excinfo:
        solve_pow(_synthetic_payload())
    error = excinfo.value
    assert error.code == "pow_failed"
    assert "PoW" in error.message
    assert error.retryable is True


@pytest.mark.skip(reason="需要服务端真实挑战才能验证；拿到网页版 token 后启用")
def test_solve_pow_returns_base64_payload_real_challenge():
    payload = json.loads(open(".runtime/real_challenge.json", encoding="utf-8").read())
    header_value = solve_pow(payload)
    decoded = json.loads(base64.b64decode(header_value).decode("utf-8"))
    assert decoded["algorithm"] == "DeepSeekHashV1"
    assert isinstance(decoded["answer"], float)

def test_concurrent_solve_is_serialized(monkeypatch):
    """并发求解必须串行进入 wasm：deepseek-pow 的求解器不是线程安全的。

    实测（P2 演练）2 路并发时约一半请求 trap（out of bounds memory access），
    直接表现成 pow_failed → 交回 Host。这里用假求解器断言"同一时刻只有一个在跑"。
    """

    import deepseek_pow

    state = {"active": 0, "peak": 0}

    class FakeSolution:
        algorithm = "DeepSeekHashV1"
        challenge = "c"
        salt = "s"
        answer = 7
        signature = "sig"

    class FakeSolver:
        def solve(self, challenge):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            state["active"] -= 1
            return FakeSolution()

    monkeypatch.setattr(deepseek_pow, "registry", SimpleNamespace(get=lambda algorithm: FakeSolver()))

    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(solve_pow(_synthetic_payload()))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 4
    assert state["peak"] == 1, "求解必须串行，不能并发进入 wasm"
    decoded = json.loads(base64.b64decode(results[0]).decode("utf-8"))
    assert decoded["answer"] == 7
