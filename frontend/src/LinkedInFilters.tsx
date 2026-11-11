import { useState } from 'react'
import type { FilterMetric } from './api'

type Anchor = { id: string; title: string; employer: string; months: number }

type Filter = {
  id: string
  name: string
  enabled: boolean
  keywords: string[]
  experience_levels: number[]
  reason?: string
  source?: string
  slot?: string
  sector?: string
  resilience_reason?: string
  evidence?: Record<string, string[]>
  anchor_roles?: Anchor[]
  career_months?: number
  generated_at?: string
}

const experience: [number, string][] = [[1, 'Estágio'], [2, 'Júnior'], [3, 'Associado'], [4, 'Pleno / sênior'], [5, 'Diretor'], [6, 'Executivo']]

function months(total: number) {
  const years = Math.floor(total / 12)
  const rest = total % 12
  return [years ? `${years} ${years === 1 ? 'ano' : 'anos'}` : '', rest ? `${rest} ${rest === 1 ? 'mês' : 'meses'}` : ''].filter(Boolean).join(' e ') || '0 meses'
}

function MetricLine({ metric }: { metric?: FilterMetric }) {
  if (!metric) return <p className="filter-metrics">Ainda sem vagas coletadas por este filtro.</p>
  const rate = metric.approval_rate === null ? 'sem triagem ainda' : `${Math.round(metric.approval_rate * 100)}% aprovadas`
  return <p className={`filter-metrics${metric.low_precision ? ' warn' : ''}`}>
    {metric.collected} coletadas ({metric.last_7d} em 7 dias, {metric.last_30d} em 30) · {metric.triaged} triadas · {metric.approved} aprovadas ({rate}) · {metric.ignored_by_user} aprovadas depois ignoradas por você
    {metric.low_precision && ' — precisão baixa: considere revisar este filtro.'}
  </p>
}

function FilterEditor({ item, slot, metric, scope, change }: { item: Filter; slot: 'main' | 'secondary'; metric?: FilterMetric; scope: string[]; change: (next: Filter) => void }) {
  const [first, setFirst] = useState(item.keywords[0] || '')
  const [second, setSecond] = useState(item.keywords[1] || '')
  function phrases(a: string, b: string) { return [a.trim(), b.trim()].filter(Boolean) }
  function toggle(code: number) {
    const current = item.experience_levels || []
    change({ ...item, experience_levels: current.includes(code) ? current.filter(value => value !== code) : [...current, code] })
  }
  return <article className="rule-card linkedin-card">
    {slot === 'secondary' && <div className="rule-head"><label className="filter-enabled"><input type="checkbox" checked={item.enabled} onChange={event => change({ ...item, enabled: event.target.checked })} /> Usar o filtro secundário na coleta</label></div>}
    <div className="rule-fields">
      <label>Nome<input value={item.name} maxLength={80} onChange={event => change({ ...item, name: event.target.value })} /></label>
      <label>Cargo<input value={first} onChange={event => { setFirst(event.target.value); change({ ...item, keywords: phrases(event.target.value, second) }) }} /></label>
      <label>Variante equivalente do mesmo cargo (opcional)<input value={second} onChange={event => { setSecond(event.target.value); change({ ...item, keywords: phrases(first, event.target.value) }) }} /></label>
    </div>
    <div className="filter-options"><strong>Experiência</strong>{experience.map(([code, label]) => <label key={code}><input type="checkbox" checked={item.experience_levels?.includes(code) || false} onChange={() => toggle(code)} />{label}</label>)}</div>
    {scope.length > 0 && <p className="section-description">Buscas derivadas do seu Perfil (a IA não escolhe local nem modelo): {scope.join(' · ')}.</p>}
    {item.anchor_roles && item.anchor_roles.length > 0 && <p className="section-description"><strong>Ancorado em:</strong> {item.anchor_roles.map(role => `${role.title}${role.employer ? ` — ${role.employer}` : ''}, ${months(role.months)}`).join('; ')}{item.career_months ? ` (total ${months(item.career_months)})` : ''}</p>}
    {item.evidence && Object.keys(item.evidence).length > 0 && <p className="section-description"><strong>Evidência:</strong> {Object.entries(item.evidence).map(([phrase, ids]) => `${phrase} → ${ids.join(', ')}`).join(' · ')}</p>}
    {(item.sector || item.resilience_reason) && <p className="section-description">{item.sector && <strong>{item.sector}. </strong>}{item.resilience_reason}</p>}
    {item.reason && <p className="section-description">{item.reason}</p>}
    <MetricLine metric={metric} />
  </article>
}

export function SearchFilter({ value, onChange, slot, metrics = {}, scope = [] }: { value: string; onChange: (raw: string) => void; slot: 'main' | 'secondary'; metrics?: Record<string, FilterMetric>; scope?: string[] }) {
  let item: Filter | null = null
  let parseError = ''
  try {
    const parsed = value.trim() ? JSON.parse(value) : null
    if (parsed && (Array.isArray(parsed) || typeof parsed !== 'object')) parseError = 'O filtro precisa ser um único objeto JSON. Gere o filtro de novo.'
    else item = parsed as Filter | null
  } catch { parseError = 'JSON inválido. Gere o filtro de novo.' }
  if (parseError) return <p className="inline-message" role="alert">{parseError}</p>
  if (!item) return <p className="empty">{slot === 'main' ? 'Filtro principal ainda não gerado: sem ele a coleta no LinkedIn não roda.' : 'Filtro secundário ainda não gerado.'}</p>
  return <div className="linkedin-editor">
    <FilterEditor key={item.generated_at || slot} item={item} slot={slot} metric={metrics[item.id]} scope={scope} change={next => onChange(JSON.stringify(next))} />
  </div>
}
