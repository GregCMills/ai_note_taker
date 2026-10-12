"""Fans events out to the browsers viewing each project, keeping each user's browsers separate."""

from fastapi import WebSocket


class Hub:
    def __init__(self):
        self._clients: dict[WebSocket, tuple[str, str | None]] = {}  # browser -> (user, scope it is viewing)

    def add(self, ws: WebSocket, user: str) -> None:
        self._clients[ws] = (user, None)

    def remove(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    def subscribe(self, ws: WebSocket, project: str | None) -> None:
        if ws in self._clients:
            self._clients[ws] = (self._clients[ws][0], project)

    def channel(self, user: str) -> "Channel":
        return Channel(self, user)

    async def send(self, user: str, project: str, message: dict) -> None:
        targets = [ws for ws, (u, p) in self._clients.items() if u == user and p == project]
        await self._send(targets, {**message, "project": project})

    async def send_all(self, user: str, message: dict) -> None:
        await self._send([ws for ws, (u, _) in self._clients.items() if u == user], message)

    async def _send(self, targets: list[WebSocket], message: dict) -> None:
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                self.remove(ws)


class Channel:
    """One user's side of the hub: what a workspace sends only reaches that user's browsers."""

    def __init__(self, hub: Hub, user: str):
        self._hub = hub
        self._user = user

    async def send(self, project: str, message: dict) -> None:
        await self._hub.send(self._user, project, message)

    async def send_all(self, message: dict) -> None:
        await self._hub.send_all(self._user, message)
