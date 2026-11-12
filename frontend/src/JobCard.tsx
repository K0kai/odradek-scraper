import { useState } from 'react'
import { api, type Assessment, type AssessmentItem, type Job } from './api'
import { formatDate } from './format'

const statusMark: Record<AssessmentItem['status'], [string, string]> = {
  met: ['✓', 'Atendido'], transferable: ['⇄', 'Transferível'], partial: ['½', 'Parcial'], unmet: ['✗', 'Não atendido'], unknown: ['?', 'Sem evidência'],
}

function AssessmentLine({ item }: { item: AssessmentItem }) {
  const [mark, label] = statusMark[item.status] || statusMark.unknown
  const inflated = item.realism === 'inflated'
  const hover = [item.note && `Aviso: ${item.note}`, item.resume_quote && `Currículo: “${item.resume_quote}”`, item.bridge && `Ponte: ${item.bridge}`, `Vaga: “${item.job_quote}”`].filter(Boolean).join('\n')
  return <li className={`assessment-line status-${item.status}${inflated ? ' inflated' : ''}`} title={hover}>
    <span className="assessment-mark" aria-label={label}>{mark}</span>
    <span className="assessment-text">{item.text}{inflated && <em> — desconsiderado: {item.realism_reason || 'incoerente com o próprio anúncio'}</em>}{item.bridge && !inflated && <em> — {item.bridge}</em>}</span>
  </li>
}

function AssessmentView({ assessment }: { assessment: Assessment }) {
  const musts = assessment.requirements.filter(item => item.kind === 'must')
  const nice = assessment.requirements.filter(item => item.kind !== 'must')
  return <details className="job-details assessment">
    <summary>Por que esta nota <span className={`track-badge ${assessment.track}`}>{assessment.track === 'transferable' ? 'Secundária' : 'Principal'}</span>{assessment.inflated_posting && <span className="track-badge inflated">Anúncio inflado</span>}</summary>
    <p className="section-description">Cobertura dos obrigatórios essenciais: {Math.round(assessment.must_cov * 100)}% (mínimo {Math.round(assessment.threshold * 100)}%). Cada marca foi verificada contra o texto do currículo; passe o mouse para ver as citações.</p>
    {assessment.blocks.length > 0 && <ul className="assessment-blocks">{assessment.blocks.map(block => <li key={block}>Bloqueio: {block}</li>)}</ul>}
    {musts.length > 0 && <><h4>Obrigatórios</h4><ul className="assessment-list">{musts.map(item => <AssessmentLine item={item} key={item.id} />)}</ul></>}
    {assessment.responsibilities.length > 0 && <><h4>O que a pessoa vai fazer</h4><ul className="assessment-list">{assessment.responsibilities.map(item => <AssessmentLine item={item} key={item.id} />)}</ul></>}
    {nice.length > 0 && <><h4>Desejáveis</h4><ul className="assessment-list">{nice.map(item => <AssessmentLine item={item} key={item.id} />)}</ul></>}
    {assessment.objections.length > 0 && <><h4>Revisão cética</h4><ul className="assessment-list">{assessment.objections.map((item, index) => <li key={index} className="assessment-line"><span className="assessment-mark">!</span><span className="assessment-text">{item.reason} <em>({item.severity})</em></span></li>)}</ul></>}
  </details>
}

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
      <label htmlFor={`notes-${job.id}`}>Anotações</label>
      <textarea id={`notes-${job.id}`} value={notes} onChange={event => setNotes(event.target.value)} rows={3} />
      <button className="secondary" disabled={busy || notes === job.notes} onClick={saveNotes}>Salvar anotação</button>
    </details>
    {job.assessment && <AssessmentView assessment={job.assessment} />}
    {job.has_cover_letter && <details className="job-details">
      <summary>Carta de apresentação</summary>
      <div className="cover-letter-text">{job.cover_letter}</div>
      <div className="job-actions">
        <a className="open-link" href={`/cover-letter.txt?job_id=${job.id}`}>Baixar carta em TXT</a>
        <a className="open-link" href={`/cover-letter.pdf?job_id=${job.id}`}>Baixar carta em PDF</a>
      </div>
    </details>}
    <div className="job-actions">
      {job.url && <a className="open-link" href={job.url} target="_blank" rel="noreferrer">Abrir vaga</a>}
      {job.status !== 'applied' && <button className="secondary" disabled={busy} onClick={() => changeStatus('applied')}>Marcar aplicada</button>}
      {job.status !== 'saved' && <button className="secondary" disabled={busy} onClick={() => changeStatus('saved')}>Salvar</button>}
      {job.status !== 'ignored' && <button className="secondary" disabled={busy} onClick={() => changeStatus('ignored')}>Ignorar</button>}
    </div>
    {message && <p className="inline-message" role="status">{message}</p>}
  </article>
}
