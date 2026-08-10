"""Shared fakes for the xostools-ng test suite.

The tools import their modules by bare name (`import xos_common`), so the
package root has to be importable before any test module is collected.
"""

import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xos_common  # noqa: E402


class FakeResponse:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, status_code: int = 200, payload=None, text: str = "",
                 malformed: bool = False):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.malformed = malformed
        self.content = b"body" if (payload is not None or text or malformed) else b""

    def json(self):
        if self.malformed or self._payload is None:
            raise ValueError("no JSON object could be decoded")
        return self._payload


class RecordedCall:
    def __init__(self, method: str, url: str, params, json_body, timeout):
        self.method = method
        self.url = url
        self.params = params
        self.json = json_body
        self.timeout = timeout


class FakeSession:
    """Records every request and answers from a handler callable."""

    def __init__(self, handler: Callable[[RecordedCall], FakeResponse],
                 registry: List["FakeSession"]):
        self.headers: Dict[str, str] = {}
        self.calls: List[RecordedCall] = []
        self._handler = handler
        registry.append(self)

    def request(self, method, url, params=None, json=None, timeout=None):
        call = RecordedCall(method, url, params, json, timeout)
        self.calls.append(call)
        return self._handler(call)


class FakeTransport:
    """Factory installed in place of `requests.Session`."""

    def __init__(self, handler: Callable[[RecordedCall], FakeResponse]):
        self.handler = handler
        self.sessions: List[FakeSession] = []

    def __call__(self) -> FakeSession:
        return FakeSession(self.handler, self.sessions)

    @property
    def calls(self) -> List[RecordedCall]:
        return [call for session in self.sessions for call in session.calls]


@pytest.fixture
def transport(monkeypatch):
    """Install a fake transport and let the test define the responses."""

    def install(handler: Callable[[RecordedCall], FakeResponse]) -> FakeTransport:
        fake = FakeTransport(handler)
        monkeypatch.setattr(xos_common.requests, "Session", fake)
        return fake

    return install


@pytest.fixture
def client_for(transport):
    """Build a client backed by a fake transport."""

    def build(handler, token: Optional[str] = "test-token", **kwargs):
        fake = transport(handler)
        client = xos_common.ForgejoClient(token=token, **kwargs)
        return client, fake

    return build
