"""
amazon_scheduler.py  —  Daily cloud jobs for Naturdao / Body Nostrum
Runs inside the Hermes Railway process (asyncio, no extra deps).

Schedule (UTC):
  02:00  backup_gbrain.py                  — GBrain pages → local JSON + GitHub
  03:00  download_amazon_reports_auto.py   — Amazon SP-API Europa → GBrain
  03:30  amazon_pl_diario.py               — P&L diario Europa → GBrain
  04:00  amazon_pl_usa.py                  — P&L diario USA → GBrain

Required Railway env vars:
  MCP_API_KEY, HERMES_URL,
  AMAZON_CLIENT_ID_EUROPA, AMAZON_CLIENT_SECRET_EUROPA, AMAZON_REFRESH_TOKEN_EUROPA,
  AMAZON_CLIENT_ID_USA, AMAZON_CLIENT_SECRET_USA, AMAZON_REFRESH_TOKEN_USA,
  GITHUB_TOKEN, GITHUB_BACKUP_REPO (default: ND2018/gbrain-backup)
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("amazon_scheduler")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

SCRIPTS_DIR = Path(__file__).parent / "scripts"

JOBS = [
    {"name": "backup-gbrain",  "hour": 2,  "minute": 0,  "script": "backup_gbrain.py"},
    {"name": "amazon-fetch",   "hour": 3,  "minute": 0,  "script": "download_amazon_reports_auto.py"},
    {"name": "amazon-pl-eu",   "hour": 3,  "minute": 30, "script": "amazon_pl_diario.py"},
    {"name": "amazon-pl-usa",  "hour": 4,  "minute": 0,  "script": "amazon_pl_usa.py"},
]


def _build_env() -> dict:
    """Build subprocess env: inherit Railway vars + alias MCP_API_KEY → MCP_KEY."""
    env = {**os.environ}
    # Scripts use MCP_KEY; Railway sets MCP_API_KEY — bridge the gap
    if env.get("MCP_API_KEY") and not env.get("MCP_KEY"):
        env["MCP_KEY"] = env["MCP_API_KEY"]
    return env


async def run_script(script_name: str) -> None:
    script_path = SCRIPTS_DIR / script_name
    log.info(f"▶ START {script_name}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(SCRIPTS_DIR),
            env=_build_env(),
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode(errors="replace").strip()
        tail = "\n".join(output.splitlines()[-30:])
        if proc.returncode == 0:
            log.info(f"✅ OK {script_name}\n{tail}")
        else:
            log.error(f"❌ FAIL {script_name} (exit {proc.returncode})\n{tail}")
    except Exception as exc:
        log.error(f"❌ ERROR {script_name}: {exc}")


async def _loop(hour: int, minute: int, script_name: str) -> None:
    """Wait until next UTC hour:minute, run, repeat daily."""
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour, minute=minute, second=5, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        log.info(f"⏳ {script_name} → next run {target.strftime('%Y-%m-%d %H:%M UTC')} (in {wait/3600:.1f}h)")
        await asyncio.sleep(wait)
        asyncio.create_task(run_script(script_name))


async def start() -> None:
    """Call this from server.py lifespan to launch all jobs."""
    missing = [v for v in (
        "MCP_API_KEY",
        "AMAZON_CLIENT_ID_EUROPA", "AMAZON_CLIENT_SECRET_EUROPA", "AMAZON_REFRESH_TOKEN_EUROPA",
        "AMAZON_CLIENT_ID_USA",    "AMAZON_CLIENT_SECRET_USA",    "AMAZON_REFRESH_TOKEN_USA",
        "GITHUB_TOKEN",
    ) if not os.environ.get(v)]
    if missing:
        log.warning(f"⚠ Amazon scheduler: missing env vars {missing} — those jobs will fail")

    for job in JOBS:
        asyncio.create_task(_loop(job["hour"], job["minute"], job["script"]))
    log.info("🟢 Scheduler activo (UTC): backup@02:00 | fetch-EU@03:00 | P&L-EU@03:30 | P&L-USA@04:00")
