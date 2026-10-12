import type { Activity, ManagerData, ProjectDetail, ProjectSummary, Settings, Status } from './types'

export const signInAgain = () => location.replace('/login.html')

const request = async <T>(method: string, url: string, body?: unknown): Promise<T> => {
  const response = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (response.status === 401) signInAgain()
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(error.detail ?? response.statusText)
  }
  return response.json()
}

export const api = {
  me: () => request<{ name: string }>('GET', '/api/me'),
  logout: () => request('POST', '/api/logout'),
  status: () => request<Status>('GET', '/api/status'),
  listProjects: () => request<{ projects: ProjectSummary[] }>('GET', '/api/projects'),
  createProject: (name: string) => request<{ name: string }>('POST', '/api/projects', { name }),
  deleteProjects: (names: string[]) => request('POST', '/api/projects/delete', { names }),
  renameProject: (old: string, next: string) => request<{ name: string }>('POST', '/api/projects/rename', { old, new: next }),
  getProject: (name: string) => request<ProjectDetail>('GET', `/api/project?name=${encodeURIComponent(name)}`),
  saveNote: (project: string, note: string, content: string) => request('PUT', '/api/note', { project, note, content }),
  revert: (project: string, sha: string) => request('POST', '/api/revert', { project, sha }),
  restore: (project: string, sha: string) => request('POST', '/api/restore', { project, sha }),
  saveInstructions: (project: string, instructions: string) =>
    request('PUT', '/api/instructions', { project, instructions }),
  activity: (project: string) => request<Activity>('GET', `/api/activity?project=${encodeURIComponent(project)}`),
  manager: () => request<ManagerData>('GET', '/api/manager'),
  getSettings: () => request<Settings>('GET', '/api/settings'),
  saveSettings: (systemPrompt: string, silenceMs: number) =>
    request<Settings>('PUT', '/api/settings', { system_prompt: systemPrompt, silence_ms: silenceMs }),
}
