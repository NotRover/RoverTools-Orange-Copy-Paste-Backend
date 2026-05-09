"""
Process-global WebSocket connection registry.

channel_name → set of (WebSocket, device_id) tuples.
Hub is purely in-memory; the Redis pub/sub bridge in pubsub.py feeds messages into it.
"""

import asyncio
from collections import defaultdict

from fastapi import WebSocket

# channel_name → set of (websocket, device_id)
_channels: dict[str, set[tuple[WebSocket, str]]] = defaultdict(set)
# websocket → device_id (for cleanup without iterating all channels)
_ws_device: dict[WebSocket, str] = {}
# websocket → set of subscribed channel names
_ws_channels: dict[WebSocket, set[str]] = {}

_lock = asyncio.Lock()


async def register(ws: WebSocket, device_id: str, channels: list[str]) -> None:
    async with _lock:
        _ws_device[ws] = device_id
        _ws_channels[ws] = set(channels)
        for ch in channels:
            _channels[ch].add((ws, device_id))


async def unregister(ws: WebSocket) -> None:
    async with _lock:
        device_id = _ws_device.pop(ws, None)
        channels = _ws_channels.pop(ws, set())
        for ch in channels:
            _channels[ch].discard((ws, device_id))
            if not _channels[ch]:
                del _channels[ch]


def connection_count() -> int:
    """Return the number of currently connected WebSocket clients (no lock — approximate)."""
    return len(_ws_device)


async def broadcast(channel: str, raw: str, exclude_device: str | None = None) -> None:
    async with _lock:
        targets = list(_channels.get(channel, set()))

    dead: list[tuple[WebSocket, str]] = []
    for ws, device_id in targets:
        if exclude_device and device_id == exclude_device:
            continue
        try:
            await ws.send_text(raw)
        except Exception:
            dead.append((ws, device_id))

    if dead:
        async with _lock:
            for item in dead:
                ws, device_id = item
                ch_set = _ws_channels.pop(ws, set())
                _ws_device.pop(ws, None)
                for ch in ch_set:
                    _channels[ch].discard(item)
                    if not _channels[ch]:
                        del _channels[ch]
