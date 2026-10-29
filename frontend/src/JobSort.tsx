type SortColumn = 'match' | 'seen' | 'posted'
type Direction = 'asc' | 'desc'

const columns: { key: SortColumn; label: string }[] = [
  { key: 'match', label: 'Match' },
  { key: 'seen', label: 'Encontrada' },
  { key: 'posted', label: 'Publicada' },
]

function activeSorts(value: string): string[] {
  return value.split(/[,+]/).filter(token => /^(match|seen|posted)_(asc|desc)$/.test(token))
}

function cycleSort(value: string, column: SortColumn): string {
  const current = activeSorts(value)
  const selected = current.find(token => token.startsWith(`${column}_`))
  const remaining = current.filter(token => !token.startsWith(`${column}_`))
  const next: Direction | null = !selected ? 'asc' : selected.endsWith('_asc') ? 'desc' : null
  return next ? [`${column}_${next}`, ...remaining].join(',') : remaining.join(',')
}

export function JobSort({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const active = activeSorts(value)
  return <div className="job-sort">
    <div className="job-sort-heading"><strong>Ordenação</strong><span>Clique para alternar: crescente → decrescente → desligada. O último critério ativado tem prioridade.</span></div>
    <div className="job-sort-options">
      {columns.map(({ key, label }) => {
        const index = active.findIndex(token => token.startsWith(`${key}_`))
        const direction = index < 0 ? null : active[index].endsWith('_asc') ? 'asc' : 'desc'
        return <button type="button" className={`job-sort-option${direction ? ' active' : ''}`} aria-pressed={Boolean(direction)} key={key} onClick={() => onChange(cycleSort(value, key))}>
          <span>{label}</span><span className="job-sort-state">{direction ? `${index + 1}º ${direction === 'asc' ? '↑' : '↓'}` : 'Desligada'}</span>
        </button>
      })}
    </div>
  </div>
}
