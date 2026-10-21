"""The model gate: one interface, recordable and replayable, so the agent and tests can run without model calls."""
from covenant_engine import model


class Fake:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, schema, model=None, timeout=0):
        self.calls += 1
        return {"ok": True, "output": {"echo": user}, "model": "fake", "cost_usd": 0, "seconds": 0, "error": None}


def test_record_then_replay(tmp_path):
    fake = Fake()
    model.set_backend(model.Recording(fake, tmp_path))
    r1 = model.call_model("sys", "hello", {"type": "object"}, model="m")
    model.set_backend(model.Replay(tmp_path))
    r2 = model.call_model("sys", "hello", {"type": "object"}, model="m")
    assert r1 == r2 and fake.calls == 1


def test_replay_miss_fails_loudly(tmp_path):
    model.set_backend(model.Replay(tmp_path))
    r = model.call_model("sys", "never recorded", {"type": "object"})
    assert r["ok"] is False and "no recorded response" in r["error"]
    model.set_backend(None)
