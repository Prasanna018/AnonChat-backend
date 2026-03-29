import asyncio
import json
from datetime import datetime
from typing import Dict, Set, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from database import get_db
from auth import decode_token, new_uuid
from utils import haversine_meters, MAX_RADIUS

router = APIRouter(tags=["websocket"])

# room_id -> { user_id -> set of WebSockets }
room_connections: Dict[str, Dict[str, Set[WebSocket]]] = {}

# Per-room asyncio lock to serialize broadcast+cleanup and prevent race conditions
room_locks: Dict[str, asyncio.Lock] = {}


def _ensure_room_lock(room_id: str) -> asyncio.Lock:
    if room_id not in room_locks:
        room_locks[room_id] = asyncio.Lock()
    return room_locks[room_id]


def _get_participant_count(room_id: str) -> int:
    if room_id not in room_connections:
        return 0
    return len(room_connections[room_id])


def get_live_participant_count(room_id: str) -> int:
    """Public API: returns the number of unique users currently connected via WebSocket."""
    return _get_participant_count(room_id)


async def broadcast(room_id: str, event: dict, exclude: Optional[WebSocket] = None):
    """Broadcast event to all connections in a room. Thread-safe via per-room lock."""
    if room_id not in room_connections:
        return

    lock = _ensure_room_lock(room_id)
    async with lock:
        if room_id not in room_connections:
            return

        targets: list[tuple[str, WebSocket]] = []
        for user_id, ws_set in room_connections[room_id].items():
            for ws in list(ws_set):
                if ws != exclude:
                    targets.append((user_id, ws))

        dead: list[WebSocket] = []
        for _, ws in targets:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)

        for ws in dead:
            _remove_socket_unsafe(room_id, ws)


async def broadcast_all(room_id: str, event: dict):
    await broadcast(room_id, event, exclude=None)


def _remove_socket_unsafe(room_id: str, websocket: WebSocket) -> Optional[str]:
    """
    Remove a single socket from the room. Assumes caller holds the room lock
    OR is in a context where concurrent access is not an issue.
    Returns user_id if that was the user's last socket (they fully left), else None.
    """
    if room_id not in room_connections:
        return None

    for user_id, ws_set in list(room_connections[room_id].items()):
        if websocket in ws_set:
            ws_set.discard(websocket)
            if not ws_set:
                room_connections[room_id].pop(user_id, None)
                if not room_connections[room_id]:
                    room_connections.pop(room_id, None)
                    room_locks.pop(room_id, None)
                return user_id
    return None


async def remove_socket_from_room(room_id: str, websocket: WebSocket) -> Optional[str]:
    """Thread-safe removal. Returns user_id if user fully left."""
    lock = _ensure_room_lock(room_id)
    async with lock:
        return _remove_socket_unsafe(room_id, websocket)


async def destroy_empty_room(room_id: str) -> None:
    """
    Permanently delete a room (and all its messages) if no one is connected.
    Called immediately when the last WebSocket participant disconnects.
    """
    # Double-check nobody reconnected in the brief window
    if get_live_participant_count(room_id) > 0:
        return

    db = get_db()
    if db is None:
        return

    try:
        await db.messages.delete_many({"room_id": room_id})
        result = await db.rooms.delete_one({"_id": room_id})
        if result.deleted_count:
            print(f"🗑️  Instantly destroyed empty room: {room_id}")
    except Exception as e:
        print(f"⚠️  Failed to destroy room {room_id}: {e}")


async def force_destroy_room_and_broadcast(room_id: str, reason: str):
    """Broadcasts termination, deletes room, and drops connections."""
    await broadcast_all(room_id, {"type": "room_closed", "reason": reason})

    db = get_db()
    if db:
        try:
            await db.messages.delete_many({"room_id": room_id})
            await db.rooms.delete_one({"_id": room_id})
        except Exception:
            pass

    lock = _ensure_room_lock(room_id)
    async with lock:
        if room_id in room_connections:
            for uid, ws_set in list(room_connections[room_id].items()):
                for ws in list(ws_set):
                    try:
                        await ws.close()
                    except:
                        pass
            room_connections.pop(room_id, None)
            room_locks.pop(room_id, None)


