import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api'
import { AgentInspector } from './components/AgentInspector'
import { HistoryPanel } from './components/HistoryPanel'
import { CloseIcon, CollapseIcon, ExpandIcon, HistoryIcon, MenuIcon, MicIcon, MoreIcon, PencilIcon, SparkIcon, StopIcon, TranscriptIcon } from './components/icons'
import { NoteMenu } from './components/NoteMenu'
import { NoteTabs } from './components/NoteTabs'
import { NoteView } from './components/NoteView'
import { ProjectList } from './components/ProjectList'
import { SettingsDialog } from './components/SettingsDialog'
import { TranscriptPanel } from './components/TranscriptPanel'
import { type MicrophoneCapture, startMicrophone } from './microphone'
import type { AgentState, Commit, Focus, ProjectDetail, ProjectSummary, Segment, ServerEvent, Status } from './types'
import { useEditHighlight } from './useEditHighlight'
import { useFullscreen } from './useFullscreen'
import { useSocket } from './useSocket'
import { useWakeLock } from './useWakeLock'

type View = 'note' | 'transcript' | 'history'

const STATUS_POLL_MS = 10000
const SUMMARY_VISIBLE_MS = 4000
const LAST_PROJECT_KEY = 'notetaker:last-project'

const buzz = () => {
  // Android only; iOS Safari has no vibration API.
  if ('vibrate' in navigator) navigator.vibrate(12)
}

const readLastProject = () => {
  try {
    return localStorage.getItem(LAST_PROJECT_KEY)
  } catch {
    return null
  }
}

