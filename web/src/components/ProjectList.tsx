import { useState } from 'react'
import type { ProjectSummary } from '../types'

type ProjectListProps = {
  projects: ProjectSummary[]
  current: string | null
  onOpen: (name: string) => void
  onCreate: () => void
  onDelete: (names: string[]) => Promise<void>
}

const noteCount = (count: number) => (count === 1 ? '1 note' : `${count} notes`)

export const ProjectList = ({ projects, current, onOpen, onCreate, onDelete }: ProjectListProps) => {
  const [selecting, setSelecting] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const toggle = (name: string) =>
    setSelected((previous) => {
      const next = new Set(previous)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  const stopSelecting = () => {
    setSelecting(false)
    setSelected(new Set())
  }

  const allSelected = projects.length > 0 && selected.size === projects.length

  const deleteSelected = async () => {
    const names = [...selected]
    const label = names.length === 1 ? `"${names[0]}"` : `${names.length} projects`
    if (!confirm(`Delete ${label}? Their history stays in git.`)) return
    await onDelete(names)
    stopSelecting()
  }

  return (
    <nav className="project-list">
      {selecting ? (
        <div className="panel-header select-bar">
          <button className="button small" onClick={stopSelecting}>
            Cancel
          </button>
          <span className="select-count">{selected.size} selected</span>
          <button
            className="button small"
            onClick={() => setSelected(allSelected ? new Set() : new Set(projects.map((p) => p.name)))}
          >
            {allSelected ? 'None' : 'All'}
          </button>
          <button className="button small danger" disabled={selected.size === 0} onClick={deleteSelected}>
            Delete
          </button>
        </div>
      ) : (
        <div className="panel-header">
          <span>Projects</span>
          <div className="actions">
            {projects.length > 0 && (
              <button className="button small" onClick={() => setSelecting(true)}>
                Select
              </button>
            )}
            <button className="button small primary" onClick={onCreate}>
              New
            </button>
          </div>
        </div>
      )}
      {projects.length === 0 && <p className="empty">No projects yet.</p>}
      <ul className="tree-list">
        {projects.map((project) => (
          <li key={project.name}>
            <button
              className={`project-row ${project.name === current && !selecting ? 'active' : ''}`}
              onClick={() => (selecting ? toggle(project.name) : onOpen(project.name))}
              aria-pressed={selecting ? selected.has(project.name) : undefined}
            >
              {selecting && <span className={`check ${selected.has(project.name) ? 'on' : ''}`} aria-hidden="true" />}
              <span className="project-name">{project.name}</span>
              <span className="project-meta">{noteCount(project.notes.length)}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
}
