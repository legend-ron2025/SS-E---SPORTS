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


from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>SS Esports Dashboard</title>

<style>
body{
  margin:0;
  font-family: Inter, system-ui;
  background: radial-gradient(circle at top,#1e293b,#020617 60%);
  color:#e5e7eb;
}

.header{
  padding:26px;
  text-align:center;
  font-size:28px;
  font-weight:800;
  background: linear-gradient(90deg,#7c3aed,#ec4899);
  box-shadow:0 12px 40px rgba(0,0,0,.6);
}

.sub{
  font-size:13px;
  opacity:.9;
}

.card{
  max-width:900px;
  margin:60px auto;
  padding:26px;
  border-radius:18px;
  background:rgba(2,6,23,.9);
  box-shadow:0 40px 120px rgba(0,0,0,.85);
  border:1px solid rgba(148,163,184,.2);
}

.badge{
  display:inline-block;
  padding:6px 14px;
  border-radius:999px;
  font-size:12px;
  background:#22c55e;
  color:#052e16;
  font-weight:700;
  box-shadow:0 0 18px #22c55e;
}
</style>
</head>

<body>
  <div class="header">
    ⚔️ SS ESPORTS CONTROL ROOM
    <div class="sub">Tournament-level dashboard • LIVE</div>
  </div>

  <div class="card">
    <span class="badge">SYSTEM ONLINE ✅</span>
    <h2>Professional Esports Dashboard</h2>
    <p>
      Your FastAPI dashboard is now rendering HTML correctly.<br>
      Dynamic lobby stats, animations, and controls will load here.
    </p>

    <p>
      🔥 Render Cloud Hosting<br>
      🚀 Hardcore Tournament Automation<br>
      🧠 Built for SS Esports
    </p>
  </div>
</body>
</html>
"""



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

