/** Dates shown throughout the panel use the same locale and time zone. */
export function formatDate(value: string | null | undefined, full = false): string {
  if (!value) return 'Sem data'
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return value
  return new Intl.DateTimeFormat('pt-BR', {
    timeZone: 'America/Sao_Paulo',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    ...(full ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(date)
}
