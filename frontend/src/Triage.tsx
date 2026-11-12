import { useCallback, useEffect, useState } from 'react'
import { api, type Job, type TriageFilters, type TriageList, type TriageTab } from './api'
import { JobCard } from './JobCard'
import { JobSort } from './JobSort'

const tabs: { key: TriageTab; label: string; hint: string; defaultSort: string }[] = [
  {
    key: 'discarded',
    label: 'Descartadas pela IA',
    hint: 'Vagas que a IA reprovou e você ainda não conferiu. Abra “Por que esta nota” para ver o que pesou e mova para revisar as que a IA errou.',
    defaultSort: 'match_desc',
  },
  {
    key: 'pending',
    label: 'Aguardando triagem',
    hint: 'Vagas coletadas que a IA ainda não avaliou (novas ou na fila). Quando a IA terminar, elas aparecem em “Vagas para revisar” ou em “Descartadas pela IA”.',
    defaultSort: 'seen_desc',
  },
]

const emptyFilters = (sort: string): TriageFilters => ({ min_match: '0', sort, date_from: '', date_to: '', q: '' })

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
  const [filters, setFilters] = useState<TriageFilters>(emptyFilters(tabs[0].defaultSort))
  const [draft, setDraft] = useState<TriageFilters>(emptyFilters(tabs[0].defaultSort))
  const [data, setData] = useState<TriageList | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => api.triage(tab, page, filters)
    .then(result => { setData(result); setError(''); if (result.page !== page) setPage(result.page) })
    .catch(err => setError((err as Error).message)), [tab, page, filters])

  useEffect(() => { void load() }, [load])

  function selectTab(next: TriageTab) {
    if (next === tab) return
    const sort = tabs.find(item => item.key === next)?.defaultSort ?? ''
    setTab(next)
    setPage(1)
    setData(null)
    setFilters(old => ({ ...old, sort }))  // período e match valem nas duas abas; a ordenação volta ao padrão da aba
    setDraft(old => ({ ...old, sort }))
  }

  function applyFilters() { setFilters({ ...draft }); setPage(1); setData(null) }
  function changeSort(sort: string) {
    setDraft(old => ({ ...old, sort }))
    setFilters(old => ({ ...old, sort }))
    setPage(1)
    setData(null)
  }
  function clearFilters() {
    const cleared = emptyFilters(active.defaultSort)
    setDraft(cleared)
    setFilters(cleared)
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
  const filtered = filters.min_match !== '0' || filters.date_from !== '' || filters.date_to !== '' || filters.q.trim() !== ''

  return <>
    <section className="filter-panel">
      <div className="filter-heading"><div><p className="eyebrow">TRIAGEM</p><h2>Vagas não selecionadas</h2></div>
        <span>{data ? `${data.total} vaga${data.total === 1 ? '' : 's'}${filtered ? ' com os filtros' : ''}` : 'Carregando…'}</span></div>
      <div className="settings-tabs" role="tablist" aria-label="Vagas não selecionadas">
        {tabs.map(item => <button role="tab" aria-selected={tab === item.key} className={tab === item.key ? 'selected' : 'secondary'} onClick={() => selectTab(item.key)} key={item.key}>
          {item.label} <span className="nav-count">{data ? data.counts[item.key] : '–'}</span>
        </button>)}
      </div>
      <p className="section-description">{active.hint}</p>
      <div className="filters">
        <label>Buscar<input type="search" placeholder="título, empresa ou local" maxLength={100} value={draft.q}
          onChange={e => setDraft(old => ({ ...old, q: e.target.value }))}
          onKeyDown={e => { if (e.key === 'Enter') applyFilters() }} /></label>
        {tab === 'discarded' && <label>Match mínimo<input type="number" min="0" max="100" value={draft.min_match} onChange={e => setDraft(old => ({ ...old, min_match: e.target.value }))} /></label>}
        <label>Encontrada de<input type="date" value={draft.date_from} onChange={e => setDraft(old => ({ ...old, date_from: e.target.value }))} /></label>
        <label>Até<input type="date" value={draft.date_to} onChange={e => setDraft(old => ({ ...old, date_to: e.target.value }))} /></label>
        <button onClick={applyFilters}>Aplicar filtros</button>
        {filtered && <button className="secondary" onClick={clearFilters}>Limpar filtros</button>}
      </div>
      <JobSort value={draft.sort} onChange={changeSort} hide={tab === 'pending' ? ['match'] : []} />
    </section>
    <section className="section triage-section">
      {tab === 'discarded' && items.length > 0 && <div className="section-head"><div /><button className="secondary" disabled={busy} onClick={() => markReviewed(items.map(job => job.id))}>Marcar esta página como conferida</button></div>}
      {error && <p className="banner error" role="alert">{error} <button onClick={() => void load()}>Tentar novamente</button></p>}
      {!data && !error ? <p className="empty">Carregando vagas…</p>
        : items.length ? <div className="job-list">{items.map(job => <div className="triage-item" key={job.id}>
          {tab === 'discarded' && discardReason(job) && <p className="triage-reason"><strong>Motivo do descarte:</strong> {discardReason(job)}</p>}
          <JobCard job={job} statusLabels={statusLabels} onChange={() => void load()} actions={tab === 'discarded' ? <>
            <button className="secondary" disabled={busy} onClick={() => moveToReview(job.id)}>Mover para revisar</button>
            <button className="secondary" disabled={busy} onClick={() => markReviewed([job.id])}>Conferida</button>
          </> : undefined} />
        </div>)}</div>
          : !error && <p className="empty">{filtered ? 'Nenhuma vaga corresponde aos filtros. Ajuste a busca, o match ou o período.' : tab === 'discarded' ? 'Nenhuma vaga descartada pela IA aguardando conferência.' : 'Nenhuma vaga aguardando triagem.'}</p>}
      {data && data.pages > 1 && <div className="pagination">
        <button className="secondary" disabled={busy || page <= 1} onClick={() => setPage(page - 1)}>Anterior</button>
        <span>Página {data.page} de {data.pages}</span>
        <button className="secondary" disabled={busy || page >= data.pages} onClick={() => setPage(page + 1)}>Próxima</button>
      </div>}
    </section>
  </>
}
