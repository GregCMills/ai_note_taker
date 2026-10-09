"""Fans events out to the browser tabs viewing each note."""

from fastapi import WebSocket


class Hub:
    def __init__(self):
        self._clients: dict[WebSocket, str | None] = {}

    def add(self, ws: WebSocket) -> None:
        self._clients[ws] = None

    def remove(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    def subscribe(self, ws: WebSocket, path: str | None) -> None:
        self._clients[ws] = path

    async def send(self, path: str, message: dict) -> None:
        await self._send([ws for ws, p in self._clients.items() if p == path], {**message, "path": path})

    async def send_all(self, message: dict) -> None:
        await self._send(list(self._clients), message)

    async def _send(self, targets: list[WebSocket], message: dict) -> None:
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                self.remove(ws)
