import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlsplit

import httpx
import numpy as np
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .accounts import SESSION_COOKIE, SESSION_SECONDS, USERS_DIR, LoginThrottle, issue_token, user_for_token, verify
from .agents import describe_turn
from .audio import SAMPLE_RATE, Segmenter, make_transcriber
from .hub import Hub
from .manager import MANAGER_SCOPE
from .notes import NoteError, clean_name
from .sections import Document, estimate_tokens
from .tls import ensure_certificate
from .workspace import Workspace

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("notetaker")

config.DATA_DIR.mkdir(parents=True, exist_ok=True)
hub = Hub()
workspaces: dict[str, Workspace] = {}
login_throttle = LoginThrottle()
transcriber = make_transcriber(
    config.ASR_ENGINE,
    granite_model=config.GRANITE_MODEL,
    whisper_model=config.WHISPER_MODEL,
    device=config.WHISPER_DEVICE,
    compute_type=config.WHISPER_COMPUTE_TYPE,
    language=config.WHISPER_LANGUAGE,
    api_url=config.STT_URL,
    api_key=config.STT_API_KEY,
    api_model=config.STT_MODEL,
)
# Reported to the UI as "whisper" for compatibility; it is whichever speech-to-text engine is configured.
whisper_state = {"state": "loading", "error": None, "engine": config.ASR_ENGINE}


def workspace_for(user: str) -> Workspace:
    if user not in workspaces:
        workspaces[user] = Workspace(user, USERS_DIR / user, hub.channel(user))
    return workspaces[user]


def current_workspace(request: Request) -> Workspace:
    user = user_for_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(401, "Not signed in")
    return workspace_for(user)


CurrentWorkspace = Annotated[Workspace, Depends(current_workspace)]


PARTIAL_EVERY_S = 1.0


class Recorder:
    """One per browser connection: splits incoming audio into segments and transcribes them in order.

    While a sentence is still being spoken it is also transcribed provisionally about once a second,
    so the browser can show words as they are said. The final transcript replaces it at the pause.
    """

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.project: str | None = None
        self._segmenter: Segmenter | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work())
        self._utterance = 0
        self._partial_task: asyncio.Task | None = None
        self._last_partial = 0.0
        self._samples = 0
        self._peak = 0
        self._ignored = 0

    def start(self, project: str) -> None:
        self.stop()
        self.project = project
        self._segmenter = Segmenter(self.workspace.settings.silence_ms, config.MAX_SEGMENT_S)
        self._samples = self._peak = 0
        log.info("%s started recording into %s", self.workspace.user, project)

    def feed(self, pcm: bytes) -> None:
        if not self._segmenter:
            # Audio with no recording started: the browser thinks it is recording but the server doesn't.
            if self._ignored == 0:
                log.warning("%s is sending audio without starting a recording; ignoring it", self.workspace.user)
            self._ignored += 1
            return
        samples = np.frombuffer(pcm, dtype=np.int16)
        self._samples += len(samples)
        if len(samples):
            self._peak = max(self._peak, int(np.abs(samples.astype(np.int32)).max()))
        for audio in self._segmenter.feed(pcm):
            self._utterance += 1
            self._queue.put_nowait((self.project, audio))
        self._maybe_partial()

    def stop(self) -> None:
        if self._segmenter:
            # A peak near 0 means the browser sent silence (e.g. a muted or blocked microphone).
            log.info(
                "%s stopped recording: %.1fs of audio received, peak level %d%%",
                self.workspace.user, self._samples / SAMPLE_RATE, round(self._peak / 327.68),
            )
            for audio in self._segmenter.flush():
                self._queue.put_nowait((self.project, audio))
            self._utterance += 1
            asyncio.create_task(self.workspace.hub.send(self.project, {"type": "partial", "text": ""}))
        self._segmenter = None

    def _maybe_partial(self) -> None:
        if not config.LIVE_TRANSCRIPT:
            return
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
            await self.workspace.hub.send(project, {"type": "partial", "text": text})

    def close(self) -> None:
        self.stop()
        self._queue.put_nowait(None)

    async def _work(self) -> None:
        hub, transcripts = self.workspace.hub, self.workspace.transcripts
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
            seconds = len(audio) / SAMPLE_RATE
            if not text:
                # Don't let speech vanish silently: show that audio arrived but nothing was recognised.
                log.info("Nothing recognised in %.1fs of audio for %s", seconds, project)
                segment = transcripts.append(project, f"(nothing recognised in {seconds:.1f} s of audio)", seconds, "error")
                await hub.send(project, {"type": "segments", "segments": [segment]})
                continue
            segment = transcripts.append(project, text, seconds)
            log.info("S%d: %.1fs of audio -> %d words", segment["id"], seconds, len(text.split()))
            await hub.send(project, {"type": "segments", "segments": [segment]})
            self.workspace.deliver(project, segment)


