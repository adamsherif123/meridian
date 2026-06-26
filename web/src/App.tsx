import { useEffect, useState } from 'react'
import CanvasPage from './pages/CanvasPage'
import SkeletonPage from './pages/SkeletonPage'
import './App.css'

type View = 'canvas' | 'skeleton'

function currentView(): View {
  return window.location.hash.startsWith('#/skeleton') ? 'skeleton' : 'canvas'
}

export default function App() {
  const [view, setView] = useState<View>(currentView)

  useEffect(() => {
    const onHash = () => setView(currentView())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  return view === 'canvas' ? <CanvasPage /> : <SkeletonPage />
}
