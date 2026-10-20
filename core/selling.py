"""销售引擎 · 内嵌进所有产品线

与 selling.js 行为一致。

许可格式: DVC-{plan}-{email_b64}-{expires}-{sig}
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

CHANNELS = {
    "n8n": "n8n-node",
    "cli": "pypi-cli",
    "mcp": "mcp-server",
    "extension": "chrome-ext",
}

CHECKOUT_BASE = "https://checkout.dodopayments.com/buy"

PLANS = {
    "pro": {"id": "pdt_0Nq62Ecr9PnrPukjAEhaM", "price": "$49"},
    "report": {"id": "pdt_0Nq63a5zRdQV94lR1uRQo", "price": "$39"},
}

FREE_LIMITS = {"runs_per_day": 100, "max_rows": 10000}
PRO_LIMITS = {"runs_per_day": -1, "max_rows": 5000000}

SIG_LEN = 16
DEFAULT_SECRET = "dvq-public-verify-2026"


def checkout_url(channel: str, plan: str = "pro") -> str:
    p = PLANS.get(plan, PLANS["pro"])
    ref = CHANNELS.get(channel, channel)
    return f"{CHECKOUT_BASE}/{p['id']}?ref={ref}"


def _state_candidates() -> list[str]:
    out = []
    if os.environ.get("DATACHECK_STATE_DIR"):
        out.append(os.environ["DATACHECK_STATE_DIR"])
    out.append(os.path.join(os.getcwd(), ".datacheck"))
    try:
        out.append(os.path.join(os.path.expanduser("~"), ".datacheck"))
    except Exception:
        pass
    import tempfile
    out.append(os.path.join(tempfile.gettempdir(), "datacheck-state"))
    return out


def state_dir() -> str:
    for d in _state_candidates():
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return d
        except Exception:
            continue
    return ""


def load_state() -> dict[str, Any]:
    d = state_dir()
    if not d:
        return {}
    p = os.path.join(d, "state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(s: dict[str, Any]) -> None:
    d = state_dir()
    if not d:
        return
    try:
        json.dump(s, open(os.path.join(d, "state.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass


@dataclass
class License:
    valid: bool
    plan: str = "free"
    email: str = ""
    expires: str = ""
    reason: str = ""


def verify_license(key: str) -> License:
    if not key or not isinstance(key, str):
        return License(valid=False, reason="no key")
    key = key.strip()
    if not key.startswith("DVC-"):
        return License(valid=False, reason="not a license key")

    rest = key[4:]
    parts = rest.split("-")
    if len(parts) < 4:
        return License(valid=False, reason="malformed key")

    sig = parts.pop()
    expires = parts.pop()
    plan = parts.pop(0)
    email_b64 = "-".join(parts)

    secret = os.environ.get("DATACHECK_LICENSE_SECRET", DEFAULT_SECRET)
    payload = f"{plan}.{email_b64}.{expires}"
    expected = hmac.new(secret.encode(), payload.encode(),
                        hashlib.sha256).hexdigest()[:SIG_LEN]
    if not hmac.compare_digest(expected, sig):
        return License(valid=False, reason="invalid signature")

    try:
        exp = datetime.strptime(expires, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return License(valid=False, reason="bad expiry")
    if exp < datetime.now(timezone.utc):
        return License(valid=False, plan=plan, reason="expired")

    try:
        email = base64.b64decode(email_b64 + "==").decode("utf-8", "ignore")
    except Exception:
        email = ""

    return License(valid=True, plan=plan, email=email, expires=expires)


def generate_license(plan: str, email: str, days: int = 365) -> str:
    secret = os.environ.get("DATACHECK_LICENSE_SECRET", DEFAULT_SECRET)
    email_b64 = base64.b64encode(email.encode()).decode().rstrip("=")
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y%m%d")
    payload = f"{plan}.{email_b64}.{expires}"
    sig = hmac.new(secret.encode(), payload.encode(),
                   hashlib.sha256).hexdigest()[:SIG_LEN]
    return f"DVC-{plan}-{email_b64}-{expires}-{sig}"


def resolve_license() -> License:
    key = os.environ.get("DATACHECK_LICENSE", "")
    if not key:
        st = load_state()
        key = st.get("license", "")
    if not key:
        return License(valid=False, reason="no license")
    return verify_license(key)


@dataclass
class Usage:
    allowed: bool
    reason: str
    runs_today: int = 0
    limit: int = 0
    should_upsell: bool = False
    upsell_message: str = ""
    checkout: str = ""


def check_usage(license: License, channel: str, rows: int) -> Usage:
    limits = PRO_LIMITS if (license.valid and license.plan in ("pro", "team")) \
        else FREE_LIMITS

    if rows > limits["max_rows"]:
        return Usage(
            allowed=False, reason="row_limit", limit=limits["max_rows"],
            should_upsell=True,
            upsell_message=(
                f"This run has {rows:,} rows. The free tier handles up to "
                f"{limits['max_rows']:,}. Pro removes the limit."
            ),
            checkout=checkout_url(channel),
        )

    st = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage_map = st.get("usage") or {}
    val = usage_map.get(today, 0)
    runs = int(val) if isinstance(val, int) else 0

    if limits["runs_per_day"] >= 0 and runs >= limits["runs_per_day"]:
        return Usage(
            allowed=False, reason="daily_limit", runs_today=runs,
            limit=limits["runs_per_day"], should_upsell=True,
            upsell_message=(
                f"You've run {runs} validations today - the free daily limit. "
                f"Pro has no cap and adds cross-table checks."
            ),
            checkout=checkout_url(channel),
        )

    usage_map[today] = runs + 1
    st["usage"] = usage_map
    st["last_channel"] = channel
    st["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(st)

    should_upsell = False
    msg = ""
    if limits["runs_per_day"] > 0:
        pct = (runs + 1) / limits["runs_per_day"]
        if pct >= 0.8:
            should_upsell = True
            msg = (
                f"You've used {runs + 1} of {limits['runs_per_day']} free "
                f"validations today. Pro removes the cap - $49 once."
            )

    return Usage(
        allowed=True, reason="ok", runs_today=runs + 1,
        limit=limits["runs_per_day"],
        should_upsell=should_upsell, upsell_message=msg,
        checkout=checkout_url(channel) if should_upsell else "",
    )


def attribution(channel: str, extra: dict | None = None) -> dict:
    return {
        "channel": CHANNELS.get(channel, channel),
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        **(extra or {}),
    }


def upsell_text(channel: str) -> str:
    url = checkout_url(channel)
    texts = {
        "n8n": (
            "Data Validator Pro - $49 once\n\n"
            "Pro adds cross-table consistency, Slack/email alerts, and 5M rows per run.\n"
            f"{url}"
        ),
        "cli": (
            "Pro ($49 once): cross-table checks, alert webhooks, no limits.\n"
            f"{url}"
        ),
        "mcp": (
            "Pro unlocks validate_cross_table and watch_dataset.\n"
            f"{url}"
        ),
        "extension": (
            "Full report - $39: every problem row with the reason, plus a cleaned CSV.\n"
            f"{url}"
        ),
    }
    return texts.get(channel, texts["cli"])


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 70)
    print("销售引擎 Python 版测试")
    print("=" * 70)

    P = 0
    F = 0

    def t(name, cond, detail=""):
        global P, F
        if cond:
            P += 1
            print(f"  PASS  {name}")
        else:
            F += 1
            print(f"  FAIL  {name}  {detail}")

    key = generate_license("pro", "buyer@example.com", 365)
    lic = verify_license(key)
    t("生成许可有效", lic.valid and lic.plan == "pro", str(lic))
    t("邮箱解析", lic.email == "buyer@example.com", lic.email)

    exp = generate_license("pro", "x@y.com", -1)
    t("过期被拒", not verify_license(exp).valid)
    t("伪造被拒", not verify_license("DVC-pro-ZmFrZQ-20991231-deadbeefdeadbeef").valid)

    free = License(valid=False, plan="free")
    u1 = check_usage(free, "mcp", 500)
    t("免费小文件可用", u1.allowed, u1.reason)
    u2 = check_usage(free, "mcp", 50000)
    t("免费大文件被拦", not u2.allowed, u2.reason)
    t("拦截有升级链接", "dodopayments" in u2.checkout, u2.checkout)
    t("链接带归因", "ref=mcp-server" in u2.checkout, u2.checkout)

    u3 = check_usage(lic, "mcp", 1000000)
    t("Pro 大文件可用", u3.allowed, u3.reason)

    for ch in ("n8n", "cli", "mcp", "extension"):
        t(f"归因 {ch}", attribution(ch)["channel"] == CHANNELS[ch])
        t(f"文案 {ch}", len(upsell_text(ch)) > 50)

    print("\n" + "=" * 70)
    print(f"结果: {P} 通过, {F} 失败")
    print("=" * 70)
    sys.exit(0 if F == 0 else 1)
