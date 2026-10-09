from hacknc.common.fingerprint import fingerprint
from hacknc.common.flags import DEFAULT_FLAGS, FlagReader


def test_fingerprint_is_order_independent() -> None:
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


def test_flag_defaults() -> None:
    assert DEFAULT_FLAGS["agent.medrec-agent"] is True
    assert DEFAULT_FLAGS["scenario.oversharing"] is False


def test_reader_merges_caches_and_survives_failures() -> None:
    calls = []

    def fetch():
        calls.append(1)
        return {"scenario.oversharing": True} if len(calls) == 1 else None

    reader = FlagReader(fetch, ttl=0)
    assert reader.on("scenario.oversharing") is True
    assert reader.on("agent.medrec-agent") is True
    assert reader.on("scenario.oversharing") is True  # store down: keep last known values

    calls.clear()
    cached = FlagReader(lambda: calls.append(1) or {}, ttl=60)
    cached.all(), cached.all()
    assert len(calls) == 1  # second read served from cache
