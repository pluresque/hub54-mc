import os
from pathlib import Path


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
INITIAL_WHITELIST_TELEGRAM_IDS = frozenset(
    int(value.strip())
    for value in os.getenv("INITIAL_WHITELIST_TELEGRAM_IDS", "").split(",")
    if value.strip().isdigit() and int(value.strip()) > 0
)
DATABASE_PATH = Path(
    os.getenv(
        "DATABASE_PATH",
        str(Path(__file__).resolve().parent / "minecraft_bot.sqlite3"),
    )
)
MINECRAFT_SERVER_ADDRESS = os.getenv(
    "MINECRAFT_SERVER_ADDRESS", "адрес сервера"
)
MINECRAFT_SERVER_VERSION = os.getenv("MINECRAFT_SERVER_VERSION", "последняя")
MINECRAFT_STATUS_HOST = os.getenv(
    "MINECRAFT_STATUS_HOST", "minecraft-paper-26-2"
)
MINECRAFT_STATUS_PORT = int(os.getenv("MINECRAFT_STATUS_PORT", "25565"))
