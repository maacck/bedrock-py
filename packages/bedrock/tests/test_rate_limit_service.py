"""Service-level rate-limit behavior: validation, key hashing, policy conflicts, and backend selection."""

from __future__ import annotations

import re
import subprocess
import sys
import uuid
from collections.abc import Iterator

import pytest
from bedrock.contrib.rate_limit import (
    InvalidRateLimitArgumentError,
    RateLimitBackendNotConfiguredError,
    RateLimitDecision,
    RateLimitPolicy,
    RateLimitPolicyConflictError,
    RateLimitService,
    register_backend,
)
from bedrock.contrib.rate_limit.backends.redis import RedisRateLimitBackend
from pydantic_settings import BaseSettings

POLICY = RateLimitPolicy(name="login", limit=5, period=60)


class _SpyBackend:
    """Structural backend that records each call and allows everything."""

    holds_async_resources = False

    def __init__(self, settings: BaseSettings | None = None) -> None:
        self._settings = settings or BaseSettings()
        self.calls: list[tuple[str, str, int | None]] = []

    @property
    def settings(self) -> BaseSettings:
        return self._settings

    def _decision(self, policy: RateLimitPolicy) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, limit=policy.limit, remaining=0, retry_after=0.0, reset_after=0.0)

    def hit(self, storage_key: str, policy: RateLimitPolicy, cost: int) -> RateLimitDecision:
        self.calls.append(("hit", storage_key, cost))
        return self._decision(policy)

    async def ahit(self, storage_key: str, policy: RateLimitPolicy, cost: int) -> RateLimitDecision:
        self.calls.append(("ahit", storage_key, cost))
        return self._decision(policy)

    def reset(self, storage_key: str) -> None:
        self.calls.append(("reset", storage_key, None))

    async def areset(self, storage_key: str) -> None:
        self.calls.append(("areset", storage_key, None))

    def close(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


@pytest.fixture
def spy() -> _SpyBackend:
    return _SpyBackend()


@pytest.fixture
def service(spy: _SpyBackend) -> Iterator[RateLimitService]:
    svc = RateLimitService()
    svc._backend = spy
    yield svc
    svc.close()


@pytest.mark.parametrize("cost", [0, -1, True, 6, 1.5, "1"])
def test_out_of_range_cost_is_rejected_before_the_backend(
    service: RateLimitService, spy: _SpyBackend, cost: object
) -> None:
    with pytest.raises(InvalidRateLimitArgumentError, match="from 1 to 5"):
        service.hit(POLICY, "user", cost=cost)  # type: ignore[arg-type]
    assert spy.calls == []


def test_cost_equal_to_limit_is_accepted(service: RateLimitService, spy: _SpyBackend) -> None:
    service.hit(POLICY, "user", cost=5)
    assert spy.calls[0][2] == 5


@pytest.mark.parametrize("key", ["", None, 42])
def test_empty_or_non_string_key_is_rejected(service: RateLimitService, key: object) -> None:
    with pytest.raises(InvalidRateLimitArgumentError, match="non-empty string"):
        service.hit(POLICY, key)  # type: ignore[arg-type]
    with pytest.raises(InvalidRateLimitArgumentError):
        service.reset(POLICY, key)  # type: ignore[arg-type]


def test_same_name_with_different_parameters_conflicts(service: RateLimitService) -> None:
    service.hit(POLICY, "user")
    service.hit(RateLimitPolicy(name="login", limit=5, period=60), "user")

    with pytest.raises(RateLimitPolicyConflictError, match="'login'"):
        service.hit(RateLimitPolicy(name="login", limit=10, period=60), "user")


def test_storage_key_is_namespaced_hashed_and_stable(service: RateLimitService, spy: _SpyBackend) -> None:
    service.hit(POLICY, "alice@example.com")
    service.hit(POLICY, "alice@example.com")
    service.hit(POLICY, "bob@example.com")
    service.hit(RateLimitPolicy(name="api", limit=5, period=60), "alice@example.com")

    keys = [key for _, key, _ in spy.calls]
    assert all(re.fullmatch(r"(login|api):[0-9a-f]{32}", key) for key in keys)
    assert not any("alice" in key or "example" in key for key in keys)
    assert keys[0] == keys[1]
    assert len({keys[0], keys[2], keys[3]}) == 3


@pytest.mark.asyncio
async def test_async_methods_use_the_async_backend_side(service: RateLimitService, spy: _SpyBackend) -> None:
    await service.ahit(POLICY, "user", cost=2)
    await service.areset(POLICY, "user")
    service.hit(POLICY, "user")
    service.reset(POLICY, "user")

    assert [(method, cost) for method, _, cost in spy.calls] == [
        ("ahit", 2),
        ("areset", None),
        ("hit", 1),
        ("reset", None),
    ]
    assert len({key for _, key, _ in spy.calls}) == 1


def test_unknown_backend_lists_available_names() -> None:
    with pytest.raises(RateLimitBackendNotConfiguredError, match=r"Unknown rate limit backend 'nope'.*redis"):
        RateLimitService().configure("nope")


def test_unknown_backend_from_env_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "nope")
    with pytest.raises(RateLimitBackendNotConfiguredError, match="RATE_LIMIT_BACKEND"):
        RateLimitService().get_backend()


def test_invalid_arguments_are_rejected_before_configuring_a_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "nope")
    svc = RateLimitService()

    with pytest.raises(InvalidRateLimitArgumentError):
        svc.hit(POLICY, "user", cost=0)
    with pytest.raises(InvalidRateLimitArgumentError):
        svc.reset(POLICY, "")
    assert svc._backend is None


def test_redis_is_the_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    svc = RateLimitService()

    assert isinstance(svc.get_backend(), RedisRateLimitBackend)
    svc.close()


@pytest.mark.parametrize(
    ("env", "expected_db"),
    [({"RATE_LIMIT_REDIS_DB": "15"}, 15), ({"RATE_LIMIT_REDIS_URL": "redis://localhost:6379/3"}, 3)],
)
def test_redis_database_comes_from_db_setting_unless_the_url_names_one(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected_db: int
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    backend = RedisRateLimitBackend()

    assert backend._client.connection_pool.connection_kwargs["db"] == expected_db
    assert backend._async_client.connection_pool.connection_kwargs["db"] == expected_db
    backend.close()


def test_registered_backend_is_selected_by_name_or_env(monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"test-custom-{uuid.uuid4().hex}"
    register_backend(name, _SpyBackend)

    assert isinstance(RateLimitService().configure(name), _SpyBackend)
    monkeypatch.setenv("RATE_LIMIT_BACKEND", name)
    assert isinstance(RateLimitService().get_backend(), _SpyBackend)


def test_missing_redis_dependency_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "redis", None)
    with pytest.raises(RateLimitBackendNotConfiguredError, match=r"bedrock-core\[rate-limit-redis\]"):
        RateLimitService().configure("redis")


def test_importing_the_package_loads_no_backend_module() -> None:
    code = (
        "import sys, bedrock.contrib.rate_limit as r; "
        "print('bedrock.contrib.rate_limit.backends.redis' in sys.modules, r.RedisRateLimitBackend.__name__)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.split() == ["False", "RedisRateLimitBackend"]
