import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const backend = process.env.INKWELL_BACKEND ?? 'http://localhost:8000'
const backendWs = backend.replace(/^http/, 'ws')

// https://vite.dev/config/
export default defineConfig({
  base: process.env.INKWELL_BASE_PATH ?? '/',
  plugins: [react()],
  test: {
    environment: 'jsdom',
  },
  server: {
    proxy: {
      '/api': backend,
      '/ws': { target: backendWs, ws: true },
    },
  },
})