const rememberProject = (name: string) => {
  try {
    localStorage.setItem(LAST_PROJECT_KEY, name)
  } catch {
    // Storage can be unavailable (private mode); remembering the project is only a convenience.
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
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [current, setCurrent] = useState<string | null>(null)
  const [tabs, setTabs] = useState<string[]>([])
  const [contents, setContents] = useState<Record<string, string>>({})
  const [activeNote, setActiveNote] = useState('')
  const [focus, setFocus] = useState<Focus | null>(null)
  const [segments, setSegments] = useState<Segment[]>([])
  const [commits, setCommits] = useState<Commit[]>([])
  const [instructions, setInstructions] = useState('')
  const [agent, setAgent] = useState<AgentState>({ state: 'idle' })
  const [transcribing, setTranscribing] = useState(false)
  const [partial, setPartial] = useState('')
  const [recording, setRecording] = useState(false)
  const [status, setStatus] = useState<Status | null>(null)
  const [view, setView] = useState<View>('note')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [activityVersion, setActivityVersion] = useState(0)
  const [editing, setEditing] = useState(false)
  const [summaryVisible, setSummaryVisible] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const currentRef = useRef(current)
  const recordingRef = useRef(recording)
  const micRef = useRef<MicrophoneCapture | null>(null)
  const focusSeq = useRef(0)
  const editingRef = useRef(false)
  currentRef.current = current
  recordingRef.current = recording
  editingRef.current = editing

  const handleEvent = useCallback((event: ServerEvent) => {
    if (event.type === 'projects') {
      setProjects(event.projects)
      return
    }
    if (event.project !== undefined && event.project !== currentRef.current) return
    switch (event.type) {
      case 'note':
        setContents((previous) => ({ ...previous, [event.note]: event.content }))
        break
      case 'tabs':
        setTabs(event.notes)
        if (!editingRef.current) setActiveNote(event.active)
        break
      case 'focus':
        // Always follow the agent: switch to the note it is editing and scroll to the change.
        // While the user is typing in the editor, stay put so their draft isn't lost.
        if (editingRef.current) break
        setActiveNote(event.note)
        setFocus({ note: event.note, section: event.section, seq: ++focusSeq.current })
        break
      case 'history':
        setCommits(event.commits)
        break
      case 'segments':
        setSegments((previous) => mergeSegments(previous, event.segments))
        if (event.segments.some((segment) => segment.status === 'pending')) setPartial('')
        break
      case 'partial':
        setPartial(event.text)
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
      case 'activity':
        setActivityVersion((version) => version + 1)
        break
    }
  }, [])

  const { connected, send } = useSocket(handleEvent, () => {
    send({ type: 'subscribe', project: currentRef.current })
    if (recordingRef.current && currentRef.current) send({ type: 'start', project: currentRef.current })
  })

  const showProject = (project: ProjectDetail) => {
    setCurrent(project.name)
    setTabs(project.notes.map((note) => note.name))
    setContents(Object.fromEntries(project.notes.map((note) => [note.name, note.content])))
    setActiveNote(project.active)
    setFocus(null)
    setSegments(project.segments)
    setCommits(project.commits)
    setInstructions(project.instructions)
    setAgent({ state: 'idle' })
    setTranscribing(false)
    setPartial('')
    setEditing(false)
    setView('note')
    setDrawerOpen(false)
    rememberProject(project.name)
  }

  const openProject = useCallback(async (name: string) => {
    try {
      showProject(await api.getProject(name))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }, [])

  useEffect(() => {
    void api.listProjects().then(({ projects: loaded }) => {
      setProjects(loaded)
      const last = readLastProject()
      const initial = loaded.find((p) => p.name === last)?.name ?? loaded[0]?.name
      if (initial) void openProject(initial)
    })
  }, [openProject])

  useEffect(() => {
    send({ type: 'subscribe', project: current })
    if (recordingRef.current && current) send({ type: 'start', project: current })
  }, [current, send])

  // Tell the server which tab is in view, so speech like "add this here" lands in that note.
  useEffect(() => {
    if (current && activeNote) send({ type: 'view', project: current, note: activeNote })
  }, [current, activeNote, send])

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

  const createProject = () =>
    run(async () => {
      const name = prompt('Project name')
      if (!name?.trim()) return
      const created = await api.createProject(name)
      await openProject(created.name)
    })

  const renameProject = () =>
    run(async () => {
      if (!current) return
      const next = prompt('Rename project to', current)
      if (!next?.trim() || next === current) return
      const renamed = await api.renameProject(current, next)
      await openProject(renamed.name)
    })

  const stopRecording = () => {
    micRef.current?.stop()
    micRef.current = null
    setRecording(false)
    send({ type: 'stop' })
  }

  const deleteProjects = async (names: string[]) => {
    if (current && names.includes(current)) {
      stopRecording()
      setCurrent(null)
      setTabs([])
      setContents({})
      setActiveNote('')
      setSegments([])
      setCommits([])
      setInstructions('')
      setPartial('')
    }
    await run(() => api.deleteProjects(names))
  }

  const deleteCurrentProject = () =>
    run(async () => {
      if (current && confirm(`Delete "${current}"? Its history stays in git.`)) await deleteProjects([current])
    })

  const toggleRecording = () =>
    run(async () => {
      buzz()
      if (recording) {
        stopRecording()
        return
      }
      if (!current) {
        setError('Create or open a project first.')
        return
      }
      micRef.current = await startMicrophone((pcm) => send(pcm))
      send({ type: 'start', project: current })
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
      const question = `Set the project back to how it was at ${commit.short}? Later changes are undone (but kept in history).`
      if (current && confirm(question)) await api.restore(current, commit.sha)
    })

  const { baselineFor, fading } = useEditHighlight(contents, agent, current)
  const live = useMemo(
    () => ({
      segments: segments.filter((segment) => segment.status === 'pending'),
      workingIds: agent.state === 'working' ? agent.segments : [],
      partial,
    }),
    [segments, agent, partial],
  )

  useWakeLock(recording)
  const fullscreen = useFullscreen()

  useEffect(() => {
    if (agent.state !== 'idle' || !agent.summary) return
    setSummaryVisible(true)
    const timer = setTimeout(() => setSummaryVisible(false), SUMMARY_VISIBLE_MS)
    return () => clearTimeout(timer)
  }, [agent])

  const allReady = connected && status?.whisper.state === 'ready' && status.llm === 'ready'
  const statusClass = allReady ? 'ok' : status?.whisper.state === 'loading' ? 'wait' : 'bad'
  const pill = error ?? (agent.state === 'idle' && !summaryVisible ? '' : describeAgent(agent))
  const toggleView = (next: View) => setView((previous) => (previous === next ? 'note' : next))
  const note = tabs.includes(activeNote) ? activeNote : tabs[0]

  return (
    <div className="app" data-view={view}>
      <header className="top-bar">
        <button className="icon-button menu-button" onClick={() => setDrawerOpen(true)} aria-label="All projects">
          <MenuIcon />
        </button>
        <div className="note-title">
          <h1>{current ?? 'Live Notes'}</h1>
        </div>
        <span className={`dot ${statusClass}`} aria-label={allReady ? 'All systems ready' : 'Not ready'} />
        {current && (
          <button className="icon-button" onClick={() => setInspectorOpen(true)} aria-label="Agent activity">
            <SparkIcon size={22} />
          </button>
        )}
        {fullscreen.supported && (
          <button
            className="icon-button"
            onClick={fullscreen.toggle}
            aria-label={fullscreen.active ? 'Exit full screen' : 'Full screen'}
          >
            {fullscreen.active ? <CollapseIcon size={22} /> : <ExpandIcon size={22} />}
          </button>
        )}
        <button className="icon-button" onClick={() => setMenuOpen(true)} aria-label="Project options">
          <MoreIcon />
        </button>
      </header>

      <div className="layout">
        <aside className={`drawer ${drawerOpen ? 'open' : ''}`}>
          <ProjectList
            projects={projects}
            current={current}
            onOpen={openProject}
            onCreate={createProject}
            onDelete={deleteProjects}
          />
        </aside>
        {drawerOpen && <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)} />}

        <main className="pane pane-note">
          {current && note ? (
            <>
              <NoteTabs notes={tabs} active={note} onSelect={setActiveNote} />
              <NoteView
                key={`${current}/${note}`}
                content={contents[note] ?? ''}
                baseline={baselineFor(note)}
                fading={fading}
                live={live}
                editing={editing}
                focus={focus?.note === note ? focus : null}
                onSave={(next) => run(() => api.saveNote(current, note, next))}
                onCloseEditor={() => setEditing(false)}
              />
              {!editing && (
                <button className="edit-fab" onClick={() => setEditing(true)} aria-label="Edit this note">
                  <PencilIcon size={20} />
                  <span>Edit</span>
                </button>
              )}
            </>
          ) : (
            <div className="empty-state">
              <p>Create a project, then tap the mic and start talking.</p>
              <button className="button primary" onClick={createProject}>
                New project
              </button>
            </div>
          )}
        </main>

        {view !== 'note' && <div className="sheet-backdrop side-backdrop" onClick={() => setView('note')} />}
        <aside className="pane pane-side">
          <section className="pane-transcript">
            <div className="panel-header">
              Transcript
              <button className="icon-button sheet-close" onClick={() => setView('note')} aria-label="Close">
                <CloseIcon size={20} />
              </button>
            </div>
            <TranscriptPanel segments={segments} transcribing={transcribing} />
          </section>
          <section className="pane-history">
            <div className="panel-header">
              History
              <button className="icon-button sheet-close" onClick={() => setView('note')} aria-label="Close">
                <CloseIcon size={20} />
              </button>
            </div>
            <HistoryPanel commits={commits} onRevert={revert} onRestore={restore} />
          </section>
        </aside>
      </div>

      <footer className="mic-bar">
        {pill && (
          <button className={`agent-pill ${error ? 'error' : agent.state}`} onClick={() => setError(null)}>
            {pill}
          </button>
        )}
        <button className={`bar-button ${view === 'history' ? 'active' : ''}`} onClick={() => toggleView('history')}>
          <HistoryIcon />
          <span>History</span>
        </button>
        <button
          className={`mic-button ${recording ? 'recording' : ''}`}
          onClick={toggleRecording}
          aria-label={recording ? 'Stop recording' : 'Start recording'}
        >
          {recording ? <StopIcon size={30} /> : <MicIcon size={30} />}
        </button>
        <button className={`bar-button ${view === 'transcript' ? 'active' : ''}`} onClick={() => toggleView('transcript')}>
          <TranscriptIcon />
          <span>Transcript</span>
        </button>
      </footer>

      {menuOpen && (
        <NoteMenu
          connected={connected}
          status={status}
          hasProject={current !== null}
          onEdit={() => setEditing(true)}
          onRename={renameProject}
          onPrompts={() => setSettingsOpen(true)}
          onDelete={deleteCurrentProject}
          onClose={() => setMenuOpen(false)}
        />
      )}
      {inspectorOpen && current && (
        <AgentInspector project={current} refreshKey={activityVersion} onClose={() => setInspectorOpen(false)} />
      )}
      {settingsOpen && (
        <SettingsDialog project={current} instructions={instructions} onClose={() => setSettingsOpen(false)} />
      )}
    </div>
  )
}
