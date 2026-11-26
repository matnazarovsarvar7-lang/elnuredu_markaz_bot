import hashlib
import hmac
import json
import time
from datetime import datetime
from urllib.parse import urlencode

from app.config import settings
from app.bot import online as O
from app.logic import moc as M
from app import online_api as API


def test_parse_dt():
    assert O.parse_dt("30.11 10:00").month == 11
    assert O.parse_dt("1.12.2026 9:05") == datetime(2026, 12, 1, 9, 5)
    assert O.parse_dt("31.02 10:00") is None and O.parse_dt("ertaga") is None


def test_init_data_signature():
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 501})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    sec = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    d["hash"] = hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()
    assert API.check_init(urlencode(d))["id"] == 501
    d["hash"] = "0" * 64
    assert API.check_init(urlencode(d)) is None


def test_clean_answers():
    sp = M.parse_spec("1-3: Test | test 4 | 1\n4-5: Ochiq | yozma 2 | 1")
    assert API._clean(sp, "aEX29") == "A--2-"
