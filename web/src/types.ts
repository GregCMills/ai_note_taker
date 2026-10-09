export type SegmentStatus = 'pending' | 'done' | 'error'

export type Segment = {
  id: number
  text: string
  time: number
  status: SegmentStatus
  commit: string | null
}

export type Commit = {
  sha: string
  short: string
  time: number
  subject: string
  body: string
}

export type ProjectSummary = {
  name: string
  notes: string[]
}

export type ProjectDetail = {
  name: string
  notes: { name: string; content: string }[]
  active: string
  segments: Segment[]
  commits: Commit[]
  instructions: string
}

export type AgentState =
  | { state: 'idle'; summary?: string }
  | { state: 'working'; segments: number[] }
  | { state: 'error'; message: string }

export type Status = {
  whisper: { state: 'loading' | 'ready' | 'error'; error: string | null }
  llm: 'ready' | 'unavailable'
}

export type Settings = {
  system_prompt: string
  default_system_prompt: string
  silence_ms: number
}

/** Where the agent last edited: the view switches to this note and scrolls to this section. */
export type Focus = { note: string; section: number | null; seq: number }

export type ServerEvent =
  | { type: 'projects'; projects: ProjectSummary[] }
  | { type: 'note'; project: string; note: string; content: string }
  | { type: 'tabs'; project: string; notes: string[]; active: string }
  | { type: 'focus'; project: string; note: string; section: number | null }
  | { type: 'history'; project: string; commits: Commit[] }
  | { type: 'segments'; project: string; segments: Segment[] }
  | { type: 'transcribing'; project: string; active: boolean }
  | { type: 'instructions'; project: string; instructions: string }
  | { type: 'partial'; project: string; text: string }
  | { type: 'activity'; project: string }
  | ({ type: 'agent'; project?: string } & AgentState)

export type LoggedPart = {
  part_kind: 'user-prompt' | 'system-prompt' | 'text' | 'thinking' | 'tool-call' | 'tool-return' | 'retry-prompt' | string
  content?: unknown
  tool_name?: string
  args?: unknown
}

export type LoggedMessage = {
  kind: 'request' | 'response'
  timestamp?: string
  parts: LoggedPart[]
  usage?: { input_tokens: number; output_tokens: number; cache_read_tokens: number }
  finish_reason?: string
}

export type SubRun = { agent: string; purpose: string; seconds: number; messages: LoggedMessage[] }

export type ActivityTurn = {
  time: number
  seconds: number
  segments: number[]
  speech: string
  summary: string
  actions: string[]
  commit: string | null
  estimate: { instructions: number; history: number; notes: number; prompt: number }
  history_turns: number
  collapsed_sections: number
  instructions: string
  prompt: string
  messages: LoggedMessage[]
  subruns: SubRun[]
}

export type Activity = {
  context: {
    window: number
    notes: number
    note_budget: number
    history: number
    history_budget: number
    history_turns: number
    history_summary: string
  }
  turns: ActivityTurn[]
}
