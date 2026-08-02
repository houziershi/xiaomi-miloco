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
    monkeypatch.setattr("miloco.doorbell.conversation.reset_agent_sessions", AsyncMock())
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
        "wakeBeforeAudio": False,
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


@pytest.mark.asyncio
async def test_conversation_end_pushes_summary_to_main_and_phone(monkeypatch):
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_OWNER_SUMMARY_ENABLED", "true")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_OWNER_SUMMARY_MAIN_SESSION_KEY", "agent:main:main")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_OWNER_SUMMARY_PHONE_PUSH_ENABLED", "true")
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(
        side_effect=[
            ("door-run-1", "ok", 100.0, "您好，请问您是哪位？"),
            ("door-run-2", "ok", 100.0, "您好，取件码是 4396，请您确认一下。"),
            ("main-run", "ok", 100.0, None),
            ("push-run", "ok", 100.0, None),
        ]
    )
    play_audio = AsyncMock(return_value={"played": True})
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.call_agent_webhook", play_audio)

    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("我是顺丰快递员，我来取快递。")) is True
    service.on_reply_audio_result(conversation_id, success=True)
    now = 191.0

    assert await service.expire_listening_windows() == 1

    assert run_turn.await_count == 4
    main_call = run_turn.await_args_list[2]
    push_call = run_turn.await_args_list[3]
    assert main_call.kwargs["session_key"] == "agent:main:main"
    assert main_call.kwargs["deliver"] is False
    assert "[司阍门口汇报]" in main_call.args[0]
    assert "我是顺丰快递员，我来取快递。" in main_call.args[0]
    assert "取件码是 4396" in main_call.args[0]
    assert push_call.kwargs["resolve_target"] == "owner-channel"
    assert push_call.kwargs["deliver"] is True
    assert push_call.args[0] == main_call.args[0]


@pytest.mark.asyncio
async def test_package_delivery_exception_summary_requests_owner_action(monkeypatch):
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_OWNER_SUMMARY_ENABLED", "true")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_OWNER_SUMMARY_MAIN_SESSION_KEY", "agent:main:main")
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(
        side_effect=[
            ("door-run-1", "ok", 100.0, "您好，请问您是哪位？"),
            ("door-run-2", "ok", 100.0, "您好，我不能代签。请说明快递公司和单号，我会记录给主人。"),
            ("main-run", "ok", 100.0, None),
        ]
    )
    send_notify = AsyncMock()
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr(
        "miloco.doorbell.conversation._miot_service",
        lambda: MagicMock(send_notify=send_notify),
    )

    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("我是顺丰快递，有贵重件需要签收。")) is True
    service.on_reply_audio_result(conversation_id, success=True)
    now = 116.0

    assert await service.expire_listening_windows() == 1
    summary = run_turn.await_args_list[2].args[0]
    assert "需要主人处理：是" in summary
    assert "快递异常" in summary
    assert "通过 Miloco 发送家庭场景手机通知" in summary
    send_notify.assert_awaited_once()
    assert "门口快递异常" in send_notify.await_args.args[0]


@pytest.mark.asyncio
async def test_food_delivery_exception_summary_requests_speaker_and_push(monkeypatch):
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_OWNER_SUMMARY_ENABLED", "true")
    monkeypatch.setenv("MILOCO_MIOT__DOORBELL_OWNER_SUMMARY_MAIN_SESSION_KEY", "agent:main:main")
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(
        side_effect=[
            ("door-run-1", "ok", 100.0, "您好，请问您是哪位？"),
            ("door-run-2", "ok", 100.0, "您好，请您先稍等一下，我马上给主人留言确认。"),
            ("main-run", "ok", 100.0, None),
        ]
    )
    send_notify = AsyncMock()
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr(
        "miloco.doorbell.conversation._miot_service",
        lambda: MagicMock(send_notify=send_notify),
    )

    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("我是外卖员，外卖破了。")) is True
    service.on_reply_audio_result(conversation_id, success=True)
    await asyncio.sleep(0)

    assert service.is_listening(conversation_id) is False
    submitted_text = run_turn.await_args_list[1].args[0]
    assert "本轮必须只对门外访客播报这句话" in submitted_text
    assert "请您先稍等一下" in submitted_text
    assert "不要向外卖员道歉" in submitted_text
    assert "不要追问订单信息" in submitted_text
    summary = run_turn.await_args_list[2].args[0]
    assert "需要主人处理：是" in summary
    assert "外卖异常" in summary
    assert "音箱直接播报一次" in summary
    assert "不要临时创建自动化" in summary
    assert "通过 Miloco 发送家庭场景手机通知" in summary
    send_notify.assert_awaited_once()
    assert "门口外卖异常" in send_notify.await_args.args[0]
    assert "外卖破了" in send_notify.await_args.args[0]


