# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.config import reset_settings
from miloco.doorbell.conversation import DoorbellConversationService
from miloco.perception.types import Speech


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_DID", "door-did")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SIID", "7")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_EIID", "1006")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SESSION_KEY", "agent:doorman:doorbell")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_REPLY_AUDIO_COMMAND", '["/bin/echo", "{text}"]')
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_VISITOR_LISTEN_SECONDS", "15")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_MAX_TURNS", "3")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SILENCE_FALLBACK_ENABLED", "true")
    monkeypatch.setenv(
        "MILOCO_MIOT__DOORBELL_SILENCE_FALLBACK_TEXT",
        "我没有听到您的声音，请稍后再按门铃。",
    )
    reset_settings()
    yield
    reset_settings()


def _speech(
    content: str,
    *,
    did: str = "door-did",
    complete: bool = True,
    needs_response: bool = False,
) -> Speech:
    return Speech(
        needs_response=needs_response,
        speaker="访客",
        content=content,
        is_complete=complete,
        source_device_ids=[did],
    )


@pytest.mark.asyncio
async def test_playback_success_opens_listening_window(monkeypatch):
    service = DoorbellConversationService(clock=lambda: 100.0, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "你是谁？"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)

    conversation_id = await service.start(
        did="door-did",
        siid=7,
        eiid=1006,
        text="门铃被按下：智能门锁",
    )
    assert service.is_listening(conversation_id) is False

    assert service.on_reply_audio_result(conversation_id, success=True) is True

    assert service.is_listening(conversation_id) is True


@pytest.mark.asyncio
async def test_playback_success_requests_stream_hold(monkeypatch):
    now = 100.0
    keep_stream_alive = AsyncMock()
    service = DoorbellConversationService(
        clock=lambda: now,
        schedule_timeouts=False,
        keep_stream_alive=keep_stream_alive,
    )
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "你是谁？"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)

    conversation_id = await service.start(
        did="door-did",
        siid=7,
        eiid=1006,
        text="门铃被按下：智能门锁",
        speech_source_dids={"door-camera-did"},
    )
    assert service.on_reply_audio_result(conversation_id, success=True) is True
    await asyncio.sleep(0)

    calls = [(call.args[0], call.args[2]) for call in keep_stream_alive.call_args_list]
    assert calls == [
        ("door-camera-did", "reply_audio_success"),
        ("door-did", "reply_audio_success"),
    ]
    assert all(call.args[1] == 15.0 for call in keep_stream_alive.call_args_list)


@pytest.mark.asyncio
async def test_start_ignores_duplicate_active_conversation_for_same_did(monkeypatch):
    service = DoorbellConversationService(clock=lambda: 100.0, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "你是谁？"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)

    first_id = await service.start(did="door-did", siid=17, eiid=2, text="有人在门口")
    second_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")

    assert second_id == first_id
    assert run_turn.await_count == 1


@pytest.mark.asyncio
async def test_playback_failure_ends_and_ignores_speech(monkeypatch):
    service = DoorbellConversationService(clock=lambda: 100.0, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "你是谁？"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")

    assert service.on_reply_audio_result(conversation_id, success=False, error="audio failed") is True

    assert await service.accept_speech(_speech("我是快递员")) is False
    assert run_turn.await_count == 1


@pytest.mark.asyncio
async def test_matching_complete_speech_forwards_visitor_message(monkeypatch):
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)

    assert await service.accept_speech(_speech("我是快递员，我来取快递。")) is True


    assert run_turn.await_count == 2
    assert run_turn.await_args.args == ("门外访客说：我是快递员，我来取快递。",)
    assert run_turn.await_args.kwargs["session_key"] == "agent:doorman:doorbell"
    assert run_turn.await_args.kwargs["extra_payload"]["doorbellReplyAudio"]["conversationId"] == conversation_id


