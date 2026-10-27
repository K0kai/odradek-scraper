import { useState } from 'react'
import { api, type Job } from './api'
import { formatDate } from './format'

export function JobCard({ job, statusLabels, onChange }: {
  job: Job
  statusLabels: Record<string, string>
  onChange: () => void
}) {
  const [notes, setNotes] = useState(job.notes || '')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  async function changeStatus(status: string) {
    setBusy(true)
    setMessage('')
    try { await api.status(job.id, status); onChange() }
    catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }

  async function saveNotes() {
    setBusy(true)
    setMessage('')
    try { const result = await api.notes(job.id, notes); setMessage(result.notice); onChange() }
    catch (error) { setMessage((error as Error).message) }
    finally { setBusy(false) }
  }

  return <article className="job-card">
    <div className="job-card-top">
      <div className="score">{job.match_score === null ? '•' : `${job.match_score}%`}<small>match</small></div>
      <div className="job-main">
        {job.url ? <a className="job-title" href={job.url} target="_blank" rel="noreferrer">{job.title}<span aria-hidden="true"> ↗</span></a> : <strong className="job-title">{job.title}</strong>}
        <p className="job-company">{job.company || 'Empresa não informada'} <span>· {job.location || 'Local não informado'}</span></p>
      </div>
      <span className="status-pill">{statusLabels[job.status] || job.status}</span>
    </div>
    <div className="job-meta"><span>{job.source}</span><span>Encontrada {formatDate(job.first_seen_at)}</span><span>Publicada {formatDate(job.posted_at)}</span></div>
    <details className="job-details"><summary>Descrição e anotações</summary>
      <p className="description">{job.description || 'Sem descrição.'}</p>
      {job.has_cover_letter && <a href={`/cover-letter.pdf?job_id=${job.id}`}>Baixar carta em PDF</a>}
      <label htmlFor={`notes-${job.id}`}>Anotações</label>
      <textarea id={`notes-${job.id}`} value={notes} onChange={event => setNotes(event.target.value)} rows={3} />
      <button className="secondary" disabled={busy || notes === job.notes} onClick={saveNotes}>Salvar anotação</button>
    </details>
    <div className="job-actions">
      {job.url && <a className="open-link" href={job.url} target="_blank" rel="noreferrer">Abrir vaga</a>}
      {job.status !== 'applied' && <button className="secondary" disabled={busy} onClick={() => changeStatus('applied')}>Marcar aplicada</button>}
      {job.status !== 'saved' && <button className="secondary" disabled={busy} onClick={() => changeStatus('saved')}>Salvar</button>}
      {job.status !== 'ignored' && <button className="secondary" disabled={busy} onClick={() => changeStatus('ignored')}>Ignorar</button>}
    </div>
    {message && <p className="inline-message" role="status">{message}</p>}
  </article>
}
