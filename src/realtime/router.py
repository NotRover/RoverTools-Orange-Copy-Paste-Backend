from fastapi import APIRouter, WebSocket

router = APIRouter(tags=["realtime"])

# Phase 3: WebSocket hub with Redis pub/sub fan-out

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    await websocket.accept()
    await websocket.send_json({"event": "ping", "payload": {"status": "stub — Phase 3"}})
    await websocket.close()
