import { useMemo, useState } from 'react'

type Folder = { name: string; path: string; folders: Folder[]; notes: string[] }

type FileTreeProps = {
  notes: string[]
  current: string | null
  onSelect: (path: string) => void
  onCreate: (folder: string) => void
}

const buildTree = (paths: string[]): Folder => {
  const root: Folder = { name: '', path: '', folders: [], notes: [] }
  for (const path of paths) {
    const parts = path.split('/')
    let folder = root
    for (const part of parts.slice(0, -1)) {
      let child = folder.folders.find((f) => f.name === part)
      if (!child) {
        child = { name: part, path: folder.path ? `${folder.path}/${part}` : part, folders: [], notes: [] }
        folder.folders.push(child)
      }
      folder = child
    }
    folder.notes.push(path)
  }
  return root
}

const noteName = (path: string) => (path.split('/').pop() ?? path).replace(/\.md$/, '')

export const FileTree = ({ notes, current, onSelect, onCreate }: FileTreeProps) => {
  const tree = useMemo(() => buildTree(notes), [notes])
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())

  const toggle = (path: string) =>
    setCollapsed((previous) => {
      const next = new Set(previous)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  const renderFolder = (folder: Folder, depth: number) => (
    <ul className="tree-list">
      {folder.folders.map((child) => (
        <li key={child.path}>
          <div className="tree-row tree-folder" style={{ paddingLeft: depth * 14 + 8 }}>
            <button className="tree-label" onClick={() => toggle(child.path)}>
              <span className="tree-icon">{collapsed.has(child.path) ? '▸' : '▾'}</span>
              {child.name}
            </button>
            <button className="icon-button" title="New note in folder" onClick={() => onCreate(child.path)}>
              +
            </button>
          </div>
          {!collapsed.has(child.path) && renderFolder(child, depth + 1)}
        </li>
      ))}
      {folder.notes.map((path) => (
        <li key={path}>
          <button
            className={`tree-row tree-note ${path === current ? 'active' : ''}`}
            style={{ paddingLeft: depth * 14 + 22 }}
            onClick={() => onSelect(path)}
          >
            {noteName(path)}
          </button>
        </li>
      ))}
    </ul>
  )

  return (
    <nav className="file-tree">
      <div className="panel-header">
        <span>Notes</span>
        <button className="button small" onClick={() => onCreate('')}>
          New
        </button>
      </div>
      {notes.length === 0 ? <p className="empty">No notes yet.</p> : renderFolder(tree, 0)}
    </nav>
  )
}
