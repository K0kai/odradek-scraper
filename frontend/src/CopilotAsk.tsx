import { useEffect, useRef, useState } from 'react'
import { api, type CopilotAsk as PendingAsk } from './api'

export function CopilotAsk({ notify }: { notify: (message: string, error?: boolean) => void }) {
  const [ask, setAsk] = useState<PendingAsk | null>(null)
  const [answer, setAnswer] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  function refresh() {
    if (document.hidden) return
    api.pendingAsk().then(result => setAsk(result.ask)).catch(() => {})
  }
  useEffect(() => {
    refresh()
    const interval = window.setInterval(refresh, 3000)
    document.addEventListener('visibilitychange', refresh)
    return () => { window.clearInterval(interval); document.removeEventListener('visibilitychange', refresh) }
  }, [])
  useEffect(() => { setAnswer(''); setFile(null); if (fileInput.current) fileInput.current.value = '' }, [ask?.id])

  async function respond() {
    if (!ask || busy || (ask.needs_file ? !file : !answer.trim())) return
    setBusy(true)
    try {
      const result = await api.answerAsk(ask.id, answer.trim(), file)
      notify(result.notice, !result.ok)
      if (result.ok) { setAsk(null); refresh() }
    } catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  async function act(action: 'cancel' | 'manual') {
    if (!ask || busy) return
    setBusy(true)
    try {
      const result = await api.askAction(ask.id, action)
      notify(result.notice, !result.ok)
      if (result.ok) { setAsk(null); refresh() }
    } catch (err) { notify((err as Error).message, true) }
    finally { setBusy(false) }
  }
  if (!ask) return null
  return <section className="copilot-ask" aria-live="polite"><div><p className="eyebrow">AÇÃO NECESSÁRIA</p><h2>O copiloto precisa da sua resposta</h2><p>{ask.question}</p>{ask.hint && <small>{ask.hint}</small>}</div>
    {ask.needs_file ? <label>Anexar arquivo<input ref={fileInput} type="file" onChange={event => setFile(event.target.files?.[0] || null)} /></label> : <label>Sua resposta<textarea value={answer} onChange={event => setAnswer(event.target.value)} rows={3} /></label>}
    <div className="job-actions"><button disabled={busy || (ask.needs_file ? !file : !answer.trim())} onClick={respond}>Enviar resposta</button><button className="secondary" disabled={busy} onClick={() => act('manual')}>Assumir no Chrome</button><button className="secondary" disabled={busy} onClick={() => act('cancel')}>Cancelar pergunta</button></div>
  </section>
}
