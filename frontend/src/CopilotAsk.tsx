import { useEffect, useRef, useState } from 'react'
import { api, type CopilotAsk as PendingAsk } from './api'

// O pedido pendente chega dentro do /api/dashboard (sem polling próprio, que mantinha o banco acordado).
export function CopilotAsk({ ask: incoming, notify, onResolved }: {
  ask: PendingAsk | null
  notify: (message: string, error?: boolean) => void
  onResolved: () => void
}) {
  const [answer, setAnswer] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [resolvedId, setResolvedId] = useState<number | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const ask = incoming && incoming.id !== resolvedId ? incoming : null

  useEffect(() => { setAnswer(''); setFile(null); if (fileInput.current) fileInput.current.value = '' }, [ask?.id])

  async function respond() {
    if (!ask || busy || (ask.needs_file ? !file : !answer.trim())) return
    setBusy(true)
    try {
      const result = await api.answerAsk(ask.id, answer.trim(), file)
      notify(result.notice, !result.ok)
      if (result.ok) { setResolvedId(ask.id); onResolved() }
    } catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  async function act(action: 'cancel' | 'manual') {
    if (!ask || busy) return
    setBusy(true)
    try {
      const result = await api.askAction(ask.id, action)
      notify(result.notice, !result.ok)
      if (result.ok) { setResolvedId(ask.id); onResolved() }
    } catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  if (!ask) return null
  return <section className="copilot-ask" aria-live="polite"><div><p className="eyebrow">AÇÃO NECESSÁRIA</p><h2>O copiloto precisa da sua resposta</h2><p>{ask.question}</p>{ask.hint && <small>{ask.hint}</small>}</div>
    {ask.needs_file ? <label>Anexar arquivo<input ref={fileInput} type="file" onChange={event => setFile(event.target.files?.[0] || null)} /></label> : <label>Sua resposta<textarea value={answer} onChange={event => setAnswer(event.target.value)} rows={3} /></label>}
    <div className="job-actions"><button disabled={busy || (ask.needs_file ? !file : !answer.trim())} onClick={respond}>Enviar resposta</button><button className="secondary" disabled={busy} onClick={() => act('manual')}>Assumir no Chrome</button><button className="secondary" disabled={busy} onClick={() => act('cancel')}>Cancelar pergunta</button></div>
  </section>
}
