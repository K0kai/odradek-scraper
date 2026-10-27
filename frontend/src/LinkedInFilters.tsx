import { useState } from 'react'

type Filter = {
  id: string
  name: string
  enabled: boolean
  keywords: string[]
  locations: string[]
  experience_levels: number[]
  workplace_types: number[]
  reason?: string
  source?: string
}

const experience: [number, string][] = [[1, 'Estágio'], [2, 'Júnior'], [3, 'Associado'], [4, 'Pleno / sênior'], [5, 'Diretor'], [6, 'Executivo']]
const workplace: [number, string][] = [[1, 'Presencial'], [2, 'Remoto'], [3, 'Híbrido']]

function FilterCard({ item, change, remove }: { item: Filter; change: (next: Filter) => void; remove: () => void }) {
  const [keywords, setKeywords] = useState(item.keywords.join(', '))
  const [locations, setLocations] = useState(item.locations.join(', '))
  function list(text: string) { return text.split(',').map(part => part.trim()).filter(Boolean) }
  function toggle(field: 'experience_levels' | 'workplace_types', code: number) {
    const current = item[field]
    change({ ...item, [field]: current.includes(code) ? current.filter(value => value !== code) : [...current, code] })
  }
  return <article className="rule-card linkedin-card"><div className="rule-head"><label className="filter-enabled"><input type="checkbox" checked={item.enabled} onChange={event => change({ ...item, enabled: event.target.checked })} /> Usar nesta busca</label><button className="secondary" onClick={remove}>Excluir</button></div>
    <div className="rule-fields"><label>Nome<input value={item.name} maxLength={80} onChange={event => change({ ...item, name: event.target.value })} /></label><label>Keywords separadas por vírgula<input value={keywords} onChange={event => { setKeywords(event.target.value); change({ ...item, keywords: list(event.target.value) }) }} /></label><label className="wide">Locais separados por vírgula<input value={locations} onChange={event => { setLocations(event.target.value); change({ ...item, locations: list(event.target.value) }) }} /></label></div>
    <div className="filter-options"><strong>Experiência</strong>{experience.map(([code, label]) => <label key={code}><input type="checkbox" checked={item.experience_levels?.includes(code) || false} onChange={() => toggle('experience_levels', code)} />{label}</label>)}</div>
    <div className="filter-options"><strong>Modelo de trabalho</strong>{workplace.map(([code, label]) => <label key={code}><input type="checkbox" checked={item.workplace_types?.includes(code) || false} onChange={() => toggle('workplace_types', code)} />{label}</label>)}</div>
    {item.reason && <p className="section-description">{item.reason}</p>}
  </article>
}

export function LinkedInFilters({ value, onChange }: { value: string; onChange: (raw: string) => void }) {
  const [name, setName] = useState('')
  const [keywords, setKeywords] = useState('')
  let items: Filter[] = []
  let parseError = ''
  try {
    const parsed = JSON.parse(value || '[]')
    if (!Array.isArray(parsed)) parseError = 'Os filtros precisam ser uma lista JSON.'
    else items = parsed as Filter[]
  } catch { parseError = 'JSON inválido. Corrija o texto antes de usar o editor visual.' }
  function update(next: Filter[]) { onChange(JSON.stringify(next)) }
  function add() {
    const terms = keywords.split(',').map(part => part.trim()).filter(Boolean)
    if (!name.trim() || terms.length === 0) return
    update([...items, { id: crypto.randomUUID(), name: name.trim(), enabled: true,
      keywords: terms, locations: ['Worldwide'], experience_levels: [2, 3, 4],
      workplace_types: [2, 3], source: 'manual', reason: '' }])
    setName(''); setKeywords('')
  }
  return <div className="linkedin-editor"><p className="section-description">Cada filtro ativo gera uma busca Apify independente. Edite abaixo e salve as preferências para aplicar.</p>
    {parseError && <p className="inline-message" role="alert">{parseError}</p>}
    {!parseError && <><div className="rule-list">{items.map((item, index) => <FilterCard item={item} key={item.id || index} change={next => update(items.map((old, at) => at === index ? next : old))} remove={() => update(items.filter((_, at) => at !== index))} />)}</div>{items.length === 0 && <p className="empty">Nenhum filtro criado. Adicione um abaixo.</p>}
      <div className="filter-add"><label>Nome do filtro<input value={name} onChange={event => setName(event.target.value)} /></label><label>Keywords<input value={keywords} onChange={event => setKeywords(event.target.value)} placeholder="Ex.: Python, Backend" /></label><button className="secondary" disabled={!name.trim() || !keywords.trim()} onClick={add}>Adicionar filtro</button></div></>}
  </div>
}
