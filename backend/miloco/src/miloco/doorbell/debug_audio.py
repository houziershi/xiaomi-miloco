# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Debug WAV recording for doorbell audio streams."""

from __future__ import annotations

import json
import logging
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)


@dataclass
class _ActiveRecording:
    did: str
    conversation_id: str
    wav_path: Path
    started_at: str
    started_monotonic: float
    wav: wave.Wave_write
    sample_count: int = 0
    frame_count: int = 0


class DoorbellDebugAudioRecorder:
    """Writes decoded doorbell PCM frames to per-conversation WAV files."""

    def __init__(self, *, sample_rate: int = 16_000) -> None:
        self._sample_rate = sample_rate
        self._lock = threading.Lock()
        self._active: dict[str, _ActiveRecording] = {}

    def start(self, did: str, conversation_id: str, directory: str | Path) -> Path | None:
        with self._lock:
            self._stop_locked(did, reason="replaced")
            target_dir = Path(directory).expanduser()
            target_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe_did = did.replace(":", "_").replace("/", "_")
            wav_path = target_dir / f"{timestamp}_{conversation_id}_{safe_did}.wav"
            wav_file = wave.open(str(wav_path), "wb")
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._sample_rate)
            self._active[did] = _ActiveRecording(
                did=did,
                conversation_id=conversation_id,
                wav_path=wav_path,
                started_at=datetime.now().isoformat(timespec="seconds"),
                started_monotonic=time.monotonic(),
                wav=wav_file,
            )
            logger.info(
                "doorbell debug audio recording started did=%s conversation_id=%s file=%s",
                did,
                conversation_id,
                wav_path,
            )
            return wav_path

    def append(self, did: str, frame: "NDArray[np.int16]") -> None:
        with self._lock:
            recording = self._active.get(did)
            if recording is None:
                return
            pcm = np.asarray(frame, dtype=np.int16).reshape(-1)
            if pcm.size == 0:
                return
            recording.wav.writeframes(np.ascontiguousarray(pcm).tobytes())
            recording.sample_count += int(pcm.size)
            recording.frame_count += 1

    def stop(self, did: str, *, reason: str) -> Path | None:
        with self._lock:
            return self._stop_locked(did, reason=reason)

    def _stop_locked(self, did: str, *, reason: str) -> Path | None:
        recording = self._active.pop(did, None)
        if recording is None:
            return None
        recording.wav.close()
        duration_seconds = recording.sample_count / self._sample_rate
        meta_path = recording.wav_path.with_suffix(".json")
        metadata = {
            "did": recording.did,
            "conversation_id": recording.conversation_id,
            "wav_path": str(recording.wav_path),
            "started_at": recording.started_at,
            "stopped_at": datetime.now().isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.monotonic() - recording.started_monotonic, 3),
            "duration_seconds": round(duration_seconds, 3),
            "sample_rate": self._sample_rate,
            "sample_count": recording.sample_count,
            "frame_count": recording.frame_count,
            "stop_reason": reason,
        }
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(
            "doorbell debug audio recording stopped did=%s conversation_id=%s file=%s duration=%.3fs samples=%d reason=%s",
            recording.did,
            recording.conversation_id,
            recording.wav_path,
            duration_seconds,
            recording.sample_count,
            reason,
        )
        return meta_path


doorbell_debug_audio_recorder = DoorbellDebugAudioRecorder()
