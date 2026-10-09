import DOMPurify from 'dompurify'
import { Marked, Renderer, type Token, type Tokens } from 'marked'

type Flagged = { changed?: boolean }

const SECTION_MARKER = /^<!-- §(\d+) -->/

const markdown = new Marked({
  renderer: {
    listitem(item: Tokens.ListItem & Flagged) {
      const html = Renderer.prototype.listitem.call(this, item)
      return item.changed ? html.replace(/^<li/, '<li class="changed"') : html
    },
  },
})

const listItems = (tokens: Token[]) =>
  tokens.flatMap((token) => (token.type === 'list' ? (token as Tokens.List).items : []))

/**
 * Renders the note, marking blocks and list items that are new or changed since `baseline`.
 * List items are compared one by one so adding a bullet highlights just that bullet.
 */
export const renderNote = (content: string, baseline: string | null): string => {
  const tokens = markdown.lexer(content)
  const previous = baseline === null ? null : markdown.lexer(baseline)
  const oldBlocks = new Set(previous?.map((token) => token.raw.trim()))
  const oldItems = new Set(previous && listItems(previous).map((item) => item.raw.trim()))

  const renderToken = (token: Token): string => {
    const rendered = (flagged: Token[]) => markdown.parser(Object.assign(flagged, { links: tokens.links }))
    const marker = token.type === 'html' ? SECTION_MARKER.exec(token.raw) : null
    // Hidden section markers become anchors, so the view can scroll to wherever the agent is editing.
    if (marker) return `<span class="section-anchor" data-section="${marker[1]}"></span>`
    if (previous === null || token.type === 'space' || token.type === 'html') return rendered([token])
    if (token.type === 'list') {
      for (const item of (token as Tokens.List).items as (Tokens.ListItem & Flagged)[]) {
        item.changed = !oldItems.has(item.raw.trim())
      }
      return rendered([token])
    }
    const block = rendered([token])
    return oldBlocks.has(token.raw.trim()) ? block : `<div class="changed">${block}</div>`
  }

  // Where text was removed, which has no highlight of its own: just before the unchanged tail of the note.
  // The view scrolls here when an edit only deletes.
  let editPoint = -1
  if (previous !== null) {
    const fromEnd = (list: Token[], i: number) => list[list.length - 1 - i]?.raw.trim()
    let same = 0
    while (same < tokens.length && same < previous.length && fromEnd(tokens, same) === fromEnd(previous, same)) same++
    if (same < previous.length) editPoint = tokens.length - same
  }
  const editMark = '<span class="edit-point"></span>'

  const html =
    tokens.map((token, index) => (index === editPoint ? editMark : '') + renderToken(token)).join('') +
    (editPoint === tokens.length ? editMark : '')

  return DOMPurify.sanitize(html)
}
