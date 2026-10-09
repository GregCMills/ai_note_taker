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
        self._speech_start: int | None = None

    def in_progress(self) -> np.ndarray | None:
        """Audio of the sentence still being spoken, if any (for provisional transcripts)."""
        if self._speech_start is None:
            return None
        return self._buffer[self._speech_start:].copy()

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
        self._speech_start = None
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
                start = SAMPLE_RATE // 2
            self._speech_start = start
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

    def transcribe(self, audio: np.ndarray, previous_text: str = "", fast: bool = False) -> str:
        self.load()
        with self._lock:
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=1 if fast else 5,
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=previous_text[-200:] or None,
            )
            parts = [s.text.strip() for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1)]
        text = " ".join(p for p in parts if p)
        if text.lower().strip(" .!?,") in HALLUCINATIONS:
            return ""
        return text


GRANITE_PROMPT = "<|audio|>transcribe the speech with proper punctuation and capitalization."


class GraniteTranscriber:
    """IBM Granite Speech (a speech-to-text LLM) with the same interface as `Transcriber`.

    More accurate than Whisper on the Open ASR Leaderboard. Its decoder writes the transcript token by token, so
    long audio costs more than with Whisper; segments here are short (one pause to the next).
    """

    def __init__(self, model: str, device: str):
        self._name = model
        self._device = device
        self._model = None
        self._processor = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

            self._processor = AutoProcessor.from_pretrained(self._name)
            self._model = AutoModelForSpeechSeq2Seq.from_pretrained(
                self._name, device_map=self._device, torch_dtype=torch.bfloat16
            )

    def transcribe(self, audio: np.ndarray, previous_text: str = "", fast: bool = False) -> str:
        import torch

        self.load()
        tokenizer = self._processor.tokenizer
        prompt = tokenizer.apply_chat_template(
            [{"role": "user", "content": GRANITE_PROMPT}], tokenize=False, add_generation_prompt=True
        )
        wav = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).unsqueeze(0)
        # Roughly 3 words a second of speech, ~1.5 tokens a word, with headroom.
        max_tokens = max(48, int(len(audio) / SAMPLE_RATE * 8))
        with self._lock, torch.inference_mode():
            inputs = self._processor(prompt, wav, device=self._device, return_tensors="pt").to(self._device)
            output = self._model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False, num_beams=1)
        new_tokens = output[0, inputs["input_ids"].shape[-1]:]
        text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        if text.lower().strip(" .!?,") in HALLUCINATIONS:
            return ""
        return text


def make_transcriber(engine: str, *, granite_model: str, whisper_model: str, device: str, compute_type: str,
                     language: str | None) -> "Transcriber | GraniteTranscriber":
    if engine == "granite":
        return GraniteTranscriber(granite_model, device)
    if engine == "whisper":
        return Transcriber(whisper_model, device, compute_type, language)
    raise ValueError(f"Unknown ASR_ENGINE {engine!r}: use 'granite' or 'whisper'")
