"""Turns a live 16 kHz PCM stream into speech segments (split at pauses) and transcribes them with Whisper."""

import threading

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps

SAMPLE_RATE = 16000
CHECK_EVERY = SAMPLE_RATE // 4
MIN_SPEECH_SAMPLES = int(SAMPLE_RATE * 0.3)

# Whisper's usual inventions on near-silent audio.
HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thanks for watching", "thank you for watching",
    "bye", "okay", "so", "subtitles by the amara.org community",
}


class Segmenter:
    def __init__(self, silence_ms: int, max_segment_s: float):
        self._silence = silence_ms * SAMPLE_RATE // 1000
        self._max = int(max_segment_s * SAMPLE_RATE)
        self._vad = VadOptions(min_silence_duration_ms=300, speech_pad_ms=200)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._unchecked = 0

    def feed(self, pcm16: bytes) -> list[np.ndarray]:
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer = np.concatenate([self._buffer, samples])
        self._unchecked += len(samples)
        if self._unchecked < CHECK_EVERY:
            return []
        self._unchecked = 0
        return self._split(final=False)

    def flush(self) -> list[np.ndarray]:
        segments = self._split(final=True)
        self._buffer = np.zeros(0, dtype=np.float32)
        return segments

    def _split(self, final: bool) -> list[np.ndarray]:
        speech = get_speech_timestamps(self._buffer, self._vad)
        if not speech:
            self._buffer = self._buffer[-SAMPLE_RATE:]
            return []

        start = speech[0]["start"]
        trailing_silence = len(self._buffer) - speech[-1]["end"]
        if final or trailing_silence >= self._silence:
            cut = speech[-1]["end"]
        elif len(self._buffer) - start >= self._max:
            # Still talking at the length limit: cut at the last internal pause if there is one.
            cut = speech[-2]["end"] if len(speech) > 1 else len(self._buffer)
        else:
            if start > SAMPLE_RATE:
                self._buffer = self._buffer[start - SAMPLE_RATE // 2:]
            return []

        segment = self._buffer[start:cut]
        self._buffer = self._buffer[cut:]
        spoken = sum(min(s["end"], cut) - s["start"] for s in speech if s["start"] < cut)
        return [segment] if spoken >= MIN_SPEECH_SAMPLES else []


class Transcriber:
    def __init__(self, model: str, device: str, compute_type: str, language: str | None):
        self._args = (model, device, compute_type)
        self._language = language
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()

    def load(self) -> None:
        model, device, compute_type = self._args
        with self._lock:
            if self._model is None:
                self._model = WhisperModel(model, device=device, compute_type=compute_type)

    def transcribe(self, audio: np.ndarray, previous_text: str = "") -> str:
        self.load()
        with self._lock:
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=5,
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=previous_text[-200:] or None,
            )
            parts = [s.text.strip() for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1)]
        text = " ".join(p for p in parts if p)
        if text.lower().strip(" .!?,") in HALLUCINATIONS:
            return ""
        return text
