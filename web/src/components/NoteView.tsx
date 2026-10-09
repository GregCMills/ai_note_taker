import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { useMemo, useState } from 'react'

type NoteViewProps = {
  path: string
  content: string
  onSave: (content: string) => Promise<void>
  onRename: () => void
  onDelete: () => void
}

export const NoteView = ({ path, content, onSave, onRename, onDelete }: NoteViewProps) => {
  const [draft, setDraft] = useState<string | null>(null)
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(content, { async: false })), [content])

  const save = async () => {
    if (draft === null) return
    await onSave(draft)
    setDraft(null)
  }

  return (
    <section className="note-view">
      <div className="panel-header">
        <span className="note-path" title={path}>
          {path}
        </span>
        <div className="actions">
          {draft === null ? (
            <>
              <button className="button small" onClick={() => setDraft(content)}>
                Edit
              </button>
              <button className="button small" onClick={onRename}>
                Rename
              </button>
              <button className="button small danger" onClick={onDelete}>
                Delete
              </button>
            </>
          ) : (
            <>
              <button className="button small primary" onClick={save}>
                Save
              </button>
              <button className="button small" onClick={() => setDraft(null)}>
                Cancel
              </button>
            </>
          )}
        </div>
      </div>
      {draft === null ? (
        <article className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <textarea className="editor" value={draft} onChange={(event) => setDraft(event.target.value)} />
      )}
    </section>
  )
}
