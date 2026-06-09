import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.INKWELL_BACKEND ?? 'http://localhost:8000'
const backendWs = backend.replace(/^http/, 'ws')

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': backend,
      '/ws': { target: backendWs, ws: true },
    },
  },
})
