import { useCallback, useEffect, useState } from 'react'
import { api, type Dashboard, type JobFilters, type JobList } from './api'
import { AssistantView, LogsView, QueueView, ResumesView, UsageView } from './Operations'
import { CopilotAsk } from './CopilotAsk'
import { SettingsView } from './Settings'
import { RulesView } from './Rules'
import odradekIcon from '../../assets/public/odradek-icon.png'
import { formatDate } from './format'
import { JobCard } from './JobCard'
import { BulkIgnore } from './BulkIgnore'
import { isView, navigation, viewLabels, type View } from './views'

function readLocation(): { view: View; filters: JobFilters } {
  const q = new URLSearchParams(location.search)
  const requestedView = q.get('view')
  return {
    view: isView(requestedView) ? requestedView : 'overview',
    filters: {
      status: q.get('status') || 'worth',
      min_match: q.get('min_match') || '0',
      sort: q.get('sort') || 'match_desc',
      date_from: q.get('date_from') || '',
      date_to: q.get('date_to') || '',
      page: q.get('page') || '1',
    },
  }
}

function useVisiblePolling(callback: () => void, delay: number, key: string) {
  useEffect(() => {
    const tick = () => { if (!document.hidden) callback() }
    tick()
    const interval = window.setInterval(tick, delay)
    document.addEventListener('visibilitychange', tick)
    return () => { window.clearInterval(interval); document.removeEventListener('visibilitychange', tick) }
  }, [callback, delay, key])
}

