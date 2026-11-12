import { useCallback, useEffect, useState } from 'react'
import { api, type Job, type TriageList, type TriageTab } from './api'
import { JobCard } from './JobCard'

const tabs: { key: TriageTab; label: string; hint: string }[] = [
  {
    key: 'discarded',
    label: 'Descartadas pela IA',
    hint: 'Vagas que a IA reprovou e você ainda não conferiu, da maior nota para a menor. Abra “Por que esta nota” para ver o que pesou e mova para revisar as que a IA errou.',
  },
  {
    key: 'pending',
    label: 'Aguardando triagem',
    hint: 'Vagas coletadas que a IA ainda não avaliou (novas ou na fila). Quando a IA terminar, elas aparecem em “Vagas para revisar” ou em “Descartadas pela IA”.',
  },
]

function discardReason(job: Job): string {
  const note = (job.notes || '').trim()
  return (note.startsWith('IA:') ? note.slice(3).trim() : note).slice(0, 600)
}

export function TriageView({ statusLabels, notify }: {
  statusLabels: Record<string, string>
  notify: (message: string, error?: boolean) => void
}) {
  const [tab, setTab] = useState<TriageTab>('discarded')
  const [page, setPage] = useState(1)
  const [data, setData] = useState<TriageList | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => api.triage(tab, page)
    .then(result => { setData(result); setError(''); if (result.page !== page) setPage(result.page) })
    .catch(err => setError((err as Error).message)), [tab, page])

  useEffect(() => { void load() }, [load])

  function selectTab(next: TriageTab) {
    if (next === tab) return
    setTab(next)
    setPage(1)
    setData(null)
  }

  async function run(action: () => Promise<string>) {
    setBusy(true)
    try { notify(await action()); await load() }
    catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }

  const markReviewed = (ids: number[]) => run(async () => (await api.markReviewed(ids)).notice)
  const moveToReview = (id: number) => run(async () => (await api.status(id, 'worth')).notice)
  const active = tabs.find(item => item.key === tab) ?? tabs[0]
  const items = data?.items ?? []

  return <section className="section triage-section">
    <div className="section-head"><div><p className="eyebrow">TRIAGEM</p><h2>Vagas não selecionadas</h2></div>
      {tab === 'discarded' && items.length > 0 && <button className="secondary" disabled={busy} onClick={() => markReviewed(items.map(job => job.id))}>Marcar esta página como conferida</button>}
    </div>
    <div className="settings-tabs" role="tablist" aria-label="Vagas não selecionadas">
      {tabs.map(item => <button role="tab" aria-selected={tab === item.key} className={tab === item.key ? 'selected' : 'secondary'} onClick={() => selectTab(item.key)} key={item.key}>
        {item.label} <span className="nav-count">{data ? data.counts[item.key] : '–'}</span>
      </button>)}
    </div>
    <p className="section-description">{active.hint}</p>
    {error && <p className="banner error" role="alert">{error} <button onClick={() => void load()}>Tentar novamente</button></p>}
    {!data && !error ? <p className="empty">Carregando vagas…</p>
      : items.length ? <div className="job-list">{items.map(job => <div className="triage-item" key={job.id}>
        {tab === 'discarded' && discardReason(job) && <p className="triage-reason"><strong>Motivo do descarte:</strong> {discardReason(job)}</p>}
        <JobCard job={job} statusLabels={statusLabels} onChange={() => void load()} actions={tab === 'discarded' ? <>
          <button className="secondary" disabled={busy} onClick={() => moveToReview(job.id)}>Mover para revisar</button>
          <button className="secondary" disabled={busy} onClick={() => markReviewed([job.id])}>Conferida</button>
        </> : undefined} />
      </div>)}</div>
        : !error && <p className="empty">{tab === 'discarded' ? 'Nenhuma vaga descartada pela IA aguardando conferência.' : 'Nenhuma vaga aguardando triagem.'}</p>}
    {data && data.pages > 1 && <div className="pager">
      <button className="secondary" disabled={busy || page <= 1} onClick={() => setPage(page - 1)}>Anterior</button>
      <span>Página {data.page} de {data.pages} · {data.total} vagas</span>
      <button className="secondary" disabled={busy || page >= data.pages} onClick={() => setPage(page + 1)}>Próxima</button>
    </div>}
  </section>
}
