import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Settings } from '../types'

type SettingsDialogProps = {
  path: string | null
  instructions: string
  onClose: () => void
}

export const SettingsDialog = ({ path, instructions, onClose }: SettingsDialogProps) => {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [prompt, setPrompt] = useState('')
  const [noteInstructions, setNoteInstructions] = useState(instructions)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void api.getSettings().then((loaded) => {
      setSettings(loaded)
      setPrompt(loaded.system_prompt)
    })
  }, [])

  const save = async () => {
    setSaving(true)
    await api.saveSettings(prompt)
    if (path && noteInstructions !== instructions) await api.saveInstructions(path, noteInstructions)
    setSaving(false)
    onClose()
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog" onClick={(event) => event.stopPropagation()}>
        {path && (
          <>
            <h2>Instructions for this note</h2>
            <p className="hint">
              Saved when you tell it how to take notes (“always put to-dos at the top”). Reminded on every update.
            </p>
            <textarea
              className="editor short"
              value={noteInstructions}
              placeholder="None yet"
              onChange={(event) => setNoteInstructions(event.target.value)}
            />
          </>
        )}
        <h2>System prompt</h2>
        <p className="hint">How the LLM shapes all notes. Tool and undo instructions are added automatically.</p>
        <textarea className="editor" value={prompt} onChange={(event) => setPrompt(event.target.value)} />
        <div className="actions">
          <button
            className="button"
            disabled={!settings}
            onClick={() => settings && setPrompt(settings.default_system_prompt)}
          >
            Reset prompt
          </button>
          <span className="spacer" />
          <button className="button" onClick={onClose}>
            Cancel
          </button>
          <button className="button primary" disabled={saving || !settings} onClick={save}>
            Save
          </button>
        </div>
      </div>
    </div>
  )
}
