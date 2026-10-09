import { Editor, defaultValueCtx, remarkStringifyOptionsCtx, rootCtx } from '@milkdown/kit/core'
import { history } from '@milkdown/kit/plugin/history'
import { listener, listenerCtx } from '@milkdown/kit/plugin/listener'
import { commonmark } from '@milkdown/kit/preset/commonmark'
import { gfm } from '@milkdown/kit/preset/gfm'
import { useEffect, useRef } from 'react'

/** Empty paragraphs come out as "<br />" lines; drop them so saving doesn't litter the note. */
const tidy = (markdown: string) => markdown.replace(/^<br \/>[ \t]*\n?/gm, '').replace(/\n{3,}/g, '\n\n').trimStart()

type RichEditorProps = {
  initial: string
  onChange: (markdown: string) => void
}

/**
 * Edits markdown in place, rendered the same way the note is displayed (Obsidian-style): typing "## " makes a
 * heading, "- " a bullet, **text** bold. Remount it (change its key) to load different content.
 */
export const RichEditor = ({ initial, onChange }: RichEditorProps) => {
  const hostRef = useRef<HTMLDivElement>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let editor: Editor | null = null
    let disposed = false
    Editor.make()
      .config((ctx) => {
        ctx.set(rootCtx, host)
        ctx.set(defaultValueCtx, initial)
        // Write markdown the way the notes already use it, so untouched text is saved unchanged.
        ctx.update(remarkStringifyOptionsCtx, (options) => ({
          ...options,
          bullet: '-' as const,
          emphasis: '*' as const,
          strong: '*' as const,
          rule: '-' as const,
        }))
        ctx.get(listenerCtx).markdownUpdated((_ctx, markdown) => onChangeRef.current(tidy(markdown)))
      })
      .use(commonmark)
      .use(gfm)
      .use(history)
      .use(listener)
      .create()
      .then((created) => {
        if (disposed) void created.destroy()
        else editor = created
      })
    return () => {
      disposed = true
      void editor?.destroy()
      host.replaceChildren()
    }
    // Created once per mount; the parent remounts it to load new content.
  }, [])

  return <div ref={hostRef} className="markdown rich-editor" />
}
