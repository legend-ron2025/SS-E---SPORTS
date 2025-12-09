import os
from fastapi import FastAPI
import uvicorn

from bot import (
    REG_BY_ID,
    BLACKLIST,
    LOBBIES,
    LOBBY_ACTIVE,
    total_confirmed_slots,
    reserved_slots_count,
    confirm_registration,
    set_active_lobby_permissions,
    audit_log,
    bot,
    GUILD_ID
)

app = FastAPI()


@app.get("/")
async def health():
    return {"status": "SS Esports Dashboard Running ✅"}


# ✅ COPY YOUR FULL DASHBOARD ROUTES HERE
# (/api/markpaid, /api/close_lobby, /api/stopup, /api/export_csv)
# AND YOUR HTML DASHBOARD ROUTE
# EXACT SAME CODE YOU ALREADY WROTE


if __name__ == "__main__":
    uvicorn.run(
        "dashboard_app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        log_level="info",
    )
