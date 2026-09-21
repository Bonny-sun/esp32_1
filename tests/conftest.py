"""Shared test setup.

`worker/ingest.py` and `analysis/baseline.py` are scripts that read env vars
and build a Supabase client at import time. Rather than change production
code, the tests provide dummy env vars + a stub `create_client` *before*
importing them, then swap `module.sb` for a scripted FakeClient per test.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "worker"), str(ROOT / "analysis")]

for key, val in {
    "MQTT_HOST": "localhost",
    "MQTT_USER": "u",
    "MQTT_PASS": "p",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "test-key",
}.items():
    os.environ.setdefault(key, val)
os.environ.pop("LINE_CHANNEL_TOKEN", None)
os.environ.pop("DEVICE_ID", None)

import supabase  # noqa: E402

supabase.create_client = lambda *_a, **_k: None  # modules do `from supabase import create_client`


class FakeQuery:
    """Chainable stand-in for a supabase-py query builder. Every call is
    recorded in `.ops`; `.execute()` asks the client's handler for the data."""

    def __init__(self, client, table):
        self.client, self.table, self.ops = client, table, []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self

        return call

    def op(self, name):
        return next((a, k) for n, a, k in self.ops if n == name)

    def has(self, name):
        return any(n == name for n, _a, _k in self.ops)

    def execute(self):
        self.client.queries.append(self)
        return SimpleNamespace(data=self.client.handler(self))


class FakeClient:
    def __init__(self, handler=lambda q: []):
        self.handler, self.queries = handler, []

    def table(self, name):
        return FakeQuery(self, name)


@pytest.fixture
def fake_client():
    return FakeClient
