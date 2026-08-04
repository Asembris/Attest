// THE REPLAY ENTRY. A separate entry point, not a branch inside `main.tsx`, so the production
// module graph contains nothing from this directory — the shipped bundle is byte-identical to
// the one the submission was verified and the video recorded against.
//
// `App` is imported unchanged. Every component below it is the real one; the only substitution
// is `api/client` -> `api/replayClient`, made by alias in vite.config.ts, so no component
// contains a line of replay-awareness.

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from '../App.tsx';
import ReplayBanner from './ReplayBanner';
import '../index.css';
import './replay.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ReplayBanner />
    <App />
  </StrictMode>,
);
