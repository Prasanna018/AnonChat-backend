from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
import cloudinary.uploader
import asyncio
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timedelta
from database import get_db
from auth import get_current_user, new_uuid
from models import RoomCreate, RoomJoin, RoomResponse, NearbyRoomsResponse
from typing import List, Optional
from routers.websocket import get_live_participant_count

router = APIRouter(prefix="/api/rooms", tags=["rooms"])

MAX_RADIUS = 500  # meters


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in meters between two WGS-84 coordinates."""
    R = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml",
    "application/pdf", "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


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
async def join_room(room_id: str, body: RoomJoin, user_id: str = Depends(get_current_user)):
    db = get_db()
    room = await db.rooms.find_one({"_id": room_id})
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if not room.get("is_active"):
        raise HTTPException(status_code=410, detail="Room is no longer active")

    # ── Location gate: must be within MAX_RADIUS metres ──────────────────────
    coords = room["center"]["coordinates"]  # [lng, lat]
    room_lat, room_lng = coords[1], coords[0]
    distance = haversine_meters(body.lat, body.lng, room_lat, room_lng)
    if distance > MAX_RADIUS:
        raise HTTPException(
            status_code=403,
            detail=f"You are {int(distance)}m away from this room. You must be within {MAX_RADIUS}m to join."
        )

    # Check participant cap
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
            "content": doc.get("content"),
            "media_url": doc.get("media_url"),
            "media_type": doc.get("media_type"),
            "timestamp": doc["timestamp"].isoformat(),
            "is_system": doc.get("is_system", False),
        })
    return {"messages": messages}


@router.post("/{room_id}/media")
async def upload_media(
    room_id: str,
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user)
):
    db = get_db()
    room = await db.rooms.find_one({"_id": room_id})
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if not room.get("is_active"):
        raise HTTPException(status_code=410, detail="Room is no longer active")

    content_type = (file.content_type or "").lower()

    # Block videos
    if content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="Video uploads are not allowed.")

    # Validate mime type
    if content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{content_type}' is not allowed. Supported: images, PDF, plain text, Word documents."
        )

    # Read file
    contents = await file.read()

    # Check actual size
    if len(contents) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds maximum allowed size of 10 MB.")

    # Determine Cloudinary resource type
    if content_type.startswith("image/"):
        resource_type = "image"
    else:
        resource_type = "raw"

    # Build upload kwargs
    upload_kwargs: dict = {
        "resource_type": resource_type,
        "folder": f"echospot/chat_media/{room_id}",
    }

    # Apply auto quality/format transformation for images
    if resource_type == "image":
        upload_kwargs["transformation"] = [
            {"width": 1200, "crop": "limit"},
            {"quality": "auto", "fetch_format": "auto"},
        ]

    # Run Cloudinary upload in thread pool to avoid blocking the event loop
    try:
        result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            contents,
            **upload_kwargs
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {str(e)}")

    media_url = result.get("secure_url")
    if not media_url:
        raise HTTPException(status_code=500, detail="Failed to retrieve URL from Cloudinary.")

    return {
        "url": media_url,
        "type": content_type or "application/octet-stream",
    }
