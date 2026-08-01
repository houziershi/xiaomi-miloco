# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""State machine for door-lock doorbell two-way voice conversations."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from miloco.config import get_settings
from miloco.utils.agent_client import call_agent_webhook, run_agent_turn_detailed

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
    listen_generation: int = 0
    seen_speeches: set[str] = field(default_factory=set)


class DoorbellConversationService:
    """Coordinates OpenClaw replies, door-lock playback, and visitor speech turns."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        schedule_timeouts: bool = True,
    ) -> None:
        self._clock = clock or time.monotonic
        self._schedule_timeouts = schedule_timeouts
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
        conversation.listen_generation += 1
        conversation.listen_deadline = (
            self._clock() + get_settings().miot.doorbell_visitor_listen_seconds
        )
        logger.info(
            "doorbell conversation listening id=%s did=%s deadline=%.3f",
            conversation_id,
            conversation.did,
            conversation.listen_deadline,
        )
        self._schedule_listen_timeout(conversation)
        return True

    async def expire_listening_windows(self) -> int:
        expired = 0
        now = self._clock()
        for conversation in list(self._active.values()):
            if conversation.state != "listening" or now <= conversation.listen_deadline:
                continue
            expired += 1
            await self._end_after_silence(conversation)
        return expired

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
        conversation.listen_generation += 1
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

    def _schedule_listen_timeout(self, conversation: _Conversation) -> None:
        if not self._schedule_timeouts:
            return
        generation = conversation.listen_generation
        delay = max(0.0, conversation.listen_deadline - self._clock())

        async def _timeout() -> None:
            await asyncio.sleep(delay)
            current = self._active.get(conversation.conversation_id)
            if (
                current is None
                or current.state != "listening"
                or current.listen_generation != generation
                or self._clock() < current.listen_deadline
            ):
                return
            await self._end_after_silence(current)

        try:
            asyncio.create_task(_timeout())
        except RuntimeError:
            logger.debug(
                "doorbell silence timeout not scheduled outside running loop id=%s",
                conversation.conversation_id,
            )

    async def _end_after_silence(self, conversation: _Conversation) -> None:
        self._active.pop(conversation.conversation_id, None)
        conversation.state = "ended"
        conversation.listen_generation += 1
        logger.info(
            "doorbell conversation silence timeout id=%s did=%s",
            conversation.conversation_id,
            conversation.did,
        )
        miot = get_settings().miot
        if not miot.doorbell_silence_fallback_enabled:
            return
        text = miot.doorbell_silence_fallback_text.strip()
        if not text or not miot.doorbell_reply_audio_command:
            return
        try:
            await call_agent_webhook(
                "doorbell_reply_audio",
                {
                    "text": text,
                    "doorbellReplyAudio": self._audio_payload(
                        conversation, include_conversation_id=False
                    ),
                },
                timeout=30.0,
            )
        except Exception as e:
            logger.warning(
                "doorbell silence fallback audio failed id=%s did=%s error=%s",
                conversation.conversation_id,
                conversation.did,
                e,
            )

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
    def _audio_payload(
        conversation: _Conversation, *, include_conversation_id: bool = True
    ) -> dict[str, object]:
        miot = get_settings().miot
        payload: dict[str, object] = {
            "did": conversation.did,
            "siid": conversation.siid,
            "eiid": conversation.eiid,
            "wakeActionIid": miot.doorbell_wake_action_iid,
            "audioCommand": miot.doorbell_reply_audio_command,
        }
        if include_conversation_id:
            payload["conversationId"] = conversation.conversation_id
        return payload


_service = DoorbellConversationService()


def get_doorbell_conversation_service() -> DoorbellConversationService:
    return _service
