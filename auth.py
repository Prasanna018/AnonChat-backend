import uuid
import random
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import os
from dotenv import load_dotenv

load_dotenv()

JWT_SECRET = os.getenv("JWT_SECRET", "echospot-secret")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "30"))

security = HTTPBearer()

ADJECTIVES = [
    "Swift", "Brave", "Clever", "Bold", "Calm", "Dark", "Epic", "Fierce",
    "Gentle", "Happy", "Icy", "Jolly", "Kind", "Lazy", "Mighty", "Noble",
    "Odd", "Pure", "Quick", "Rare", "Silent", "Tiny", "Urban", "Vivid",
    "Wild", "Zany", "Amber", "Blue", "Coral", "Dusty", "Emerald", "Frosty",
    "Golden", "Hidden", "Iron", "Jade", "Keen", "Lunar", "Mystic", "Neon",
    "Onyx", "Pixel", "Quantum", "Rusty", "Solar", "Turbo", "Ultra", "Velvet",
    "Wise", "Xenon"
]

NOUNS = [
    "Panda", "Tiger", "Wolf", "Eagle", "Fox", "Bear", "Hawk", "Lion",
    "Mink", "Orca", "Puma", "Raven", "Shark", "Toad", "Viper", "Whale",
    "Yak", "Zebra", "Ant", "Bat", "Cat", "Deer", "Elk", "Frog",
    "Goat", "Hare", "Ibis", "Jay", "Kite", "Lynx", "Moth", "Newt",
    "Owl", "Pike", "Quail", "Ram", "Slug", "Swan", "Tern", "Urial",
    "Vole", "Wasp", "Xerus", "Yeti", "Zorro", "Cobra", "Drake", "Finch"
]


def generate_display_name() -> str:
    adj = random.choice(ADJECTIVES)
    noun = random.choice(NOUNS)
    return f"{adj}{noun}"


def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS)
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": datetime.utcnow(),
        "type": "anon"
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        return user_id
    except JWTError:
        return None


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


def new_uuid() -> str:
    return str(uuid.uuid4())
