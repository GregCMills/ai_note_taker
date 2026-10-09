import { useEffect, useRef } from 'react'
import type { Segment } from '../types'

type TranscriptPanelProps = {
  segments: Segment[]
  transcribing: boolean
}

const STATUS_LABEL = { pending: 'waiting for LLM', done: 'in notes', error: 'failed' } as const

export const TranscriptPanel = ({ segments, transcribing }: TranscriptPanelProps) => {
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [segments.length, transcribing])

  return (
    <div className="side-list">
      {segments.length === 0 && !transcribing && <p className="empty">Nothing said yet. Tap the mic and talk.</p>}
      {segments.map((segment) => (
        <div key={segment.id} className={`segment ${segment.status}`}>
          <div className="segment-meta">
            <span className="segment-id">S{segment.id}</span>
            <span>{STATUS_LABEL[segment.status]}</span>
            {segment.commit && <code>{segment.commit.slice(0, 7)}</code>}
          </div>
          <p>{segment.text}</p>
        </div>
      ))}
      {transcribing && <div className="segment transcribing">Transcribing…</div>}
      <div ref={endRef} />
    </div>
  )
}
