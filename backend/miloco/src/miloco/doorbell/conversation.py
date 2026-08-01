# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""State machine for door-lock doorbell two-way voice conversations."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from miloco.config import get_settings
from miloco.utils.agent_client import run_agent_turn_detailed

if TYPE_CHECKING:
    from miloco.perception.types import Speech

logger = logging.getLogger(__name__)

DOORBELL_INTERCOM_PROMPT = (
    "你正在通过智能门锁和门外访客对话。门铃响起时，请先简短询问对方身份和来意。"
    "后续每次收到“门外访客说：...”时，请继续用简短中文回复，适合直接转成音频播放给门外访客。"
    "不要输出 Markdown、列表或很长的解释。"
)


@dataclass
class _Conversation:
    conversation_id: str
    did: str
    siid: int
    eiid: int
    visitor_turns: int = 0
    state: str = "waiting_playback"
    listen_deadline: float = 0.0
    seen_speeches: set[str] = field(default_factory=set)


class DoorbellConversationService:
    """Coordinates OpenClaw replies, door-lock playback, and visitor speech turns."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._active: dict[str, _Conversation] = {}

    async def start(self, *, did: str, siid: int, eiid: int, text: str) -> str | None:
        settings = get_settings()
        miot = settings.miot
        if not miot.doorbell_conversation_enabled or not miot.doorbell_session_key:
            return None
        conversation_id = str(uuid.uuid4())
        conversation = _Conversation(
            conversation_id=conversation_id,
            did=did,
            siid=siid,
            eiid=eiid,
        )
        self._active[conversation_id] = conversation
        await self._run_turn(conversation, text)
        return conversation_id

    def is_listening(self, conversation_id: str) -> bool:
        conversation = self._active.get(conversation_id)
        return bool(
            conversation
            and conversation.state == "listening"
            and self._clock() <= conversation.listen_deadline
        )

    def on_reply_audio_result(
        self, conversation_id: str, *, success: bool, error: str | None = None
    ) -> bool:
        conversation = self._active.get(conversation_id)
        if conversation is None:
            logger.warning(
                "doorbell audio callback ignored: unknown conversation_id=%s",
                conversation_id,
            )
            return False
        if not success:
            conversation.state = "ended"
            self._active.pop(conversation_id, None)
            logger.warning(
                "doorbell conversation ended after playback failure id=%s did=%s error=%s",
                conversation_id,
                conversation.did,
                error,
            )
            return True
        if conversation.visitor_turns >= get_settings().miot.doorbell_max_turns:
            conversation.state = "ended"
            self._active.pop(conversation_id, None)
            logger.info(
                "doorbell conversation ended after max turns id=%s did=%s turns=%s",
                conversation_id,
                conversation.did,
                conversation.visitor_turns,
            )
            return True
        conversation.state = "listening"
        conversation.listen_deadline = (
            self._clock() + get_settings().miot.doorbell_visitor_listen_seconds
        )
        logger.info(
            "doorbell conversation listening id=%s did=%s deadline=%.3f",
            conversation_id,
            conversation.did,
            conversation.listen_deadline,
        )
        return True

    async def accept_speech(self, speech: Speech) -> bool:
        conversation = self._find_listening_conversation(speech)
        if conversation is None:
            return False
        content = speech.content.strip()
        if not speech.is_complete or not content:
            return False
        if content in conversation.seen_speeches:
            return False
        conversation.seen_speeches.add(content)
        conversation.visitor_turns += 1
        conversation.state = "waiting_playback"
        visitor_text = f"{get_settings().miot.doorbell_visitor_message_prefix}{content}"
        await self._run_turn(conversation, visitor_text)
        if conversation.visitor_turns >= get_settings().miot.doorbell_max_turns:
            logger.info(
                "doorbell conversation reached max visitor turns id=%s did=%s turns=%s",
                conversation.conversation_id,
                conversation.did,
                conversation.visitor_turns,
            )
        return True

    def _find_listening_conversation(self, speech: Speech) -> _Conversation | None:
        now = self._clock()
        source_dids = set(speech.source_device_ids or [])
        for conversation in list(self._active.values()):
            if conversation.state != "listening":
                continue
            if now > conversation.listen_deadline:
                conversation.state = "ended"
                self._active.pop(conversation.conversation_id, None)
                continue
            if conversation.did in source_dids:
                return conversation
        return None

    async def _run_turn(self, conversation: _Conversation, text: str) -> None:
        settings = get_settings()
        trace_id = str(uuid.uuid4())
        await run_agent_turn_detailed(
            text,
            session_key=settings.miot.doorbell_session_key or "",
            lane="miloco-interactive",
            trace_id=trace_id,
            wait_timeout_ms=settings.dispatcher.turn_wait_timeout_ms,
            extra_payload={
                "extraSystemPrompt": DOORBELL_INTERCOM_PROMPT,
                "doorbellReplyAudio": self._audio_payload(conversation),
            },
        )

    @staticmethod
    def _audio_payload(conversation: _Conversation) -> dict[str, object]:
        miot = get_settings().miot
        return {
            "conversationId": conversation.conversation_id,
            "did": conversation.did,
            "siid": conversation.siid,
            "eiid": conversation.eiid,
            "wakeActionIid": miot.doorbell_wake_action_iid,
            "audioCommand": miot.doorbell_reply_audio_command,
        }


_service = DoorbellConversationService()


def get_doorbell_conversation_service() -> DoorbellConversationService:
    return _service
