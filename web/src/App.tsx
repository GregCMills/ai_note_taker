import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { FileTree } from './components/FileTree'
import { HistoryPanel } from './components/HistoryPanel'
import { NoteView } from './components/NoteView'
import { SettingsDialog } from './components/SettingsDialog'
import { TranscriptPanel } from './components/TranscriptPanel'
import { type MicrophoneCapture, startMicrophone } from './microphone'
import type { AgentState, Commit, Segment, ServerEvent, Status } from './types'
import { useSocket } from './useSocket'

type View = 'note' | 'transcript' | 'history'

const STATUS_POLL_MS = 10000
const LAST_NOTE_KEY = 'notetaker:last-note'

const readLastNote = () => {
  try {
    return localStorage.getItem(LAST_NOTE_KEY)
  } catch {
    return null
  }
}

const rememberNote = (path: string) => {
  try {
    localStorage.setItem(LAST_NOTE_KEY, path)
  } catch {
    // Storage can be unavailable (private mode); remembering the note is only a convenience.
  }
}

const mergeSegments = (existing: Segment[], incoming: Segment[]) => {
  const byId = new Map(existing.map((segment) => [segment.id, segment]))
  for (const segment of incoming) byId.set(segment.id, segment)
  return [...byId.values()].sort((a, b) => a.id - b.id)
}

const describeAgent = (agent: AgentState) => {
  if (agent.state === 'working') return `Updating notes from ${agent.segments.map((id) => `S${id}`).join(' ')}…`
  if (agent.state === 'error') return agent.message
  return agent.summary ?? ''
}

