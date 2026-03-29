from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ----- GeoJSON -----
class GeoPoint(BaseModel):
    type: str = "Point"
    coordinates: List[float]  # [lng, lat]


# ----- User -----
class UserCreate(BaseModel):
    pass  # Anonymous — no fields needed


class UserResponse(BaseModel):
    user_id: str
    display_name: str
    token: str


class LocationUpdate(BaseModel):
    lat: float
    lng: float


# ----- Room -----
class RoomCreate(BaseModel):
    lat: float
    lng: float


class RoomResponse(BaseModel):
    room_id: str
    center: GeoPoint
    radius: int
    created_by: str
    created_at: datetime
    last_activity: datetime
    is_active: bool
    participant_count: int
    distance: Optional[float] = None  # meters from user


class NearbyRoomsResponse(BaseModel):
    rooms: List[RoomResponse]


# ----- Message -----
class MessageCreate(BaseModel):
    content: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None


class MessageResponse(BaseModel):
    message_id: str
    room_id: str
    user_id: str
    display_name: str
    content: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    timestamp: datetime
    is_system: bool = False


# ----- WebSocket payloads -----
class WSMessage(BaseModel):
    type: str  # "send_message" | "typing"
    content: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    is_typing: Optional[bool] = None


class WSEvent(BaseModel):
    type: str  # "message" | "history" | "user_joined" | "user_left" | "room_closed" | "typing"
    message_id: Optional[str] = None
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    content: Optional[str] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    timestamp: Optional[str] = None
    reason: Optional[str] = None
    is_typing: Optional[bool] = None
    is_system: Optional[bool] = False
    participant_count: Optional[int] = None
