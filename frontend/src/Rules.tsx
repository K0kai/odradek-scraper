import { useEffect, useState } from 'react'
import { api, type FormRule } from './api'

const blank = (): FormRule => ({ key: '', aliases: '', mode: 'text', value_from: '', value: '' })
const modes = [
  ['text', 'Texto'], ['select', 'Seleção'], ['salary', 'Salário'],
  ['file', 'Arquivo'], ['cover_letter', 'Carta'], ['skip', 'Ignorar'],
] as const
const modeLabel = (mode: string) => modes.find(([value]) => value === mode)?.[1] || mode

export function RulesView({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [items, setItems] = useState<FormRule[] | null>(null)
  const [saved, setSaved] = useState<FormRule[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const dirty = items !== null && JSON.stringify(items) !== JSON.stringify(saved)
  const visible = items?.map((item, index) => ({ item, index })).filter(({ item, index }) =>
    index === editing || `${item.key} ${item.aliases} ${item.mode} ${item.value_from}`.toLocaleLowerCase('pt-BR').includes(query.trim().toLocaleLowerCase('pt-BR'))
  ) || []
  useEffect(() => {
    api.formRules().then(result => { setItems(result.items); setSaved(result.items); setError('') }).catch(err => setError(err.message))
  }, [])
  function change(index: number, field: keyof FormRule, value: string) {
    setItems(old => old?.map((item, at) => at === index ? { ...item, [field]: value } : item) || null)
  }
  function remove(index: number) {
    setItems(old => old?.filter((_, at) => at !== index) || null)
    setEditing(old => old === index ? null : old !== null && old > index ? old - 1 : old)
  }
  function add() {
    setQuery('')
    setEditing(items?.length || 0)
    setItems(old => [...(old || []), blank()])
  }
  async function save() {
    if (!items || busy) return
    setBusy(true)
    try {
      const result = await api.saveFormRules(items)
      setSaved(items.map(item => ({ ...item })))
      notify(result.notice)
    } catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  return <section className="section"><div className="section-head"><div><p className="eyebrow">PREENCHIMENTO ASSISTIDO</p><h2>Regras de formulário</h2></div><span className="settings-state">{dirty ? 'Alterações não salvas' : 'Sem alterações'}</span></div>
    <p className="section-description">Cada regra reconhece campos e define como preenchê-los. Abra uma linha para editar; as mudanças são enviadas juntas ao salvar.</p>
    {error && <p className="banner error" role="alert">{error}</p>}
    {!items ? <p className="empty">Carregando regras…</p> : <>
      <div className="rules-toolbar"><label>Buscar regra<input type="search" value={query} onChange={event => { setQuery(event.target.value); setEditing(null) }} placeholder="Chave, alias ou modo" /></label><span>{visible.length} de {items.length} regras</span></div>
      <div className="rules-compact-list">{visible.map(({ item, index }) => <article className={`rules-row${editing === index ? ' editing' : ''}`} key={index}>
        <button type="button" className="rules-row-trigger" aria-label={`${editing === index ? 'Fechar' : 'Editar'} regra ${item.key || index + 1}`} aria-expanded={editing === index} aria-controls={`rule-editor-${index}`} onClick={() => setEditing(old => old === index ? null : index)}><span className="rules-row-info"><strong>{item.key || `Nova regra ${index + 1}`}</strong><span>{modeLabel(item.mode)}{item.value_from ? ` · ${item.value_from}` : item.value ? ' · Valor fixo' : ''}</span></span><span className="rules-chevron" aria-hidden="true" /></button>
        <div className="rules-row-panel" id={`rule-editor-${index}`} aria-hidden={editing !== index} inert={editing !== index}><div className="rules-row-panel-inner"><div className="rules-row-editor"><div className="rule-fields"><label>Chave<input value={item.key} onChange={event => change(index, 'key', event.target.value)} /></label><label>Modo<select value={item.mode} onChange={event => change(index, 'mode', event.target.value)}>{modes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="wide">Aliases<input value={item.aliases} onChange={event => change(index, 'aliases', event.target.value)} /></label><label>Valor das configurações<input value={item.value_from} onChange={event => change(index, 'value_from', event.target.value)} placeholder="ex.: candidate_phone" /></label><label>Valor fixo<input value={item.value} onChange={event => change(index, 'value', event.target.value)} /></label></div><div className="rules-row-actions"><button className="secondary" disabled={items.length <= 1} onClick={() => remove(index)}>Remover regra</button></div></div></div></div>
      </article>)}</div>
      {visible.length === 0 && <p className="empty">Nenhuma regra corresponde à busca.</p>}
      <div className="settings-footer"><button className="secondary" onClick={add}>Adicionar regra</button><button disabled={!dirty || busy} onClick={save}>{busy ? 'Salvando…' : 'Salvar regras'}</button></div>
    </>}
  </section>
}
