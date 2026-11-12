import { useEffect, useState } from 'react'
import { api, type FilterMetric, type SettingsGroup } from './api'
import { SearchFilter } from './LinkedInFilters'

type Values = Record<string, string>
type Kind = 'text' | 'number' | 'textarea' | 'select' | 'password' | 'toggle' | 'sources' | 'slider' | 'filter'
type Field = {
  key: string
  group: SettingsGroup
  label: string
  kind?: Kind
  min?: number
  max?: number
  def?: number
  rows?: number
  options?: [string, string][]
  wide?: boolean
  hint?: string
  secret?: string
  format?: (value: number) => string
  show?: (get: (key: string) => string) => boolean
}
type Section = {
  title: string
  description?: string
  fields: Field[]
  fold?: boolean
  generate?: 'main' | 'secondary'
  action?: 'smtp-test'
}
type Tab = { id: string; label: string; sections: Section[] }

const GROUPS: SettingsGroup[] = ['search', 'profile', 'ai', 'automation', 'smtp']
const notInformed: [string, string] = ['not_informed', 'Prefiro não informar']

const sourceOptions: [string, string][] = [
  ['apify', 'LinkedIn (via Apify)'], ['remotive', 'Remotive'], ['remoteok', 'RemoteOK'], ['adzuna', 'Adzuna'],
]
const parseSources = (value: string) => value.split(',').map(item => item.trim().toLowerCase()).filter(Boolean)
const hasSource = (get: (key: string) => string, ...ids: string[]) => parseSources(get('sources')).some(id => ids.includes(id))
function toggleSource(value: string, id: string, on: boolean) {
  const items = parseSources(value)
  return (on ? (items.includes(id) ? items : [...items, id]) : items.filter(item => item !== id)).join(',')
}

const toleranceCurve: [number, number][] = [[0, 100], [50, 80], [75, 70], [100, 55]]
function coverage(value: number) {
  for (let i = 1; i < toleranceCurve.length; i++) {
    const [x0, y0] = toleranceCurve[i - 1]
    const [x1, y1] = toleranceCurve[i]
    if (value <= x1) return Math.round(y0 + (y1 - y0) * (value - x0) / (x1 - x0))
  }
  return toleranceCurve[toleranceCurve.length - 1][1]
}

