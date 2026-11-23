import { useEffect } from 'react'

// Depois de IDLE_MS sem interação a aba para de consultar o servidor (e o banco). Qualquer clique, tecla, rolagem
// ou foco retoma na hora com uma atualização. Evita que uma aba esquecida aberta mantenha o banco acordado.
export const IDLE_MS = 10 * 60 * 1000

let lastActivity = Date.now()
let installed = false
const resumeListeners = new Set<() => void>()

export const isIdle = () => Date.now() - lastActivity > IDLE_MS

function onActivity() {
  const wasIdle = isIdle()
  lastActivity = Date.now()
  if (wasIdle) resumeListeners.forEach(listener => listener())
}

function install() {
  if (installed) return
  installed = true
  for (const name of ['pointerdown', 'keydown', 'wheel', 'touchstart', 'scroll', 'mousemove', 'focus']) {
    window.addEventListener(name, onActivity, { passive: true, capture: true })
  }
}

/**
 * Chama `callback` agora e a cada `delay` ms enquanto a aba está visível e o usuário ativo; `delay` null desliga.
 * Quando a identidade de `callback` muda (filtros, revisão) a atualização acontece de novo na hora.
 */
export function useIdlePolling(callback: () => void, delay: number | null) {
  useEffect(() => {
    install()
    if (delay === null) return
    const tick = () => { if (!document.hidden && !isIdle()) callback() }
    tick()
    const interval = window.setInterval(tick, delay)
    document.addEventListener('visibilitychange', tick)
    resumeListeners.add(tick)
    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', tick)
      resumeListeners.delete(tick)
    }
  }, [callback, delay])
}