@pytest.mark.asyncio
async def test_agent_turn_injects_active_doorman_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "doorman-tasks"
    tasks_dir.mkdir()
    (tasks_dir / "active.json").write_text(
        json.dumps(
            {
                "task_id": "task-1",
                "device_id": "front_door_lock_001",
                "type": "package_delivery",
                "content": "顺丰快递要寄出，取件码 4396。",
                "created_at": "2026-08-01T20:56:27+08:00",
                "expires_at": "2099-08-01T23:59:59+08:00",
                "status": "active",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tasks_dir / "expired.json").write_text(
        json.dumps(
            {
                "task_id": "task-2",
                "type": "food_delivery",
                "content": "过期外卖任务",
                "expires_at": "2000-01-01T00:00:00+08:00",
                "status": "active",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MILOCO_DOORMAN_TASKS_DIR", str(tasks_dir))
    service = DoorbellConversationService(clock=lambda: 100.0, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)

    await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")

    prompt = run_turn.await_args.kwargs["extra_payload"]["extraSystemPrompt"]
    assert "当前有效门房任务" in prompt
    assert "顺丰快递要寄出，取件码 4396。" in prompt
    assert "过期外卖任务" not in prompt


@pytest.mark.asyncio
async def test_matching_complete_speech_accepts_related_camera_did(monkeypatch):
    service = DoorbellConversationService(clock=lambda: 100.0, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    conversation_id = await service.start(
        did="door-did",
        siid=7,
        eiid=1006,
        text="门铃被按下",
        speech_source_dids={"door-did", "door-camera-did"},
    )
    service.on_reply_audio_result(conversation_id, success=True)

    assert await service.accept_speech(_speech("我是快递员", did="door-camera-did")) is True

    assert run_turn.await_count == 2
    assert run_turn.await_args.args == ("门外访客说：我是快递员",)


@pytest.mark.asyncio
async def test_ignores_incomplete_wrong_device_duplicate_and_timeout(monkeypatch):
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)

    assert await service.accept_speech(_speech("没说完", complete=False)) is False
    assert await service.accept_speech(_speech("隔壁说话", did="other-did")) is False
    assert await service.accept_speech(_speech("我是快递员")) is True
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("我是快递员")) is False
    now = 116.0
    assert await service.accept_speech(_speech("超时了")) is False
    assert run_turn.await_count == 2


@pytest.mark.asyncio
async def test_max_turns_ends_after_configured_visitor_messages(monkeypatch):
    service = DoorbellConversationService(clock=lambda: 100.0, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "回复"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")

    for content in ["第一句", "第二句", "第三句"]:
        service.on_reply_audio_result(conversation_id, success=True)
        assert await service.accept_speech(_speech(content)) is True

    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("第四句")) is False
    assert run_turn.await_count == 4


@pytest.mark.asyncio
async def test_silence_timeout_plays_fixed_fallback_and_ends(monkeypatch):
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    play_audio = AsyncMock(return_value={"played": True})
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.call_agent_webhook", play_audio)
    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)

    now = 116.0
    assert await service.expire_listening_windows() == 1

    assert service.is_listening(conversation_id) is False
    assert run_turn.await_count == 1
    play_audio.assert_awaited_once()
    assert play_audio.await_args.args[0] == "doorbell_reply_audio"
    payload = play_audio.await_args.args[1]
    assert payload["text"] == "我没有听到您的声音，请稍后再按门铃。"
    assert payload["doorbellReplyAudio"] == {
        "did": "door-did",
        "siid": 7,
        "eiid": 1006,
        "wakeActionIid": "action.17.3",
        "audioCommand": ["/bin/echo", "{text}"],
    }


@pytest.mark.asyncio
async def test_silence_fallback_skips_when_speech_arrives(monkeypatch):
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    play_audio = AsyncMock(return_value={"played": True})
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.call_agent_webhook", play_audio)
    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("我是快递员")) is True

    now = 116.0
    assert await service.expire_listening_windows() == 0

    play_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_audio_activity_extends_listening_window(monkeypatch):
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    play_audio = AsyncMock(return_value={"played": True})
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.call_agent_webhook", play_audio)
    conversation_id = await service.start(
        did="door-did",
        siid=7,
        eiid=1006,
        text="门铃被按下",
        speech_source_dids={"door-did", "door-camera-did"},
    )
    service.on_reply_audio_result(conversation_id, success=True)

    now = 114.0
    assert service.observe_audio_activity(
        source_dids={"door-camera-did"},
        speech_probability=0.93,
        audio_energy=0.22,
        reason="test",
    ) == 1
    now = 116.0
    assert await service.expire_listening_windows() == 0

    assert service.is_listening(conversation_id) is True
    play_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_audio_activity_extension_requests_stream_hold(monkeypatch):
    now = 100.0
    keep_stream_alive = AsyncMock()
    service = DoorbellConversationService(
        clock=lambda: now,
        schedule_timeouts=False,
        keep_stream_alive=keep_stream_alive,
    )
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    conversation_id = await service.start(
        did="door-did",
        siid=7,
        eiid=1006,
        text="门铃被按下",
    )
    service.on_reply_audio_result(conversation_id, success=True)
    await asyncio.sleep(0)
    keep_stream_alive.reset_mock()
    now = 114.0

    assert service.observe_audio_activity(
        source_dids={"door-did"},
        speech_probability=0.93,
        audio_energy=0.22,
        reason="test",
    ) == 1
    await asyncio.sleep(0)

    keep_stream_alive.assert_awaited_once()
    assert keep_stream_alive.await_args.args[0] == "door-did"
    assert keep_stream_alive.await_args.args[2] == "audio_activity"


@pytest.mark.asyncio
async def test_debug_audio_records_all_speech_source_dids(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_DEBUG_AUDIO_ENABLED", "true")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_DEBUG_AUDIO_DIR", str(tmp_path))
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    recorder = MagicMock()
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.doorbell_debug_audio_recorder", recorder)

    conversation_id = await service.start(
        did="door-did",
        siid=7,
        eiid=1006,
        text="门铃被按下",
        speech_source_dids={"door-camera-did"},
    )
    service.on_reply_audio_result(conversation_id, success=True)
    now = 116.0
    assert await service.expire_listening_windows() == 1

    started_dids = {call.args[0] for call in recorder.start.call_args_list}
    stopped_dids = {call.args[0] for call in recorder.stop.call_args_list}
    assert started_dids == {"door-did", "door-camera-did"}
    assert stopped_dids == {"door-did", "door-camera-did"}


@pytest.mark.asyncio
async def test_expired_speech_candidate_stops_debug_audio(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_DEBUG_AUDIO_ENABLED", "true")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_DEBUG_AUDIO_DIR", str(tmp_path))
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    recorder = MagicMock()
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.doorbell_debug_audio_recorder", recorder)

    conversation_id = await service.start(
        did="door-did",
        siid=7,
        eiid=1006,
        text="门铃被按下",
        speech_source_dids={"door-camera-did"},
    )
    service.on_reply_audio_result(conversation_id, success=True)
    now = 116.0

    assert await service.accept_speech(_speech("我是快递员", did="door-camera-did")) is False

    stopped_dids = {call.args[0] for call in recorder.stop.call_args_list}
    assert stopped_dids == {"door-did", "door-camera-did"}


@pytest.mark.asyncio
async def test_silence_fallback_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_SILENCE_FALLBACK_ENABLED", "false")
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(return_value=("run-1", "ok", 100.0, "请说"))
    play_audio = AsyncMock(return_value={"played": True})
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.call_agent_webhook", play_audio)
    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)

    now = 116.0
    assert await service.expire_listening_windows() == 1

    play_audio.assert_not_awaited()