const tabs: Tab[] = [
  {
    id: 'search', label: 'Busca', sections: [
      {
        title: 'Filtro principal', description: 'TI e programação, a área de maior experiência. Sem ele a coleta no LinkedIn não roda.', generate: 'main',
        fields: [{ key: 'linkedin_main_filter_json', group: 'search', label: 'Filtro principal', kind: 'filter' }],
      },
      {
        title: 'Filtro secundário', description: 'Opcional, só é gerado pelo botão: uma área de reserva, menos afetada por crises, onde TI + experiência administrativa se aplicam.', generate: 'secondary',
        fields: [
          { key: 'linkedin_secondary_filter_json', group: 'search', label: 'Filtro secundário', kind: 'filter' },
          { key: 'secondary_area_hint', group: 'search', label: 'Dica da área para a IA (opcional)', hint: 'Ex.: administrativo com sistemas.', wide: true },
        ],
      },
      {
        title: 'Fontes de vagas',
        fields: [
          { key: 'sources', group: 'search', label: 'Onde buscar', kind: 'sources', wide: true },
          { key: 'apify_monthly_credit_limit_usd', group: 'search', label: 'Limite mensal do Apify (USD)', kind: 'number', hint: 'A coleta do LinkedIn para ao atingir o limite.', show: get => hasSource(get, 'apify') },
          { key: 'keywords', group: 'search', label: 'Palavras-chave', kind: 'textarea', wide: true, hint: 'Filtram as vagas de Remotive, RemoteOK e Adzuna. O LinkedIn usa os filtros acima.', show: get => hasSource(get, 'remotive', 'remoteok', 'adzuna') },
          { key: 'adzuna_countries', group: 'search', label: 'Países do Adzuna', hint: 'Códigos separados por vírgula, ex.: br,us,gb.', show: get => hasSource(get, 'adzuna') },
        ],
      },
      {
        title: 'Alcance',
        fields: [
          { key: 'linkedin_max_age_days', group: 'search', label: 'Idade máxima das vagas do LinkedIn', kind: 'slider', min: 1, max: 30, def: 7, wide: true, format: days => days <= 1 ? 'até 24 horas' : `até ${days} dias`, hint: 'As mais recentes são sempre buscadas primeiro; este é só o limite até onde a busca alarga (24h, depois 7 dias, depois este valor).' },
          { key: 'linkedin_preferred_countries', group: 'search', label: 'Países preferidos (até 5)', wide: true, hint: 'Ex.: Brazil, Portugal. Vagas desses países são buscadas e triadas primeiro; nenhum país é excluído.' },
          { key: 'geo_eligibility_enabled', group: 'search', label: 'Respeitar elegibilidade geográfica', kind: 'toggle', wide: true, hint: 'Fora do seu país só vagas remotas e abertas a estrangeiros; híbrido/presencial só na sua cidade.' },
        ],
      },
      {
        title: 'Avançado', fold: true,
        fields: [
          { key: 'locations', group: 'search', label: 'Locais extras dos actors personalizados', kind: 'textarea', wide: true, hint: 'Só vale para actors Apify personalizados. Remoto, seu país e sua cidade já entram automaticamente.' },
          { key: 'linkedin_main_share', group: 'search', label: 'Fatia do filtro principal (%)', kind: 'number', min: 50, max: 95, hint: 'De 50 a 95. Divide as vagas por busca entre principal e secundário.' },
          { key: 'apify_job_count', group: 'search', label: 'Vagas por busca (padrão)', kind: 'number', hint: 'Usado só por actors que não definem a própria contagem. Máximo 100.' },
          { key: 'apify_actors_json', group: 'search', label: 'Actors do Apify (JSON)', kind: 'textarea', rows: 8, wide: true, hint: 'Vazio usa só o actor padrão do LinkedIn. Edite para adicionar outros, como Indeed.' },
          { key: 'linkedin_filter_prompt', group: 'search', label: 'Instruções da IA — filtro principal', kind: 'textarea', rows: 8, wide: true, hint: 'Usadas apenas ao gerar o filtro.' },
          { key: 'transferable_prompt', group: 'search', label: 'Instruções da IA — filtro secundário', kind: 'textarea', rows: 8, wide: true, hint: 'Usadas apenas ao gerar o filtro.' },
        ],
      },
    ],
  },
  {
    id: 'triage', label: 'Triagem', sections: [
      {
        title: 'Decisão da IA',
        fields: [
          { key: 'auto_apply', group: 'automation', label: 'Triar vagas novas automaticamente após cada coleta', kind: 'toggle', wide: true },
          { key: 'minimum_match_score', group: 'automation', label: 'Score mínimo — área principal (%)', kind: 'number', min: 0, max: 100 },
          { key: 'transferable_minimum_score', group: 'automation', label: 'Score mínimo — área secundária (%)', kind: 'number', min: 0, max: 100 },
          { key: 'job_selection_optimism', group: 'ai', label: 'Tolerância da seleção', kind: 'slider', min: 0, max: 100, def: 75, wide: true, format: value => `${value} — exige ${coverage(value)}% dos requisitos obrigatórios`, hint: 'De rígido (0) a flexível (100): define quanto dos requisitos obrigatórios a vaga precisa cobrir.' },
        ],
      },
      {
        title: 'Avançado', fold: true,
        fields: [
          { key: 'job_selection_prompt', group: 'ai', label: 'Instruções da seleção', kind: 'textarea', rows: 8, wide: true },
          { key: 'queue_max_workers', group: 'automation', label: 'Triagens simultâneas', kind: 'number', min: 1, max: 8, hint: 'De 1 a 8.' },
          { key: 'queue_max_attempts', group: 'automation', label: 'Tentativas por vaga', kind: 'number', min: 1, max: 200, hint: 'Reenvios quando a IA está sem cota ou sobrecarregada.' },
          { key: 'queue_ttl_hours', group: 'automation', label: 'Validade na fila (horas)', kind: 'number', min: 1, max: 168 },
        ],
      },
    ],
  },
  {
    id: 'profile', label: 'Perfil', sections: [
      {
        title: 'Contato',
        fields: [
          { key: 'candidate_name', group: 'profile', label: 'Nome' }, { key: 'candidate_email', group: 'profile', label: 'E-mail' },
          { key: 'candidate_phone', group: 'profile', label: 'Telefone' }, { key: 'candidate_linkedin', group: 'profile', label: 'LinkedIn' },
          { key: 'candidate_current_company', group: 'profile', label: 'Empresa atual' }, { key: 'candidate_cpf', group: 'profile', label: 'CPF' },
        ],
      },
      {
        title: 'Endereço', description: 'A cidade e o país definem a elegibilidade geográfica e onde o LinkedIn é pesquisado.',
        fields: [
          { key: 'candidate_city', group: 'profile', label: 'Cidade' }, { key: 'candidate_state', group: 'profile', label: 'Estado' },
          { key: 'candidate_country', group: 'profile', label: 'País' }, { key: 'candidate_postal_code', group: 'profile', label: 'CEP' },
          { key: 'candidate_street', group: 'profile', label: 'Endereço', wide: true },
        ],
      },
      {
        title: 'Pretensão e contratação',
        fields: [
          { key: 'salary_expectation_brl', group: 'profile', label: 'Pretensão CLT (BRL)' },
          { key: 'salary_expectation_usd', group: 'profile', label: 'Pretensão (USD)' },
          { key: 'candidate_contract_type', group: 'profile', label: 'Contratação preferida', kind: 'select', options: [['employee', 'CLT / efetivo'], ['contractor', 'PJ / contractor'], ['ask', 'Perguntar']] },
        ],
      },
      {
        title: 'Experiência para a IA', description: 'Fatos que a IA pode usar sobre você na triagem e nas cartas.',
        fields: [
          { key: 'candidate_facts_pt', group: 'ai', label: 'Fatos profissionais (português)', kind: 'textarea', wide: true },
          { key: 'candidate_facts_en', group: 'ai', label: 'Professional facts (English)', kind: 'textarea', wide: true },
          { key: 'portfolio_profile_url', group: 'profile', label: 'URL do portfólio', wide: true, hint: 'Endereço do /ai/profile.json (schema 2). Fonte extra de evidência, sincronizada a cada coleta.' },
        ],
      },
      {
        title: 'Diversidade (opcional)', fold: true,
        fields: [
          { key: 'candidate_gender', group: 'profile', label: 'Gênero', kind: 'select', options: [notInformed, ['female', 'Mulher'], ['male', 'Homem'], ['other', 'Outro / não binário']] },
          { key: 'candidate_race', group: 'profile', label: 'Raça / etnia', kind: 'select', options: [notInformed, ['white', 'Branca'], ['black', 'Preta'], ['pardo', 'Parda'], ['asian', 'Amarela / asiática'], ['indigenous', 'Indígena']] },
          { key: 'candidate_pcd', group: 'profile', label: 'PCD', kind: 'select', options: [notInformed, ['no', 'Não'], ['yes', 'Sim']] },
          { key: 'candidate_lgbtq', group: 'profile', label: 'LGBTQ+', kind: 'select', options: [notInformed, ['no', 'Hétero / cis'], ['gay', 'Gay'], ['lesbian', 'Lésbica'], ['bisexual', 'Bissexual'], ['pansexual', 'Pansexual'], ['asexual', 'Assexual'], ['other', 'Outra'], ['yes', 'Sim, genérico']] },
          { key: 'candidate_diversity_note', group: 'profile', label: 'Nota de diversidade', kind: 'textarea', wide: true },
        ],
      },
    ],
  },
  {
    id: 'accounts', label: 'Contas e chaves', sections: [
      {
        title: 'Inteligência artificial',
        fields: [
          { key: 'ai_provider', group: 'ai', label: 'Provedor', kind: 'select', options: [['gemini', 'Gemini'], ['openai', 'OpenAI']] },
          { key: 'ai_model', group: 'ai', label: 'Modelo' },
          { key: 'api_key', group: 'ai', label: 'Chave da IA', kind: 'password', secret: 'api_key_set', wide: true, hint: 'Salva para o provedor selecionado acima.' },
        ],
      },
      {
        title: 'Apify e Adzuna', description: 'Credenciais das fontes de vagas. Só preencha as que você usa.',
        fields: [
          { key: 'apify_token', group: 'ai', label: 'Apify token', kind: 'password', secret: 'apify_token_set', wide: true },
          { key: 'adzuna_app_id', group: 'ai', label: 'Adzuna App ID', kind: 'password', secret: 'adzuna_app_id_set' },
          { key: 'adzuna_app_key', group: 'ai', label: 'Adzuna API key', kind: 'password', secret: 'adzuna_app_key_set' },
        ],
      },
      {
        title: 'E-mail (SMTP)', description: 'Também é usado para enviar candidaturas por e-mail a vagas aprovadas que trazem um endereço de contato.', action: 'smtp-test',
        fields: [
          { key: 'smtp_host', group: 'smtp', label: 'Servidor' }, { key: 'smtp_port', group: 'smtp', label: 'Porta', kind: 'number' },
          { key: 'smtp_user', group: 'smtp', label: 'Usuário' }, { key: 'smtp_from', group: 'smtp', label: 'Remetente' },
          { key: 'smtp_password', group: 'smtp', label: 'Senha', kind: 'password', secret: 'smtp_password_set' },
          { key: 'smtp_use_tls', group: 'smtp', label: 'Usar TLS', kind: 'toggle' },
        ],
      },
    ],
  },
]
const allFields = tabs.flatMap(tab => tab.sections.flatMap(section => section.fields))

