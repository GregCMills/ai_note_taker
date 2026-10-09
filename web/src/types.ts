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

export type NoteDetail = {
  path: string
  content: string
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
}

export type ServerEvent =
  | { type: 'notes'; notes: string[] }
  | { type: 'note'; path: string; content: string }
  | { type: 'history'; path: string; commits: Commit[] }
  | { type: 'segments'; path: string; segments: Segment[] }
  | { type: 'transcribing'; path: string; active: boolean }
  | { type: 'instructions'; path: string; instructions: string }
  | ({ type: 'agent'; path?: string } & AgentState)
