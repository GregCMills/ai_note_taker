/**
 * Bring `element` into view by scrolling only its nearest scrolling container (the note), never the page.
 * `scrollIntoView` scrolls every ancestor it can, which moved the whole app when the note was already at its end.
 */
export const scrollWithin = (element: Element, block: 'center' | 'end') => {
  const pane = element.closest('.pane-note, .manager-scroll') ?? element.parentElement?.closest('[data-scroll]')
  if (!(pane instanceof HTMLElement)) return
  const box = element.getBoundingClientRect()
  const frame = pane.getBoundingClientRect()
  const offset = block === 'end' ? box.bottom - frame.bottom : box.top + box.height / 2 - (frame.top + frame.height / 2)
  pane.scrollTo({ top: pane.scrollTop + offset, behavior: 'smooth' })
}