function Slider({ field, value, onChange }: { field: Field; value: string; onChange: (value: string) => void }) {
  const parsed = Number(value)
  const current = value.trim() !== '' && Number.isFinite(parsed) ? parsed : (field.def ?? field.min ?? 0)
  return <span className="range-field">
    <input type="range" min={field.min} max={field.max} step={1} value={current} onChange={event => onChange(event.target.value)} aria-valuetext={field.format?.(current)} />
    <output>{field.format ? field.format(current) : current}</output>
  </span>
}

function FieldView({ field, value, secretSet, onChange }: { field: Field; value: string; secretSet: boolean; onChange: (value: string) => void }) {
  const wide = field.wide ? ' wide' : ''
  const hint = field.hint ? <small className="settings-hint">{field.hint}</small> : null
  if (field.kind === 'toggle') {
    return <label className={`settings-toggle${wide}`}>
      <input type="checkbox" checked={value === '1'} onChange={event => onChange(event.target.checked ? '1' : '0')} />
      <span>{field.label}{hint}</span>
    </label>
  }
  if (field.kind === 'sources') {
    const active = parseSources(value)
    return <div className={`source-field${wide}`}>
      <span className="field-label">{field.label}</span>
      <div className="source-checks">{sourceOptions.map(([id, label]) => <label key={id}>
        <input type="checkbox" checked={active.includes(id)} onChange={event => onChange(toggleSource(value, id, event.target.checked))} />{label}
      </label>)}</div>
      {hint}
    </div>
  }
  return <label className={wide.trim()}>
    {field.label}
    {field.secret && <em className={`secret-state${secretSet ? ' ok' : ''}`}>{secretSet ? 'Configurada ✓' : 'Não configurada'}</em>}
    {field.kind === 'slider' ? <Slider field={field} value={value} onChange={onChange} />
      : field.kind === 'textarea' ? <textarea rows={field.rows ?? 3} value={value} onChange={event => onChange(event.target.value)} />
        : field.kind === 'select' ? <select value={value} onChange={event => onChange(event.target.value)}>{field.options?.map(([optionValue, label]) => <option value={optionValue} key={optionValue}>{label}</option>)}</select>
          : <input type={field.kind === 'password' ? 'password' : field.kind === 'number' ? 'number' : 'text'} min={field.min} max={field.max} value={value}
            placeholder={field.secret ? 'Deixe vazio para manter a atual' : undefined}
            onChange={event => onChange(event.target.value)} autoComplete={field.kind === 'password' ? 'new-password' : 'off'} />}
    {hint}
  </label>
}

