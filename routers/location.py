from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime
from database import get_db
from auth import get_current_user
from models import LocationUpdate

router = APIRouter(prefix="/api/location", tags=["location"])


@router.post("")
async def update_location(body: LocationUpdate, user_id: str = Depends(get_current_user)):
    db = get_db()
    result = await db.users.update_one(
        {"_id": user_id},
        {
            "$set": {
                "current_location": {
                    "type": "Point",
                    "coordinates": [body.lng, body.lat],
                },
                "last_active": datetime.utcnow(),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "updated"}
