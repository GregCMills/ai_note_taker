"""Per-note agent memory: spoken instructions, conversation turns, the running summary, and section summaries."""

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter


@dataclass
class SectionInfo:
    digest: str = ""
    summary: str = ""
    touched: int = 0


@dataclass
class NoteMemory:
    instructions: str = ""
    history_summary: str = ""
    turns: list[list[ModelMessage]] = field(default_factory=list)
    sections: dict[int, SectionInfo] = field(default_factory=dict)
    turn: int = 0
    next_section_id: int = 1

    def history(self) -> list[ModelMessage]:
        return [message for turn in self.turns for message in turn]

    def touch(self, section_id: int) -> None:
        self.sections.setdefault(section_id, SectionInfo()).touched = self.turn

    def to_json(self) -> dict:
        return {
            "instructions": self.instructions,
            "history_summary": self.history_summary,
            "turns": [ModelMessagesTypeAdapter.dump_python(turn, mode="json") for turn in self.turns],
            "sections": {str(k): vars(v) for k, v in self.sections.items()},
            "turn": self.turn,
            "next_section_id": self.next_section_id,
        }

    @classmethod
    def from_json(cls, data: dict) -> "NoteMemory":
        return cls(
            instructions=data.get("instructions", ""),
            history_summary=data.get("history_summary", ""),
            turns=[ModelMessagesTypeAdapter.validate_python(turn) for turn in data.get("turns", [])],
            sections={int(k): SectionInfo(**v) for k, v in data.get("sections", {}).items()},
            turn=data.get("turn", 0),
            next_section_id=data.get("next_section_id", 1),
        )


class MemoryStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()

    def _file(self, path: str) -> Path:
        return self.root / f"{path}.json"

    def load(self, path: str) -> NoteMemory:
        file = self._file(path)
        with self._lock:
            return NoteMemory.from_json(json.loads(file.read_text())) if file.exists() else NoteMemory()

    def save(self, path: str, memory: NoteMemory) -> None:
        file = self._file(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            file.write_text(json.dumps(memory.to_json()))

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if self._file(old).exists():
                self._file(new).parent.mkdir(parents=True, exist_ok=True)
                self._file(old).rename(self._file(new))

    def delete(self, path: str) -> None:
        with self._lock:
            self._file(path).unlink(missing_ok=True)
