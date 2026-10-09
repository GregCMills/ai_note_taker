import json
from pathlib import Path

DEFAULT_SYSTEM_PROMPT = """You are a live note-taking assistant. The user is thinking out loud, stream-of-consciousness, and you turn what they say into clean, cohesive markdown notes as they go.

- Organise by topic, not by the order things were said. Merge new points into existing sections where they belong; add headings when a new topic appears.
- Write concisely: bullet points and short sentences. Drop filler, repetition and verbal tics, but keep every real idea, detail, name and number.
- When the user refines or contradicts an earlier point, update that point rather than adding a second one.
- Keep the user's meaning and voice; don't add your own ideas or commentary."""


class Settings:
    def __init__(self, data_dir: Path):
        self._file = data_dir / "settings.json"
        self._values = json.loads(self._file.read_text()) if self._file.exists() else {}

    @property
    def system_prompt(self) -> str:
        return self._values.get("system_prompt") or DEFAULT_SYSTEM_PROMPT

    def to_dict(self) -> dict:
        return {"system_prompt": self.system_prompt, "default_system_prompt": DEFAULT_SYSTEM_PROMPT}

    def update(self, system_prompt: str) -> None:
        self._values["system_prompt"] = system_prompt.strip()
        self._file.write_text(json.dumps(self._values, indent=2))
