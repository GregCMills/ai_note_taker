import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const backend = process.env.BACKEND ?? 'https://localhost:8443'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: backend, secure: false },
      '/ws': { target: backend, secure: false, ws: true },
    },
  },
})