@pytest.mark.asyncio
async def test_food_delivery_context_treats_generic_problem_as_exception(monkeypatch):
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(
        side_effect=[
            ("run-1", "ok", 100.0, "您好，请问您是哪位？"),
            ("run-2", "ok", 100.0, "您好，外卖请挂在门把手上，谢谢。"),
            ("run-3", "ok", 100.0, "您好，请您先稍等一下，我马上给主人留言确认。"),
        ]
    )
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)

    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("我是外卖员")) is True
    first_food_text = run_turn.await_args_list[1].args[0]
    assert "门房系统已识别到外卖异常" not in first_food_text
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("这个餐有点问题")) is True

    problem_text = run_turn.await_args_list[2].args[0]
    assert "门房系统已识别到外卖异常" in problem_text
    assert "请您先稍等一下" in problem_text
    assert "不要追问订单信息" in problem_text


@pytest.mark.asyncio
async def test_agent_prompt_includes_current_conversation_state(monkeypatch):
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(
        side_effect=[
            ("run-1", "ok", 100.0, "您好，请问您是哪位？"),
            ("run-2", "ok", 100.0, "您好，取件码是 9527，请您确认一下。"),
            ("run-3", "ok", 100.0, "包裹在门口地垫旁边，谢谢。"),
        ]
    )
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)

    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("我是京东快递的过来取快递")) is True
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("好的，快递在哪里")) is True

    prompt = run_turn.await_args_list[2].kwargs["extra_payload"]["extraSystemPrompt"]
    assert "本次访客已说：我是京东快递的过来取快递；好的，快递在哪里" in prompt
    assert "本次司阍已回复：您好，请问您是哪位？；您好，取件码是 9527，请您确认一下。" in prompt
    assert "不要再次重复号码" in prompt
    assert "应回答包裹位置，不要说让对方放快递" in prompt
    assert "不要再说“门口指定位置”" in prompt


@pytest.mark.asyncio
async def test_package_pickup_with_matching_task_injects_code_instruction(monkeypatch, tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "pickup.json").write_text(
        json.dumps(
            {
                "task_id": "pickup-1",
                "type": "package_delivery",
                "content": "京东快递要寄出，取件码 9527。",
                "expires_at": "2099-01-01T23:59:59+08:00",
                "status": "active",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MILOCO_DOORMAN_TASKS_DIR", str(tasks_dir))
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(
        side_effect=[
            ("run-1", "ok", 100.0, "您好，请问您是哪位？"),
            ("run-2", "ok", 100.0, "您好，取件码是 9527，请您核对快递信息后取走，谢谢。"),
        ]
    )
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)

    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("我是京东快递的快递员过来取快递")) is True

    submitted_text = run_turn.await_args_list[1].args[0]
    assert "本轮必须只对门外访客播报这句话" in submitted_text
    assert "取件码是 9527" in submitted_text
    assert "禁止向访客询问、索要或要求确认取件码" in submitted_text


@pytest.mark.asyncio
async def test_closing_reply_ends_conversation(monkeypatch):
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    run_turn = AsyncMock(
        side_effect=[
            ("run-1", "ok", 100.0, "您好，请问您是哪位？"),
            ("run-2", "ok", 100.0, "不客气，慢走。"),
        ]
    )
    reset_sessions = AsyncMock()
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.reset_agent_sessions", reset_sessions)

    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("好的，多谢")) is True

    assert service.on_reply_audio_result(conversation_id, success=True) is True
    assert service.is_listening(conversation_id) is False
    assert await service.accept_speech(_speech("完整")) is False
    assert run_turn.await_count == 2
    await asyncio.sleep(0)
    reset_sessions.assert_awaited_once_with(
        [("agent:doorman:doorbell", "miloco-interactive")],
        delete_transcript=True,
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_closing_reply_ends_when_audio_callback_arrives_before_response(monkeypatch):
    reset_settings()
    now = 100.0
    service = DoorbellConversationService(clock=lambda: now, schedule_timeouts=False)
    conversation_id: str | None = None

    async def run_turn_side_effect(text: str, **_kwargs):
        if text.startswith("门外访客说："):
            assert conversation_id is not None
            assert service.on_reply_audio_result(conversation_id, success=True) is True
            assert service.is_listening(conversation_id) is True
            return "run-2", "ok", 100.0, "好的，再见，祝您顺利！"
        return "run-1", "ok", 100.0, "您好，请问您是哪位？"

    run_turn = AsyncMock(side_effect=run_turn_side_effect)
    reset_sessions = AsyncMock()
    monkeypatch.setattr("miloco.doorbell.conversation.run_agent_turn_detailed", run_turn)
    monkeypatch.setattr("miloco.doorbell.conversation.reset_agent_sessions", reset_sessions)

    conversation_id = await service.start(did="door-did", siid=7, eiid=1006, text="门铃被按下")
    service.on_reply_audio_result(conversation_id, success=True)
    assert await service.accept_speech(_speech("好的，谢谢")) is True

    assert service.is_listening(conversation_id) is False
    assert await service.accept_speech(_speech("不应继续接收")) is False
    assert run_turn.await_count == 2
    await asyncio.sleep(0)
    reset_sessions.assert_awaited_once_with(
        [("agent:doorman:doorbell", "miloco-interactive")],
        delete_transcript=True,
        timeout=10.0,
    )
