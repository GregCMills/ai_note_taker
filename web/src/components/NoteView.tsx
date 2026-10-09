import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { markAppScroll } from '../appScroll'
import { renderNote } from '../highlight'

// The rendered editor is large, so it is only downloaded the first time a note is edited.
const RichEditor = lazy(() => import('./RichEditor').then((module) => ({ default: module.RichEditor })))
import type { Focus, Segment } from '../types'

type LiveSpeech = {
  segments: Segment[]
  workingIds: number[]
  partial: string
}

type NoteViewProps = {
  content: string
  baseline: string | null
  fading: boolean
  live: LiveSpeech
  editing: boolean
  focus: Focus | null
  onSave: (content: string) => Promise<void>
  onCloseEditor: () => void
}

const NEAR_BOTTOM_PX = 160

/** Where an edit landed: the last highlighted change in the section, or null if nothing there is highlighted. */
const changedTarget = (article: HTMLElement, section: number): Element | null => {
  const anchor = article.querySelector(`[data-section="${section}"]`)
  let target: Element | null = null
  for (let node = anchor?.nextElementSibling; node && !node.matches('.section-anchor'); node = node.nextElementSibling) {
    const changed = node.matches('.changed') ? node : node.querySelector('.changed:last-of-type')
    if (changed) target = changed
  }
  return target
}

const sectionStart = (article: HTMLElement, section: number): Element | null =>
  article.querySelector(`[data-section="${section}"]`)?.nextElementSibling ?? null

const LiveTranscript = ({ segments, workingIds, partial }: LiveSpeech) => {
  if (segments.length === 0 && !partial) return null
  return (
    <div className="live-transcript" aria-live="polite">
      {segments.map((segment) => (
        <span key={segment.id} className={workingIds.includes(segment.id) ? 'live-working' : 'live-pending'}>
          {segment.text}{' '}
        </span>
      ))}
      {partial && <span className="live-partial">{partial}</span>}
      <span className="live-cursor" />
    </div>
  )
}

const SECTION_MARKER = /^<!-- §\d+ -->[ \t]*\n?/gm

/** The note as the user should edit it: without the hidden section markers (the server re-attaches them). */
const editable = (content: string) => content.replace(SECTION_MARKER, '')

type EditorProps = { content: string; onSave: (content: string) => Promise<void>; onCancel: () => void }

const Editor = ({ content, onSave, onCancel }: EditorProps) => {
  const [opened, setOpened] = useState(() => editable(content))
  const [draft, setDraft] = useState(opened)
  const [saving, setSaving] = useState(false)
  const [source, setSource] = useState(false)
  // Bumped to reload the rich editor with `draft` (it keeps its own state while mounted).
  const [loads, setLoads] = useState(0)
  const changedMeanwhile = editable(content) !== opened

  const save = async () => {
    setSaving(true)
    await onSave(draft)
    setSaving(false)
    onCancel()
  }

  const takeLatest = () => {
    setOpened(editable(content))
    setDraft(editable(content))
    setLoads((count) => count + 1)
  }

  const toggleSource = () => {
    if (source) setLoads((count) => count + 1)
    setSource(!source)
  }

  return (
    <div className="editor-view">
      <div className="editor-bar">
        <button className="button" onClick={onCancel}>
          Cancel
        </button>
        <button className={`button small ${source ? 'primary' : ''}`} onClick={toggleSource} aria-pressed={source}>
          Source
        </button>
        <button className="button primary" disabled={saving} onClick={save}>
          Save
        </button>
      </div>
      {changedMeanwhile && (
        <div className="editor-warning">
          The agent changed this note while you were editing. Saving will replace its changes.
          <button className="button small" onClick={takeLatest}>
            Load latest
          </button>
        </div>
      )}
      {source ? (
        <textarea
          className="editor"
          value={draft}
          placeholder="Type or paste into this note…"
          autoFocus
          onChange={(event) => setDraft(event.target.value)}
        />
      ) : (
        <div className="rich-editor-scroll">
          <Suspense fallback={<p className="hint editor-loading">Loading editor…</p>}>
            <RichEditor key={loads} initial={draft} onChange={setDraft} />
          </Suspense>
        </div>
      )}
    </div>
  )
}

export const NoteView = ({ content, baseline, fading, live, editing, focus, onSave, onCloseEditor }: NoteViewProps) => {
  const endRef = useRef<HTMLDivElement>(null)
  const articleRef = useRef<HTMLElement>(null)
  const html = useMemo(() => renderNote(content, baseline), [content, baseline])
  const liveLength = live.segments.reduce((total, s) => total + s.text.length, 0) + live.partial.length

  // Follow the live transcript, but only if the reader is already near the bottom.
  useEffect(() => {
    const end = endRef.current
    const scroller = end?.closest('.pane-note')
    if (!end || !scroller) return
    const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight
    if (distance < NEAR_BOTTOM_PX + end.offsetHeight) {
      markAppScroll()
      end.scrollIntoView({ block: 'end', behavior: 'smooth' })
    }
  }, [liveLength])

  // Follow the agent: bring its latest change into view (an addition at the end scrolls to the end). Later
  // re-renders only scroll if there is a highlighted change to show, never when the highlight just fades, which
  // used to jump back to the top of the section.
  const followedFocus = useRef(0)
  useEffect(() => {
    const article = articleRef.current
    if (!article || !focus || focus.section === null) return
    const changed = changedTarget(article, focus.section)
    const target = changed ?? (followedFocus.current === focus.seq ? null : sectionStart(article, focus.section))
    followedFocus.current = focus.seq
    if (!target) return
    markAppScroll()
    target.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [focus, html])

  if (editing) return <Editor content={content} onSave={onSave} onCancel={onCloseEditor} />

  return (
    <section className="note-view">
      <article ref={articleRef} className={`markdown ${fading ? 'fading' : ''}`} dangerouslySetInnerHTML={{ __html: html }} />
      <LiveTranscript {...live} />
      <div ref={endRef} className="note-end" />
    </section>
  )
}
