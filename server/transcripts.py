"""Per-note log of transcribed speech segments (S1, S2, ...), stored as JSON lines beside the notes repo."""

import json
import threading
import time
from pathlib import Path


class TranscriptStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._cache: dict[str, list[dict]] = {}

    def _file(self, path: str) -> Path:
        return self.root / f"{path}.jsonl"

    def _load(self, path: str) -> list[dict]:
        if path not in self._cache:
            file = self._file(path)
            lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
            self._cache[path] = [json.loads(line) for line in lines if line.strip()]
        return self._cache[path]

    def _save(self, path: str) -> None:
        file = self._file(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("".join(json.dumps(s) + "\n" for s in self._cache[path]), encoding="utf-8")

    def all(self, path: str) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._load(path)]

    def append(self, path: str, text: str) -> dict:
        with self._lock:
            segments = self._load(path)
            segment = {
                "id": segments[-1]["id"] + 1 if segments else 1,
                "text": text,
                "time": time.time(),
                "status": "pending",
                "commit": None,
            }
            segments.append(segment)
            self._save(path)
            return dict(segment)

    def update(self, path: str, ids: list[int], **fields) -> list[dict]:
        with self._lock:
            changed = []
            for segment in self._load(path):
                if segment["id"] in ids:
                    segment.update(fields)
                    changed.append(dict(segment))
            self._save(path)
            return changed

    def recent_done(self, path: str, limit: int) -> list[dict]:
        with self._lock:
            done = [s for s in self._load(path) if s["status"] == "done"]
            return [dict(s) for s in done[-limit:]]

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            self._cache.pop(old, None)
            if self._file(old).exists():
                self._file(new).parent.mkdir(parents=True, exist_ok=True)
                self._file(old).rename(self._file(new))

    def delete(self, path: str) -> None:
        with self._lock:
            self._cache.pop(path, None)
            self._file(path).unlink(missing_ok=True)
