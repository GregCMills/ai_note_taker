import { useCallback, useEffect, useState } from 'react'

// Edge on Android can ignore the request without settling its promise; past this, say so instead of doing nothing.
const ENTER_TIMEOUT_MS = 1500

/** Full-screen toggle. iPhone Safari has no full-screen API for pages, so `supported` is false there. */
export const useFullscreen = (onError: (message: string) => void) => {
  const supported = typeof document !== 'undefined' && document.fullscreenEnabled
  const [active, setActive] = useState(false)

  useEffect(() => {
    const update = () => setActive(document.fullscreenElement !== null)
    document.addEventListener('fullscreenchange', update)
    return () => document.removeEventListener('fullscreenchange', update)
  }, [])

  const toggle = useCallback(async () => {
    if (document.fullscreenElement) {
      void document.exitFullscreen()
      return
    }
    // One request per tap: browsers allow one, so a retry would only fail with "Permissions check failed".
    // No navigationUI option: Chrome hides its bars anyway, and it is one less thing for other browsers to trip on.
    const outcome = await Promise.race([
      document.documentElement.requestFullscreen().then(
        () => null,
        (error: Error) => error.message || error.name,
      ),
      new Promise<string>((resolve) => setTimeout(() => resolve('the browser ignored the request'), ENTER_TIMEOUT_MS)),
    ])
    if (!document.fullscreenElement) onError(`Full screen didn't start: ${outcome ?? 'the browser stayed as it was'}`)
  }, [onError])

  return { supported, active, toggle }
}
