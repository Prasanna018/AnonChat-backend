import asyncio
import json
from datetime import datetime
from typing import Dict, Set, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from database import get_db
from auth import decode_token, new_uuid

router = APIRouter(tags=["websocket"])

# room_id -> { user_id -> set of WebSockets }
room_connections: Dict[str, Dict[str, Set[WebSocket]]] = {}


def _get_participant_count(room_id: str) -> int:
    if room_id not in room_connections:
        return 0
    return len(room_connections[room_id])


async def broadcast(room_id: str, event: dict, exclude: Optional[WebSocket] = None):
    """Broadcast event to all connections in a room."""
    if room_id not in room_connections:
        return
    dead_sockets = []
    
    for user_ws_set in room_connections[room_id].values():
        for ws in user_ws_set:
            if ws == exclude:
                continue
            try:
                await ws.send_json(event)
            except Exception:
                dead_sockets.append(ws)
                
    # Cleanup dead sockets
    for ws in dead_sockets:
        _remove_socket_from_room(room_id, ws)


async def broadcast_all(room_id: str, event: dict):
    await broadcast(room_id, event, exclude=None)


def _remove_socket_from_room(room_id: str, websocket: WebSocket) -> Optional[str]:
    """Removes a socket and returns the user_id if that was their last active socket in the room."""
    if room_id not in room_connections:
        return None
        
    for user_id, ws_set in list(room_connections[room_id].items()):
        if websocket in ws_set:
            ws_set.discard(websocket)
            if not ws_set:
                # User has no more active tabs in this room
                room_connections[room_id].pop(user_id, None)
                if not room_connections[room_id]:
                    room_connections.pop(room_id, None)
                return user_id
    return None


@router.websocket("/api/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, token: str = Query(...)):
    await websocket.accept()

    # Auth
    user_id = decode_token(token)
    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    db = get_db()
    user = await db.users.find_one({"_id": user_id})
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    room = await db.rooms.find_one({"_id": room_id})
    if not room or not room.get("is_active"):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    display_name = user["display_name"]

    # Register connection securely
    if room_id not in room_connections:
        room_connections[room_id] = {}
    if user_id not in room_connections[room_id]:
        room_connections[room_id][user_id] = set()
        
    is_new_user_in_room = len(room_connections[room_id][user_id]) == 0
    room_connections[room_id][user_id].add(websocket)

    participant_count = _get_participant_count(room_id)

    # If it's a completely new user to the room, notify everyone
    if is_new_user_in_room:
        await broadcast(
            room_id,
            {
                "type": "user_joined",
                "user_name": display_name,
                "participant_count": participant_count,
                "timestamp": datetime.utcnow().isoformat(),
            },
            exclude=websocket,
        )

    # Send state to the connecting client
    await websocket.send_json({
        "type": "participant_count",
        "participant_count": participant_count,
        "timestamp": datetime.utcnow().isoformat(),
    })

    # Load history
    cursor = db.messages.find({"room_id": room_id}).sort("timestamp", 1).limit(50)
    history = []
    async for doc in cursor:
        history.append({
            "type": "message",
            "message_id": doc["_id"],
            "user_id": doc["user_id"],
            "user_name": doc["display_name"],
            "content": doc["content"],
            "timestamp": doc["timestamp"].isoformat(),
            "is_system": doc.get("is_system", False),
        })
    
    try:
        await websocket.send_json({"type": "history", "messages": history})
    except Exception:
        # Client disconnected immediately
        _remove_socket_from_room(room_id, websocket)
        return

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            if msg_type == "send_message":
                content = (data.get("content") or "").strip()
                if not content or len(content) > 2000:
                    continue

                now = datetime.utcnow()
                msg_doc = {
                    "_id": new_uuid(),
                    "room_id": room_id,
                    "user_id": user_id,
                    "display_name": display_name,
                    "content": content,
                    "timestamp": now,
                    "is_system": False,
                }
                await db.messages.insert_one(msg_doc)
                await db.rooms.update_one({"_id": room_id}, {"$set": {"last_activity": now}})

                event = {
                    "type": "message",
                    "message_id": msg_doc["_id"],
                    "user_id": user_id,
                    "user_name": display_name,
                    "content": content,
                    "timestamp": now.isoformat(),
                    "is_system": False,
                }
                await broadcast_all(room_id, event)

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        # Properly clean up socket, checking if user completely left the room
        left_user_id = _remove_socket_from_room(room_id, websocket)
        
        if left_user_id:
            new_participant_count = _get_participant_count(room_id)
            await broadcast(
                room_id,
                {
                    "type": "user_left",
                    "user_name": display_name,
                    "participant_count": new_participant_count,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
            # Update DB sync with actual live WebSocket logic
            await db.rooms.update_one(
                {"_id": room_id},
                {
                    "$pull": {"participants": user_id}, 
                    "$set": {"participant_count": new_participant_count}
                }
            )
