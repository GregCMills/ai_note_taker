import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agentlog import AgentLog
from .agents import NoteAgents, UiHooks, describe_turn
from .audio import SAMPLE_RATE, Segmenter, Transcriber
from .hub import Hub
from .memory import MemoryStore
from .notes import NoteError, NoteStore, clean_name
from .sections import Document, estimate_tokens
from .settings import Settings
from .tls import ensure_certificate
from .transcripts import TranscriptStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("notetaker")

config.DATA_DIR.mkdir(parents=True, exist_ok=True)
notes = NoteStore(config.DATA_DIR / "notes")
transcripts = TranscriptStore(config.DATA_DIR / "transcripts")
settings = Settings(config.DATA_DIR)
hub = Hub()
memories = MemoryStore(config.DATA_DIR / "memory")
activity = AgentLog(config.DATA_DIR / "agentlog")
agents = NoteAgents(notes, memories, activity)
transcriber = Transcriber(
    config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE, config.WHISPER_LANGUAGE
)
whisper_state = {"state": "loading", "error": None}


viewing: dict[str, str] = {}  # project -> the note (tab) the user last looked at


async def push_projects() -> None:
    await hub.send_all({"type": "projects", "projects": project_summaries()})


async def push_note(project: str, note: str) -> None:
    if notes.note_exists(project, note):
        await hub.send(project, {"type": "note", "note": note, "content": notes.read(project, note)})


async def push_tabs(project: str, active: str) -> None:
    await hub.send(project, {"type": "tabs", "notes": notes.notes(project), "active": active})


async def push_instructions(project: str) -> None:
    await hub.send(project, {"type": "instructions", "instructions": memories.load(project).instructions})


async def push_history(project: str) -> None:
    await hub.send(project, {"type": "history", "commits": notes.log(project)})


def ui_hooks(project: str) -> UiHooks:
    async def notes_changed(changed: list[str], focus: str, section: int | None) -> None:
        for note in changed:
            await push_note(project, note)
        await hub.send(project, {"type": "focus", "note": focus, "section": section})

    async def tabs_changed(active: str) -> None:
        await push_tabs(project, active)
        await push_projects()
        await hub.send(project, {"type": "focus", "note": active, "section": None})

    return UiHooks(notes_changed, tabs_changed)


class ProjectSession:
    """Queues speech for one project and runs the agents over it one batch at a time."""

    def __init__(self, project: str):
        self.project = project
        self.lock = asyncio.Lock()
        self._pending: list[dict] = []
        self._task: asyncio.Task | None = None

    def add(self, segment: dict) -> None:
        self._pending.append(segment)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._pending:
            batch, self._pending = self._pending, []
            async with self.lock:
                await self._process(batch)

    async def _process(self, batch: list[dict]) -> None:
        project, ids = self.project, [s["id"] for s in batch]
        await hub.send(project, {"type": "agent", "state": "working", "segments": ids})
        for attempt in range(3):
            try:
                sha, summary = await agents.process(
                    project, batch, settings.system_prompt, ui_hooks(project), viewing.get(project)
                )
                break
            except Exception as error:
                log.exception("Agent failed on %s", project)
                await hub.send(project, {"type": "agent", "state": "error", "message": f"LLM error: {error}"})
                await asyncio.sleep(3 * (attempt + 1))
        else:
            changed = transcripts.update(project, ids, status="error")
            await hub.send(project, {"type": "segments", "segments": changed})
            return

        changed = transcripts.update(project, ids, status="done", commit=sha)
        await hub.send(project, {"type": "segments", "segments": changed})
        await push_history(project)
        await push_instructions(project)
        await hub.send(project, {"type": "agent", "state": "idle", "summary": summary})
        await hub.send(project, {"type": "activity"})


sessions: dict[str, ProjectSession] = {}


def session(project: str) -> ProjectSession:
    if project not in sessions:
        sessions[project] = ProjectSession(project)
    return sessions[project]


def project_summaries() -> list[dict]:
    return [{"name": name, "notes": notes.notes(name)} for name in notes.projects()]


PARTIAL_EVERY_S = 1.0