async def load_whisper() -> None:
    try:
        await asyncio.to_thread(transcriber.load)
        whisper_state["state"] = "ready"
        log.info("Speech-to-text model loaded (%s)", config.ASR_ENGINE)
    except Exception as error:
        log.exception("Whisper failed to load")
        whisper_state.update(state="error", error=str(error))


@asynccontextmanager
async def lifespan(_: FastAPI):
    loader = asyncio.create_task(load_whisper())
    yield
    loader.cancel()


# No interactive API docs: the server faces the internet.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def sign_in_first(request: Request, call_next):
    """Send signed-out visitors to the login page instead of an app that can't load anything."""
    if request.url.path in ("/", "/index.html") and user_for_token(request.cookies.get(SESSION_COOKIE)) is None:
        return RedirectResponse("/login.html")
    return await call_next(request)


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


class LoginBody(BaseModel):
    name: str
    password: str


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    name, ip = body.name.strip().lower(), request.client.host if request.client else "?"
    if not login_throttle.allowed(ip, name):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    if not await asyncio.to_thread(verify, name, body.password):
        login_throttle.failed(ip, name)
        log.warning("Failed login for %r from %s", name, ip)
        raise HTTPException(401, "Wrong name or password")
    login_throttle.succeeded(ip, name)
    response.set_cookie(
        SESSION_COOKIE, issue_token(name), max_age=SESSION_SECONDS, httponly=True, secure=True, samesite="lax"
    )
    return {"name": name}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=True, samesite="lax")
    return {"ok": True}


@app.get("/api/me")
async def me(workspace: CurrentWorkspace):
    return {"name": workspace.user}


@app.get("/api/status")
async def status(_: CurrentWorkspace):
    # /models is part of the OpenAI API, so this works for llama.cpp and hosted LLMs alike.
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            llm_ok = (await client.get(config.LLM_URL + "/models", headers=headers)).status_code == 200
    except httpx.HTTPError:
        llm_ok = False
    return {"whisper": whisper_state, "llm": "ready" if llm_ok else "unavailable"}


@app.get("/api/projects")
async def list_projects(workspace: CurrentWorkspace):
    return {"projects": workspace.project_summaries()}


@app.post("/api/projects")
async def create_project(body: NameBody, workspace: CurrentWorkspace):
    return {"name": await workspace.create_project_op(body.name)}


@app.post("/api/projects/delete")
async def delete_projects(body: NamesBody, workspace: CurrentWorkspace):
    await workspace.delete_projects_op(body.names)
    return {"ok": True}


@app.post("/api/projects/rename")
async def rename_project(body: RenameBody, workspace: CurrentWorkspace):
    return {"name": await workspace.rename_project_op(body.old, body.new)}


@app.get("/api/manager")
async def get_manager(workspace: CurrentWorkspace):
    """The manager screen: its transcript and its recent replies."""
    return {
        "segments": workspace.transcripts.all(MANAGER_SCOPE)[-50:],
        "turns": [
            {"time": t["time"], "speech": t["speech"], "reply": t["summary"], "actions": t["actions"]}
            for t in workspace.activity.recent(MANAGER_SCOPE, 30)
        ],
        "deleted": workspace.notes.deleted_projects()[:20],
    }


