import type { ApifyQuota, TokenHarborQuota } from './api'
import { formatDate } from './format'

function tone(percent: number) { return percent >= 90 ? 'bad' : percent >= 70 ? 'warn' : 'ok' }

export function QuotaBar({ percent, label }: { percent: number; label: string }) {
  const value = Math.min(100, Math.max(0, percent))
  return <div className={`progress-track ${tone(value)}`} role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(value)}><div style={{ width: `${value}%` }} /></div>
}

export function apifyUsage(quota: ApifyQuota | null | undefined) {
  const limit = Number(quota?.local_limit_usd || quota?.api_limit_usd || 0)
  const used = Number(quota?.used_usd || 0)
  return { limit, used, percent: limit > 0 ? Math.min(100, Math.max(0, used / limit * 100)) : 0 }
}

/** Faixa mostrada na visão geral enquanto a coleta roda: o backend renova a cota a cada ~15 s. */
export function ApifyQuotaStrip({ quota }: { quota: ApifyQuota | null | undefined }) {
  if (!quota || (!quota.fetched_at && !quota.error)) return null
  if (quota.error) return <section className="quota-strip"><p className="eyebrow">COTA APIFY AO VIVO</p><p className="usage-caption">{quota.error}</p></section>
  const { limit, used, percent } = apifyUsage(quota)
  return <section className="quota-strip"><div className="quota-strip-head"><p className="eyebrow">COTA APIFY AO VIVO</p><span>US$ {used.toFixed(2)} <small>de US$ {limit.toFixed(2)} · {percent.toFixed(0)}%</small></span></div>
    <QuotaBar percent={percent} label="Consumo da cota Apify" />
    <p className="usage-caption">{quota.cycle_label || 'Ciclo não informado'} · atualizada {formatDate(quota.fetched_at || null, true)}</p></section>
}

export function TokenHarborCard({ quota }: { quota: TokenHarborQuota | undefined }) {
  if (!quota?.configured) return <article className="usage-card-new"><p className="eyebrow">TOKENHARBOR</p><h3>Fallback gratuito</h3><p className="usage-caption">Chave não configurada. Salve a chave em Configurações → Contas e chaves para acompanhar a cota dos modelos gratuitos.</p></article>
  if (quota.error) return <article className="usage-card-new"><p className="eyebrow">TOKENHARBOR</p><h3>Fallback gratuito</h3><p className="inline-message">{quota.error}</p></article>
  const free = quota.used_pct ?? 0
  const plan = quota.plan_used_pct ?? 0
  return <article className="usage-card-new"><p className="eyebrow">TOKENHARBOR</p><h3>Modelos gratuitos <small>{quota.on_plan ? quota.plan_name : 'sem plano'}</small></h3>
    {!quota.models_enabled && <p className="inline-message">Os modelos gratuitos estão desativados na conta: as chamadas :free retornam 403. Ative-os no painel do TokenHarbor.</p>}
    {quota.exhausted && <p className="inline-message">Cota gratuita esgotada: os modelos :free ficam parados até {formatDate(quota.reset_at || null, true)}.</p>}
    <div className="usage-big">{free}% <small>da cota gratuita usada</small></div>
    <QuotaBar percent={free} label="Cota gratuita do TokenHarbor" />
    {quota.on_plan && <><p className="usage-caption">Plano {quota.plan_name}: {plan}% usado{quota.plan_exhausted ? ' (esgotado)' : ''}</p><QuotaBar percent={plan} label="Cota do plano do TokenHarbor" /></>}
    <p className="usage-caption">{quota.activated ? `Janela de 7 dias reinicia em ${formatDate(quota.reset_at || null, true)}` : 'A janela de 7 dias começa na primeira chamada.'}</p>
    <p className="usage-caption">Modelos: {quota.models?.length ? quota.models.join(', ') : '—'}</p>
    <p className="usage-caption">A API informa só percentuais. Última consulta: {formatDate(quota.fetched_at || null, true)}</p></article>
}
