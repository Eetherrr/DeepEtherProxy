"""PoW（工作量证明）求解封装。

网页版每个 completion 请求都要带一次性 `x-ds-pow-response` 头，值是对服务端下发的
挑战做 SHA3 WASM 运算后的答案。求解交给 `deepseek-pow`（随包内置 wasm，离线可用），
本模块只负责：懒加载、把服务端响应里嵌套的挑战抠出来、失败时给中文错误。
"""

from typing import Any, Final

import base64
import json
import threading

from ..constants import DEFAULT_POW_ALGORITHM, FIELD_TARGET_PATH
from ..errors import DeepEtherError, ErrorCode

_REQUIRED_CHALLENGE_KEYS: Final[tuple[str, ...]] = ("challenge", "salt", "difficulty")

# deepseek-pow 的 wasm 求解器是**进程内共享实例**，不是线程安全的。
# 并发求解会互相踩线性内存，实测 2 路并发时约一半请求 trap
# （wasm trap: out of bounds memory access / call stack exhausted），
# 表现为 pow_failed → 本次调用失败，等于并发越高越用不了网页版（P2 演练实测）。
# 求解只要 ~100ms，加一把锁串行化即可，代价远小于误判失败。
_SOLVE_LOCK: Final[threading.Lock] = threading.Lock()


def _find_challenge(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """在服务端响应里递归寻找挑战对象。

    Args:
        node: 待搜索的 JSON 节点。
        depth: 当前递归深度（防止异常结构导致无限递归）。

    Returns:
        dict[str, Any] | None: 找到的挑战字典。
    """

    if depth > 6:
        return None
    if isinstance(node, dict):
        if all(key in node for key in _REQUIRED_CHALLENGE_KEYS):
            return node
        for value in node.values():
            found = _find_challenge(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_challenge(item, depth + 1)
            if found is not None:
                return found
    return None


def extract_challenge(payload: dict[str, Any]) -> dict[str, Any]:
    """从 `create_pow_challenge` 的响应里提取挑战。

    Args:
        payload: 服务端响应 JSON。

    Returns:
        dict[str, Any]: 挑战字典。

    Raises:
        DeepEtherError: 响应结构里找不到挑战时抛出（通常意味着协议漂移）。
    """

    challenge = _find_challenge(payload)
    if challenge is None:
        raise DeepEtherError(
            ErrorCode.PROTOCOL_ERROR,
            "上游没有返回可用的 PoW 挑战，协议可能已变更",
            detail=f"响应片段: {json.dumps(payload, ensure_ascii=False)[:300]}",
        )
    return challenge


def solve_pow(challenge_payload: dict[str, Any]) -> str:
    """求解挑战并返回可直接放进请求头的 base64 载荷。

    该函数是同步 CPU 计算（约 0.3s），调用方应放进线程里执行，避免阻塞事件循环。

    Args:
        challenge_payload: `create_pow_challenge` 的响应 JSON。

    Returns:
        str: `x-ds-pow-response` 头的值。

    Raises:
        DeepEtherError: 依赖缺失、算法不支持或求解失败时抛出。
    """

    challenge_data = extract_challenge(challenge_payload)
    algorithm = str(challenge_data.get("algorithm") or DEFAULT_POW_ALGORITHM)

    try:
        from deepseek_pow import Challenge, registry
    except ImportError as exc:
        raise DeepEtherError(
            ErrorCode.POW_FAILED,
            "缺少 PoW 求解依赖 deepseek-pow，请确认插件依赖安装成功",
            detail=str(exc),
        ) from exc

    try:
        challenge = Challenge(
            algorithm=algorithm,
            challenge=str(challenge_data["challenge"]),
            salt=str(challenge_data["salt"]),
            difficulty=challenge_data["difficulty"],
            expire_at=challenge_data.get("expire_at") or challenge_data.get("expireAt") or 0,
            signature=challenge_data.get("signature"),
        )
        solver = registry.get(algorithm)
        # 必须串行：wasm 实例共享且非线程安全，并发求解会 trap。
        with _SOLVE_LOCK:
            solution = solver.solve(challenge)
    except DeepEtherError:
        raise
    except Exception as exc:  # noqa: BLE001 - 第三方库异常类型不稳定
        raise DeepEtherError(
            ErrorCode.POW_FAILED,
            "PoW 求解失败，算法可能已升级（DeepSeekHashV2？）",
            detail=f"{type(exc).__name__}: {exc}",
            retryable=True,
        ) from exc

    # 注意：不能直接用 `solution.to_answer_payload()`，它缺少服务端要求的 `target_path`，
    # 少了这个字段上游会返回 40301 INVALID_POW_RESPONSE（2026-10-21 实测）。
    answer = solution.answer
    answer_value: float | int = int(answer) if float(answer).is_integer() else answer
    answer_payload = {
        "algorithm": solution.algorithm,
        "challenge": solution.challenge,
        "salt": solution.salt,
        "answer": answer_value,
        "signature": solution.signature,
        "target_path": str(challenge_data.get("target_path") or FIELD_TARGET_PATH),
    }
    raw = json.dumps(answer_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")
