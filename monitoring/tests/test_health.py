"""Tests for ShopHealthStore: failure streaks across runs."""
import time

import pytest

from src.services.health import DEAD_AFTER_RUNS, HEALTH_KEY, ShopHealthStore


@pytest.fixture()
def store(tmp_path):
    return ShopHealthStore(db_path=tmp_path / "health.db")


def _fail(url, code="dns", detail="домен не резолвится"):
    return {"url": url, "failed": True, "code": code, "detail": detail}


def _ok(url):
    return {"url": url, "failed": False, "code": None, "detail": None}


# --- streaks -----------------------------------------------------------


def test_streak_increases_on_every_failure(store):
    for _ in range(3):
        store.record_run([_fail("a.example")], now=1000.0)
    assert store.streaks()["a.example"] == 3


def test_success_resets_the_streak(store):
    store.record_run([_fail("a.example")], now=1000.0)
    store.record_run([_fail("a.example")], now=1001.0)
    store.record_run([_ok("a.example")], now=1002.0)
    assert store.streaks()["a.example"] == 0
    assert store.dead() == []


def test_failure_after_success_starts_at_one(store):
    store.record_run([_ok("a.example")], now=1000.0)
    store.record_run([_fail("a.example")], now=1001.0)
    assert store.streaks()["a.example"] == 1


def test_dead_appears_only_at_the_threshold(store):
    for i in range(DEAD_AFTER_RUNS - 1):
        store.record_run([_fail("a.example")], now=1000.0 + i)
    assert store.dead() == []
    store.record_run([_fail("a.example")], now=9999.0)
    dead = store.dead()
    assert len(dead) == 1
    assert dead[0]["url"] == "a.example"
    assert dead[0]["fails_streak"] == DEAD_AFTER_RUNS
    assert dead[0]["last_detail"] == "домен не резолвится"
    assert dead[0]["last_code"] == "dns"


def test_last_ok_at_is_preserved_across_failures(store):
    store.record_run([_ok("a.example")], now=1000.0)
    store.record_run([_fail("a.example")], now=1001.0)
    dead = store.dead(threshold=1)
    assert dead[0]["last_ok_at"] == 1000.0
    assert dead[0]["last_fail_at"] == 1001.0


# --- persistence -------------------------------------------------------


def test_state_survives_a_new_store_instance(tmp_path):
    path = tmp_path / "health.db"
    first = ShopHealthStore(db_path=path)
    first.record_run([_fail("a.example")], now=1000.0)
    second = ShopHealthStore(db_path=path)
    assert second.streaks()["a.example"] == 1


def test_write_happens_once_per_run_not_once_per_site(tmp_path, monkeypatch):
    """A 306-site run must not turn into 306 SQLite transactions."""
    calls = {"n": 0}
    import src.services.health as health_mod

    original = health_mod.SqliteDict

    def counting(*args, **kwargs):
        if kwargs.get("autocommit"):
            calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(health_mod, "SqliteDict", counting)

    store = ShopHealthStore(db_path=tmp_path / "health.db")
    store.record_run([_fail(f"site-{i}.example") for i in range(306)], now=1000.0)

    assert calls["n"] == 1


# --- resilience --------------------------------------------------------


def test_missing_key_reads_as_empty(store):
    assert store.streaks() == {}
    assert store.dead() == []


def test_unreadable_store_degrades_to_empty(tmp_path, monkeypatch):
    import src.services.health as health_mod

    def boom(*args, **kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr(health_mod, "SqliteDict", boom)
    store = ShopHealthStore(db_path=tmp_path / "health.db")
    assert store.streaks() == {}
    store.record_run([_fail("a.example")])  # must not raise
    assert store.streaks() == {}


def test_items_without_url_are_ignored(store):
    store.record_run([{"failed": True, "code": "dns"}], now=1000.0)
    assert store.streaks() == {}