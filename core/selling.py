"""
内建销售层 · 许可验证 + 用量追踪 + 归因

这是「工具自己卖」的核心：
  · 用户装了产品 → 免费用 → 用到上限 → 提示升级 → 直接付款
  · 全程不需要我们出现

三个模块：
  1. License  —— 验证许可，解锁付费功能
  2. Usage    —— 记录用量，在合适时机提示升级
  3. Attribution —— 标记来源，知道哪条产品线转化最好

设计原则：
  · 离线可用（不联网也能验证大多数情况）
  · 不强制（不给许可也能用免费版）
  · 不打扰（只在真的用到上限时提示）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ============================================================
# 配置
# ============================================================
# 产品线标识（用于归因）
CHANNELS = {
    "n8n": "n8n-node",
    "cli": "pypi-cli",
    "mcp": "mcp-server",
    "extension": "chrome-ext",
}

# 免费版限制
FREE_LIMITS = {
    "daily_runs": 100,        # 每天免费校验次数
    "max_rows": 10_000,       # 单次最大行数
    "cross_table": False,      # 跨表一致性
    "alerts": False,           # 告警集成
    "time_series": False,      # 时间序列校验
}

PRO_LIMITS = {
    "daily_runs": -1,          # 无限
    "max_rows": 5_000_000,
    "cross_table": True,
    "alerts": True,
    "time_series": True,
}

# Dodo 支付链接（带归因参数）
def checkout_url(channel: str) -> str:
    base = "https://checkout.dodopayments.com/buy/pdt_0Nq62Ecr9PnrPukjAEhaM"
    return f"{base}?ref={CHANNELS.get(channel, channel)}"


# ============================================================
# 许可
# ============================================================
@dataclass
class LicenseInfo:
    valid: bool
    plan: str = "free"          # free | pro | team
    email: str = ""
    expires: str = ""
    seats: int = 1
    reason: str = ""


SIG_LEN = 16  # 签名截断长度，必须与 generate_license 一致


def _verify_signature(payload: str, signature: str, secret: str) -> bool:
    """验证许可签名（防止用户自己伪造）"""
    expected = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:SIG_LEN]
    return hmac.compare_digest(expected, signature)


def verify_license(key: str) -> LicenseInfo:
    """验证 license key

    格式: DVQ-{plan}-{email_b64}-{expires}-{signature}
    签名用共享密钥，用户拿不到密钥就无法伪造。
    """
    if not key or not key.startswith("DVQ-"):
        return LicenseInfo(valid=False, reason="not a license key")

    # 从右边切分：signature 和 expires 不含 '-'，email_b64 可能含
    # 格式: DVQ-{plan}-{email_b64}-{expires}-{sig}
    prefix, rest = key.split("-", 1)  # DVQ, {plan}-{email_b64}-{expires}-{sig}
    try:
        plan, email_b64, expires, sig = rest.rsplit("-", 3)
    except ValueError:
        return LicenseInfo(valid=False, reason="malformed key")

    # 验证签名
    import base64
    secret = os.environ.get("DVQ_LICENSE_SECRET", "dvq-public-verify-2026")
    payload = f"{plan}.{email_b64}.{expires}"
    if not _verify_signature(payload, sig, secret):
        return LicenseInfo(valid=False, reason="invalid signature")

    # 验证过期
    try:
        exp_dt = datetime.strptime(expires, "%Y%m%d").replace(tzinfo=timezone.utc)
        if exp_dt < datetime.now(timezone.utc):
            return LicenseInfo(valid=False, plan=plan, reason="expired")
    except ValueError:
        return LicenseInfo(valid=False, reason="bad expiry")

    try:
        email = base64.urlsafe_b64decode(email_b64 + "==").decode()
    except Exception:
        email = ""

    seats = 5 if plan == "team" else 1
    return LicenseInfo(valid=True, plan=plan, email=email,
                       expires=expires, seats=seats)


def generate_license(plan: str, email: str, days: int = 365) -> str:
    """生成 license key（只在收款后调用，不对外暴露）"""
    import base64
    from datetime import timedelta

    secret = os.environ.get("DVQ_LICENSE_SECRET", "dvq-public-verify-2026")
    email_b64 = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y%m%d")
    payload = f"{plan}.{email_b64}.{expires}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"DVQ-{plan}-{email_b64}-{expires}-{sig}"


# ============================================================
# 用量追踪
# ============================================================
def _state_path() -> str:
    """状态文件位置

    候选顺序：
      1. DVQ_STATE_DIR 环境变量
      2. 工作区 .dvq（默认）
      3. 用户主目录 ~/.dvq
    任何一个可写就用它；全都不行就退化为内存态（不持久化）。
    """
    candidates = []
    if os.environ.get("DVQ_STATE_DIR"):
        candidates.append(os.environ["DVQ_STATE_DIR"])
    candidates.append(os.path.join(os.getcwd(), ".dvq"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".dvq"))

    for base in candidates:
        try:
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".w")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return os.path.join(base, "state.json")
        except Exception:
            continue
    # 全不可写：返回空路径，_load/_save 会安全退化
    return ""


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p:
        return {}
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(s: dict[str, Any]) -> None:
    if not _state_path():
        return
    try:
        json.dump(s, open(_state_path(), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass


@dataclass
class UsageCheck:
    allowed: bool
    reason: str
    runs_today: int
    limit: int
    should_upsell: bool
    upsell_message: str = ""
    checkout: str = ""


def check_usage(license: LicenseInfo, channel: str, rows: int) -> UsageCheck:
    """检查是否还能用，以及是否该提示升级"""
    limits = PRO_LIMITS if license.valid and license.plan in ("pro", "team") else FREE_LIMITS

    state = _load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage = state.get("usage", {})
    runs_today = usage.get(today, 0) if isinstance(usage.get(today), int) else 0

    # 检查行数
    if rows > limits["max_rows"]:
        return UsageCheck(
            allowed=False,
            reason=f"Row limit exceeded: {rows:,} > {limits['max_rows']:,}",
            runs_today=runs_today,
            limit=limits["max_rows"],
            should_upsell=True,
            upsell_message=(
                f"This file has {rows:,} rows. The free version handles up to "
                f"{limits['max_rows']:,}. Pro removes the limit."
            ),
            checkout=checkout_url(channel),
        )

    # 检查次数
    if limits["daily_runs"] >= 0 and runs_today >= limits["daily_runs"]:
        return UsageCheck(
            allowed=False,
            reason=f"Daily limit reached ({limits['daily_runs']})",
            runs_today=runs_today,
            limit=limits["daily_runs"],
            should_upsell=True,
            upsell_message=(
                f"You've run {runs_today} validations today — the free limit. "
                f"Pro has no daily cap and adds cross-table checks + alerts."
            ),
            checkout=checkout_url(channel),
        )

    # 记录这次
    usage[today] = runs_today + 1
    # 只保留最近 30 天
    cutoff = (datetime.now(timezone.utc).timestamp() - 30 * 86400)
    usage = {k: v for k, v in usage.items()
             if datetime.strptime(k, "%Y-%m-%d").timestamp() > cutoff}
    state["usage"] = usage
    state["last_channel"] = channel
    _save_state(state)

    # 是否该提示（用到 80% 时温和提示）
    should_upsell = False
    msg = ""
    if limits["daily_runs"] > 0:
        pct = (runs_today + 1) / limits["daily_runs"]
        if pct >= 0.8:
            should_upsell = True
            msg = (
                f"You've used {runs_today + 1} of {limits['daily_runs']} "
                f"free validations today. Pro removes the cap and adds "
                f"cross-table checks — $49 once."
            )

    return UsageCheck(
        allowed=True, reason="ok", runs_today=runs_today + 1,
        limit=limits["daily_runs"],
        should_upsell=should_upsell, upsell_message=msg,
        checkout=checkout_url(channel),
    )


# ============================================================
# 归因
# ============================================================
def attribution_payload(channel: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成归因数据，付款时带上，就知道哪条产品线转化最好"""
    return {
        "channel": CHANNELS.get(channel, channel),
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
        **(extra or {}),
    }


