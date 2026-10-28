from __future__ import annotations

import importlib

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import liveops.service as service
from liveops.db import Base


@pytest.fixture
def api_client(monkeypatch):
    module = importlib.import_module("liveops.app")
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("APP_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(module, "init_db", lambda: None)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(service.RunDispatcher, "start_scheduler", lambda self: None)
    with TestClient(module.app) as client:
        yield client
    engine.dispose()


def test_three_switches_and_two_destinations_save_independently(api_client):
    before = api_client.get("/api/settings").json()["settings"]
    assert before["data_collection_enabled"] == "true"
    assert before["anchor_screenshot_enabled"] == "true"
    assert before["hourly_screenshot_enabled"] == "false"
    values = {"data_collection_enabled": "true", "anchor_screenshot_enabled": "false",
              "hourly_screenshot_enabled": "true", "feishu_chat_id": "anchor-destination",
              "feishu_hourly_chat_id": "business-destination"}
    assert api_client.put("/api/settings", json={"values": values}).status_code == 200
    after = api_client.get("/api/settings").json()["settings"]
    assert all(after[key] == value for key, value in values.items())
    page = api_client.get("/").text
    assert all(f'id="{key}"' in page for key in values)
    status = api_client.get("/api/automation/status").json()
    assert status["hourly_enabled"] and status["next_hourly_at"] is None


@pytest.mark.parametrize("values", [
    {"data_collection_enabled": "1"}, {"hourly_screenshot_enabled": "yes"},
    {"anchor_screenshot_enabled": ""}, {"hourly_message_template": "{gmv}"},
    {"hourly_message_template": "{hour_index.__class__}"}, {"hourly_message_template": "{unfinished"},
])
def test_invalid_switches_and_hourly_templates_are_rejected(api_client, values):
    assert api_client.put("/api/settings", json={"values": values}).status_code == 422


def test_test_messages_use_the_selected_group_and_reject_unknown_targets(api_client, monkeypatch):
    module = importlib.import_module("liveops.app")
    destinations = []

    class Bot:
        def __init__(self, app, secret, chat):
            destinations.append(chat)

        async def send_test_message(self):
            return {"message_id": "test-receipt", "sent": True}

        async def close(self):
            pass

    monkeypatch.setattr(module, "FeishuClient", Bot)
    api_client.put("/api/settings", json={"values": {
        "feishu_chat_id": "anchor-destination", "feishu_hourly_chat_id": "business-destination"}})
    assert api_client.post("/api/integrations/feishu/send-test?target=anchor").status_code == 200
    assert api_client.post("/api/integrations/feishu/send-test?target=hourly").status_code == 200
    assert destinations == ["anchor-destination", "business-destination"]
    assert api_client.post("/api/integrations/feishu/send-test?target=other").status_code == 422
    assert api_client.post("/api/runs/unknown/retry-screenshots").status_code == 404
