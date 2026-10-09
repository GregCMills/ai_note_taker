const TARGET_RATE = 16000
const CHUNK_SAMPLES = 1600

// Downsamples the mic to 16 kHz mono PCM16 and posts ~100 ms chunks back to the main thread.
const WORKLET = `
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this.ratio = sampleRate / ${TARGET_RATE}
    this.phase = 0
    this.sum = 0
    this.count = 0
    this.out = new Int16Array(${CHUNK_SAMPLES})
    this.length = 0
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0]
    if (!channel) return true
    for (let i = 0; i < channel.length; i++) {
      this.sum += channel[i]
      this.count++
      this.phase++
      if (this.phase >= this.ratio) {
        this.phase -= this.ratio
        const sample = Math.max(-1, Math.min(1, this.sum / this.count))
        this.out[this.length++] = sample * 32767
        this.sum = 0
        this.count = 0
        if (this.length === this.out.length) {
          this.port.postMessage(this.out.buffer, [this.out.buffer])
          this.out = new Int16Array(${CHUNK_SAMPLES})
          this.length = 0
        }
      }
    }
    return true
  }
}
registerProcessor('pcm-capture', PcmCapture)
`

export type MicrophoneCapture = { stop: () => void }

export const startMicrophone = async (onChunk: (pcm: ArrayBuffer) => void): Promise<MicrophoneCapture> => {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('Microphone needs HTTPS. Open this page via https:// and accept the certificate.')
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  })
  const context = new AudioContext()
  const moduleUrl = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }))
  await context.audioWorklet.addModule(moduleUrl)
  URL.revokeObjectURL(moduleUrl)

  const source = context.createMediaStreamSource(stream)
  const capture = new AudioWorkletNode(context, 'pcm-capture')
  capture.port.onmessage = (event: MessageEvent<ArrayBuffer>) => onChunk(event.data)
  source.connect(capture)

  return {
    stop: () => {
      source.disconnect()
      capture.disconnect()
      stream.getTracks().forEach((track) => track.stop())
      void context.close()
    },
  }
}
