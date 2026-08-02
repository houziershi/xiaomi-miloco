from __future__ import annotations

import json
import wave

import numpy as np
from miloco.doorbell.debug_audio import DoorbellDebugAudioRecorder


def test_debug_audio_recorder_writes_wav_and_sidecar(tmp_path):
    recorder = DoorbellDebugAudioRecorder(sample_rate=16_000)
    path = recorder.start("door-did", "conversation-1", tmp_path)

    recorder.append("door-did", np.array([0, 100, -100], dtype=np.int16))
    recorder.append("door-did", np.array([[200, -200]], dtype=np.int16))
    meta_path = recorder.stop("door-did", reason="test-complete")

    assert path is not None
    assert meta_path is not None
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16_000
        assert wav.getnframes() == 5

    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    assert metadata["did"] == "door-did"
    assert metadata["conversation_id"] == "conversation-1"
    assert metadata["sample_count"] == 5
    assert metadata["stop_reason"] == "test-complete"
