// Scrolls the app makes itself (following an agent edit, the live transcript, restoring your place) must not be
// mistaken for the user scrolling, or the bottom bar folds and unfolds while the agent works.
const SMOOTH_SCROLL_MS = 900

let until = 0

/** Call just before scrolling the note on the user's behalf. */
export const markAppScroll = () => {
  until = performance.now() + SMOOTH_SCROLL_MS
}

export const isAppScroll = () => performance.now() < until

/**
 * Bring `element` into view by scrolling only its nearest scrolling container (the note), never the page.
 * `scrollIntoView` scrolls every ancestor it can, which moved the whole app when the note was already at its end.
 */
export const scrollWithin = (element: Element, block: 'center' | 'end') => {
  const pane = element.closest('.pane-note, .manager-scroll') ?? element.parentElement?.closest('[data-scroll]')
  if (!(pane instanceof HTMLElement)) return
  markAppScroll()
  const box = element.getBoundingClientRect()
  const frame = pane.getBoundingClientRect()
  const offset = block === 'end' ? box.bottom - frame.bottom : box.top + box.height / 2 - (frame.top + frame.height / 2)
  pane.scrollTo({ top: pane.scrollTop + offset, behavior: 'smooth' })
}
