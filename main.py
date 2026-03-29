from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
import cloudinary.api

load_dotenv()

from database import connect_db, close_db, get_db
from routers import auth, location, rooms, websocket as ws_router

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

# Configure Cloudinary
cloudinary.config(
  cloud_name = os.getenv('CLOUDINARY_CLOUD_NAME'),
  api_key = os.getenv('CLOUDINARY_API_KEY'),
  api_secret = os.getenv('CLOUDINARY_API_SECRET'),
  secure = True
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    # Start background task for room expiration
    task = asyncio.create_task(expire_rooms_task())
    yield
    task.cancel()
    await close_db()


app = FastAPI(title="EchoSpot API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(location.router)
app.include_router(rooms.router)
app.include_router(ws_router.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "EchoSpot API"}


async def expire_rooms_task():
    """Background task: deactivates rooms after 2h no messages or 12h total."""
    while True:
        try:
            db = get_db()
            if db is not None:
                now = datetime.utcnow()
                two_hours_ago = now - timedelta(hours=2)
                twelve_hours_ago = now - timedelta(hours=12)

                result = await db.rooms.update_many(
                    {
                        "is_active": True,
                        "$or": [
                            {"last_activity": {"$lt": two_hours_ago}},
                            {"created_at": {"$lt": twelve_hours_ago}},
                        ],
                    },
                    {"$set": {"is_active": False}},
                )
                if result.modified_count > 0:
                    print(f"♻️  Expired {result.modified_count} room(s)")
        except Exception as e:
            print(f"⚠️  Room expiration error: {e}")
        await asyncio.sleep(60)  # check every minute