export const App = () => {
  const [notes, setNotes] = useState<string[]>([])
  const [current, setCurrent] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [segments, setSegments] = useState<Segment[]>([])
  const [commits, setCommits] = useState<Commit[]>([])
  const [instructions, setInstructions] = useState('')
  const [agent, setAgent] = useState<AgentState>({ state: 'idle' })
  const [transcribing, setTranscribing] = useState(false)
  const [recording, setRecording] = useState(false)
  const [status, setStatus] = useState<Status | null>(null)
  const [view, setView] = useState<View>('note')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const currentRef = useRef(current)
  const recordingRef = useRef(recording)
  const micRef = useRef<MicrophoneCapture | null>(null)
  currentRef.current = current
  recordingRef.current = recording

  const handleEvent = useCallback((event: ServerEvent) => {
    if (event.type === 'notes') {
      setNotes(event.notes)
      return
    }
    if (event.path !== undefined && event.path !== currentRef.current) return
    switch (event.type) {
      case 'note':
        setContent(event.content)
        break
      case 'history':
        setCommits(event.commits)
        break
      case 'segments':
        setSegments((previous) => mergeSegments(previous, event.segments))
        break
      case 'instructions':
        setInstructions(event.instructions)
        break
      case 'transcribing':
        setTranscribing(event.active)
        break
      case 'agent':
        setAgent(event)
        break
    }
  }, [])

  const { connected, send } = useSocket(handleEvent, () => {
    send({ type: 'subscribe', path: currentRef.current })
    if (recordingRef.current && currentRef.current) send({ type: 'start', path: currentRef.current })
  })

  const openNote = useCallback(async (path: string) => {
    try {
      const note = await api.getNote(path)
      setCurrent(note.path)
      setContent(note.content)
      setSegments(note.segments)
      setCommits(note.commits)
      setInstructions(note.instructions)
      setAgent({ state: 'idle' })
      setTranscribing(false)
      setDrawerOpen(false)
      rememberNote(note.path)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }, [])

  useEffect(() => {
    void api.listNotes().then(({ notes: loaded }) => {
      setNotes(loaded)
      const last = readLastNote()
      const initial = last && loaded.includes(last) ? last : loaded[0]
      if (initial) void openNote(initial)
    })
  }, [openNote])

  useEffect(() => {
    send({ type: 'subscribe', path: current })
    if (recordingRef.current && current) send({ type: 'start', path: current })
  }, [current, send])

  useEffect(() => {
    const poll = () => void api.status().then(setStatus).catch(() => setStatus(null))
    poll()
    const timer = setInterval(poll, STATUS_POLL_MS)
    return () => clearInterval(timer)
  }, [])

  const run = async (action: () => Promise<unknown>) => {
    try {
      await action()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  const createNote = (folder: string) =>
    run(async () => {
      const name = prompt('Note name (use / for folders)', folder ? `${folder}/` : '')
      if (!name?.trim()) return
      const { path } = await api.createNote(name)
      setNotes((previous) => [...new Set([...previous, path])].sort())
      await openNote(path)
    })

  const renameNote = () =>
    run(async () => {
      if (!current) return
      const next = prompt('Rename note to', current)
      if (!next?.trim() || next === current) return
      const { path } = await api.renameNote(current, next)
      await openNote(path)
    })

  const deleteNote = () =>
    run(async () => {
      if (!current || !confirm(`Delete ${current}? Its history stays in git.`)) return
      await api.deleteNote(current)
      stopRecording()
      setCurrent(null)
      setContent('')
      setSegments([])
      setCommits([])
    })

  const stopRecording = () => {
    micRef.current?.stop()
    micRef.current = null
    setRecording(false)
    send({ type: 'stop' })
  }

  const toggleRecording = () =>
    run(async () => {
      if (recording) {
        stopRecording()
        return
      }
      if (!current) {
        setError('Create or open a note first.')
        return
      }
      micRef.current = await startMicrophone((pcm) => send(pcm))
      send({ type: 'start', path: current })
      setRecording(true)
    })

  const revert = (commit: Commit) =>
    run(async () => {
      if (current && confirm(`Undo the change from ${commit.short}?\n\n${commit.subject}`)) {
        await api.revert(current, commit.sha)
      }
    })

  const restore = (commit: Commit) =>
    run(async () => {
      if (current && confirm(`Set the note back to how it was at ${commit.short}? Later changes are undone (but kept in history).`)) {
        await api.restore(current, commit.sha)
      }
    })

  const whisperState = status?.whisper.state ?? 'unknown'
  const llmState = status?.llm ?? 'unknown'

  return (
    <div className="app" data-view={view}>
      <header className="top-bar">
        <button className="icon-button menu-button" onClick={() => setDrawerOpen((open) => !open)} aria-label="Notes">
          ☰
        </button>
        <h1>Live Notes</h1>
        <div className="status">
          <span className={`dot ${connected ? 'ok' : 'bad'}`} title="Server connection" />
          <span className={`dot ${whisperState === 'ready' ? 'ok' : whisperState === 'loading' ? 'wait' : 'bad'}`} title={`Whisper: ${whisperState}`} />
          <span className={`dot ${llmState === 'ready' ? 'ok' : 'bad'}`} title={`LLM: ${llmState}`} />
        </div>
        <button className="button small" onClick={() => setSettingsOpen(true)}>
          Prompts
        </button>
      </header>

      <div className="tabs">
        {(['note', 'transcript', 'history'] as const).map((tab) => (
          <button key={tab} className={`tab ${view === tab ? 'active' : ''}`} onClick={() => setView(tab)}>
            {tab[0]?.toUpperCase() + tab.slice(1)}
          </button>
        ))}
      </div>

      <div className="layout">
        <aside className={`drawer ${drawerOpen ? 'open' : ''}`}>
          <FileTree notes={notes} current={current} onSelect={openNote} onCreate={createNote} />
        </aside>
        {drawerOpen && <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)} />}

        <main className="pane pane-note">
          {current ? (
            <NoteView
              key={current}
              path={current}
              content={content}
              onSave={(next) => run(() => api.saveNote(current, next))}
              onRename={renameNote}
              onDelete={deleteNote}
            />
          ) : (
            <div className="empty-state">
              <p>Create a note, then tap the mic and start talking.</p>
              <button className="button primary" onClick={() => createNote('')}>
                New note
              </button>
            </div>
          )}
        </main>

        <aside className="pane pane-side">
          <section className="pane-transcript">
            <div className="panel-header">Transcript</div>
            <TranscriptPanel segments={segments} transcribing={transcribing} />
          </section>
          <section className="pane-history">
            <div className="panel-header">History</div>
            <HistoryPanel commits={commits} onRevert={revert} onRestore={restore} />
          </section>
        </aside>
      </div>

      <footer className="mic-bar">
        <div className={`agent-status ${agent.state}`}>{error ?? describeAgent(agent)}</div>
        {error && (
          <button className="icon-button" onClick={() => setError(null)} aria-label="Dismiss">
            ×
          </button>
        )}
        <button
          className={`mic-button ${recording ? 'recording' : ''}`}
          onClick={toggleRecording}
          aria-label={recording ? 'Stop recording' : 'Start recording'}
        >
          <svg viewBox="0 0 24 24" width="28" height="28" aria-hidden="true">
            {recording ? (
              <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
            ) : (
              <path
                fill="currentColor"
                d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2Z"
              />
            )}
          </svg>
        </button>
      </footer>

      {settingsOpen && (
        <SettingsDialog path={current} instructions={instructions} onClose={() => setSettingsOpen(false)} />
      )}
    </div>
  )
}