def get_upsell_text(channel: str, plan: str = "pro") -> str:
    """各渠道的升级文案（针对不同用户群）"""
    texts = {
        "n8n": (
            "### Data Validator Pro\n\n"
            "The free node covers duplicates, empty values, types and dates.\n\n"
            "**Pro adds what actually costs you money when it breaks:**\n\n"
            "- Cross-table consistency — catch mismatched keys between two sheets\n"
            "- Slack / email alerts when a run finds problems\n"
            "- Files up to 5M rows\n\n"
            "One-time $49. No subscription.\n\n"
            f"[Get Pro]({checkout_url('n8n')})"
        ),
        "cli": (
            "### dvq Pro\n\n"
            "Free: single-table checks, 100 runs/day, 10k rows.\n\n"
            "Pro ($49 once):\n"
            "- Cross-table consistency\n"
            "- Alert webhooks (Slack, Discord, custom)\n"
            "- No row or run limits\n\n"
            f"[Get Pro]({checkout_url('cli')})"
        ),
        "mcp": (
            "### Data Validator Pro (MCP)\n\n"
            "Your agent can already validate single tables for free.\n\n"
            "Pro unlocks:\n"
            "- `validate_cross_table` — compare two datasets\n"
            "- `watch_dataset` — get notified when data breaks\n\n"
            f"[Get Pro]({checkout_url('mcp')})"
        ),
        "extension": (
            "### Full Report — $39\n\n"
            "You've seen the summary. The full report includes:\n\n"
            "- Every problem row with the exact reason\n"
            "- A cleaned CSV you can download\n"
            "- The specific fix, column by column\n\n"
            f"[Get the report]({checkout_url('extension')})"
        ),
    }
    return texts.get(channel, texts["cli"])


