import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.tsx'
import { purgeLegacySessionToken } from './services/legacyAuthCleanup'
import './index.css'

// Migration: drop any pre-cookie session JWT left in localStorage before render.
purgeLegacySessionToken()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