class Recorder:
    """One per browser connection: splits incoming audio into segments and transcribes them in order.

    While a sentence is still being spoken it is also transcribed provisionally about once a second,
    so the browser can show words as they are said. The final transcript replaces it at the pause.
    """

    def __init__(self):
        self.project: str | None = None
        self._segmenter: Segmenter | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work())
        self._utterance = 0
        self._partial_task: asyncio.Task | None = None
        self._last_partial = 0.0

    def start(self, project: str) -> None:
        self.stop()
        self.project = project
        self._segmenter = Segmenter(settings.silence_ms, config.MAX_SEGMENT_S)

    def feed(self, pcm: bytes) -> None:
        if not self._segmenter:
            return
        for audio in self._segmenter.feed(pcm):
            self._utterance += 1
            self._queue.put_nowait((self.project, audio))
        self._maybe_partial()

    def stop(self) -> None:
        if self._segmenter:
            for audio in self._segmenter.flush():
                self._queue.put_nowait((self.project, audio))
            self._utterance += 1
            asyncio.create_task(hub.send(self.project, {"type": "partial", "text": ""}))
        self._segmenter = None

    def _maybe_partial(self) -> None:
        if self._partial_task and not self._partial_task.done():
            return
        if time.monotonic() - self._last_partial < PARTIAL_EVERY_S or not self._queue.empty():
            return
        audio = self._segmenter.in_progress()
        if audio is None or len(audio) < SAMPLE_RATE // 2:
            return
        self._last_partial = time.monotonic()
        self._partial_task = asyncio.create_task(self._partial(self.project, self._utterance, audio))

    async def _partial(self, project: str, utterance: int, audio) -> None:
        try:
            text = await asyncio.to_thread(transcriber.transcribe, audio, "", True)
        except Exception:
            return
        # Drop it if the sentence has already been finalised in the meantime.
        if utterance == self._utterance and project == self.project:
            await hub.send(project, {"type": "partial", "text": text})

    def close(self) -> None:
        self.stop()
        self._queue.put_nowait(None)

    async def _work(self) -> None:
        while (item := await self._queue.get()) is not None:
            project, audio = item
            await hub.send(project, {"type": "transcribing", "active": True})
            try:
                previous = transcripts.all(project)[-1:]
                text = await asyncio.to_thread(transcriber.transcribe, audio, previous[0]["text"] if previous else "")
            except Exception as error:
                log.exception("Transcription failed")
                await hub.send(project, {"type": "agent", "state": "error", "message": f"Whisper error: {error}"})
                text = ""
            await hub.send(project, {"type": "transcribing", "active": False})
            if text:
                segment = transcripts.append(project, text)
                await hub.send(project, {"type": "segments", "segments": [segment]})
                session(project).add(segment)


async def load_whisper() -> None:
    try:
        await asyncio.to_thread(transcriber.load)
        whisper_state["state"] = "ready"
        log.info("Whisper model loaded")
    except Exception as error:
        log.exception("Whisper failed to load")
        whisper_state.update(state="error", error=str(error))


@asynccontextmanager
async def lifespan(_: FastAPI):
    loader = asyncio.create_task(load_whisper())
    yield
    loader.cancel()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(NoteError)
async def note_error(_, error: NoteError):
    return JSONResponse({"detail": str(error)}, status_code=400)


class NameBody(BaseModel):
    name: str


class NamesBody(BaseModel):
    names: list[str]


class RenameBody(BaseModel):
    old: str
    new: str


class NoteBody(BaseModel):
    project: str
    note: str
    content: str


class CommitBody(BaseModel):
    project: str
    sha: str


class InstructionsBody(BaseModel):
    project: str
    instructions: str


class SettingsBody(BaseModel):
    system_prompt: str
    silence_ms: int | None = None


@app.get("/api/status")
async def status():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            llm_ok = (await client.get(config.LLM_URL.removesuffix("/v1") + "/health")).status_code == 200
    except httpx.HTTPError:
        llm_ok = False
    return {"whisper": whisper_state, "llm": "ready" if llm_ok else "unavailable"}


@app.get("/api/projects")
async def list_projects():
    return {"projects": project_summaries()}


@app.post("/api/projects")
async def create_project(body: NameBody):
    name = notes.create_project(body.name)
    await push_projects()
    return {"name": name}


@app.post("/api/projects/delete")
async def delete_projects(body: NamesBody):
    names = [clean_name(n, "project") for n in body.names]
    for name in names:
        await session(name).lock.acquire()
    try:
        notes.delete_projects(names)
        for name in names:
            transcripts.delete(name)
            memories.delete(name)
            activity.delete(name)
            viewing.pop(name, None)
    finally:
        for name in names:
            session(name).lock.release()
            sessions.pop(name, None)
    await push_projects()
    return {"ok": True}


@app.post("/api/projects/rename")
async def rename_project(body: RenameBody):
    old = clean_name(body.old, "project")
    async with session(old).lock:
        new = notes.rename_project(old, body.new)
        transcripts.rename(old, new)
        memories.rename(old, new)
        activity.rename(old, new)
    sessions.pop(old, None)
    viewing.pop(old, None)
    await push_projects()
    return {"name": new}