@router.websocket("/api/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, token: str = Query(...)):
    await websocket.accept()

    # --- Auth ---
    user_id = decode_token(token)
    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    db = get_db()
    if db is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    user = await db.users.find_one({"_id": user_id})
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    room = await db.rooms.find_one({"_id": room_id})
    if not room or not room.get("is_active"):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    display_name = user["display_name"]

    # --- Register connection ---
    lock = _ensure_room_lock(room_id)
    async with lock:
        if room_id not in room_connections:
            room_connections[room_id] = {}
        if user_id not in room_connections[room_id]:
            room_connections[room_id][user_id] = set()

        is_new_user_in_room = len(room_connections[room_id][user_id]) == 0
        room_connections[room_id][user_id].add(websocket)

    participant_count = _get_participant_count(room_id)

    # Notify others if this is a fresh user (not a second tab)
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

    # Send current participant count to this client
    try:
        await websocket.send_json({
            "type": "participant_count",
            "participant_count": participant_count,
            "timestamp": datetime.utcnow().isoformat(),
        })
    except Exception:
        await remove_socket_from_room(room_id, websocket)
        return

    # --- Send message history ---
    try:
        cursor = db.messages.find({"room_id": room_id}).sort("timestamp", 1).limit(75)
        history = []
        async for doc in cursor:
            history.append({
                "type": "message",
                "message_id": doc["_id"],
                "user_id": doc["user_id"],
                "user_name": doc["display_name"],
                "content": doc.get("content"),
                "media_url": doc.get("media_url"),
                "media_type": doc.get("media_type"),
                "timestamp": doc["timestamp"].isoformat(),
                "is_system": doc.get("is_system", False),
            })
        await websocket.send_json({"type": "history", "messages": history})
    except Exception:
        await remove_socket_from_room(room_id, websocket)
        return

    # --- Message receive loop ---
    try:
        while True:
            raw = await websocket.receive_text()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            # --- Location Enforcement ---
            lat = data.get("lat")
            lng = data.get("lng")
            if lat is not None and lng is not None:
                room_lat = room["center"]["coordinates"][1]
                room_lng = room["center"]["coordinates"][0]
                distance = haversine_meters(lat, lng, room_lat, room_lng)
                
                if distance > MAX_RADIUS:
                    if user_id == room.get("created_by"):
                        await force_destroy_room_and_broadcast(
                            room_id, 
                            "Creator moved out of 500m radius. The room is now destroyed."
                        )
                        break
                    else:
                        try:
                            await websocket.send_json({
                                "type": "room_closed", 
                                "reason": "You have left the 500m room boundary."
                            })
                        except:
                            pass
                        break

            # --- Ping / Pong keepalive ---
            if msg_type == "ping":
                try:
                    await websocket.send_json({
                        "type": "pong",
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                except Exception:
                    break
                continue

            # --- Send a chat message ---
            if msg_type == "send_message":
                content = (data.get("content") or "").strip()
                media_url = data.get("media_url")
                media_type = data.get("media_type")

                if not content and not media_url:
                    continue
                if len(content) > 2000:
                    continue

                now = datetime.utcnow()
                msg_doc = {
                    "_id": new_uuid(),
                    "room_id": room_id,
                    "user_id": user_id,
                    "display_name": display_name,
                    "content": content if content else None,
                    "media_url": media_url,
                    "media_type": media_type,
                    "timestamp": now,
                    "is_system": False,
                }

                try:
                    await db.messages.insert_one(msg_doc)
                    await db.rooms.update_one(
                        {"_id": room_id},
                        {"$set": {"last_activity": now}},
                    )
                except Exception:
                    continue

                event = {
                    "type": "message",
                    "message_id": msg_doc["_id"],
                    "user_id": user_id,
                    "user_name": display_name,
                    "content": content if content else None,
                    "media_url": media_url,
                    "media_type": media_type,
                    "timestamp": now.isoformat(),
                    "is_system": False,
                }
                await broadcast_all(room_id, event)

    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        left_user_id = await remove_socket_from_room(room_id, websocket)

        if left_user_id:
            new_participant_count = _get_participant_count(room_id)

            # Broadcast departure to remaining participants
            if new_participant_count > 0:
                await broadcast(
                    room_id,
                    {
                        "type": "user_left",
                        "user_name": display_name,
                        "participant_count": new_participant_count,
                        "timestamp": datetime.utcnow().isoformat(),
                    },
                )

            try:
                await db.rooms.update_one(
                    {"_id": room_id},
                    {
                        "$pull": {"participants": user_id},
                        "$set": {"participant_count": new_participant_count},
                    },
                )
            except Exception:
                pass

            # ── Instant room destruction when the last user leaves ──────────
            if new_participant_count == 0:
                asyncio.create_task(destroy_empty_room(room_id))
