import { useEffect, useRef } from 'react'
import type { ManagerData, ProjectSummary, Segment } from '../types'

type ManagerViewProps = {
  projects: ProjectSummary[]
  data: ManagerData | null
  live: { segments: Segment[]; workingIds: number[]; partial: string }
  onOpen: (name: string) => void
  onCreate: () => void
}

const noteCount = (count: number) => (count === 1 ? '1 note' : `${count} notes`)

/** The screen with no project open: your projects, and a conversation with the project manager. */
export const ManagerView = ({ projects, data, live, onOpen, onCreate }: ManagerViewProps) => {
  const endRef = useRef<HTMLDivElement>(null)
  const turns = [...(data?.turns ?? [])].reverse()

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [turns.length, live.segments.length, live.partial])

  return (
    <section className="manager-view">
      <div className="manager-projects">
        {projects.map((project) => (
          <button key={project.name} className="project-card" onClick={() => onOpen(project.name)}>
            <span className="project-name">{project.name}</span>
            <span className="project-meta">{noteCount(project.notes.length)}</span>
          </button>
        ))}
        <button className="project-card new" onClick={onCreate}>
          + New project
        </button>
      </div>
      {data && data.deleted.length > 0 && (
        <p className="hint manager-deleted">
          Deleted, can be brought back: {data.deleted.map((d) => d.name).join(', ')}
        </p>
      )}

      <div className="manager-chat">
        {turns.length === 0 && live.segments.length === 0 && !live.partial && (
          <p className="empty">
            Tap the mic and talk to the project manager: “make a project called Garden”, “open my story”, “delete the
            test projects”, “bring back Holiday”.
          </p>
        )}
        {turns.map((turn) => (
          <div key={turn.time} className="chat-pair">
            <div className="chat-bubble you">{turn.speech.replace(/^New speech from the user:\n/, '').replace(/^S\d+: /gm, '')}</div>
            <div className="chat-bubble manager">{turn.reply}</div>
          </div>
        ))}
        {(live.segments.length > 0 || live.partial) && (
          <div className="chat-bubble you live">
            {live.segments.map((segment) => (
              <span key={segment.id} className={live.workingIds.includes(segment.id) ? 'live-working' : ''}>
                {segment.text}{' '}
              </span>
            ))}
            {live.partial && <span className="live-partial">{live.partial}</span>}
          </div>
        )}
        <div ref={endRef} />
      </div>
    </section>
  )
}
