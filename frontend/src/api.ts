export type Collector = {
  state: 'running' | 'stopping' | 'stopped'
  message: string
  last_run: string | null
  last_found: number
}

export type Job = {
  id: number
  title: string
  company: string
  location: string
  source: string
  url: string
  posted_at: string | null
  first_seen_at: string
  status: string
  description: string
  notes: string
  match_score: number | null
  has_cover_letter: boolean
}

export type Run = {
  id: number
  started_at: string
  finished_at: string | null
  state: string
  found_count: number
  message: string
}

export type Dashboard = {
  counts: Record<string, number>
  jobs: Job[]
  runs: Run[]
  collector: Collector
  status_labels: Record<string, string>
}

export type JobList = {
  items: Job[]
  total: number
  page: number
  pages: number
  page_size: number
}

export type JobFilters = {
  status: string
  min_match: string
  sort: string
  date_from: string
  date_to: string
  page: string
}

export type QueueItem = {
  id: number
  kind: string
  status: string
  attempts: number
  max_attempts: number
  next_run_at: string | null
  created_at: string
  updated_at: string
  last_step: string
  progress: string
  outcome: string
}

export type QueueData = { counts: Record<string, number>; items: QueueItem[] }
export type LogItem = { id: number; created_at: string; level: string; source: string; message: string }
export type UsageData = {
  provider: string
  model: string
  apify: {
    used_usd?: number
    api_limit_usd?: number
    local_limit_usd?: number
    pct_of_local?: number
    pct_of_api?: number
    cycle_label?: string
    fetched_at?: string
    error?: string
  }
  ai: {
    day_label: string
    month_label: string
    day: Record<string, number>
    month: Record<string, number>
  }
}
export type ResumeRecord = {
  language: 'pt' | 'en'
  filename: string
  status: string
  message: string
  error: string
  summary: string
  analysis_json: string
  analyzed_at: string | null
  provider: string
  model: string
}
export type ResumesData = { pt: ResumeRecord | null; en: ResumeRecord | null }
export type CopilotAsk = {
  id: number
  job_id: number
  question: string
  hint: string
  kind: string
  needs_file: boolean
  created_at: string
}
export type SettingsGroup = 'search' | 'profile' | 'ai' | 'automation' | 'smtp'
export type FormRule = { key: string; aliases: string; mode: string; value_from: string; value: string }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}) },
    ...init,
  })
  if (!response.ok) {
    if (response.status === 401) {
      window.location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`)
    }
    let message = `Falha na comunicação (${response.status}).`
    try {
      const error = await response.json() as { error?: string; notice?: string }
      if (error.error || error.notice) message = error.error || error.notice || message
    } catch { /* Keep the HTTP error. */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

export const api = {
  session: () => request<{ auth_required: boolean; expires_at: number | null }>('/api/session'),
  dashboard: () => request<Dashboard>('/api/dashboard'),
  jobs: (filters: JobFilters) => request<JobList>(`/api/jobs?${new URLSearchParams(filters)}`),
  collector: (action: 'start' | 'stop') => request<{ notice: string }>(`/api/collector/${action}`, { method: 'POST' }),
  status: (id: number, status: string) => request<{ notice: string }>(`/api/jobs/${id}/status`, { method: 'POST', body: JSON.stringify({ status }) }),
  notes: (id: number, notes: string) => request<{ notice: string }>(`/api/jobs/${id}/notes`, { method: 'POST', body: JSON.stringify({ notes }) }),
  queue: () => request<QueueData>('/api/queue'),
  logs: () => request<{ items: LogItem[] }>('/api/logs'),
  queueItem: (id: number, action: 'cancel' | 'retry') => request<{ notice: string }>(`/api/queue/${id}/${action}`, { method: 'POST' }),
  queueAction: (action: 'retry-all' | 'evaluate-new' | 'clear') => request<{ notice: string }>(`/api/queue/${action}`, { method: 'POST' }),
  clearLogs: () => request<{ notice: string }>('/api/logs/clear', { method: 'POST' }),
  usage: () => request<UsageData>('/api/usage'),
  refreshUsage: () => request<{ notice: string; usage: UsageData }>('/api/usage/refresh', { method: 'POST' }),
  assistant: (message: string, history: { role: string; content: string }[]) => request<{ answer: string }>('/api/assistant', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' },
    body: new URLSearchParams({ message, history: JSON.stringify(history) }).toString(),
  }),
  resumes: () => request<ResumesData>('/api/resumes'),
  uploadResume: (language: 'pt' | 'en', file: File) => {
    const form = new FormData()
    form.set('language', language)
    form.set('file', file)
    return request<{ ok: boolean; notice: string }>('/api/resumes/upload', { method: 'POST', headers: { Accept: 'application/json' }, body: form })
  },
  reanalyzeResume: (language: 'pt' | 'en') => request<{ ok: boolean; notice: string }>('/api/resumes/reanalyze', {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ language }).toString(),
  }),
  pendingAsk: () => request<{ ask: CopilotAsk | null }>('/api/copilot-asks/pending'),
  answerAsk: (id: number, answer: string, file: File | null) => {
    const form = new FormData()
    form.set('id', String(id))
    form.set('answer', answer)
    if (file) form.set('file', file)
    return request<{ ok: boolean; notice: string }>('/api/copilot-asks/answer', { method: 'POST', headers: { Accept: 'application/json' }, body: form })
  },
  askAction: (id: number, action: 'cancel' | 'manual') => request<{ ok: boolean; notice: string }>(`/api/copilot-asks/${action}`, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ id: String(id) }).toString(),
  }),
  settings: (group: SettingsGroup) => request<{ group: SettingsGroup; values: Record<string, string> }>(`/api/settings/${group}`),
  saveSettings: (group: SettingsGroup, values: Record<string, string>) => request<{ notice: string; values: Record<string, string> }>(`/api/settings/${group}`, {
    method: 'POST', body: JSON.stringify(values),
  }),
  generateLinkedInFilters: (values: Record<string, string>) => request<{ notice: string; values: Record<string, string> }>('/api/linkedin-filters/generate', {
    method: 'POST', body: JSON.stringify(values),
  }),
  formRules: () => request<{ items: FormRule[] }>('/api/form-rules'),
  saveFormRules: (items: FormRule[]) => request<{ notice: string }>('/api/form-rules', { method: 'POST', body: JSON.stringify({ items }) }),
  testSmtp: () => request<{ notice: string }>('/api/smtp/test', { method: 'POST' }),
}
