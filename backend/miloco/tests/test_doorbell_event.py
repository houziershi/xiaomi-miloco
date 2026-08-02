# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for forwarding configured MIoT doorbell events to OpenClaw."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    proxy._doorbell_subscription_retry_task = None
    proxy._lock_camera_tasks = {}
    proxy._lock_camera_hold_deadlines = {}
    proxy._doorbell_identity_last_triggered = {}
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
async def test_sync_doorbell_subscription_schedules_retry_on_failure():
    proxy = _bare_proxy()
    proxy._miot_client.sub_device_events_async.side_effect = RuntimeError("Not authorized")
    schedule_retry = MagicMock()
    proxy._schedule_doorbell_subscription_retry = schedule_retry

    await proxy._sync_doorbell_subscription()

    schedule_retry.assert_called_once_with(("door-did", 7, 1006))
    assert proxy._subscribed_doorbell_event is None


@pytest.mark.asyncio
async def test_keep_lock_camera_stream_alive_extends_existing_task(monkeypatch):
    proxy = _bare_proxy()
    proxy._lock_devices = {"door-did": {}}
    release = asyncio.Event()
    starts = 0

    async def fake_session(did: str) -> None:
        nonlocal starts
        starts += 1
        await release.wait()

    monkeypatch.setattr(proxy, "_lock_camera_recording_session", fake_session)

    await proxy.keep_lock_camera_stream_alive("door-did", 10.0, "doorbell_event")
    first_deadline = proxy._lock_camera_hold_deadlines["door-did"]
    await proxy.keep_lock_camera_stream_alive("door-did", 30.0, "reply_audio_success")
    second_deadline = proxy._lock_camera_hold_deadlines["door-did"]
    await asyncio.sleep(0)

    assert starts == 1
    assert second_deadline > first_deadline
    release.set()
    await proxy._lock_camera_tasks["door-did"]


def test_held_lock_camera_dids_returns_unexpired_and_cleans_expired(monkeypatch):
    proxy = _bare_proxy()
    monkeypatch.setattr(client_module.time, "monotonic", lambda: 100.0)
    proxy._lock_camera_hold_deadlines = {
        "expired-did": 99.0,
        "door-did": 101.0,
    }

    assert proxy.held_lock_camera_dids() == {"door-did"}
    assert proxy._lock_camera_hold_deadlines == {"door-did": 101.0}


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
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:doorman:doorbell")
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
        "wakeBeforeAudio": False,
        "wakeActionIid": "action.17.3",
        "audioCommand": ["/bin/echo", "{did}", "{text}"],
    }
    assert payload["doorbellReplyAudio"]["conversationId"]


@pytest.mark.asyncio
async def test_someone_at_door_event_does_not_start_conversation(monkeypatch):
    proxy = _bare_proxy()
    proxy._doorbell_listener = SimpleNamespace(on_event=AsyncMock())
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:doorman:doorbell")
    monkeypatch.setenv(
        "MILOCO_MIOT__DOORBELL_REPLY_AUDIO_COMMAND", '["/bin/echo", "{text}"]'
    )
    reset_settings()
    run_turn = AsyncMock(return_value=("run-1", "ok", 123.0, "请说"))
    monkeypatch.setattr(conversation_module, "run_agent_turn_detailed", run_turn)

    await proxy._on_device_event(MIoTDeviceEvent(did="door-did", siid=17, eiid=2))

    run_turn.assert_not_awaited()
    proxy._doorbell_listener.on_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_event_starts_proactive_doorman_conversation(monkeypatch):
    proxy = _bare_proxy()
    proxy._doorbell_listener = SimpleNamespace(on_event=AsyncMock())
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_IDENTITY_TRIGGER_ENABLED", "true")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:doorman:doorbell")
    monkeypatch.setenv(
        "MILOCO_MIOT__DOORBELL_REPLY_AUDIO_COMMAND",
        '["/bin/echo", "{text}"]',
    )
    reset_settings()
    run_turn = AsyncMock(return_value=("run-1", "ok", 123.0, "请说"))
    monkeypatch.setattr(conversation_module, "run_agent_turn_detailed", run_turn)

    await proxy._on_device_event(
        MIoTDeviceEvent(
            did="door-did",
            siid=17,
            eiid=2,
            raw={
                "method": "event_occured",
                "params": {
                    "did": "door-did",
                    "siid": 17,
                    "eiid": 2,
                    "arguments": [
                        {"piid": 8, "value": 1785651884},
                        {"piid": 31, "value": 3},
                    ],
                },
            },
        )
    )

    run_turn.assert_awaited_once()
    message = run_turn.await_args.args[0]
    assert "门锁识别到门外访客身份：京东快递员" in message
    assert "访客未按门铃" in message
    assert "您好，请问有什么需要帮忙的" in message
    assert run_turn.await_args.kwargs["session_key"] == "agent:doorman:doorbell"
    proxy._doorbell_listener.on_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_event_ignores_unknown_provider(monkeypatch):
    proxy = _bare_proxy()
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_IDENTITY_TRIGGER_ENABLED", "true")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:doorman:doorbell")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_REPLY_AUDIO_COMMAND", '["/bin/echo", "{text}"]')
    reset_settings()
    run_turn = AsyncMock(return_value=("run-1", "ok", 123.0, "请说"))
    monkeypatch.setattr(conversation_module, "run_agent_turn_detailed", run_turn)

    await proxy._on_device_event(
        MIoTDeviceEvent(
            did="door-did",
            siid=17,
            eiid=2,
            raw={"params": {"arguments": [{"piid": 31, "value": 0}]}},
        )
    )

    run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_event_deduplicates_with_cooldown(monkeypatch):
    proxy = _bare_proxy()
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_IDENTITY_TRIGGER_ENABLED", "true")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_IDENTITY_COOLDOWN_SECONDS", "120")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:doorman:doorbell")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_REPLY_AUDIO_COMMAND", '["/bin/echo", "{text}"]')
    reset_settings()
    now = 100.0
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now)
    run_turn = AsyncMock(return_value=("run-1", "ok", 123.0, "请说"))
    monkeypatch.setattr(conversation_module, "run_agent_turn_detailed", run_turn)
    event = MIoTDeviceEvent(
        did="door-did",
        siid=17,
        eiid=2,
        raw={"params": {"arguments": [{"piid": 31, "value": 4}]}},
    )

    await proxy._on_device_event(event)
    conversation_module.get_doorbell_conversation_service()._active.clear()
    now = 150.0
    await proxy._on_device_event(event)

    run_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_doorbell_device_event_does_not_enter_doorbell_listener():
    proxy = _bare_proxy()
    proxy._doorbell_listener = SimpleNamespace(on_event=AsyncMock())

    await proxy._on_device_event(MIoTDeviceEvent(did="door-did", siid=24, eiid=1))

    proxy._doorbell_listener.on_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_doorbell_conversation_accepts_same_named_lock_camera_source(monkeypatch):
    proxy = _bare_proxy()
    proxy._camera_info_dict = {
        "door-camera-did": SimpleNamespace(name="智能门锁 2"),
        "other-camera-did": SimpleNamespace(name="其他摄像机"),
    }
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:doorman:doorbell")
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
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:doorman:doorbell")
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