@app.get("/api/project")
async def get_project(name: str):
    project = clean_name(name, "project")
    if not notes.project_exists(project):
        raise NoteError(f"No project {project}")
    tabs = notes.notes(project)
    memory = memories.load(project)
    return {
        "name": project,
        "notes": [{"name": n, "content": notes.read(project, n)} for n in tabs],
        "active": next(n for n in (viewing.get(project), memory.active_note, *tabs) if n in tabs),
        "segments": transcripts.all(project),
        "commits": notes.log(project),
        "instructions": memory.instructions,
    }


def keep_section_ids(old_content: str, new_content: str) -> str:
    """The editor hides section markers; give edited sections back their old ids by matching headings."""
    old = Document.parse(old_content, assign=False)
    new = Document.parse(new_content, assign=False)
    ids_by_title: dict[str, list[int]] = {}
    for section in old.sections:
        ids_by_title.setdefault(section.title.lower(), []).append(section.id)
    taken = {s.id for s in new.sections if s.id is not None}
    for section in new.sections:
        candidates = [i for i in ids_by_title.get(section.title.lower(), []) if i not in taken]
        if section.id is None and candidates:
            section.id = candidates[0]
            taken.add(section.id)
    # Sections still without an id are numbered when the agent next loads the project.
    return new.serialize()


@app.get("/api/activity")
async def get_activity(project: str, limit: int = 30):
    """What the agents have been doing: recent turns in full, plus the current context sizes and budgets."""
    project = clean_name(project, "project")
    memory = memories.load(project)
    note_tokens = sum(estimate_tokens(notes.read(project, n)) for n in notes.notes(project))
    return {
        "context": {
            "window": config.LLM_CONTEXT,
            "notes": note_tokens,
            "note_budget": config.NOTE_TOKEN_BUDGET,
            "history": sum(estimate_tokens(describe_turn(t)) for t in memory.turns),
            "history_budget": config.HISTORY_TOKEN_BUDGET,
            "history_turns": len(memory.turns),
            "history_summary": memory.history_summary,
        },
        "turns": activity.recent(project, limit),
    }


@app.put("/api/note")
async def save_note(body: NoteBody):
    project = clean_name(body.project, "project")
    async with session(project).lock:
        notes.write(project, body.note, keep_section_ids(notes.read(project, body.note), body.content))
        notes.commit(project, f'Manual edit of "{body.note}"')
    await push_note(project, body.note)
    await push_history(project)
    return {"ok": True}


async def _after_history_change(project: str) -> None:
    tabs = notes.notes(project)
    active = next(n for n in (viewing.get(project), *tabs) if n in tabs)
    await push_tabs(project, active)
    await push_projects()
    for note in tabs:
        await push_note(project, note)
    await push_history(project)


@app.post("/api/revert")
async def revert_commit(body: CommitBody):
    project = clean_name(body.project, "project")
    async with session(project).lock:
        notes.revert(project, body.sha)
        notes.commit(project, f"Revert {body.sha[:7]} (manual)")
    await _after_history_change(project)
    return {"ok": True}


@app.post("/api/restore")
async def restore_commit(body: CommitBody):
    project = clean_name(body.project, "project")
    async with session(project).lock:
        notes.restore(project, body.sha)
        notes.commit(project, f"Restore to {body.sha[:7]} (manual)")
    await _after_history_change(project)
    return {"ok": True}


@app.put("/api/instructions")
async def put_instructions(body: InstructionsBody):
    project = clean_name(body.project, "project")
    async with session(project).lock:
        memory = memories.load(project)
        memory.instructions = body.instructions.strip()
        memories.save(project, memory)
    await push_instructions(project)
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    return settings.to_dict()


@app.put("/api/settings")
async def put_settings(body: SettingsBody):
    settings.update(body.system_prompt, body.silence_ms)
    return settings.to_dict()


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    hub.add(ws)
    recorder = Recorder()
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes"):
                recorder.feed(message["bytes"])
                continue
            data = json.loads(message.get("text") or "{}")
            try:
                if data.get("type") == "subscribe":
                    hub.subscribe(ws, clean_name(data["project"], "project") if data.get("project") else None)
                elif data.get("type") == "view":
                    viewing[clean_name(data["project"], "project")] = data["note"]
                elif data.get("type") == "start":
                    recorder.start(clean_name(data["project"], "project"))
                elif data.get("type") == "stop":
                    recorder.stop()
            except (NoteError, KeyError) as error:
                # KeyError: a malformed message, e.g. from a page loaded before an update. Reloading fixes it.
                await ws.send_json({"type": "agent", "state": "error", "message": f"Bad request ({error}); try reloading"})
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(ws)
        recorder.close()


if config.WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    cert, key = ensure_certificate(config.DATA_DIR / "certs", config.TLS_HOSTS)
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, ssl_certfile=str(cert), ssl_keyfile=str(key))
