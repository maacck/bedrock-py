"""Tests for typed cache namespaces and coder behavior."""

from __future__ import annotations

import asyncio

from bedrock.contrib.cache.coder import BytesCoder, Coder
from bedrock.contrib.cache.service import CacheService
from pydantic import BaseModel


class ExampleEntity(BaseModel):
    """Small entity used to validate typed cache reads."""

    id: int
    payload: dict[str, int]


class TestCoder:
    """Tests for the default cache coder."""

    def test_decode_as_type_supports_scalars_and_models(self) -> None:
        assert Coder.decode_as_type(b"1", type_=int) == 1
        assert Coder.decode_as_type(b'{"count":2}', type_=dict[str, int]) == {"count": 2}
        assert Coder.decode_as_type(
            b'{"id":3,"payload":{"count":4}}',
            type_=ExampleEntity,
        ) == ExampleEntity(id=3, payload={"count": 4})


class TestCacheNamespace:
    """Tests for typed cache namespaces and slots."""

    def test_slot_reuses_namespace_prefix_and_default_ttl(self) -> None:
        cache = CacheService()
        slot = cache.namespace("idp:application").slot(
            "entity:{app_id}",
            value_type=ExampleEntity,
            ttl=123,
        )

        entity = ExampleEntity(id=1, payload={"count": 7})
        slot.set(entity, app_id="demo")

        stored, ttl = cache.get_with_ttl("idp:application:entity:demo", type_=ExampleEntity)

        assert stored == entity
        assert ttl is not None
        assert 0 < ttl <= 123
        assert slot.get(app_id="demo") == entity

    def test_slot_validates_parameters(self) -> None:
        cache = CacheService()
        slot = cache.namespace("idp:application").slot("entity:{app_id}")

        try:
            slot.build_key()
        except ValueError as exc:
            assert str(exc) == "Missing cache key parameters: app_id."
        else:
            raise AssertionError("Expected missing cache key parameters to raise.")

        try:
            slot.build_key(app_id="demo", extra="oops")
        except ValueError as exc:
            assert str(exc) == "Unexpected cache key parameters: extra."
        else:
            raise AssertionError("Expected unexpected cache key parameters to raise.")

    def test_slot_can_use_custom_bytes_coder(self) -> None:
        cache = CacheService()
        slot = cache.namespace("tokens").slot(
            "raw:{token_id}",
            coder=BytesCoder,
        )

        slot.set(b"opaque-token", token_id="abc")

        assert slot.get(token_id="abc") == b"opaque-token"

    def test_slot_get_or_load_populates_on_miss(self) -> None:
        cache = CacheService()
        slot = cache.namespace("idp:application").slot(
            "entity:{app_id}",
            value_type=ExampleEntity,
            ttl=123,
        )
        entity = ExampleEntity(id=1, payload={"count": 7})
        calls = {"count": 0}

        def loader() -> ExampleEntity:
            calls["count"] += 1
            return entity

        loaded = slot.get_or_load(loader, app_id="demo")
        again = slot.get_or_load(loader, app_id="demo")

        assert loaded == entity
        assert again == entity
        assert calls["count"] == 1
        assert slot.get(app_id="demo") == entity

    def test_slot_get_or_load_keeps_cached_falsy_values(self) -> None:
        cache = CacheService()
        slot = cache.namespace("counters").slot("hits:{user_id}", value_type=int)
        slot.set(0, user_id="demo")
        calls = {"count": 0}

        def loader() -> int:
            calls["count"] += 1
            return 7

        assert slot.get_or_load(loader, user_id="demo") == 0
        assert calls["count"] == 0

    def test_slot_aget_or_load_supports_async_loader(self) -> None:
        cache = CacheService()
        slot = cache.namespace("idp:application").slot(
            "entity:{app_id}",
            value_type=ExampleEntity,
        )
        entity = ExampleEntity(id=2, payload={"count": 9})
        calls = {"count": 0}

        async def loader() -> ExampleEntity:
            calls["count"] += 1
            return entity

        async def scenario() -> None:
            loaded = await slot.aget_or_load(loader, app_id="demo")
            again = await slot.aget_or_load(loader, app_id="demo")
            assert loaded == entity
            assert again == entity
            assert calls["count"] == 1

        asyncio.run(scenario())
