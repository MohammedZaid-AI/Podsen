"""Test-wide safety net: no test may ever touch the real Supabase project.

rank.py and app.py load .env at import, which holds the real DATABASE_URL and
Supabase keys. Before every test this swaps them for fakes: DATABASE_URL is removed
(SQLite in a temp folder instead) and the Supabase URL points at a reserved .invalid
domain that can never resolve, so even a missing stub fails instead of calling out.
"""

import os

import pytest

# Set before any test module is imported: app.py and rank.py skip loading .env, so
# the real keys never enter the test process (or the subprocesses some tests start).
# Without this, importing app inside a test would re-fill a DATABASE_URL the fixture
# below had just removed, because load_dotenv fills in missing variables.
os.environ["PODSEN_SKIP_DOTENV"] = "1"

FAKE_SUPABASE_URL = "https://podsen-test.supabase.invalid"
FAKE_ANON_KEY = "anon-test-key-not-real"
# Distinctive on purpose: tests search every response and log for this string.
FAKE_SERVICE_ROLE_KEY = "service-role-TEST-SECRET-7f3a9c-never-show"


@pytest.fixture(autouse=True)
def _isolate_from_real_services(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SUPABASE_URL", FAKE_SUPABASE_URL)
    monkeypatch.setenv("SUPABASE_ANON_KEY", FAKE_ANON_KEY)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", FAKE_SERVICE_ROLE_KEY)
    # Background cache refreshes (v2/refresh.py) run inline in tests: deterministic, and
    # never a thread still writing to this test's temp database after it has ended.
    from v2 import refresh

    monkeypatch.setattr(refresh, "MODE", "inline")
    monkeypatch.setattr(refresh, "recorded", [])
