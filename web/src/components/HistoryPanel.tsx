import type { Commit } from '../types'

type HistoryPanelProps = {
  commits: Commit[]
  onRevert: (commit: Commit) => void
  onRestore: (commit: Commit) => void
}

const formatTime = (seconds: number) =>
  new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })

export const HistoryPanel = ({ commits, onRevert, onRestore }: HistoryPanelProps) => (
  <div className="side-list">
    {commits.map((commit, index) => (
      <div key={commit.sha} className="commit">
        <div className="segment-meta">
          <code>{commit.short}</code>
          <span>{formatTime(commit.time)}</span>
        </div>
        <p className="commit-subject">{commit.subject}</p>
        {commit.body && <p className="commit-body">{commit.body}</p>}
        <div className="actions">
          <button className="button small" title="Undo just this change" onClick={() => onRevert(commit)}>
            Revert
          </button>
          {index > 0 && (
            <button className="button small" title="Go back to this version" onClick={() => onRestore(commit)}>
              Restore
            </button>
          )}
        </div>
      </div>
    ))}
  </div>
)
