import { useEffect, useRef, useState } from 'react'
import type { AgentState } from './types'

const FADE_MS = 3000

/**
 * Snapshots every note when the agent starts editing, so its changes can be highlighted in whichever tab they
 * land. Highlights build up while the agent works and fade out a few seconds after it finishes.
 */
export const useEditHighlight = (notes: Record<string, string>, agent: AgentState, project: string | null) => {
  const [baselines, setBaselines] = useState<Record<string, string> | null>(null)
  const [fading, setFading] = useState(false)
  const notesRef = useRef(notes)
  const fadeTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  notesRef.current = notes

  useEffect(() => {
    clearTimeout(fadeTimer.current)
    setBaselines(null)
    setFading(false)
  }, [project])

  useEffect(() => {
    if (agent.state === 'working') {
      clearTimeout(fadeTimer.current)
      setFading(false)
      setBaselines((current) => current ?? { ...notesRef.current })
      return
    }
    setFading(true)
    fadeTimer.current = setTimeout(() => {
      setBaselines(null)
      setFading(false)
    }, FADE_MS)
    return () => clearTimeout(fadeTimer.current)
  }, [agent])

  // A note created during the turn has no snapshot: everything in it is new.
  const baselineFor = (note: string) => (baselines === null ? null : (baselines[note] ?? ''))
  return { baselineFor, fading }
}
