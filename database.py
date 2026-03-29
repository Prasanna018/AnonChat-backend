from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import GEOSPHERE, ASCENDING, DESCENDING
import os
from dotenv import load_dotenv

load_dotenv()

MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "echospot")

client: AsyncIOMotorClient = None
db = None


async def connect_db():
    global client, db
    client = AsyncIOMotorClient(MONGODB_URL)
    db = client[DATABASE_NAME]
    await create_indexes()
    print(f"✅ Connected to MongoDB: {DATABASE_NAME}")


async def close_db():
    global client
    if client:
        client.close()
        print("❌ Disconnected from MongoDB")


async def create_indexes():
    # Users collection
    await db.users.create_index([("current_location", GEOSPHERE)])
    await db.users.create_index([("last_active", DESCENDING)])

    # Rooms collection
    await db.rooms.create_index([("center", GEOSPHERE)])
    await db.rooms.create_index([("is_active", ASCENDING)])
    await db.rooms.create_index([("last_activity", DESCENDING)])
    await db.rooms.create_index([("created_at", DESCENDING)])

    # Messages collection
    await db.messages.create_index([("room_id", ASCENDING), ("timestamp", DESCENDING)])

    print("✅ Indexes created")


def get_db():
    return db
