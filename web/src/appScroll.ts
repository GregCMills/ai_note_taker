// Scrolls the app makes itself (following an agent edit, the live transcript, restoring your place) must not be
// mistaken for the user scrolling, or the bottom bar folds and unfolds while the agent works.
const SMOOTH_SCROLL_MS = 900

let until = 0

/** Call just before scrolling the note on the user's behalf. */
export const markAppScroll = () => {
  until = performance.now() + SMOOTH_SCROLL_MS
}

export const isAppScroll = () => performance.now() < until
