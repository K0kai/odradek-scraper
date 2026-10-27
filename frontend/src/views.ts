export const viewLabels = {
  overview: 'Visão geral',
  jobs: 'Vagas para revisar',
  queue: 'Filas de IA',
  assistant: 'Assistente',
  logs: 'Logs',
  usage: 'Uso e cotas',
  resumes: 'Currículos',
  settings: 'Configurações',
  rules: 'Regras de formulário',
} as const

export type View = keyof typeof viewLabels
export const navigation = Object.keys(viewLabels) as View[]

export function isView(value: string | null): value is View {
  return value !== null && Object.hasOwn(viewLabels, value)
}
