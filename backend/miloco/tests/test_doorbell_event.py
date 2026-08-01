# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for forwarding configured MIoT doorbell events to OpenClaw."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from miloco.config import reset_settings
from miloco.doorbell import conversation as conversation_module
from miloco.miot import client as client_module
from miloco.miot.client import MiotProxy
from miot.types import MIoTDeviceEvent


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_DID", "door-did")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SIID", "7")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_EIID", "1006")
    conversation_module.get_doorbell_conversation_service()._active.clear()
    reset_settings()
    yield
    conversation_module.get_doorbell_conversation_service()._active.clear()
    reset_settings()


def _bare_proxy() -> MiotProxy:
    proxy = MiotProxy.__new__(MiotProxy)
    proxy._miot_client = AsyncMock()
    proxy._miot_client.sub_legacy_device_event_async = AsyncMock()
    proxy._miot_client.sub_device_event_async = AsyncMock()
    proxy._subscribed_doorbell_event = None
    proxy._device_info_dict = {
        "door-did": SimpleNamespace(name="智能门锁", room_name="玄关")
    }
    proxy._camera_info_dict = {}
    return proxy


@pytest.mark.asyncio
async def test_sync_doorbell_subscription_uses_configured_event():
    proxy = _bare_proxy()

    await proxy._sync_doorbell_subscription()

    proxy._miot_client.sub_device_events_async.assert_awaited_once_with("door-did")
    proxy._miot_client.sub_device_event_async.assert_not_awaited()
    proxy._miot_client.sub_legacy_device_event_async.assert_not_awaited()
    assert proxy._subscribed_doorbell_event == ("door-did", 7, 1006)


@pytest.mark.asyncio
async def test_sync_doorbell_subscription_force_retries_existing_target():
    proxy = _bare_proxy()
    proxy._subscribed_doorbell_event = ("door-did", 7, 1006)

    await proxy._sync_doorbell_subscription(force=True)

    proxy._miot_client.sub_device_events_async.assert_awaited_once_with("door-did")
    assert proxy._subscribed_doorbell_event == ("door-did", 7, 1006)


@pytest.mark.asyncio
async def test_doorbell_event_dispatches_visible_owner_message(monkeypatch):
    proxy = _bare_proxy()
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(client_module, "dispatch_event", dispatch)

    await proxy._on_device_event(
        MIoTDeviceEvent(
            did="door-did",
            siid=7,
            eiid=1006,
            raw={
                "method": "event_occured",
                "params": {
                    "did": "door-did",
                    "arguments": [{"piid": 1, "value": 1785559087}],
                },
            },
        )
    )

    dispatch.assert_awaited_once()
    event_type, items, builder = dispatch.await_args.args
    assert event_type == "device_event"
    assert items == ["门铃被按下：智能门锁（玄关）。事件值：1785559087"]
    assert builder(items) == items[0]


@pytest.mark.asyncio
async def test_doorbell_event_requests_openclaw_reply_audio(monkeypatch):
    proxy = _bare_proxy()
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:main:door")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_WAKE_ACTION_IID", "action.17.3")
    monkeypatch.setenv(
        "MILOCO_MIOT__DOORBELL_REPLY_AUDIO_COMMAND",
        '["/bin/echo", "{did}", "{text}"]',
    )
    reset_settings()

    run_turn = AsyncMock(return_value=("run-1", "ok", 123.0, "请说"))
    monkeypatch.setattr(conversation_module, "run_agent_turn_detailed", run_turn)

    await proxy._on_device_event(MIoTDeviceEvent(did="door-did", siid=7, eiid=1006))

    proxy._miot_client.http_client.action_async.assert_not_awaited()
    run_turn.assert_awaited_once()
    payload = run_turn.await_args.kwargs["extra_payload"]
    assert "你正在通过智能门锁和门外访客对话" in payload["extraSystemPrompt"]
    assert payload["doorbellReplyAudio"] == {
        "conversationId": payload["doorbellReplyAudio"]["conversationId"],
        "did": "door-did",
        "siid": 7,
        "eiid": 1006,
        "wakeActionIid": "action.17.3",
        "audioCommand": ["/bin/echo", "{did}", "{text}"],
    }
    assert payload["doorbellReplyAudio"]["conversationId"]


@pytest.mark.asyncio
async def test_doorbell_conversation_accepts_same_named_lock_camera_source(monkeypatch):
    proxy = _bare_proxy()
    proxy._camera_info_dict = {
        "door-camera-did": SimpleNamespace(name="智能门锁 2"),
        "other-camera-did": SimpleNamespace(name="其他摄像机"),
    }
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:main:door")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_REPLY_AUDIO_COMMAND", '["/bin/echo", "{text}"]')
    reset_settings()

    run_turn = AsyncMock(return_value=("run-1", "ok", 123.0, "请说"))
    monkeypatch.setattr(conversation_module, "run_agent_turn_detailed", run_turn)

    await proxy._on_device_event(MIoTDeviceEvent(did="door-did", siid=7, eiid=1006))

    conversation = next(iter(conversation_module.get_doorbell_conversation_service()._active.values()))
    assert conversation.speech_source_dids == {"door-did", "door-camera-did"}


@pytest.mark.asyncio
async def test_doorbell_conversation_accepts_configured_speech_source_dids(monkeypatch):
    proxy = _bare_proxy()
    proxy._camera_info_dict = {}
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:main:door")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_REPLY_AUDIO_COMMAND", '["/bin/echo", "{text}"]')
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SPEECH_SOURCE_DIDS", '["door-camera-did"]')
    reset_settings()

    run_turn = AsyncMock(return_value=("run-1", "ok", 123.0, "请说"))
    monkeypatch.setattr(conversation_module, "run_agent_turn_detailed", run_turn)

    await proxy._on_device_event(MIoTDeviceEvent(did="door-did", siid=7, eiid=1006))

    conversation = next(iter(conversation_module.get_doorbell_conversation_service()._active.values()))
    assert conversation.speech_source_dids == {"door-did", "door-camera-did"}


@pytest.mark.asyncio
async def test_non_matching_device_event_is_ignored(monkeypatch):
    proxy = _bare_proxy()
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(client_module, "dispatch_event", dispatch)

    await proxy._on_device_event(MIoTDeviceEvent(did="door-did", siid=7, eiid=9999))

    dispatch.assert_not_awaited()
