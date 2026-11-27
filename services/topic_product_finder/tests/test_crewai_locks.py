"""CrewAI's internal locks must not use Redis: our Redis user may not run CLIENT SETNAME (ACL), which crashed every
crew before its first model call in production."""


def test_crewai_locks_use_a_local_file_not_redis(monkeypatch):
    from crewai_core import lock_store

    from product_finder import crews
    monkeypatch.setattr(lock_store, "_REDIS_URL", "redis://restricted-user@redis:6379/0")   # as in the cluster
    monkeypatch.setattr(lock_store, "_redis_available", lambda: (_ for _ in ()).throw(AssertionError("Redis used")))
    assert lock_store._backend is crews._file_lock
    with lock_store.lock("kickoff_task_outputs", timeout=5):
        pass                                                   # acquired + released on a /tmp file


def test_default_country_comes_from_config_not_us():
    from product_finder.params import params
    assert params().search.default_country == "FR"
