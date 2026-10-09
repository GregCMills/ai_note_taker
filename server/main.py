import asyncio
import json
import logging
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agents import NoteAgents
from .audio import Segmenter, Transcriber
from .hub import Hub
from .memory import MemoryStore
from .notes import NoteError, NoteStore
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
agents = NoteAgents(notes, memories)
transcriber = Transcriber(
    config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE, config.WHISPER_LANGUAGE
)
whisper_state = {"state": "loading", "error": None}


async def push_note(path: str) -> None:
    if notes.exists(path):
        await hub.send(path, {"type": "note", "content": notes.read(path)})


async def push_instructions(path: str) -> None:
    await hub.send(path, {"type": "instructions", "instructions": memories.load(path).instructions})


async def push_history(path: str) -> None:
    await hub.send(path, {"type": "history", "commits": notes.log(path)})


class NoteSession:
    """Queues speech for one note and runs the agent over it one batch at a time."""

    def __init__(self, path: str):
        self.path = path
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
        path, ids = self.path, [s["id"] for s in batch]
        await hub.send(path, {"type": "agent", "state": "working", "segments": ids})
        for attempt in range(3):
            try:
                sha, summary = await agents.process(
                    path, batch, settings.system_prompt, on_change=lambda: push_note(path)
                )
                break
            except Exception as error:
                log.exception("Agent failed on %s", path)
                await hub.send(path, {"type": "agent", "state": "error", "message": f"LLM error: {error}"})
                await asyncio.sleep(3 * (attempt + 1))
        else:
            changed = transcripts.update(path, ids, status="error")
            await hub.send(path, {"type": "segments", "segments": changed})
            return

        changed = transcripts.update(path, ids, status="done", commit=sha)
        await hub.send(path, {"type": "segments", "segments": changed})
        await push_note(path)
        await push_history(path)
        await push_instructions(path)
        await hub.send(path, {"type": "agent", "state": "idle", "summary": summary})


sessions: dict[str, NoteSession] = {}


def session(path: str) -> NoteSession:
    if path not in sessions:
        sessions[path] = NoteSession(path)
    return sessions[path]


class Recorder:
    """One per browser connection: splits incoming audio into segments and transcribes them in order."""

    def __init__(self):
        self.path: str | None = None
        self._segmenter: Segmenter | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work())

    def start(self, path: str) -> None:
        self.stop()
        self.path = path
        self._segmenter = Segmenter(config.SILENCE_MS, config.MAX_SEGMENT_S)

    def feed(self, pcm: bytes) -> None:
        if self._segmenter:
            for audio in self._segmenter.feed(pcm):
                self._queue.put_nowait((self.path, audio))

    def stop(self) -> None:
        if self._segmenter:
            for audio in self._segmenter.flush():
                self._queue.put_nowait((self.path, audio))
        self._segmenter = None

    def close(self) -> None:
        self.stop()
        self._queue.put_nowait(None)

    async def _work(self) -> None:
        while (item := await self._queue.get()) is not None:
            path, audio = item
            await hub.send(path, {"type": "transcribing", "active": True})
            try:
                previous = transcripts.all(path)[-1:]
                text = await asyncio.to_thread(transcriber.transcribe, audio, previous[0]["text"] if previous else "")
            except Exception as error:
                log.exception("Transcription failed")
                await hub.send(path, {"type": "agent", "state": "error", "message": f"Whisper error: {error}"})
                text = ""
            await hub.send(path, {"type": "transcribing", "active": False})
            if text:
                segment = transcripts.append(path, text)
                await hub.send(path, {"type": "segments", "segments": [segment]})
                session(path).add(segment)


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


class PathBody(BaseModel):
    path: str


class ContentBody(BaseModel):
    path: str
    content: str


class RenameBody(BaseModel):
    old: str
    new: str


class CommitBody(BaseModel):
    path: str
    sha: str


class InstructionsBody(BaseModel):
    path: str
    instructions: str


class SettingsBody(BaseModel):
    system_prompt: str


@app.get("/api/status")
async def status():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            llm_ok = (await client.get(config.LLM_URL.removesuffix("/v1") + "/health")).status_code == 200
    except httpx.HTTPError:
        llm_ok = False
    return {"whisper": whisper_state, "llm": "ready" if llm_ok else "unavailable"}


@app.get("/api/notes")
async def list_notes():
    return {"notes": notes.paths()}


@app.post("/api/notes")
async def create_note(body: PathBody):
    path = notes.create(body.path)
    await hub.send_all({"type": "notes", "notes": notes.paths()})
    return {"path": path}


@app.get("/api/note")
async def get_note(path: str):
    path = notes.normalise(path)
    return {
        "path": path,
        "content": notes.read(path),
        "segments": transcripts.all(path),
        "commits": notes.log(path),
        "instructions": memories.load(path).instructions,
    }


@app.put("/api/note")
async def save_note(body: ContentBody):
    path = notes.normalise(body.path)
    async with session(path).lock:
        notes.write(path, body.content)
        notes.commit(path, "Manual edit")
    await push_note(path)
    await push_history(path)
    return {"ok": True}


@app.delete("/api/note")
async def delete_note(path: str):
    path = notes.normalise(path)
    async with session(path).lock:
        notes.delete(path)
        transcripts.delete(path)
        memories.delete(path)
    sessions.pop(path, None)
    await hub.send_all({"type": "notes", "notes": notes.paths()})
    return {"ok": True}


@app.post("/api/rename")
async def rename_note(body: RenameBody):
    old = notes.normalise(body.old)
    async with session(old).lock:
        new = notes.rename(old, body.new)
        transcripts.rename(old, new)
        memories.rename(old, new)
    sessions.pop(old, None)
    await hub.send_all({"type": "notes", "notes": notes.paths()})
    return {"path": new}


@app.post("/api/revert")
async def revert_commit(body: CommitBody):
    path = notes.normalise(body.path)
    async with session(path).lock:
        notes.revert(path, body.sha)
        notes.commit(path, f"Revert {body.sha[:7]} (manual)")
    await push_note(path)
    await push_history(path)
    return {"ok": True}


@app.post("/api/restore")
async def restore_commit(body: CommitBody):
    path = notes.normalise(body.path)
    async with session(path).lock:
        notes.restore(path, body.sha)
        notes.commit(path, f"Restore to {body.sha[:7]} (manual)")
    await push_note(path)
    await push_history(path)
    return {"ok": True}


@app.put("/api/instructions")
async def put_instructions(body: InstructionsBody):
    path = notes.normalise(body.path)
    async with session(path).lock:
        memory = memories.load(path)
        memory.instructions = body.instructions.strip()
        memories.save(path, memory)
    await push_instructions(path)
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    return settings.to_dict()


@app.put("/api/settings")
async def put_settings(body: SettingsBody):
    settings.update(body.system_prompt)
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
                    hub.subscribe(ws, notes.normalise(data["path"]) if data.get("path") else None)
                elif data.get("type") == "start":
                    recorder.start(notes.normalise(data["path"]))
                elif data.get("type") == "stop":
                    recorder.stop()
            except NoteError as error:
                await ws.send_json({"type": "agent", "state": "error", "message": str(error)})
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
