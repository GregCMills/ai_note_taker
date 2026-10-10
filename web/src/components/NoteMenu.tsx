import type { Status } from '../types'

type NoteMenuProps = {
  connected: boolean
  status: Status | null
  hasProject: boolean
  onEdit: () => void
  onRename: () => void
  onPrompts: () => void
  onDelete: () => void
  onClose: () => void
}

const StatusLine = ({ label, ok, detail }: { label: string; ok: boolean; detail: string }) => (
  <div className="status-line">
    <span className={`dot ${ok ? 'ok' : 'bad'}`} />
    <span>{label}</span>
    <span className="status-detail">{detail}</span>
  </div>
)

export const NoteMenu = ({ connected, status, hasProject, onEdit, onRename, onPrompts, onDelete, onClose }: NoteMenuProps) => {
  const choose = (action: () => void) => () => {
    onClose()
    action()
  }

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="menu-sheet" role="menu" onClick={(event) => event.stopPropagation()}>
        <div className="sheet-grip" />
        {hasProject && (
          <>
            <button className="menu-item" role="menuitem" onClick={choose(onEdit)}>
              Edit this note
            </button>
            <button className="menu-item" role="menuitem" onClick={choose(onRename)}>
              Rename project
            </button>
          </>
        )}
        <button className="menu-item" role="menuitem" onClick={choose(onPrompts)}>
          Settings
        </button>
        {hasProject && (
          <button className="menu-item danger" role="menuitem" onClick={choose(onDelete)}>
            Delete project
          </button>
        )}
        <div className="menu-status">
          <StatusLine label="Server" ok={connected} detail={connected ? 'connected' : 'reconnecting'} />
          <StatusLine
            label={`Speech-to-text${status?.whisper.engine ? ` (${status.whisper.engine})` : ''}`}
            ok={status?.whisper.state === 'ready'}
            detail={status?.whisper.error ?? status?.whisper.state ?? 'unknown'}
          />
          <StatusLine label="LLM" ok={status?.llm === 'ready'} detail={status?.llm ?? 'unknown'} />
        </div>
      </div>
    </div>
  )
}