@app.get("/api/project")
async def get_project(name: str, workspace: CurrentWorkspace):
    notes = workspace.notes
    project = clean_name(name, "project")
    if not notes.project_exists(project):
        raise NoteError(f"No project {project}")
    tabs = notes.notes(project)
    memory = workspace.memories.load(project)
    return {
        "name": project,
        "notes": [{"name": n, "content": notes.read(project, n)} for n in tabs],
        "active": next(n for n in (workspace.viewing.get(project), memory.active_note, *tabs) if n in tabs),
        "segments": workspace.transcripts.all(project),
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
async def get_activity(project: str, workspace: CurrentWorkspace, limit: int = 30):
    """What the agents have been doing: recent turns in full, plus the current context sizes and budgets."""
    notes = workspace.notes
    project = project if project == MANAGER_SCOPE else clean_name(project, "project")
    memory = workspace.memories.load(project)
    tabs = [] if project == MANAGER_SCOPE else notes.notes(project)
    note_tokens = sum(estimate_tokens(notes.read(project, n)) for n in tabs)
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
        "turns": workspace.activity.recent(project, limit),
    }


@app.put("/api/note")
async def save_note(body: NoteBody, workspace: CurrentWorkspace):
    notes = workspace.notes
    project = clean_name(body.project, "project")
    async with workspace.session(project).lock:
        notes.write(project, body.note, keep_section_ids(notes.read(project, body.note), body.content))
        notes.commit(project, f'Manual edit of "{body.note}"')
    await workspace.push_note(project, body.note)
    await workspace.push_history(project)
    return {"ok": True}


@app.post("/api/revert")
async def revert_commit(body: CommitBody, workspace: CurrentWorkspace):
    project = clean_name(body.project, "project")
    async with workspace.session(project).lock:
        workspace.notes.revert(project, body.sha)
        workspace.notes.commit(project, f"Revert {body.sha[:7]} (manual)")
    await workspace.after_history_change(project)
    return {"ok": True}


@app.post("/api/restore")
async def restore_commit(body: CommitBody, workspace: CurrentWorkspace):
    project = clean_name(body.project, "project")
    async with workspace.session(project).lock:
        workspace.notes.restore(project, body.sha)
        workspace.notes.commit(project, f"Restore to {body.sha[:7]} (manual)")
    await workspace.after_history_change(project)
    return {"ok": True}


@app.put("/api/instructions")
async def put_instructions(body: InstructionsBody, workspace: CurrentWorkspace):
    project = clean_name(body.project, "project")
    async with workspace.session(project).lock:
        memory = workspace.memories.load(project)
        memory.instructions = body.instructions.strip()
        workspace.memories.save(project, memory)
    await workspace.push_instructions(project)
    return {"ok": True}


@app.get("/api/settings")
async def get_settings(workspace: CurrentWorkspace):
    return workspace.settings.to_dict()


@app.put("/api/settings")
async def put_settings(body: SettingsBody, workspace: CurrentWorkspace):
    workspace.settings.update(body.system_prompt, body.silence_ms)
    return workspace.settings.to_dict()


def same_origin(ws: WebSocket) -> bool:
    """Browsers send the session cookie on WebSockets opened by other sites too, so check who opened it."""
    origin = ws.headers.get("origin")
    return origin is not None and urlsplit(origin).netloc == ws.headers.get("host")


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    user = user_for_token(ws.cookies.get(SESSION_COOKIE))
    if user is None or not same_origin(ws):
        await ws.close(code=1008)
        return
    workspace = workspace_for(user)
    await ws.accept()
    hub.add(ws, user)
    recorder = Recorder(workspace)
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
                    hub.subscribe(ws, clean_name(data["project"], "project") if data.get("project") else MANAGER_SCOPE)
                elif data.get("type") == "view":
                    workspace.viewing[clean_name(data["project"], "project")] = data["note"]
                elif data.get("type") == "start":
                    recorder.start(clean_name(data["project"], "project") if data.get("project") else MANAGER_SCOPE)
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