const clone = (source: Partial<Record<SettingsGroup, Values>>) => {
  const copy: Partial<Record<SettingsGroup, Values>> = {}
  for (const group of GROUPS) if (source[group]) copy[group] = { ...source[group] }
  return copy
}

export function SettingsView({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [tabId, setTabId] = useState(tabs[0].id)
  const [server, setServer] = useState<Partial<Record<SettingsGroup, Values>>>({})
  const [drafts, setDrafts] = useState<Partial<Record<SettingsGroup, Values>>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [metrics, setMetrics] = useState<Record<string, FilterMetric>>({})
  const [reloadKey, setReloadKey] = useState(0)
  const ready = GROUPS.every(group => drafts[group])
  const tab = tabs.find(item => item.id === tabId) ?? tabs[0]

  useEffect(() => {
    let live = true
    Promise.all(GROUPS.map(group => api.settings(group))).then(results => {
      if (!live) return
      const loaded: Partial<Record<SettingsGroup, Values>> = {}
      results.forEach((result, index) => { loaded[GROUPS[index]] = result.values })
      setServer(loaded)
      setDrafts(clone(loaded))
      setError('')
    }).catch(err => { if (live) setError(err.message) })
    return () => { live = false }
  }, [reloadKey])

  useEffect(() => {
    if (tabId !== 'search') return
    let live = true
    api.filterMetrics().then(result => { if (live) setMetrics(result.metrics) }).catch(() => undefined)
    return () => { live = false }
  }, [tabId])

  const get = (key: string) => {
    for (const group of GROUPS) { const values = drafts[group]; if (values && key in values) return values[key] }
    return ''
  }
  const isDirty = (field: Field) => (drafts[field.group]?.[field.key] || '') !== (server[field.group]?.[field.key] || '')
  const dirtyFields = ready ? allFields.filter(isDirty) : []
  const dirtyGroups = GROUPS.filter(group => dirtyFields.some(field => field.group === group))
  const smtpDirty = dirtyGroups.includes('smtp')
  let scope: string[] = []
  try { scope = (JSON.parse(drafts.search?.scope_json || '[]') as { label: string }[]).map(item => item.label) } catch { scope = [] }

  function change(field: Field, value: string) {
    setDrafts(old => ({ ...old, [field.group]: { ...old[field.group], [field.key]: value } }))
  }
  async function save() {
    if (busy || !dirtyGroups.length) return
    setBusy(true)
    const saved: string[] = []
    try {
      for (const group of dirtyGroups) {
        const result = await api.saveSettings(group, drafts[group]!)
        setServer(old => ({ ...old, [group]: result.values }))
        setDrafts(old => ({ ...old, [group]: { ...result.values } }))
        saved.push(result.notice)
      }
      notify(saved.length > 1 ? 'Configurações salvas.' : saved[0])
    } catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  async function testSmtp() {
    setBusy(true)
    try { const result = await api.testSmtp(); notify(result.notice) }
    catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  async function generateFilter(kind: 'main' | 'secondary') {
    const searchDraft = drafts.search
    if (!searchDraft || busy) return
    setBusy(true)
    try {
      const result = await (kind === 'main' ? api.generateMainFilter(searchDraft) : api.generateSecondaryFilter(searchDraft))
      setServer(old => ({ ...old, search: result.values }))
      setDrafts(old => ({ ...old, search: { ...result.values } }))
      notify(result.notice)
    } catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }

  function renderField(field: Field) {
    if (field.show && !field.show(get)) return null
    if (field.kind === 'filter') {
      const slot = field.key === 'linkedin_main_filter_json' ? 'main' : 'secondary'
      return <div className="wide" key={field.key}><SearchFilter slot={slot} value={get(field.key)} onChange={value => change(field, value)} metrics={metrics} scope={scope} /></div>
    }
    return <FieldView key={field.key} field={field} value={get(field.key)} secretSet={get(field.secret || '') === '1'} onChange={value => change(field, value)} />
  }
  function renderSection(section: Section) {
    const body = <>
      {section.description && <p className="section-description">{section.description}</p>}
      <div className="settings-grid">{section.fields.map(renderField)}</div>
      {(section.generate || section.action) && <div className="settings-card-actions">
        {section.generate && <button className="secondary" disabled={busy} onClick={() => generateFilter(section.generate!)}>Gerar com IA</button>}
        {section.generate && <small className="settings-hint">Salva a busca e substitui só este filtro.</small>}
        {section.action === 'smtp-test' && <button className="secondary" disabled={busy || smtpDirty} onClick={testSmtp}>Enviar e-mail de teste</button>}
        {section.action === 'smtp-test' && smtpDirty && <small className="settings-hint">Salve antes de testar.</small>}
      </div>}
    </>
    return section.fold
      ? <details className="settings-card settings-advanced" key={section.title}><summary>{section.title}</summary>{body}</details>
      : <section className="settings-card" key={section.title}><h3>{section.title}</h3>{body}</section>
  }

  return <section className="section settings-section"><div className="section-head"><div><p className="eyebrow">PREFERÊNCIAS</p><h2>Configurações</h2></div></div>
    <div className="settings-tabs" role="tablist" aria-label="Grupos de configuração">{tabs.map(item => <button role="tab" aria-selected={tabId === item.id} className={tabId === item.id ? 'selected' : 'secondary'} onClick={() => setTabId(item.id)} key={item.id}>{item.label}{dirtyFields.some(field => item.sections.some(section => section.fields.includes(field))) ? ' •' : ''}</button>)}</div>
    {error && <p className="banner error" role="alert">{error} <button onClick={() => { setError(''); setDrafts({}); setReloadKey(key => key + 1) }}>Tentar novamente</button></p>}
    {!ready ? (!error && <p className="empty">Carregando configurações…</p>) : <>
      <div className="settings-cards">{tab.sections.map(renderSection)}</div>
      <div className="settings-bar">
        <span className="settings-state">{dirtyFields.length ? `${dirtyFields.length} alteraç${dirtyFields.length === 1 ? 'ão não salva' : 'ões não salvas'}` : 'Tudo salvo'}</span>
        <button className="secondary" disabled={!dirtyFields.length || busy} onClick={() => setDrafts(clone(server))}>Descartar</button>
        <button disabled={!dirtyFields.length || busy} onClick={save}>{busy ? 'Aguarde…' : 'Salvar alterações'}</button>
      </div>
    </>}
  </section>
}
