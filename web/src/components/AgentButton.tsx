import type { AgentState, AgentStep } from '../types'
import { BookIcon, BulbIcon, SparkIcon, WrenchIcon } from './icons'

type AgentButtonProps = {
  agent: AgentState
  step: AgentStep | null
  onClick: () => void
}

const STEP_LABEL = { thinking: 'Thinking', tool: 'Using a tool', context: 'Reading the notes' } as const

/** Opens the agent inspector. While an agent works it spins and shows what it is doing right now. */
export const AgentButton = ({ agent, step, onClick }: AgentButtonProps) => {
  const working = agent.state === 'working'
  const current = working ? (step?.step ?? 'thinking') : null
  const label = current ? `${STEP_LABEL[current]}${step?.detail ? `: ${step.detail}` : ''}` : 'Agent activity'
  return (
    <button className={`icon-button agent-button ${working ? 'working' : ''}`} onClick={onClick} aria-label={label} title={label}>
      {working && <span className="agent-spinner" aria-hidden="true" />}
      {current === 'thinking' && <BulbIcon size={20} />}
      {current === 'tool' && <WrenchIcon size={19} />}
      {current === 'context' && <BookIcon size={19} />}
      {current === null && <SparkIcon size={22} />}
    </button>
  )
}
