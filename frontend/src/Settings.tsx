import { useEffect, useState } from 'react'
import { api, type FilterMetric, type SettingsGroup } from './api'
import { SearchFilter } from './LinkedInFilters'

type Field = {
  key: string
  label: string
  kind?: 'text' | 'number' | 'textarea' | 'select' | 'checkbox' | 'password'
  options?: [string, string][]
  wide?: boolean
}

const yesNo: [string, string][] = [['1', 'Sim'], ['0', 'Não']]
const fields: Record<SettingsGroup, Field[]> = {
  search: [
    { key: 'keywords', label: 'Cargos e termos (filtram as outras fontes; o LinkedIn usa o filtro Principal)', kind: 'textarea', wide: true },
    { key: 'locations', label: 'Locais para actors custom/Adzuna (não filtram vagas coletadas)', kind: 'textarea', wide: true },
    { key: 'sources', label: 'Fontes: remotive, remoteok, adzuna, apify' },
    { key: 'adzuna_countries', label: 'Países Adzuna' },
    { key: 'apify_monthly_credit_limit_usd', label: 'Limite mensal Apify (USD)', kind: 'number' },
    { key: 'apify_job_count', label: 'Máximo de vagas por ciclo Apify', kind: 'number' },
    { key: 'apify_actors_json', label: 'Actors Apify (JSON)', kind: 'textarea', wide: true },
    { key: 'geo_eligibility_enabled', label: 'Elegibilidade geográfica: exterior só remoto e aberto a estrangeiros; híbrido/presencial só na minha cidade', kind: 'select', options: yesNo },
    { key: 'linkedin_main_share', label: 'Fatia do filtro Principal no count do Apify (%, 50 a 95)', kind: 'number' },
    { key: 'linkedin_filter_prompt', label: 'Filtro principal: instruções da IA', kind: 'textarea', wide: true },
    { key: 'linkedin_main_filter_json', label: 'Filtro principal — TI e programação (a área de maior experiência)', wide: true },
    { key: 'transferable_prompt', label: 'Filtro secundário: instruções da IA', kind: 'textarea', wide: true },
    { key: 'secondary_area_hint', label: 'Filtro secundário: dica opcional da área (ex.: administrativo com sistemas)', wide: true },
    { key: 'linkedin_secondary_filter_json', label: 'Filtro secundário — área de reserva, menos afetada pela crise', wide: true },
  ],
  profile: [
    { key: 'candidate_name', label: 'Nome' }, { key: 'candidate_email', label: 'E-mail' },
    { key: 'candidate_phone', label: 'Telefone' }, { key: 'candidate_linkedin', label: 'LinkedIn' },
    { key: 'candidate_city', label: 'Cidade' }, { key: 'candidate_state', label: 'Estado' },
    { key: 'candidate_country', label: 'País' }, { key: 'candidate_street', label: 'Endereço' },
    { key: 'candidate_postal_code', label: 'CEP' }, { key: 'candidate_current_company', label: 'Empresa atual' },
    { key: 'candidate_cpf', label: 'CPF' },
    { key: 'salary_expectation_brl', label: 'Pretensão CLT (BRL)' },
    { key: 'salary_expectation_usd', label: 'Pretensão (USD)' },
    { key: 'salary_currency_preference', label: 'Moeda preferida', kind: 'select', options: [['auto', 'Detectar'], ['BRL', 'BRL'], ['USD', 'USD']] },
    { key: 'salary_pj_multiplier', label: 'Multiplicador PJ', kind: 'number' },
    { key: 'candidate_contract_type', label: 'Contratação', kind: 'select', options: [['employee', 'CLT / efetivo'], ['contractor', 'PJ / contractor'], ['ask', 'Perguntar']] },
    { key: 'candidate_gender', label: 'Gênero', kind: 'select', options: [['not_informed', 'Prefiro não informar'], ['female', 'Mulher'], ['male', 'Homem'], ['other', 'Outro / não binário']] },
    { key: 'candidate_race', label: 'Raça / etnia', kind: 'select', options: [['not_informed', 'Prefiro não informar'], ['white', 'Branca'], ['black', 'Preta'], ['pardo', 'Parda'], ['asian', 'Amarela / asiática'], ['indigenous', 'Indígena']] },
    { key: 'candidate_pcd', label: 'PCD', kind: 'select', options: [['not_informed', 'Prefiro não informar'], ['no', 'Não'], ['yes', 'Sim']] },
    { key: 'candidate_lgbtq', label: 'LGBTQ+', kind: 'select', options: [['not_informed', 'Prefiro não informar'], ['no', 'Hétero / cis'], ['gay', 'Gay'], ['lesbian', 'Lésbica'], ['bisexual', 'Bissexual'], ['pansexual', 'Pansexual'], ['asexual', 'Assexual'], ['other', 'Outra'], ['yes', 'Sim, genérico']] },
    { key: 'candidate_diversity_note', label: 'Nota de diversidade', kind: 'textarea', wide: true },
    { key: 'portfolio_profile_url', label: 'URL do portfólio (/ai/profile.json, schema 2) — fonte extra de evidência', wide: true },
  ],
  ai: [
    { key: 'ai_provider', label: 'Provedor', kind: 'select', options: [['gemini', 'Gemini'], ['openai', 'OpenAI']] },
    { key: 'ai_model', label: 'Modelo' },
    { key: 'api_key', label: 'Chave do provedor (vazio mantém a atual)', kind: 'password', wide: true },
    { key: 'adzuna_app_id', label: 'Adzuna App ID (vazio mantém)', kind: 'password' },
    { key: 'adzuna_app_key', label: 'Adzuna API key (vazio mantém)', kind: 'password' },
    { key: 'apify_token', label: 'Apify token (vazio mantém)', kind: 'password', wide: true },
    { key: 'candidate_facts_pt', label: 'Fatos profissionais em português', kind: 'textarea', wide: true },
    { key: 'candidate_facts_en', label: 'Professional facts in English', kind: 'textarea', wide: true },
    { key: 'job_selection_optimism', label: 'Tolerância da seleção (0 rígido a 100 flexível): define a cobertura mínima dos requisitos obrigatórios (0 → 100%, 50 → 80%, 75 → 70%, 100 → 55%)', kind: 'number' },
    { key: 'job_selection_prompt', label: 'Instruções da seleção', kind: 'textarea', wide: true },
  ],
  automation: [
    { key: 'auto_apply', label: 'Triar vagas novas após a coleta', kind: 'select', options: yesNo },
    { key: 'minimum_match_score', label: 'Score mínimo da área principal (%)', kind: 'number' },
    { key: 'transferable_minimum_score', label: 'Score mínimo da área secundária (%)', kind: 'number' },
    { key: 'queue_max_workers', label: 'Workers da fila', kind: 'number' },
    { key: 'queue_max_attempts', label: 'Máximo de tentativas', kind: 'number' },
    { key: 'queue_ttl_hours', label: 'TTL da fila (horas)', kind: 'number' },
    { key: 'copilot_ask_sound', label: 'Som nas perguntas do copiloto', kind: 'select', options: yesNo },
    { key: 'copilot_ask_sound_volume', label: 'Volume do som (0 a 1)', kind: 'number' },
  ],
  smtp: [
    { key: 'smtp_host', label: 'Servidor SMTP' }, { key: 'smtp_port', label: 'Porta', kind: 'number' },
    { key: 'smtp_user', label: 'Usuário SMTP' }, { key: 'smtp_from', label: 'Remetente' },
    { key: 'smtp_use_tls', label: 'Usar TLS', kind: 'select', options: yesNo },
    { key: 'smtp_password', label: 'Senha SMTP (vazio mantém)', kind: 'password', wide: true },
  ],
}
const labels: Record<SettingsGroup, string> = {
  search: 'Busca e coleta', profile: 'Perfil', ai: 'IA e integrações',
  automation: 'Automação', smtp: 'SMTP',
}
const groups = Object.keys(labels) as SettingsGroup[]

