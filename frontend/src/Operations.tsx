import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type Assessment, type LogItem, type PortfolioStatus, type QueueData, type QueueItem, type ResumeRecord, type ResumesData, type StandbyStatus, type UsageData } from './api'
import { useIdlePolling } from './polling'
import { formatDate } from './format'
import { AssessmentView } from './JobCard'
import { apifyUsage, QuotaBar, TokenHarborCard } from './Quota'

const kinds: Record<string, string> = {
  resume_analysis: 'Análise de currículo',
  job_apply: 'Triagem de vaga',
  worth_reverify: 'Verificação de vaga',
  linkedin_apply: 'Candidatura por navegador',
}
const statuses: Record<string, string> = {
  pending: 'Pendente', running: 'Executando', retry_wait: 'Aguardando nova tentativa',
  succeeded: 'Concluído', failed: 'Falhou', cancelled: 'Cancelado',
}

// Consulta ao abrir e a cada `delay` ms (null = só ao abrir/manual); para sozinha com a aba parada (ver polling.ts).
function usePolling<T>(loader: () => Promise<T>, setData: (value: T) => void, setError: (error: string) => void, delay: number | null) {
  const refresh = useCallback(() => {
    if (document.hidden) return
    loader().then(data => { setData(data); setError('') }).catch(error => setError(error.message))
  }, [loader, setData, setError])
  useIdlePolling(refresh, delay)
  return refresh
}

function QueueSubjectView({ item, statusLabels }: { item: QueueItem; statusLabels: Record<string, string> }) {
  const subject = item.subject
  if (!subject) return null
  if (subject.type === 'resume') return <p className="queue-subject"><strong>Currículo {subject.language.toUpperCase() || '—'}</strong></p>
  if (subject.type === 'worth') return <p className="queue-subject"><strong>Verificação diária{subject.scheduled_date ? ` de ${subject.scheduled_date}` : ''}</strong></p>
  if (subject.missing) return <p className="queue-subject"><strong>Vaga #{subject.job_id}</strong> <em>(removida)</em></p>
  const title = subject.title || 'Sem título'
  return <div className="queue-subject">
    <p><strong>{subject.url ? <a href={subject.url} target="_blank" rel="noreferrer">{title}</a> : title}</strong></p>
    <div className="operation-meta"><span>Vaga #{subject.job_id}</span>{subject.company && <span>{subject.company}</span>}{subject.location && <span>{subject.location}</span>}{subject.job_status && <span>Status: {statusLabels[subject.job_status] || subject.job_status}</span>}</div>
  </div>
}

function QueueDecisionView({ item }: { item: QueueItem }) {
  const decision = item.decision
  const [audit, setAudit] = useState<Assessment | null | undefined>(undefined)
  const [loading, setLoading] = useState(false)
  const [auditError, setAuditError] = useState('')
  if (!decision) return null
  async function loadAudit() {
    setLoading(true); setAuditError('')
    try { setAudit((await api.queueAssessment(item.id)).assessment) }
    catch (err) { setAuditError((err as Error).message) }
    finally { setLoading(false) }
  }
  return <div className="queue-decision">
    <div className="operation-meta">
      <span className={`queue-status ${decision.approved ? 'succeeded' : 'failed'}`}>{decision.approved ? 'Aprovada' : 'Descartada'}</span>
      {decision.score !== null && <span>Score {decision.score}/100</span>}
      {decision.model && <span>{decision.provider ? `${decision.provider}/` : ''}{decision.model}</span>}
      {decision.decided_at && <span>Decidido {formatDate(decision.decided_at, true)}</span>}
    </div>
    {decision.reason && <p className="triage-reason"><strong>Raciocínio da IA:</strong> {decision.reason}</p>}
    {audit === undefined && <button className="secondary" disabled={loading} onClick={loadAudit}>{loading ? 'Carregando…' : 'Ver checklist da IA'}</button>}
    {auditError && <p className="banner error" role="alert">{auditError}</p>}
    {audit === null && <p className="section-description">Sem checklist detalhado para esta decisão.</p>}
    {audit && <AssessmentView assessment={audit} open />}
  </div>
}

