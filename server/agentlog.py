"""Full record of what the agents did each turn, for the in-app inspector.

Unlike the agent memory (which trims old turns to save context), this keeps every prompt, tool call, tool result
and token count as they were. Only the most recent turns per project are kept.
"""

import json
import threading
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

KEEP_TURNS = 100
PART_FIELDS = ("part_kind", "content", "tool_name", "args")
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens")


def slim_messages(messages: list[ModelMessage]) -> list[dict]:
    """Messages as JSON, without the provider bookkeeping the inspector does not need."""
    slim = []
    for message in ModelMessagesTypeAdapter.dump_python(messages, mode="json"):
        entry = {
            "kind": message["kind"],
            "timestamp": message.get("timestamp"),
            "parts": [{k: part[k] for k in PART_FIELDS if k in part} for part in message["parts"]],
        }
        if message.get("instructions"):
            entry["instructions"] = message["instructions"]
        if usage := message.get("usage"):
            entry["usage"] = {k: usage.get(k, 0) for k in USAGE_FIELDS}
            entry["finish_reason"] = message.get("finish_reason")
        slim.append(entry)
    return slim


class AgentLog:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()

    def _file(self, project: str) -> Path:
        return self.root / f"{project}.jsonl"

    def append(self, project: str, record: dict) -> None:
        file = self._file(project)
        file.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
            lines = [*lines[-(KEEP_TURNS - 1):], json.dumps(record)]
            file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def recent(self, project: str, limit: int) -> list[dict]:
        """Newest first."""
        file = self._file(project)
        with self._lock:
            lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
        return [json.loads(line) for line in reversed(lines[-limit:])]

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            if self._file(old).exists():
                self._file(old).rename(self._file(new))

    def delete(self, project: str) -> None:
        with self._lock:
            self._file(project).unlink(missing_ok=True)
