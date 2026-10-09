import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Settings } from '../types'

type SettingsDialogProps = {
  project: string | null
  instructions: string
  onClose: () => void
}

export const SettingsDialog = ({ project, instructions, onClose }: SettingsDialogProps) => {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [prompt, setPrompt] = useState('')
  const [silenceMs, setSilenceMs] = useState(800)
  const [noteInstructions, setNoteInstructions] = useState(instructions)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void api.getSettings().then((loaded) => {
      setSettings(loaded)
      setPrompt(loaded.system_prompt)
      setSilenceMs(loaded.silence_ms)
    })
  }, [])

  const save = async () => {
    setSaving(true)
    await api.saveSettings(prompt, silenceMs)
    if (project && noteInstructions !== instructions) await api.saveInstructions(project, noteInstructions)
    setSaving(false)
    onClose()
  }

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog" onClick={(event) => event.stopPropagation()}>
        {project && (
          <>
            <h2>Instructions for this project</h2>
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
        <h2>Pause length</h2>
        <p className="hint">
          How long you pause before what you said is sent. Longer suits narration, where you pause mid-sentence.
          Applies from the next time you tap the mic.
        </p>
        <div className="range-row">
          <input
            type="range"
            min={500}
            max={3000}
            step={100}
            value={silenceMs}
            onChange={(event) => setSilenceMs(Number(event.target.value))}
          />
          <span className="range-value">{(silenceMs / 1000).toFixed(1)} s</span>
        </div>
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