// A lista traz só o final do histórico; o completo é buscado quando o usuário abre o card (e acompanhado se estiver rodando).
function QueueProgress({ item }: { item: QueueItem }) {
  const [open, setOpen] = useState(item.status === 'running')
  const [full, setFull] = useState<string | null>(null)
  const [error, setError] = useState('')
  const wantsFull = open && !!item.progress_more
  const load = useCallback(() => {
    api.queueProgress(item.id).then(result => { setFull(result.progress); setError('') }).catch(err => setError(err.message))
  }, [item.id])
  useIdlePolling(load, wantsFull && item.status === 'running' ? 10000 : null)
  useEffect(() => { if (wantsFull && item.status !== 'running') load() }, [wantsFull, item.status, load])
  const text = wantsFull && full !== null ? full : item.progress
  return <details open={open} onToggle={event => setOpen((event.currentTarget as HTMLDetailsElement).open)}>
    <summary>Histórico do processamento{item.progress_more && !(wantsFull && full !== null) ? ' (parcial)' : ''}</summary>
    {error && <p className="inline-message">{error}</p>}<pre>{text}</pre>
  </details>
}

function QueueCard({ item, statusLabels, action }: { item: QueueItem; statusLabels: Record<string, string>; action: (id: number, name: 'cancel' | 'retry') => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  async function run(name: 'cancel' | 'retry') { setBusy(true); try { await action(item.id, name) } finally { setBusy(false) } }
  return <article className="operation-card"><div className="operation-head"><div><strong>#{item.id} · {kinds[item.kind] || item.kind}</strong><small>Criado {formatDate(item.created_at, true)}</small></div><span className={`queue-status ${item.status}`}>{statuses[item.status] || item.status}</span></div>
    <QueueSubjectView item={item} statusLabels={statusLabels} />
    <div className="operation-meta"><span>Tentativas: {item.attempts}/{item.max_attempts}</span>{item.next_run_at && <span>Próxima: {formatDate(item.next_run_at, true)}</span>}</div>
    <QueueDecisionView item={item} />
    {item.last_step && <p>{item.last_step}</p>}{item.outcome && <p className="operation-outcome">{item.outcome}</p>}
    {item.progress && <QueueProgress item={item} />}
    <div className="job-actions">{['pending', 'running', 'retry_wait'].includes(item.status) && <button className="secondary" disabled={busy} onClick={() => run('cancel')}>Cancelar</button>}{item.kind !== 'linkedin_apply' && ['running', 'retry_wait', 'failed', 'cancelled'].includes(item.status) && <button className="secondary" disabled={busy} onClick={() => run('retry')}>Reenfileirar</button>}</div>
  </article>
}

export function QueueView({ notify, statusLabels }: { notify: (message: string, error?: boolean) => void; statusLabels: Record<string, string> }) {
  const [data, setData] = useState<QueueData | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const refresh = usePolling(api.queue, setData, setError, data?.items.some(item => item.status === 'running') ? 10000 : 30000)
  async function action(name: 'retry-all' | 'evaluate-new' | 'clear') {
    if (name === 'clear' && !confirmClear) { setConfirmClear(true); return }
    setBusy(true)
    try { const result = await api.queueAction(name); notify(result.notice); setConfirmClear(false); refresh() }
    catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  async function itemAction(id: number, name: 'cancel' | 'retry') {
    try { const result = await api.queueItem(id, name); notify(result.notice); refresh() }
    catch (err) { notify((err as Error).message, true) }
  }
  return <><section className="filter-panel"><p className="eyebrow">PROCESSAMENTO</p><h2>Fila de IA</h2><p className="section-description">Acompanhe análises e candidaturas. Itens com erro podem ser reenfileirados.</p>
    <div className="queue-stats">{(['pending', 'running', 'retry_wait', 'succeeded', 'failed'] as const).map(key => <div key={key}><strong>{data?.counts[key] || 0}</strong><span>{statuses[key]}</span></div>)}</div>
    <div className="job-actions"><button disabled={busy} onClick={() => action('evaluate-new')}>Triar vagas novas</button><button className="secondary" disabled={busy} onClick={() => action('retry-all')}>Reenfileirar falhas</button><button className="secondary" disabled={busy} onClick={() => action('clear')}>{confirmClear ? 'Confirmar limpeza' : 'Limpar concluídos'}</button>{confirmClear && <button className="secondary" onClick={() => setConfirmClear(false)}>Cancelar</button>}</div>
  </section>{error && <p className="banner error" role="alert">{error} <button onClick={refresh}>Tentar novamente</button></p>}{!data ? <p className="empty">Carregando fila…</p> : data.items.length ? <div className="operation-list">{data.items.map(item => <QueueCard key={item.id} item={item} statusLabels={statusLabels} action={itemAction} />)}</div> : <p className="empty">Nenhum job na fila.</p>}</>
}

export function LogsView({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [data, setData] = useState<LogItem[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  const loader = useCallback(() => api.logs().then(result => result.items), [])
  const refresh = usePolling(loader, setData, setError, 30000)
  async function clear() {
    if (!confirmClear) { setConfirmClear(true); return }
    setBusy(true)
    try { const result = await api.clearLogs(); notify(result.notice); setConfirmClear(false); refresh() }
    catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  return <section className="section"><div className="section-head"><div><p className="eyebrow">ATIVIDADE DO SISTEMA</p><h2>Logs recentes</h2></div><div className="job-actions"><button className="secondary" onClick={refresh}>Atualizar</button><button className="secondary" disabled={busy} onClick={clear}>{confirmClear ? 'Confirmar limpeza' : 'Limpar logs'}</button>{confirmClear && <button className="secondary" onClick={() => setConfirmClear(false)}>Cancelar</button>}</div></div>
    {error && <p className="banner error" role="alert">{error}</p>}
    {!data ? <p className="empty">Carregando logs…</p> : data.length ? <div className="log-list">{data.map(item => <div className="log-item" key={item.id}><time>{formatDate(item.created_at, true)}</time><span className={`log-level ${item.level}`}>{item.level}</span><strong>{item.source}</strong><span>{item.message}</span></div>)}</div> : <p className="empty">Nenhum evento registrado.</p>}
  </section>
}

function StandbyCard({ standby, notify, onDone }: { standby?: StandbyStatus; notify: (message: string, error?: boolean) => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  if (!standby?.configured) return null
  const last = standby.last
  async function sync() {
    setBusy(true)
    try { const result = await api.standbySync(); notify(result.notice, !result.ok) }
    catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false); onDone() }
  }
  return <article className="usage-card-new"><p className="eyebrow">BANCO STANDBY</p><h3>Cópia de segurança</h3>
    {last ? <><div className="usage-big">{last.ok ? 'Em dia' : 'Com falha'} <small>{formatDate(last.at, true)}</small></div>
      <p className="usage-caption">{last.ok ? `${last.rows} linha(s) em ${last.seconds}s.` : last.error}</p></> : <p className="usage-caption">Nenhuma cópia feita ainda. A rotina diária roda de madrugada.</p>}
    <div className="job-actions"><button className="secondary" disabled={busy} onClick={sync}>{busy ? 'Copiando…' : 'Sincronizar standby agora'}</button></div>
  </article>
}

export function UsageView({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [data, setData] = useState<UsageData | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const refresh = usePolling(api.usage, setData, setError, 60000)  // o servidor limita as consultas externas (15 s com coleta ativa, 5 min parado)
  async function update() {
    setBusy(true)
    try { const result = await api.refreshUsage(); setData(result.usage); notify(result.notice, !result.ok) }
    catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  const apify = data?.apify
  const { limit, used, percent } = apifyUsage(apify)
  return <><section className="section"><div className="section-head"><div><p className="eyebrow">CUSTOS E LIMITES</p><h2>Uso e cotas</h2></div><button className="secondary" disabled={busy} onClick={update}>{busy ? 'Consultando…' : 'Atualizar cotas agora'}</button></div>
    {error && <p className="banner error" role="alert">{error} <button onClick={refresh}>Tentar novamente</button></p>}
    {!data ? <p className="empty">Carregando uso…</p> : <div className="usage-grid-new"><article className="usage-card-new"><p className="eyebrow">APIFY</p><h3>Consumo do ciclo</h3>{apify?.error && <p className="inline-message">{apify.error}</p>}<div className="usage-big">US$ {used.toFixed(2)} <small>de US$ {limit.toFixed(2)}</small></div><QuotaBar percent={percent} label="Consumo da cota Apify" /><p className="usage-caption">{percent.toFixed(0)}% do limite · {apify?.cycle_label || 'Ciclo não informado'}</p><p className="usage-caption">Atualiza sozinha. Última consulta: {formatDate(apify?.fetched_at || null, true)}</p></article>
      <TokenHarborCard quota={data.tokenharbor} />
      <StandbyCard standby={data.standby} notify={notify} onDone={refresh} />
      <article className="usage-card-new"><p className="eyebrow">INTELIGÊNCIA ARTIFICIAL</p><h3>{data.provider === 'gemini' ? 'Gemini' : data.provider === 'openai' ? 'OpenAI' : data.provider} <small>{data.model}</small></h3><p className="usage-caption">Estimativa local das chamadas feitas por este aplicativo.</p><div className="usage-metrics-new"><div><span>Chamadas hoje</span><strong>{data.ai.day.calls_ok || 0}</strong></div><div><span>Erros de quota hoje</span><strong>{data.ai.day.calls_quota_error || 0}</strong></div><div><span>Tokens hoje</span><strong>{data.ai.day.tokens_in || 0} / {data.ai.day.tokens_out || 0}</strong></div><div><span>Chamadas no mês</span><strong>{data.ai.month.calls_ok || 0}</strong></div><div><span>Tokens no mês</span><strong>{data.ai.month.tokens_in || 0} / {data.ai.month.tokens_out || 0}</strong></div></div></article></div>}
  </section></>
}

type ChatTurn = { role: 'user' | 'assistant'; content: string }

export function AssistantView({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [question, setQuestion] = useState('')
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [busy, setBusy] = useState(false)
  async function submit() {
    const text = question.trim()
    if (!text || busy) return
    const previous = turns
    setQuestion('')
    setTurns([...previous, { role: 'user', content: text }])
    setBusy(true)
    try {
      const result = await api.assistant(text, previous)
      setTurns(current => [...current, { role: 'assistant', content: result.answer }])
    } catch (err) {
      setQuestion(text)
      notify((err as Error).message, true)
    } finally { setBusy(false) }
  }
  return <section className="section chat-section"><div className="section-head"><div><p className="eyebrow">APOIO A FORMULÁRIOS</p><h2>Assistente</h2></div><button className="secondary" disabled={busy || turns.length === 0} onClick={() => setTurns([])}>Limpar conversa</button></div>
    <p className="section-description">Cole a pergunta de um formulário. A resposta usa seu perfil e os currículos analisados.</p>
    <div className="chat-messages" aria-live="polite">{turns.length === 0 && <p className="empty">A conversa começa quando você enviar uma pergunta.</p>}{turns.map((turn, index) => <div className={`chat-turn ${turn.role}`} key={index}><span>{turn.role === 'user' ? 'Você' : 'Assistente'}</span><p>{turn.content}</p>{turn.role === 'assistant' && <button className="secondary" onClick={() => navigator.clipboard.writeText(turn.content).then(() => notify('Resposta copiada.')).catch(() => notify('Não foi possível copiar a resposta.', true))}>Copiar</button>}</div>)}{busy && <p className="chat-wait">Preparando resposta…</p>}</div>
    <div className="chat-compose"><label htmlFor="assistant-question">Pergunta ou campo do formulário</label><textarea id="assistant-question" value={question} onChange={event => setQuestion(event.target.value)} rows={4} maxLength={4000} placeholder="Ex.: Quantos anos de experiência você tem com Python?" onKeyDown={event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submit() } }} /><div><span>Ctrl + Enter para enviar</span><button disabled={busy || !question.trim()} onClick={submit}>Enviar pergunta</button></div></div>
  </section>
}

function ResumeCard({ language, record, refresh, notify }: {
  language: 'pt' | 'en'; record: ResumeRecord | null; refresh: () => void
  notify: (message: string, error?: boolean) => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  async function upload() {
    if (!file || busy) return
    setBusy(true)
    try {
      const result = await api.uploadResume(language, file)
      notify(result.notice, !result.ok)
      if (result.ok) { setFile(null); if (fileInput.current) fileInput.current.value = ''; refresh() }
    } catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  async function reanalyze() {
    setBusy(true)
    try { const result = await api.reanalyzeResume(language); notify(result.notice, !result.ok); refresh() }
    catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  return <article className="resume-card"><div className="resume-head"><div><p className="eyebrow">{language === 'pt' ? 'PORTUGUÊS' : 'ENGLISH'}</p><h3>Currículo {language.toUpperCase()}</h3></div><span className="status-pill">{record?.status || 'Não enviado'}</span></div>
    {record ? <div className="resume-info"><strong>{record.filename || 'currículo.pdf'}</strong><span>{record.message || 'Sem mensagem de análise.'}</span><span>Última análise: {formatDate(record.analyzed_at, true)}</span><span>{record.provider && `${record.provider} · ${record.model}`}</span>{record.error && <p className="inline-message">{record.error}</p>}</div> : <p className="section-description">Envie um PDF para analisar e usar o currículo na triagem.</p>}
    {record?.summary && <details className="resume-details"><summary>Resumo da análise</summary><pre>{record.summary}</pre></details>}
    {record?.analysis_json && <details className="resume-details"><summary>Dossiê completo</summary><pre>{record.analysis_json}</pre></details>}
    <label htmlFor={`resume-file-${language}`}>Selecionar PDF</label><input ref={fileInput} id={`resume-file-${language}`} type="file" accept="application/pdf,.pdf" onChange={event => setFile(event.target.files?.[0] || null)} />
    <div className="job-actions"><button disabled={!file || busy} onClick={upload}>Enviar currículo</button>{record && <button className="secondary" disabled={busy} onClick={reanalyze}>Reanalisar com IA</button>}</div>
  </article>
}

function PortfolioCard({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [status, setStatus] = useState<PortfolioStatus | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const refresh = usePolling(api.portfolio, setStatus, setError, null)  // sob demanda: só ao abrir e ao sincronizar
  useEffect(() => { refresh() }, [refresh])
  async function sync() {
    setBusy(true)
    try { const result = await api.syncPortfolio(); setStatus(result.status); notify(result.notice, !result.ok) }
    catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  const counts = status?.counts
  return <article className="resume-card"><div className="resume-head"><div><p className="eyebrow">FONTE EXTRA</p><h3>Portfólio online</h3></div><span className="status-pill">{!status ? '…' : !status.url ? 'Desligado' : status.error ? 'Com erro' : status.version ? 'Sincronizado' : 'Pendente'}</span></div>
    {error && <p className="inline-message">{error} <button className="secondary" onClick={refresh}>Tentar novamente</button></p>}
    {status && (status.url ? <div className="resume-info"><strong>{status.url}</strong>{status.version && counts && <span>{counts.roles} cargo(s) · {counts.projects} projeto(s) · {counts.skills} skill(s) · {counts.education} formação(ões)</span>}<span>Última sincronização: {formatDate(status.synced_at, true)}</span>{status.error && <p className="inline-message">{status.error}</p>}</div> : <p className="section-description">Informe a URL do portfólio em Configurações › Perfil para usar projetos e cargos atuais como evidência na triagem.</p>)}
    <div className="job-actions"><button disabled={busy || !status?.url} onClick={sync}>Sincronizar portfólio</button></div>
  </article>
}

export function ResumesView({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [data, setData] = useState<ResumesData | null>(null)
  const [error, setError] = useState('')
  const refresh = usePolling(api.resumes, setData, setError, 60000)  // após enviar/reanalisar o card chama refresh()
  return <section className="section"><div className="section-head"><div><p className="eyebrow">PERFIL PROFISSIONAL</p><h2>Currículos</h2></div><button className="secondary" onClick={refresh}>Atualizar</button></div>
    {error && <p className="banner error" role="alert">{error} <button onClick={refresh}>Tentar novamente</button></p>}
    {!data ? <p className="empty">Carregando currículos…</p> : <div className="resume-grid"><ResumeCard language="pt" record={data.pt} refresh={refresh} notify={notify} /><ResumeCard language="en" record={data.en} refresh={refresh} notify={notify} /><PortfolioCard notify={notify} /></div>}
  </section>
}