export function SettingsView({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [group, setGroup] = useState<SettingsGroup>('search')
  const [server, setServer] = useState<Partial<Record<SettingsGroup, Record<string, string>>>>({})
  const [drafts, setDrafts] = useState<Partial<Record<SettingsGroup, Record<string, string>>>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [metrics, setMetrics] = useState<Record<string, FilterMetric>>({})
  let scope: string[] = []
  try { scope = (JSON.parse(drafts.search?.scope_json || '[]') as { label: string }[]).map(item => item.label) } catch { scope = [] }

  useEffect(() => {
    if (group !== 'search') return
    let live = true
    api.filterMetrics().then(result => { if (live) setMetrics(result.metrics) }).catch(() => undefined)
    return () => { live = false }
  }, [group])

  useEffect(() => {
    if (drafts[group]) return
    let live = true
    api.settings(group).then(result => {
      if (!live) return
      setServer(old => ({ ...old, [group]: result.values }))
      setDrafts(old => ({ ...old, [group]: { ...result.values } }))
      setError('')
    }).catch(err => { if (live) setError(err.message) })
    return () => { live = false }
  }, [group, drafts])

  const draft = drafts[group]
  const stored = server[group]
  const dirty = Boolean(draft && stored && fields[group].some(field => (draft[field.key] || '') !== (stored[field.key] || '')))
  function change(key: string, value: string) {
    setDrafts(old => ({ ...old, [group]: { ...old[group], [key]: value } }))
  }
  async function save() {
    if (!draft || busy) return
    setBusy(true)
    try {
      const result = await api.saveSettings(group, draft)
      const clean = { ...result.values }
      setServer(old => ({ ...old, [group]: clean }))
      setDrafts(old => ({ ...old, [group]: { ...clean } }))
      notify(result.notice)
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
  return <section className="section settings-section"><div className="section-head"><div><p className="eyebrow">PREFERÊNCIAS</p><h2>Configurações</h2></div><span className="settings-state">{dirty ? 'Alterações não salvas' : 'Sem alterações'}</span></div>
    <div className="settings-tabs" role="tablist" aria-label="Grupos de configuração">{groups.map(item => <button role="tab" aria-selected={group === item} className={group === item ? 'selected' : 'secondary'} onClick={() => setGroup(item)} key={item}>{labels[item]}{drafts[item] && server[item] && fields[item].some(field => (drafts[item]![field.key] || '') !== (server[item]![field.key] || '')) ? ' •' : ''}</button>)}</div>
    {error && <p className="banner error" role="alert">{error} <button onClick={() => { setError(''); setDrafts(old => ({ ...old, [group]: undefined })) }}>Tentar novamente</button></p>}
    {!draft ? <p className="empty">Carregando configurações…</p> : <>
      <div className="settings-grid">{fields[group].map(field => field.key === 'linkedin_main_filter_json' || field.key === 'linkedin_secondary_filter_json'
        ? <div className="wide" key={field.key}><h3>{field.label}</h3>{field.key === 'linkedin_secondary_filter_json' && <p className="section-description">Opcional e só gerado pelo botão, nunca automaticamente: um cargo em área menos sensível a crise onde TI + experiência administrativa se aplicam. Vagas dele usam o score mínimo da área secundária e exigem cobertura das responsabilidades centrais.</p>}<SearchFilter slot={field.key === 'linkedin_main_filter_json' ? 'main' : 'secondary'} value={draft[field.key] || ''} onChange={value => change(field.key, value)} metrics={metrics} scope={scope} /></div>
        : <label className={field.wide ? 'wide' : ''} key={field.key}>{field.label}
          {field.kind === 'textarea' ? <textarea rows={field.key.includes('prompt') || field.key.includes('json') ? 8 : 3} value={draft[field.key] || ''} onChange={event => change(field.key, event.target.value)} />
            : field.kind === 'select' ? <select value={draft[field.key] || ''} onChange={event => change(field.key, event.target.value)}>{field.options?.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>
              : <input type={field.kind === 'password' ? 'password' : field.kind === 'number' ? 'number' : 'text'} value={draft[field.key] || ''} onChange={event => change(field.key, event.target.value)} autoComplete={field.kind === 'password' ? 'new-password' : 'off'} />}
        </label>)}</div>
      <div className="settings-footer"><button disabled={!dirty || busy} onClick={save}>{busy ? 'Aguarde…' : `Salvar ${labels[group].toLowerCase()}`}</button>{group === 'search' && <button className="secondary" disabled={busy} onClick={() => generateFilter('main')}>Gerar filtro principal com IA</button>}{group === 'search' && <button className="secondary" disabled={busy} onClick={() => generateFilter('secondary')}>Gerar filtro secundário com IA</button>}{group === 'smtp' && <button className="secondary" disabled={busy || dirty} onClick={testSmtp}>Enviar e-mail de teste</button>}<span>{group === 'search' ? 'A geração salva as configurações de busca e substitui só o filtro escolhido.' : dirty ? 'Alterações locais ainda não foram enviadas.' : 'As preferências exibidas estão salvas.'}</span></div>
    </>}
  </section>
}
