import { useEffect, useRef } from 'react'

type NoteTabsProps = {
  notes: string[]
  active: string
  onSelect: (note: string) => void
}

export const NoteTabs = ({ notes, active, onSelect }: NoteTabsProps) => {
  const barRef = useRef<HTMLDivElement>(null)

  // Keep the active tab visible when the agent switches to one that is scrolled out of view.
  useEffect(() => {
    barRef.current?.querySelector('.note-tab.active')?.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' })
  }, [active, notes])

  return (
    <div className="tabs-row">
      <div className="note-tabs" ref={barRef} role="tablist">
        {notes.map((note) => (
          <button
            key={note}
            role="tab"
            aria-selected={note === active}
            className={`note-tab ${note === active ? 'active' : ''}`}
            onClick={() => onSelect(note)}
          >
            {note}
          </button>
        ))}
      </div>
    </div>
  )
}
