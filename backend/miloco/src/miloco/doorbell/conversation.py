# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""State machine for door-lock doorbell two-way voice conversations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from miloco.config import get_settings
from miloco.doorbell.debug_audio import doorbell_debug_audio_recorder
from miloco.utils.agent_client import (
    call_agent_webhook,
    reset_agent_sessions,
    run_agent_turn_detailed,
)
from miloco.utils.paths import miloco_home

if TYPE_CHECKING:
    from miloco.perception.types import Speech

logger = logging.getLogger(__name__)

TZ_SHANGHAI = timezone(timedelta(hours=8))

DOORBELL_INTERCOM_PROMPT = (
    "你正在通过智能门锁和门外访客对话。门铃响起时，首轮必须先主动询问：你好，你是谁？有什么事情吗？"
    "后续每次收到“门外访客说：...”时，请继续用简短中文回复，适合直接转成音频播放给门外访客。"
    "最终回复只能是要通过门锁播报给访客的话。不要描述你的处理过程，不要说“已回复/已询问/已通知/正在处理”，"
    "不要总结访客说了什么，不要向主人汇报门铃状态，不要输出思考过程、Markdown、列表或很长的解释。"
)

DOORBELL_SESSION_STATE_PROMPT = (
    "\n\n当前门铃会话状态：\n"
    "- 下面的访客/司阍摘要只代表本次门铃会话，不要把更早历史当成本次对话。\n"
    "- 如果本次已经向同一快递员转达过取件码或寄件码，访客只说“好/好的/对/对的”时，不要再次重复号码。\n"
    "- 如果已确认是取件/寄件取件场景，访客问“快递在哪里/包裹在哪里”时，应回答包裹位置，不要说让对方放快递。\n"
    "- 默认包裹位置用“门口地垫旁边”；不要再说“门口指定位置”。\n"
)

_PACKAGE_CARRIERS = [
    "顺丰",
    "京东",
    "中通",
    "圆通",
    "申通",
    "韵达",
    "极兔",
    "邮政",
    "EMS",
    "德邦",
    "菜鸟",
]
_PICKUP_WORDS = ["取快递", "取件", "拿快递", "拿件", "寄件", "退货"]
_CODE_RE = re.compile(r"(?:取件码|寄件码)\s*[:：是]?\s*([A-Za-z0-9-]{3,})")


def _default_doorman_tasks_dirs() -> list[Path]:
    configured = os.environ.get("MILOCO_DOORMAN_TASKS_DIR")
    if configured:
        return [Path(configured).expanduser()]
    home = Path.home()
    return [
        home / ".openclaw" / "workspace" / "doorman" / "data" / "tasks",
        home / ".openclaw" / "workspace" / "agents" / "doorman" / "data" / "tasks",
    ]


def _parse_task_expiry(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_SHANGHAI)
    return parsed


