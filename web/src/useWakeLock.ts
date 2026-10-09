import { useEffect } from 'react'

/**
 * Keeps the screen on while `active`. Phones cut the microphone when they lock, so this matters while recording.
 * The lock is dropped whenever the page is hidden, so it is re-requested when the page becomes visible again.
 */
export const useWakeLock = (active: boolean) => {
  useEffect(() => {
    if (!active || !('wakeLock' in navigator)) return
    let lock: WakeLockSentinel | null = null
    let released = false

    const request = async () => {
      try {
        lock = await navigator.wakeLock.request('screen')
        if (released) void lock.release()
      } catch {
        // Denied (e.g. low battery mode); recording still works while the screen stays on.
      }
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') void request()
    }

    void request()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      released = true
      document.removeEventListener('visibilitychange', onVisible)
      void lock?.release()
    }
  }, [active])
}
