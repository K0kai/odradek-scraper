import { useState } from 'react'
import { api, type BulkIgnoreFilters } from './api'

const emptyFilters: BulkIgnoreFilters = {
  country: '', found_from: '', found_to: '', posted_from: '', posted_to: '',
  match_min: '', match_max: '', all: false,
}

export function BulkIgnore({ onDone, notify }: { onDone: () => void; notify: (message: string, isError?: boolean) => void }) {
  const [filters, setFilters] = useState<BulkIgnoreFilters>(emptyFilters)
  const [preview, setPreview] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)

  function change<K extends keyof BulkIgnoreFilters>(key: K, value: BulkIgnoreFilters[K]) {
    setFilters(old => ({ ...old, [key]: value }))
    setPreview(null)
  }

  async function run(action: 'preview' | 'apply') {
    setBusy(true)
    try {
      if (action === 'preview') {
        setPreview((await api.previewBulkIgnore(filters)).count)
      } else {
        if (preview === null || preview < 1) return
        const result = await api.applyBulkIgnore(filters, preview)
        notify(result.notice)
        setPreview(null)
        setFilters(emptyFilters)
        onDone()
      }
    } catch (error) {
      notify((error as Error).message, true)
      setPreview(null)
    } finally {
      setBusy(false)
    }
  }

  return <details className="bulk-ignore">
    <summary>Ignorar vagas em revisão por critérios</summary>
    <p>Afeta somente vagas em “Para revisar”. País procura o texto informado na localização da vaga.</p>
    <div className="bulk-ignore-grid">
      <label>País na localização<input value={filters.country} disabled={filters.all || busy} placeholder="Ex.: Brazil" onChange={e => change('country', e.target.value)} /></label>
      <label>Encontrada de<input type="date" value={filters.found_from} disabled={filters.all || busy} onChange={e => change('found_from', e.target.value)} /></label>
      <label>Encontrada até<input type="date" value={filters.found_to} disabled={filters.all || busy} onChange={e => change('found_to', e.target.value)} /></label>
      <label>Publicada de<input type="date" value={filters.posted_from} disabled={filters.all || busy} onChange={e => change('posted_from', e.target.value)} /></label>
      <label>Publicada até<input type="date" value={filters.posted_to} disabled={filters.all || busy} onChange={e => change('posted_to', e.target.value)} /></label>
      <label>Match mínimo<input type="number" min="0" max="100" value={filters.match_min} disabled={filters.all || busy} onChange={e => change('match_min', e.target.value)} /></label>
      <label>Match máximo<input type="number" min="0" max="100" value={filters.match_max} disabled={filters.all || busy} onChange={e => change('match_max', e.target.value)} /></label>
    </div>
    <label className="bulk-ignore-all"><input type="checkbox" checked={filters.all} disabled={busy} onChange={e => {
      setFilters(e.target.checked ? { ...emptyFilters, all: true } : emptyFilters)
      setPreview(null)
    }} /> Ignorar todas as vagas em revisão</label>
    <div className="bulk-ignore-actions">
      <button className="secondary" disabled={busy} onClick={() => run('preview')}>Ver quantidade</button>
      {preview !== null && <span role="status">{preview} vaga{preview === 1 ? '' : 's'} corresponde{preview === 1 ? '' : 'm'} aos critérios.</span>}
      {preview !== null && preview > 0 && <button className="bulk-ignore-confirm" disabled={busy} onClick={() => run('apply')}>
        Confirmar e ignorar {preview} vaga{preview === 1 ? '' : 's'}
      </button>}
    </div>
  </details>
}
