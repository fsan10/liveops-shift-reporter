from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///var/test-liveops.sqlite3")
os.environ.setdefault("ARTIFACT_DIR", "var/test-artifacts")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from liveops.app import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def test_artifact_root() -> None:
    Path("var").mkdir(exist_ok=True)