# ============================================================
# CLI 演示
# ============================================================
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 70)
    print("内建销售层 · 演示")
    print("=" * 70)

    # 1. 生成许可
    print("\n[1] 许可生成与验证")
    key = generate_license("pro", "buyer@example.com", days=365)
    print(f"  生成: {key}")
    info = verify_license(key)
    print(f"  验证: valid={info.valid} plan={info.plan} email={info.email}")

    # 2. 伪造测试
    print("\n[2] 伪造防护")
    fake = "DVQ-pro-YnV5ZXJAZXhhbXBsZS5jb20-20271231-deadbeefdeadbeef"
    finfo = verify_license(fake)
    print(f"  伪造 key: valid={finfo.valid} reason={finfo.reason}")

    # 3. 用量检查
    print("\n[3] 用量检查")
    free = LicenseInfo(valid=False, plan="free")
    u = check_usage(free, "cli", rows=500)
    print(f"  免费用户跑 500 行: allowed={u.allowed} ({u.runs_today}/{u.limit})")

    u2 = check_usage(free, "cli", rows=50_000)
    print(f"  免费用户跑 5 万行: allowed={u2.allowed}")
    print(f"    提示: {u2.upsell_message[:80]}...")

    u3 = check_usage(info, "cli", rows=1_000_000)
    print(f"  Pro 用户跑 100 万行: allowed={u3.allowed}")

    # 4. 归因
    print("\n[4] 归因")
    for ch in CHANNELS:
        a = attribution_payload(ch)
        print(f"  {ch:<10} -> {a['channel']:<16} {checkout_url(ch)[-20:]}")

    # 5. 升级文案
    print("\n[5] n8n 渠道升级文案")
    print(get_upsell_text("n8n")[:400])
