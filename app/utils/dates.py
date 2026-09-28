"""India-time helpers used for business dates."""
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

def today_ist():
    return datetime.now(IST).date()
