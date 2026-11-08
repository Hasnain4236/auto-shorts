import pytest
import requests

from pipeline import http


class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, payload: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        return self.responses.pop(0)


@pytest.fixture
def sleeps(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(http, "_sleep", waits.append)
    return waits


def test_retries_429_honouring_retry_after(monkeypatch, sleeps):
    fake = FakeSession([FakeResponse(429, {"retry-after": "5"}), FakeResponse(200, payload={"ok": True})])
    monkeypatch.setattr(http, "session", lambda: fake)
    assert http.get_json("https://en.wikipedia.org/w/api.php") == {"ok": True}
    assert sleeps == [5.0] and fake.calls == 2


def test_gives_up_after_max_retries(monkeypatch, sleeps):
    fake = FakeSession([FakeResponse(429)] * (http.MAX_RETRIES + 1))
    monkeypatch.setattr(http, "session", lambda: fake)
    with pytest.raises(requests.HTTPError):
        http.get_json("https://example.org")
    assert len(sleeps) == http.MAX_RETRIES and sleeps == sorted(sleeps)  # backoff grows


def test_other_errors_are_not_retried(monkeypatch, sleeps):
    fake = FakeSession([FakeResponse(403)])
    monkeypatch.setattr(http, "session", lambda: fake)
    with pytest.raises(requests.HTTPError):
        http.get_json("https://example.org")
    assert sleeps == [] and fake.calls == 1


def test_user_agent_identifies_the_bot():
    assert http.USER_AGENT.startswith("CrazyFindingsBot/") and "https://" in http.USER_AGENT
