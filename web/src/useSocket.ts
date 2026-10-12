import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { ServerEvent } from './types'

const RECONNECT_MS = 1500

export const useSocket = (onEvent: (event: ServerEvent) => void, onOpen: () => void) => {
  const socketRef = useRef<WebSocket | null>(null)
  const handlersRef = useRef({ onEvent, onOpen })
  const [connected, setConnected] = useState(false)
  handlersRef.current = { onEvent, onOpen }

  useEffect(() => {
    let closed = false
    let retry: ReturnType<typeof setTimeout>

    const connect = () => {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
      const socket = new WebSocket(`${protocol}://${location.host}/ws`)
      socket.binaryType = 'arraybuffer'
      socket.onopen = () => {
        setConnected(true)
        handlersRef.current.onOpen()
      }
      socket.onmessage = (message) => handlersRef.current.onEvent(JSON.parse(message.data))
      socket.onclose = () => {
        setConnected(false)
        if (closed) return
        // The server refuses the socket once the session has expired; api.me then goes back to the login page.
        void api.me().catch(() => {})
        retry = setTimeout(connect, RECONNECT_MS)
      }
      socketRef.current = socket
    }

    connect()
    return () => {
      closed = true
      clearTimeout(retry)
      socketRef.current?.close()
    }
  }, [])

  const send = useCallback((message: object | ArrayBuffer) => {
    const socket = socketRef.current
    if (socket?.readyState !== WebSocket.OPEN) return
    socket.send(message instanceof ArrayBuffer ? message : JSON.stringify(message))
  }, [])

  return { connected, send }
}
