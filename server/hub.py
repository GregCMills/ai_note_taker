"""Fans events out to the browsers viewing each project."""

from fastapi import WebSocket


class Hub:
    def __init__(self):
        self._clients: dict[WebSocket, str | None] = {}

    def add(self, ws: WebSocket) -> None:
        self._clients[ws] = None

    def remove(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    def subscribe(self, ws: WebSocket, project: str | None) -> None:
        self._clients[ws] = project

    async def send(self, project: str, message: dict) -> None:
        await self._send([ws for ws, p in self._clients.items() if p == project], {**message, "project": project})

    async def send_all(self, message: dict) -> None:
        await self._send(list(self._clients), message)

    async def _send(self, targets: list[WebSocket], message: dict) -> None:
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                self.remove(ws)
