import type { NoteDetail, Settings, Status } from './types'

const request = async <T>(method: string, url: string, body?: unknown): Promise<T> => {
  const response = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(error.detail ?? response.statusText)
  }
  return response.json()
}

const query = (path: string) => `?path=${encodeURIComponent(path)}`

export const api = {
  status: () => request<Status>('GET', '/api/status'),
  listNotes: () => request<{ notes: string[] }>('GET', '/api/notes'),
  createNote: (path: string) => request<{ path: string }>('POST', '/api/notes', { path }),
  getNote: (path: string) => request<NoteDetail>('GET', `/api/note${query(path)}`),
  saveNote: (path: string, content: string) => request('PUT', '/api/note', { path, content }),
  deleteNote: (path: string) => request('DELETE', `/api/note${query(path)}`),
  renameNote: (old: string, next: string) => request<{ path: string }>('POST', '/api/rename', { old, new: next }),
  revert: (path: string, sha: string) => request('POST', '/api/revert', { path, sha }),
  restore: (path: string, sha: string) => request('POST', '/api/restore', { path, sha }),
  saveInstructions: (path: string, instructions: string) => request('PUT', '/api/instructions', { path, instructions }),
  getSettings: () => request<Settings>('GET', '/api/settings'),
  saveSettings: (systemPrompt: string) => request<Settings>('PUT', '/api/settings', { system_prompt: systemPrompt }),
}
