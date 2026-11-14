import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// In development the API runs separately (`uv run python -m omniws`) and Vite forwards /api to it.
const api = process.env.OMNI_API ?? 'http://127.0.0.1:8300'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': { target: api, changeOrigin: false, ws: true },
    },
  },
})
