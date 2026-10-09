import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Activity, ActivityTurn, LoggedMessage, LoggedPart } from '../types'
import { CloseIcon } from './icons'

type AgentInspectorProps = {
  project: string
  refreshKey: number
  onClose: () => void
}

const tokens = (count: number) => (count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count))

const asText = (value: unknown) => (typeof value === 'string' ? value : JSON.stringify(value, null, 2))

const formatArgs = (args: unknown) => {
  if (typeof args !== 'string') return JSON.stringify(args, null, 2)
  try {
    return JSON.stringify(JSON.parse(args), null, 2)
  } catch {
    return args
  }
}

const Meter = ({ label, used, limit, note }: { label: string; used: number; limit: number; note?: string }) => (
  <div className="meter">
    <div className="meter-row">
      <span>{label}</span>
      <span className="meter-value">
        {tokens(used)} / {tokens(limit)}
      </span>
    </div>
    <div className="meter-track">
      <div className={`meter-fill ${used > limit ? 'over' : ''}`} style={{ width: `${Math.min(100, (used / limit) * 100)}%` }} />
    </div>
    {note && <div className="meter-note">{note}</div>}
  </div>
)

const PartView = ({ part }: { part: LoggedPart }) => {
  switch (part.part_kind) {
    case 'thinking':
      return (
        <div className="log-part thinking">
          <div className="log-label">Reasoning</div>
          <pre>{asText(part.content)}</pre>
        </div>
      )
    case 'text':
      return (
        <div className="log-part said">
          <div className="log-label">Said</div>
          <pre>{asText(part.content)}</pre>
        </div>
      )
    case 'tool-call':
      return (
        <div className="log-part call">
          <div className="log-label">Tool call · {part.tool_name}</div>
          <pre>{formatArgs(part.args)}</pre>
        </div>
      )
    case 'tool-return':
      return (
        <div className="log-part result">
          <div className="log-label">Result · {part.tool_name}</div>
          <pre>{asText(part.content)}</pre>
        </div>
      )
    case 'retry-prompt':
      return (
        <div className="log-part retry">
          <div className="log-label">Retry requested</div>
          <pre>{asText(part.content)}</pre>
        </div>
      )
    case 'user-prompt':
      return (
        <div className="log-part prompt">
          <div className="log-label">Message</div>
          <pre>{asText(part.content)}</pre>
        </div>
      )
    default:
      return null
  }
}

const Steps = ({ messages, skipPrompt }: { messages: LoggedMessage[]; skipPrompt: boolean }) => {
  let step = 0
  return (
    <div className="log-steps">
      {messages.map((message, index) => {
        const parts = message.parts.filter((part) => !(skipPrompt && part.part_kind === 'user-prompt'))
        if (message.kind === 'response') step += 1
        return (
          <div key={index} className={`log-message ${message.kind}`}>
            {message.kind === 'response' && (
              <div className="log-step">
                Step {step}
                {message.usage && (
                  <span>
                    {' '}
                    · in {tokens(message.usage.input_tokens)}
                    {message.usage.cache_read_tokens > 0 && ` (${tokens(message.usage.cache_read_tokens)} cached)`} · out{' '}
                    {tokens(message.usage.output_tokens)}
                  </span>
                )}
              </div>
            )}
            {parts.map((part, partIndex) => (
              <PartView key={partIndex} part={part} />
            ))}
          </div>
        )
      })}
    </div>
  )
}

const TurnCard = ({ turn, open }: { turn: ActivityTurn; open: boolean }) => {
  const responses = turn.messages.filter((message) => message.kind === 'response')
  const input = Math.max(0, ...responses.map((message) => message.usage?.input_tokens ?? 0))
  const output = responses.reduce((total, message) => total + (message.usage?.output_tokens ?? 0), 0)
  const time = new Date(turn.time * 1000).toLocaleTimeString(undefined, { timeStyle: 'short' })

  return (
    <details className="turn-card" open={open}>
      <summary>
        <div className="turn-head">
          <span className="segment-id">{turn.segments.map((id) => `S${id}`).join(' ')}</span>
          <span>{time}</span>
          <span>{turn.seconds}s</span>
          <span>{responses.length} steps</span>
          <span>
            {tokens(input)} in · {tokens(output)} out
          </span>
        </div>
        <div className="turn-summary">{turn.summary || turn.actions.join('; ') || 'no change'}</div>
      </summary>
      <div className="turn-body">
        <div className="log-part speech">
          <div className="log-label">Speech</div>
          <pre>{turn.speech}</pre>
        </div>
        <div className="turn-estimate">
          Context estimate: rules {tokens(turn.estimate.instructions)} · history {tokens(turn.estimate.history)} (
          {turn.history_turns} turns) · notes {tokens(turn.estimate.notes)} · rest of message{' '}
          {tokens(Math.max(0, turn.estimate.prompt - turn.estimate.notes))}
          {turn.collapsed_sections > 0 && ` · ${turn.collapsed_sections} sections collapsed`}
        </div>
        <details className="log-fold">
          <summary>System prompt ({tokens(turn.estimate.instructions)})</summary>
          <pre>{turn.instructions}</pre>
        </details>
        <details className="log-fold">
          <summary>Message sent ({tokens(turn.estimate.prompt)})</summary>
          <pre>{turn.prompt}</pre>
        </details>
        <Steps messages={turn.messages} skipPrompt />
        {turn.subruns.map((run, index) => (
          <details key={index} className="log-fold subrun">
            <summary>
              {run.agent} · {run.purpose} · {run.seconds}s
            </summary>
            <Steps messages={run.messages} skipPrompt={false} />
          </details>
        ))}
      </div>
    </details>
  )
}

export const AgentInspector = ({ project, refreshKey, onClose }: AgentInspectorProps) => {
  const [activity, setActivity] = useState<Activity | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .activity(project)
      .then(setActivity)
      .catch((caught) => setError(caught instanceof Error ? caught.message : String(caught)))
  }, [project, refreshKey])

  const latest = activity?.turns[0]
  const lastInput = latest
    ? Math.max(0, ...latest.messages.map((message) => message.usage?.input_tokens ?? 0))
    : 0

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog inspector" onClick={(event) => event.stopPropagation()}>
        <div className="inspector-head">
          <h2>Agent activity</h2>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            <CloseIcon size={20} />
          </button>
        </div>
        {error && <p className="hint">{error}</p>}
        {activity && (
          <>
            <div className="context-card">
              <Meter
                label="Last request"
                used={lastInput}
                limit={activity.context.window}
                note="Tokens the scribe's biggest request sent, out of the model's context window."
              />
              <Meter
                label="Notes"
                used={activity.context.notes}
                limit={activity.context.note_budget}
                note="Estimate. Past the budget, older sections are shown to the scribe as summaries."
              />
              <Meter
                label={`Chat history (${activity.context.history_turns} turns)`}
                used={activity.context.history}
                limit={activity.context.history_budget}
                note="Estimate. Past the budget, older turns are folded into a summary."
              />
              {activity.context.history_summary && (
                <details className="log-fold">
                  <summary>Summary of older turns</summary>
                  <pre>{activity.context.history_summary}</pre>
                </details>
              )}
            </div>
            {activity.turns.length === 0 && <p className="hint">No agent turns yet. Tap the mic and talk.</p>}
            {activity.turns.map((turn, index) => (
              <TurnCard key={`${turn.time}`} turn={turn} open={index === 0} />
            ))}
          </>
        )}
      </div>
    </div>
  )
}
