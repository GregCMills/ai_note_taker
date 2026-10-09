import { useCallback, useEffect, useState } from 'react'

/** Full-screen toggle. iPhone Safari has no full-screen API for pages, so `supported` is false there. */
export const useFullscreen = () => {
  const supported = typeof document !== 'undefined' && document.fullscreenEnabled
  const [active, setActive] = useState(false)

  useEffect(() => {
    const update = () => setActive(document.fullscreenElement !== null)
    document.addEventListener('fullscreenchange', update)
    return () => document.removeEventListener('fullscreenchange', update)
  }, [])

  const toggle = useCallback(() => {
    if (document.fullscreenElement) void document.exitFullscreen()
    else void document.documentElement.requestFullscreen({ navigationUI: 'hide' }).catch(() => {})
  }, [])

  return { supported, active, toggle }
}
