import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.tsx'
import { purgeLegacySessionToken } from './services/legacyAuthCleanup'
import { captureLinkPrefill } from './lib/linkPrefill'
import './index.css'

// Migration: drop any pre-cookie session JWT left in localStorage before render.
purgeLegacySessionToken()
captureLinkPrefill()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