export function App() {
  const initial = readLocation()
  const [view, setView] = useState<View>(initial.view)
  const [filters, setFilters] = useState<JobFilters>(initial.filters)
  const [draft, setDraft] = useState<JobFilters>(initial.filters)
  const [dashboard, setDashboard] = useState<Dashboard | null>(null)
  const [jobs, setJobs] = useState<JobList | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [authRequired, setAuthRequired] = useState(false)
  const [revision, setRevision] = useState(0)
  const statusLabels = dashboard?.status_labels || {}

  useEffect(() => { api.session().then(data => setAuthRequired(data.auth_required)).catch(() => {}) }, [])

  useEffect(() => {
    const q = new URLSearchParams(filters)
    q.set('view', view)
    history.replaceState(null, '', `${location.pathname}?${q}`)
  }, [view, filters])

  useEffect(() => {
    const onBack = () => { const next = readLocation(); setView(next.view); setFilters(next.filters); setDraft(next.filters) }
    window.addEventListener('popstate', onBack)
    return () => window.removeEventListener('popstate', onBack)
  }, [])

  const refreshDashboard = useCallback(() => {
    api.dashboard().then(data => { setDashboard(data); setError('') }).catch(e => setError(e.message))
  }, [revision])
  const refreshJobs = useCallback(() => {
    if (view !== 'jobs') return
    api.jobs(filters).then(data => { setJobs(data); setError('') }).catch(e => setError(e.message))
  }, [view, JSON.stringify(filters), revision])

  useVisiblePolling(refreshDashboard, 6000, 'dashboard')
  useVisiblePolling(refreshJobs, 10000, JSON.stringify(filters))

  function reload() { setRevision(value => value + 1) }
  function show(next: View) {
    if (next !== view) {
      const q = new URLSearchParams(filters)
      q.set('view', next)
      history.pushState(null, '', `${location.pathname}?${q}`)
    }
    setView(next)
    setNotice('')
    setError('')
  }
  function notify(message: string, isError = false) { if (isError) setError(message); else { setNotice(message); setError('') } }
  function updateDraft(name: keyof JobFilters, value: string) { setDraft(old => ({ ...old, [name]: value })) }
  function applyFilters() { setFilters({ ...draft, page: '1' }); setJobs(null) }

  async function collectorAction(action: 'start' | 'stop') {
    setBusy(true); setNotice('')
    try { const result = await api.collector(action); setNotice(result.notice); reload() }
    catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><img src={odradekIcon} alt="" width="30" height="30" /></span><div><strong>Odradek</strong><small>coleta · triagem · candidaturas</small></div></div>
      <nav aria-label="Navegação principal" className="nav">
        <span className="nav-label">OPERAR</span>
        {navigation.map(item => <button key={item} className={view === item ? 'nav-item selected' : 'nav-item'} onClick={() => show(item)}>
          {viewLabels[item]} {item === 'jobs' && <span className="nav-count">{dashboard?.counts.worth || 0}</span>}
        </button>)}
      </nav>
      <div className="sidebar-bottom"><span className={`state-dot ${dashboard?.collector.state || ''}`} />{dashboard?.collector.state === 'running' ? 'Coleta em execução' : dashboard?.collector.state === 'stopping' ? 'Parando coleta' : 'Coleta parada'}</div>
    </aside>
    <main className="main">
      <header className="topbar"><div><p className="eyebrow">PAINEL DE OPERAÇÃO</p><h1>{viewLabels[view]}</h1></div><div className="topbar-actions"><button className="secondary" onClick={reload}>Atualizar</button>{authRequired && <form method="post" action="/logout"><button className="secondary" type="submit">Sair</button></form>}</div></header>
      {error && <div className="banner error" role="alert">{error} <button onClick={reload}>Tentar novamente</button></div>}
      {notice && <div className="banner" role="status">{notice}</div>}
      <CopilotAsk notify={notify} />
      {view === 'overview' ? <>
        <section className="hero"><div><p className="eyebrow">COLETA</p><h2>{dashboard?.collector.state === 'running' ? 'Buscando novas oportunidades' : 'Pronto para uma nova busca'}</h2><p>{dashboard?.collector.message || 'Carregando estado da coleta…'}</p></div><div className="hero-actions"><button disabled={busy || dashboard?.collector.state === 'running'} onClick={() => collectorAction('start')}>Iniciar bot</button><button className="secondary" disabled={busy || dashboard?.collector.state === 'stopped'} onClick={() => collectorAction('stop')}>Parar bot</button></div></section>
        <div className="stats">{([['new', 'Novas'], ['worth', 'Para revisar'], ['saved', 'Salvas'], ['applied', 'Aplicadas']] as const).map(([key, label]) => <div className="stat" key={key}><span>{label}</span><strong>{dashboard ? dashboard.counts[key] ?? 0 : '–'}</strong></div>)}</div>
        <section className="section"><div className="section-head"><div><p className="eyebrow">ÚLTIMAS CAPTURAS</p><h2>Vagas recentes</h2></div><button className="text-link" onClick={() => show('jobs')}>Ver vagas para revisar →</button></div>
          {!dashboard ? <p className="empty">Carregando vagas…</p> : dashboard.jobs.length ? <div className="compact-jobs">{dashboard.jobs.slice(0, 8).map(job => <div className="compact-job" key={job.id}><div>{job.url ? <a href={job.url} target="_blank" rel="noreferrer">{job.title} ↗</a> : <strong>{job.title}</strong>}<small>{job.company || 'Empresa não informada'} · {job.location || 'Local não informado'}</small></div><span>{statusLabels[job.status] || job.status}</span></div>)}</div> : <p className="empty">Nenhuma vaga ainda. Inicie uma busca para preencher o painel.</p>}
        </section>
        <section className="section"><div className="section-head"><div><p className="eyebrow">ATIVIDADE</p><h2>Execuções recentes</h2></div></div>{dashboard?.runs.length ? <ul className="runs">{dashboard.runs.map(run => <li key={run.id}><time>{formatDate(run.started_at, true)}</time><span>{run.message || run.state}</span></li>)}</ul> : <p className="empty">Nenhuma execução registrada.</p>}</section>
      </> : view === 'jobs' ? <>
        <section className="filter-panel"><div className="filter-heading"><div><p className="eyebrow">TRIAGEM</p><h2>Encontre o que importa</h2></div><span>{jobs ? `${jobs.total} vaga${jobs.total === 1 ? '' : 's'}` : 'Carregando…'}</span></div>
          <div className="filters"><label>Status<select value={draft.status} onChange={e => updateDraft('status', e.target.value)}>{Object.entries(statusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><label>Match mínimo<input type="number" min="0" max="100" value={draft.min_match} onChange={e => updateDraft('min_match', e.target.value)} /></label><label>Ordenar por<select value={draft.sort} onChange={e => updateDraft('sort', e.target.value)}><option value="match_desc">Maior match</option><option value="match_asc">Menor match</option><option value="seen_desc">Encontrada: mais recente</option><option value="seen_asc">Encontrada: mais antiga</option><option value="posted_desc">Publicada: mais recente</option><option value="posted_asc">Publicada: mais antiga</option></select></label><label>Encontrada de<input type="date" value={draft.date_from} onChange={e => updateDraft('date_from', e.target.value)} /></label><label>Até<input type="date" value={draft.date_to} onChange={e => updateDraft('date_to', e.target.value)} /></label><button onClick={applyFilters}>Aplicar filtros</button></div>
        </section>
        <BulkIgnore onDone={reload} notify={notify} />
        {!jobs ? <p className="empty">Carregando vagas…</p> : jobs.items.length ? <div className="job-list">{jobs.items.map(job => <JobCard job={job} statusLabels={statusLabels} onChange={reload} key={job.id} />)}</div> : <p className="empty">Nenhuma vaga corresponde aos filtros. Ajuste o status, o match ou o período.</p>}
        {jobs && jobs.pages > 1 && <div className="pagination"><button className="secondary" disabled={jobs.page <= 1} onClick={() => setFilters(old => ({ ...old, page: String(jobs.page - 1) }))}>Anterior</button><span>Página {jobs.page} de {jobs.pages}</span><button className="secondary" disabled={jobs.page >= jobs.pages} onClick={() => setFilters(old => ({ ...old, page: String(jobs.page + 1) }))}>Próxima</button></div>}
      </> : view === 'queue' ? <QueueView notify={notify} /> : view === 'logs' ? <LogsView notify={notify} /> : view === 'usage' ? <UsageView notify={notify} /> : view === 'resumes' ? <ResumesView notify={notify} /> : view === 'assistant' ? <AssistantView notify={notify} /> : null}
      <div hidden={view !== 'settings'}><SettingsView notify={notify} /></div>
      <div hidden={view !== 'rules'}><RulesView notify={notify} /></div>
    </main>
  </div>
}
