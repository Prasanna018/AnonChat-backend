from fastapi import APIRouter, Depends, HTTPException, Query
from datetime import datetime, timedelta
from database import get_db
from auth import get_current_user, new_uuid
from models import RoomCreate, RoomResponse, NearbyRoomsResponse
from typing import List, Optional
from routers.websocket import get_live_participant_count

router = APIRouter(prefix="/api/rooms", tags=["rooms"])

MAX_RADIUS = 500  # meters


def _room_doc_to_response(doc: dict, distance: Optional[float] = None, live_count: Optional[int] = None) -> RoomResponse:
    """
    Build a RoomResponse from a MongoDB document.
    If live_count is provided, it overrides the stored participant_count so the
    sidebar always reflects the number of users currently connected via WebSocket.
    """
    count = live_count if live_count is not None else doc.get("participant_count", 0)
    return RoomResponse(
        room_id=doc["_id"],
        center=doc["center"],
        radius=doc.get("radius", MAX_RADIUS),
        created_by=doc["created_by"],
        created_at=doc["created_at"],
        last_activity=doc["last_activity"],
        is_active=doc["is_active"],
        participant_count=count,
        distance=distance,
    )


@router.get("/nearby", response_model=NearbyRoomsResponse)
async def get_nearby_rooms(
    lat: float = Query(...), lng: float = Query(...), user_id: str = Depends(get_current_user)
):
    db = get_db()
    pipeline = [
        {
            "$geoNear": {
                "near": {"type": "Point", "coordinates": [lng, lat]},
                "distanceField": "distance",
                "maxDistance": MAX_RADIUS,
                "spherical": True,
                "query": {"is_active": True},
            }
        },
        {"$sort": {"distance": 1}},
        {"$limit": 50},
    ]
    cursor = db.rooms.aggregate(pipeline)
    rooms = []
    async for doc in cursor:
        room_id = doc["_id"]
        # Use live WebSocket count — 0 means nobody is online right now
        live_count = get_live_participant_count(room_id)
        rooms.append(_room_doc_to_response(doc, doc.get("distance"), live_count=live_count))
    return NearbyRoomsResponse(rooms=rooms)


@router.post("", response_model=RoomResponse)
async def create_room(body: RoomCreate, user_id: str = Depends(get_current_user)):
    db = get_db()

    # Rate limiting: max 10 rooms per hour per user
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_count = await db.rooms.count_documents({
        "created_by": user_id,
        "created_at": {"$gte": one_hour_ago}
    })
    if recent_count >= 10:
        raise HTTPException(status_code=429, detail="Room creation limit reached. Try again later.")

    user = await db.users.find_one({"_id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.utcnow()
    room_id = new_uuid()
    room_doc = {
        "_id": room_id,
        "center": {
            "type": "Point",
            "coordinates": [body.lng, body.lat],
        },
        "radius": MAX_RADIUS,
        "created_by": user_id,
        "created_at": now,
        "last_activity": now,
        "is_active": True,
        "participant_count": 1,
        "participants": [user_id],
    }
    await db.rooms.insert_one(room_doc)

    # System message: room created
    await db.messages.insert_one({
        "_id": new_uuid(),
        "room_id": room_id,
        "user_id": "system",
        "display_name": "System",
        "content": f"Room created. People within 500m can now join.",
        "timestamp": now,
        "is_system": True,
    })

    return _room_doc_to_response(room_doc)


@router.post("/{room_id}/join")
async def join_room(room_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    room = await db.rooms.find_one({"_id": room_id})
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if not room.get("is_active"):
        raise HTTPException(status_code=410, detail="Room is no longer active")

    # Check participant cap (optional: 100 max)
    if room.get("participant_count", 0) >= 100:
        raise HTTPException(status_code=403, detail="Room is full. Try another room or create one.")

    user = await db.users.find_one({"_id": user_id})
    display_name = user["display_name"] if user else "Anonymous"

    already_in = user_id in room.get("participants", [])
    if not already_in:
        await db.rooms.update_one(
            {"_id": room_id},
            {
                "$addToSet": {"participants": user_id},
                "$inc": {"participant_count": 1},
                "$set": {"last_activity": datetime.utcnow()},
            },
        )
        now = datetime.utcnow()
        await db.messages.insert_one({
            "_id": new_uuid(),
            "room_id": room_id,
            "user_id": "system",
            "display_name": "System",
            "content": f"{display_name} joined the room.",
            "timestamp": now,
            "is_system": True,
        })

    updated_room = await db.rooms.find_one({"_id": room_id})
    return {"status": "joined", "room": _room_doc_to_response(updated_room)}


@router.post("/{room_id}/leave")
async def leave_room(room_id: str, user_id: str = Depends(get_current_user)):
    db = get_db()
    room = await db.rooms.find_one({"_id": room_id})
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    user = await db.users.find_one({"_id": user_id})
    display_name = user["display_name"] if user else "Anonymous"

    if user_id in room.get("participants", []):
        await db.rooms.update_one(
            {"_id": room_id},
            {
                "$pull": {"participants": user_id},
                "$inc": {"participant_count": -1},
                "$set": {"last_activity": datetime.utcnow()},
            },
        )
        now = datetime.utcnow()
        await db.messages.insert_one({
            "_id": new_uuid(),
            "room_id": room_id,
            "user_id": "system",
            "display_name": "System",
            "content": f"{display_name} left the room.",
            "timestamp": now,
            "is_system": True,
        })

    return {"status": "left"}


@router.get("/{room_id}/messages")
async def get_messages(room_id: str, limit: int = 50, user_id: str = Depends(get_current_user)):
    db = get_db()
    room = await db.rooms.find_one({"_id": room_id})
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    cursor = db.messages.find({"room_id": room_id}).sort("timestamp", 1).limit(limit)
    messages = []
    async for doc in cursor:
        messages.append({
            "message_id": doc["_id"],
            "room_id": doc["room_id"],
            "user_id": doc["user_id"],
            "display_name": doc["display_name"],
            "content": doc["content"],
            "timestamp": doc["timestamp"].isoformat(),
            "is_system": doc.get("is_system", False),
        })
    return {"messages": messages}
