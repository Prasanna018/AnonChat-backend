from fastapi import APIRouter, HTTPException
from datetime import datetime
from database import get_db
from auth import generate_display_name, create_access_token, new_uuid
from models import UserResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/anon", response_model=UserResponse)
async def create_anonymous_user():
    db = get_db()
    user_id = new_uuid()
    display_name = generate_display_name()
    token = create_access_token(user_id)

    user_doc = {
        "_id": user_id,
        "display_name": display_name,
        "current_location": None,
        "last_active": datetime.utcnow(),
        "created_at": datetime.utcnow(),
    }

    await db.users.insert_one(user_doc)

    return UserResponse(user_id=user_id, display_name=display_name, token=token)