def _load_active_doorman_tasks(now: datetime | None = None) -> list[dict[str, object]]:
    current = now or datetime.now(TZ_SHANGHAI)
    tasks: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for tasks_dir in _default_doorman_tasks_dirs():
        if not tasks_dir.is_dir():
            continue
        for task_file in sorted(tasks_dir.glob("*.json")):
            try:
                task = json.loads(task_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("doorman task ignored: unreadable file=%s", task_file)
                continue
            if not isinstance(task, dict) or task.get("status") != "active":
                continue
            expires_at = _parse_task_expiry(task.get("expires_at"))
            if expires_at is not None and current > expires_at:
                continue
            task_id = str(task.get("task_id") or task_file)
            if task_id in seen_ids:
                continue
            seen_ids.add(task_id)
            tasks.append(task)
    tasks.sort(key=lambda task: str(task.get("created_at", "")), reverse=True)
    return tasks


def _format_doorman_tasks_prompt(tasks: list[dict[str, object]]) -> str:
    if not tasks:
        return ""
    lines = ["\n\n当前有效门房任务："]
    for index, task in enumerate(tasks[:5], start=1):
        task_type = str(task.get("type") or "general_instruction")
        content = str(task.get("content") or "").strip()
        expires_at = str(task.get("expires_at") or "").strip()
        if not content:
            continue
        suffix = f"；有效期至 {expires_at}" if expires_at else ""
        lines.append(f"{index}. [{task_type}] {content}{suffix}")
    if len(lines) == 1:
        return ""
    lines.append("只能在访客来意匹配时转达相关任务内容；不要透露未匹配任务或家庭隐私。")
    return "\n".join(lines)


def _format_conversation_state_prompt(conversation: _Conversation) -> str:
    visitor = "；".join(conversation.visitor_messages[-5:]) or "暂无"
    assistant = "；".join(conversation.assistant_messages[-5:]) or "暂无"
    return (
        DOORBELL_SESSION_STATE_PROMPT
        + f"- 本次访客已说：{visitor}\n"
        + f"- 本次司阍已回复：{assistant}\n"
    )


def _matched_package_pickup_reply(visitor_text: str, tasks: list[dict[str, object]]) -> str | None:
    if not any(word in visitor_text for word in _PICKUP_WORDS):
        return None
    visitor_carrier = _matched_carrier(visitor_text)
    if not visitor_carrier:
        return None
    for task in tasks:
        content = str(task.get("content") or "")
        if visitor_carrier not in content:
            continue
        if not any(word in content for word in ["寄出", "取件", "寄件", "退货"]):
            continue
        code_match = _CODE_RE.search(content)
        if not code_match:
            continue
        code = code_match.group(1)
        return (
            f"您好，取件码是 {code}，包裹在门口地垫旁边，"
            "请您核对快递信息后取走，谢谢。"
        )
    return None


def _matched_carrier(text: str) -> str | None:
    upper_text = text.upper()
    for carrier in _PACKAGE_CARRIERS:
        if carrier.upper() in upper_text:
            return carrier
    return None


def _with_deterministic_doorman_instruction(text: str, tasks: list[dict[str, object]]) -> str:
    reply = _matched_package_pickup_reply(text, tasks)
    if not reply:
        return text
    return (
        f"{text}\n\n"
        "门房系统已匹配到主人同步的上门取件/寄件任务。"
        f"本轮必须只对门外访客播报这句话：{reply}"
        "禁止向访客询问、索要或要求确认取件码/寄件码。"
    )


@dataclass
class _Conversation:
    conversation_id: str
    did: str
    siid: int
    eiid: int
    speech_source_dids: set[str]
    visitor_turns: int = 0
    state: str = "waiting_playback"
    listen_deadline: float = 0.0
    listen_generation: int = 0
    seen_speeches: set[str] = field(default_factory=set)
    started_at: datetime = field(default_factory=lambda: datetime.now(TZ_SHANGHAI))
    visitor_messages: list[str] = field(default_factory=list)
    assistant_messages: list[str] = field(default_factory=list)
    owner_summary_sent: bool = False


class DoorbellConversationService:
    """Coordinates OpenClaw replies, door-lock playback, and visitor speech turns."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        schedule_timeouts: bool = True,
        keep_stream_alive: Callable[[str, float, str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._schedule_timeouts = schedule_timeouts
        self._active: dict[str, _Conversation] = {}
        self._keep_stream_alive = keep_stream_alive

    def set_keep_stream_alive(
        self, callback: Callable[[str, float, str], Awaitable[None] | None] | None
    ) -> None:
        self._keep_stream_alive = callback

    async def start(
        self,
        *,
        did: str,
        siid: int,
        eiid: int,
        text: str,
        speech_source_dids: set[str] | None = None,
    ) -> str | None:
        settings = get_settings()
        miot = settings.miot
        if not miot.doorbell_conversation_enabled or not miot.doorbell_session_key:
            logger.info(
                "doorbell conversation skipped did=%s enabled=%s session_key_configured=%s",
                did,
                miot.doorbell_conversation_enabled,
                bool(miot.doorbell_session_key),
            )
            return None
        existing = self._find_active_conversation_for_did(did)
        if existing is not None:
            logger.warning(
                "doorbell conversation start ignored: active conversation exists did=%s existing_id=%s state=%s turns=%s siid=%s eiid=%s text=%s",
                did,
                existing.conversation_id,
                existing.state,
                existing.visitor_turns,
                siid,
                eiid,
                text,
            )
            return existing.conversation_id
        conversation_id = str(uuid.uuid4())
        conversation = _Conversation(
            conversation_id=conversation_id,
            did=did,
            siid=siid,
            eiid=eiid,
            speech_source_dids=set(speech_source_dids or {did}) | {did},
        )
        self._active[conversation_id] = conversation
        self._start_debug_audio_recording(conversation)
        logger.info(
            "doorbell conversation started id=%s did=%s siid=%s eiid=%s speech_source_dids=%s text=%s",
            conversation_id,
            did,
            siid,
            eiid,
            sorted(conversation.speech_source_dids),
            text,
        )
        await self._run_turn(conversation, text)
        return conversation_id

    def is_listening(self, conversation_id: str) -> bool:
        conversation = self._active.get(conversation_id)
        return bool(
            conversation
            and conversation.state == "listening"
            and self._clock() <= conversation.listen_deadline
        )

    def _find_active_conversation_for_did(self, did: str) -> _Conversation | None:
        for conversation in self._active.values():
            if conversation.did == did and conversation.state != "ended":
                return conversation
        return None

    def on_reply_audio_result(
        self, conversation_id: str, *, success: bool, error: str | None = None
    ) -> bool:
        conversation = self._active.get(conversation_id)
        if conversation is None:
            logger.warning(
                "doorbell audio callback ignored: unknown conversation_id=%s success=%s error=%s active=%s",
                conversation_id,
                success,
                error,
                self._active_summary(),
            )
            return False
        logger.info(
            "doorbell audio callback received id=%s did=%s success=%s error=%s state=%s turns=%s",
            conversation_id,
            conversation.did,
            success,
            error,
            conversation.state,
            conversation.visitor_turns,
        )
        if not success:
            self._stop_debug_audio_recording(conversation, reason="playback_failed")
            conversation.state = "ended"
            self._active.pop(conversation_id, None)
            self._schedule_owner_summary(conversation, reason="playback_failed")
            self._schedule_doorman_session_reset(conversation, reason="playback_failed")
            logger.warning(
                "doorbell conversation ended after playback failure id=%s did=%s error=%s",
                conversation_id,
                conversation.did,
                error,
            )
            return True
        if self._end_after_closing_reply(conversation, source="audio_callback"):
            return True
        if conversation.visitor_turns >= get_settings().miot.doorbell_max_turns:
            self._stop_debug_audio_recording(conversation, reason="max_turns")
            conversation.state = "ended"
            self._active.pop(conversation_id, None)
            self._schedule_owner_summary(conversation, reason="max_turns")
            self._schedule_doorman_session_reset(conversation, reason="max_turns")
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
            "doorbell conversation listening id=%s did=%s generation=%s listen_seconds=%.3f deadline=%.3f speech_source_dids=%s",
            conversation_id,
            conversation.did,
            conversation.listen_generation,
            get_settings().miot.doorbell_visitor_listen_seconds,
            conversation.listen_deadline,
            sorted(conversation.speech_source_dids),
        )
        self._schedule_listen_timeout(conversation)
        self._request_stream_hold(conversation, reason="reply_audio_success")
        return True

    async def expire_listening_windows(self) -> int:
        expired = 0
        now = self._clock()
        for conversation in list(self._active.values()):
            if conversation.state != "listening" or now <= conversation.listen_deadline:
                continue
            expired += 1
            logger.info(
                "doorbell conversation expire scan matched id=%s did=%s now=%.3f deadline=%.3f",
                conversation.conversation_id,
                conversation.did,
                now,
                conversation.listen_deadline,
            )
            await self._end_after_silence(conversation)
        return expired

    def observe_audio_activity(
        self,
        *,
        source_dids: set[str],
        speech_probability: float,
        audio_energy: float,
        reason: str,
    ) -> int:
        settings = get_settings().miot
        extend_seconds = settings.doorbell_audio_activity_extend_seconds
        if extend_seconds <= 0:
            return 0
        if (
            speech_probability < settings.doorbell_audio_activity_min_speech_probability
            and audio_energy < settings.doorbell_audio_activity_min_energy
        ):
            return 0
        now = self._clock()
        extended = 0
        for conversation in list(self._active.values()):
            if conversation.state != "listening":
                continue
            if not (conversation.speech_source_dids & source_dids):
                continue
            new_deadline = max(conversation.listen_deadline, now + extend_seconds)
            if new_deadline <= conversation.listen_deadline:
                logger.info(
                    "doorbell audio activity observed without extension id=%s did=%s source_dids=%s speech_probability=%.3f audio_energy=%.3f deadline=%.3f reason=%s",
                    conversation.conversation_id,
                    conversation.did,
                    sorted(source_dids),
                    speech_probability,
                    audio_energy,
                    conversation.listen_deadline,
                    reason,
                )
                continue
            conversation.listen_deadline = new_deadline
            conversation.listen_generation += 1
            extended += 1
            logger.info(
                "doorbell audio activity extended listening id=%s did=%s source_dids=%s speech_probability=%.3f audio_energy=%.3f generation=%s deadline=%.3f reason=%s",
                conversation.conversation_id,
                conversation.did,
                sorted(source_dids),
                speech_probability,
                audio_energy,
                conversation.listen_generation,
                conversation.listen_deadline,
                reason,
            )
            self._schedule_listen_timeout(conversation)
            self._request_stream_hold(conversation, reason="audio_activity")
        return extended

    async def accept_speech(self, speech: Speech) -> bool:
        conversation = self._find_listening_conversation(speech)
        if conversation is None:
            active = self._active_summary()
            log = logger.info if active else logger.debug
            log(
                "doorbell speech ignored: no listening conversation content=%s source_dids=%s complete=%s active=%s",
                speech.content,
                speech.source_device_ids,
                speech.is_complete,
                active,
            )
            return False
        content = speech.content.strip()
        if not speech.is_complete or not content:
            logger.info(
                "doorbell speech ignored: incomplete or empty id=%s content=%s complete=%s",
                conversation.conversation_id,
                speech.content,
                speech.is_complete,
            )
            return False
        if content in conversation.seen_speeches:
            logger.info(
                "doorbell speech ignored: duplicate id=%s content=%s",
                conversation.conversation_id,
                content,
            )
            return False
        conversation.seen_speeches.add(content)
        conversation.visitor_messages.append(content)
        conversation.visitor_turns += 1
        conversation.state = "waiting_playback"
        conversation.listen_generation += 1
        logger.info(
            "doorbell speech accepted id=%s did=%s source_dids=%s turns=%s content=%s",
            conversation.conversation_id,
            conversation.did,
            speech.source_device_ids,
            conversation.visitor_turns,
            content,
        )
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
                logger.info(
                    "doorbell speech candidate skipped: conversation not listening id=%s did=%s state=%s actual_source_dids=%s",
                    conversation.conversation_id,
                    conversation.did,
                    conversation.state,
                    sorted(source_dids),
                )
                continue
            if now > conversation.listen_deadline:
                logger.info(
                    "doorbell speech ignored: listening expired id=%s did=%s source_dids=%s deadline=%.3f now=%.3f",
                    conversation.conversation_id,
                    conversation.did,
                    sorted(source_dids),
                    conversation.listen_deadline,
                    now,
                )
                conversation.state = "ended"
                self._active.pop(conversation.conversation_id, None)
                self._stop_debug_audio_recording(conversation, reason="listen_expired")
                self._schedule_owner_summary(conversation, reason="listen_expired")
                self._schedule_doorman_session_reset(conversation, reason="listen_expired")
                continue
            if conversation.speech_source_dids & source_dids:
                return conversation
            logger.info(
                "doorbell speech ignored: source did mismatch id=%s did=%s expected=%s actual=%s",
                conversation.conversation_id,
                conversation.did,
                sorted(conversation.speech_source_dids),
                sorted(source_dids),
            )
        return None

    def _schedule_listen_timeout(self, conversation: _Conversation) -> None:
        if not self._schedule_timeouts:
            return
        generation = conversation.listen_generation
        delay = max(0.0, conversation.listen_deadline - self._clock())
        logger.info(
            "doorbell silence timeout scheduled id=%s did=%s generation=%s delay=%.3f",
            conversation.conversation_id,
            conversation.did,
            generation,
            delay,
        )

        async def _timeout() -> None:
            await asyncio.sleep(delay)
            current = self._active.get(conversation.conversation_id)
            if (
                current is None
                or current.state != "listening"
                or current.listen_generation != generation
                or self._clock() < current.listen_deadline
            ):
                logger.info(
                    "doorbell silence timeout skipped id=%s generation=%s current=%s",
                    conversation.conversation_id,
                    generation,
                    self._conversation_summary(current),
                )
                return
            await self._end_after_silence(current)

        try:
            asyncio.create_task(_timeout())
        except RuntimeError:
            logger.debug(
                "doorbell silence timeout not scheduled outside running loop id=%s",
                conversation.conversation_id,
            )

    def _request_stream_hold(self, conversation: _Conversation, *, reason: str) -> None:
        if self._keep_stream_alive is None:
            return
        duration = max(0.0, conversation.listen_deadline - self._clock())
        if duration <= 0:
            return
        for source_did in sorted(conversation.speech_source_dids):
            try:
                loop = asyncio.get_running_loop()
                result = self._keep_stream_alive(source_did, duration, reason)
                if result is not None:
                    loop.create_task(result)
            except RuntimeError:
                logger.debug(
                    "doorbell stream hold not scheduled outside running loop id=%s did=%s reason=%s",
                    conversation.conversation_id,
                    source_did,
                    reason,
                )
            except Exception as e:
                logger.warning(
                    "doorbell stream hold request failed id=%s did=%s duration=%.3f reason=%s error=%s",
                    conversation.conversation_id,
                    source_did,
                    duration,
                    reason,
                    e,
                )

    async def _end_after_silence(self, conversation: _Conversation) -> None:
        self._active.pop(conversation.conversation_id, None)
        self._stop_debug_audio_recording(conversation, reason="silence_timeout")
        conversation.state = "ended"
        conversation.listen_generation += 1
        await self._push_owner_summary(conversation, reason="silence_timeout")
        await self._reset_doorman_session(conversation, reason="silence_timeout")
        logger.info(
            "doorbell conversation silence timeout id=%s did=%s turns=%s seen_speeches=%s",
            conversation.conversation_id,
            conversation.did,
            conversation.visitor_turns,
            sorted(conversation.seen_speeches),
        )
        miot = get_settings().miot
        if not miot.doorbell_silence_fallback_enabled:
            return
        text = miot.doorbell_silence_fallback_text.strip()
        if not text or not miot.doorbell_reply_audio_command:
            logger.info(
                "doorbell silence fallback skipped id=%s did=%s text_configured=%s audio_command_configured=%s",
                conversation.conversation_id,
                conversation.did,
                bool(text),
                bool(miot.doorbell_reply_audio_command),
            )
            return
        try:
            logger.info(
                "doorbell silence fallback audio requested id=%s did=%s text=%s",
                conversation.conversation_id,
                conversation.did,
                text,
            )
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
        active_tasks = _load_active_doorman_tasks()
        turn_text = _with_deterministic_doorman_instruction(text, active_tasks)
        logger.info(
            "doorbell agent turn starting id=%s did=%s trace_id=%s turns=%s state=%s text=%s",
            conversation.conversation_id,
            conversation.did,
            trace_id,
            conversation.visitor_turns,
            conversation.state,
            turn_text,
        )
        _run_id, _status, _rtt_ms, response_text = await run_agent_turn_detailed(
            turn_text,
            session_key=settings.miot.doorbell_session_key or "",
            lane="miloco-interactive",
            trace_id=trace_id,
            wait_timeout_ms=settings.dispatcher.turn_wait_timeout_ms,
            extra_payload={
                "extraSystemPrompt": DOORBELL_INTERCOM_PROMPT
                + _format_conversation_state_prompt(conversation)
                + _format_doorman_tasks_prompt(active_tasks),
                "doorbellReplyAudio": self._audio_payload(conversation),
            },
        )
        if response_text:
            conversation.assistant_messages.append(response_text.strip())
            if conversation.state == "listening":
                self._end_after_closing_reply(conversation, source="agent_response")
        logger.info(
            "doorbell agent turn submitted id=%s did=%s trace_id=%s turns=%s state=%s",
            conversation.conversation_id,
            conversation.did,
            trace_id,
            conversation.visitor_turns,
            conversation.state,
        )

    def _end_after_closing_reply(self, conversation: _Conversation, *, source: str) -> bool:
        if not self._should_end_after_reply(conversation):
            return False
        self._stop_debug_audio_recording(conversation, reason="reply_closed")
        conversation.state = "ended"
        self._active.pop(conversation.conversation_id, None)
        self._schedule_owner_summary(conversation, reason="reply_closed")
        self._schedule_doorman_session_reset(conversation, reason="reply_closed")
        logger.info(
            "doorbell conversation ended after closing reply id=%s did=%s turns=%s source=%s reply=%s",
            conversation.conversation_id,
            conversation.did,
            conversation.visitor_turns,
            source,
            conversation.assistant_messages[-1] if conversation.assistant_messages else "",
        )
        return True

    def _schedule_doorman_session_reset(self, conversation: _Conversation, *, reason: str) -> None:
        try:
            asyncio.create_task(self._reset_doorman_session(conversation, reason=reason))
        except RuntimeError:
            logger.debug(
                "doorbell doorman session reset not scheduled outside running loop id=%s reason=%s",
                conversation.conversation_id,
                reason,
            )

    async def _reset_doorman_session(self, conversation: _Conversation, *, reason: str) -> None:
        session_key = (get_settings().miot.doorbell_session_key or "").strip()
        if not session_key:
            return
        try:
            await reset_agent_sessions(
                [(session_key, "miloco-interactive")],
                delete_transcript=True,
                timeout=10.0,
            )
        except Exception as e:
            logger.warning(
                "doorbell doorman session reset failed id=%s session_key=%s reason=%s error=%s",
                conversation.conversation_id,
                session_key,
                reason,
                e,
            )
            return
        logger.info(
            "doorbell doorman session reset id=%s session_key=%s reason=%s",
            conversation.conversation_id,
            session_key,
            reason,
        )

    @staticmethod
    def _conversation_summary(conversation: _Conversation | None) -> dict[str, object] | None:
        if conversation is None:
            return None
        return {
            "id": conversation.conversation_id,
            "did": conversation.did,
            "state": conversation.state,
            "turns": conversation.visitor_turns,
            "generation": conversation.listen_generation,
            "deadline": round(conversation.listen_deadline, 3),
        }

    def _active_summary(self) -> list[dict[str, object]]:
        return [
            summary
            for conversation in self._active.values()
            if (summary := self._conversation_summary(conversation)) is not None
        ]

    def _schedule_owner_summary(self, conversation: _Conversation, *, reason: str) -> None:
        try:
            asyncio.create_task(self._push_owner_summary(conversation, reason=reason))
        except RuntimeError:
            logger.debug(
                "doorbell owner summary not scheduled outside running loop id=%s reason=%s",
                conversation.conversation_id,
                reason,
            )

    async def _push_owner_summary(self, conversation: _Conversation, *, reason: str) -> None:
        miot = get_settings().miot
        if not miot.doorbell_owner_summary_enabled or conversation.owner_summary_sent:
            return
        conversation.owner_summary_sent = True
        summary = self._build_owner_summary(conversation, reason=reason)
        main_session_key = miot.doorbell_owner_summary_main_session_key.strip()
        if main_session_key:
            try:
                await run_agent_turn_detailed(
                    summary,
                    session_key=main_session_key,
                    lane="miloco-interactive",
                    trace_id=str(uuid.uuid4()),
                    wait_timeout_ms=min(get_settings().dispatcher.turn_wait_timeout_ms, 30_000),
                    deliver=False,
                    extra_payload={
                        "extraSystemPrompt": (
                            "这是司阍自动同步给主 Agent 的门口状态摘要。"
                            "请纳入上下文；不要把它当作门外访客输入，也不要请求司阍权限。"
                        )
                    },
                )
            except Exception as e:
                logger.warning(
                    "doorbell owner summary main sync failed id=%s session_key=%s error=%s",
                    conversation.conversation_id,
                    main_session_key,
                    e,
                )
        if miot.doorbell_owner_summary_phone_push_enabled:
            try:
                await run_agent_turn_detailed(
                    summary,
                    session_key=main_session_key or "agent:main:main",
                    lane="miloco-interactive",
                    trace_id=str(uuid.uuid4()),
                    wait_timeout_ms=min(get_settings().dispatcher.turn_wait_timeout_ms, 30_000),
                    deliver=True,
                    resolve_target="owner-channel",
                    extra_payload={
                        "extraSystemPrompt": (
                            "你正在把司阍门口汇报推送给主人手机。"
                            "请原样转发用户消息全文，不要添加解释、前缀、后缀或 Markdown。"
                        )
                    },
                )
            except Exception as e:
                logger.warning(
                    "doorbell owner summary phone push failed id=%s error=%s",
                    conversation.conversation_id,
                    e,
                )

    @staticmethod
    def _build_owner_summary(conversation: _Conversation, *, reason: str) -> str:
        reason_labels = {
            "silence_timeout": "静默超时，门口会话已结束",
            "listen_expired": "监听超时，后续语音未进入会话",
            "playback_failed": "门锁播报失败，会话已结束",
            "max_turns": "已达到最大对话轮数，会话已结束",
            "reply_closed": "司阍已播报结束语，会话已结束",
        }
        visitor = "；".join(conversation.visitor_messages[-3:]) or "未识别到访客语音"
        assistant = "；".join(conversation.assistant_messages[-3:]) or "无司阍回复记录"
        needs_owner = DoorbellConversationService._needs_owner_attention(
            conversation,
            reason=reason,
        )
        lines = [
            "[司阍门口汇报]",
            f"时间：{datetime.now(TZ_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')}",
            f"访客：{visitor}",
            f"司阍：{assistant}",
            f"状态：{reason_labels.get(reason, reason)}",
            f"需要主人处理：{'是' if needs_owner else '否'}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _needs_owner_attention(conversation: _Conversation, *, reason: str) -> bool:
        if reason in {"playback_failed", "listen_expired"}:
            return True
        combined = "\n".join(conversation.visitor_messages + conversation.assistant_messages)
        attention_keywords = [
            "没有取件码",
            "没有可转达",
            "联系主人",
            "转达给主人",
            "留言",
            "物业",
            "维修",
            "开门",
            "无法",
            "不能",
            "紧急",
        ]
        return any(keyword in combined for keyword in attention_keywords)

    @staticmethod
    def _should_end_after_reply(conversation: _Conversation) -> bool:
        if not conversation.assistant_messages:
            return False
        reply = conversation.assistant_messages[-1].strip()
        closing_keywords = [
            "慢走",
            "再见",
            "有事再按门铃",
            "有事再联系",
            "不用客气",
            "不客气",
        ]
        return any(keyword in reply for keyword in closing_keywords)

    @staticmethod
    def _audio_payload(
        conversation: _Conversation, *, include_conversation_id: bool = True
    ) -> dict[str, object]:
        miot = get_settings().miot
        payload: dict[str, object] = {
            "did": conversation.did,
            "siid": conversation.siid,
            "eiid": conversation.eiid,
            "wakeBeforeAudio": miot.doorbell_wake_before_reply_audio_enabled,
            "wakeActionIid": miot.doorbell_wake_action_iid,
            "audioCommand": miot.doorbell_reply_audio_command,
        }
        if include_conversation_id:
            payload["conversationId"] = conversation.conversation_id
        return payload

    @staticmethod
    def _start_debug_audio_recording(conversation: _Conversation) -> None:
        miot = get_settings().miot
        if not miot.doorbell_debug_audio_enabled:
            return
        target_dir = (
            Path(miot.doorbell_debug_audio_dir).expanduser()
            if miot.doorbell_debug_audio_dir
            else miloco_home() / "debug" / "doorbell-audio"
        )
        try:
            for source_did in sorted(conversation.speech_source_dids):
                doorbell_debug_audio_recorder.start(
                    source_did,
                    conversation.conversation_id,
                    target_dir,
                )
        except Exception as e:
            logger.warning(
                "doorbell debug audio recording failed to start id=%s did=%s error=%s",
                conversation.conversation_id,
                conversation.did,
                e,
            )

    @staticmethod
    def _stop_debug_audio_recording(conversation: _Conversation, *, reason: str) -> None:
        for source_did in sorted(conversation.speech_source_dids):
            doorbell_debug_audio_recorder.stop(source_did, reason=reason)


_service = DoorbellConversationService()


def get_doorbell_conversation_service() -> DoorbellConversationService:
    return _service
