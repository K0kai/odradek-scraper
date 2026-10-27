import { defineConfig } from 'vite'

export default defineConfig({
  base: '/ui/',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/cover-letter.pdf': 'http://127.0.0.1:8765',
    },
  },
})
