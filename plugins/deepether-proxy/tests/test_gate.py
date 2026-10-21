"""并发闸门与熔断器。"""

import asyncio

import pytest

from deepether.errors import DeepEtherError
from deepether.gate import CircuitBreaker, ConcurrencyGate


def test_gate_limits_concurrency():
    async def scenario():
        gate = ConcurrencyGate(max_concurrency=2, queue_wait_sec=5)
        peak = 0
        current = 0

        async def worker():
            nonlocal peak, current
            async with gate:
                current += 1
                peak = max(peak, current)
                await asyncio.sleep(0.05)
                current -= 1

        await asyncio.gather(*[worker() for _ in range(6)])
        return peak

    assert asyncio.run(scenario()) == 2


def test_gate_rejects_after_queue_timeout():
    async def scenario():
        gate = ConcurrencyGate(max_concurrency=1, queue_wait_sec=0.05)
        await gate.acquire()
        try:
            with pytest.raises(DeepEtherError) as excinfo:
                await gate.acquire()
            return excinfo.value
        finally:
            gate.release()

    error = asyncio.run(scenario())
    assert error.code == "concurrency_rejected"
    assert error.retryable is True


def test_gate_release_decrements_in_flight():
    async def scenario():
        gate = ConcurrencyGate(max_concurrency=1, queue_wait_sec=1)
        await gate.acquire()
        assert gate.in_flight == 1
        gate.release()
        return gate.in_flight

    assert asyncio.run(scenario()) == 0


def test_circuit_opens_after_threshold():
    breaker = CircuitBreaker(fail_threshold=3, cooldown_sec=10, clock=lambda: 0.0)
    assert breaker.state == "closed"
    assert breaker.allow() is True
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow() is False


def test_circuit_half_open_after_cooldown_allows_single_probe():
    now = {"value": 0.0}
    breaker = CircuitBreaker(fail_threshold=1, cooldown_sec=10, clock=lambda: now["value"])
    breaker.record_failure()
    assert breaker.allow() is False

    now["value"] = 11.0
    assert breaker.state == "half_open"
    assert breaker.allow() is True
    assert breaker.allow() is False  # 探针在飞，不再放行


def test_circuit_success_closes():
    breaker = CircuitBreaker(fail_threshold=1, cooldown_sec=10, clock=lambda: 0.0)
    breaker.record_failure()
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() is True

def test_half_open_probe_slot_can_be_released():
    """探针请求被取消时必须能归还名额，否则熔断器永久卡在 half_open → 永久降级。"""

    now = {"value": 0.0}
    breaker = CircuitBreaker(fail_threshold=1, cooldown_sec=10, clock=lambda: now["value"])
    breaker.record_failure()
    assert breaker.allow() is False

    now["value"] = 11.0
    assert breaker.state == "half_open"
    assert breaker.allow() is True
    assert breaker.probe_in_flight is True
    assert breaker.allow() is False  # 探针在飞，其余请求降级

    breaker.abort_probe()  # 模拟探针请求被取消
    assert breaker.probe_in_flight is False
    assert breaker.allow() is True  # 名额归还后仍可继续探针

    breaker.record_success()
    assert breaker.state == "closed"
