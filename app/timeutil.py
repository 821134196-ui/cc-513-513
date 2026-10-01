"""时间与编号工具。业务日期统一为本地时区的 naive datetime，ISO 文本存储。"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta

DATE_FMT = "%Y-%m-%d"
TS_FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def now_ts() -> str:
    return now().strftime(TS_FMT)


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


def parse_appt(s: str) -> datetime:
    """支持 'YYYY-MM-DD HH:MM' 与完整时间戳。"""
    s = s.strip().replace("T", " ")
    for fmt in (TS_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s}")


def d(x: datetime | date) -> str:
    return x.strftime(DATE_FMT)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def confirm_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def days_between(a: str, b: str) -> int:
    """业务口径：按日历日相减（b-a）。"""
    da = datetime.strptime(a, DATE_FMT).date()
    db = datetime.strptime(b, DATE_FMT).date()
    return (db - da).days


def is_protected(checkin_date: str, deal_date_str: str, days: int) -> bool:
    """保护期为带看签到日次日起 N 个日历日。

    带看日 D，保护期 = [D+1, D+N]；D+N 当天成交仍在保护期内，
    D+(N+1) 成交为保护期外。边界测试即围绕这两天。
    """
    D0 = datetime.strptime(checkin_date, DATE_FMT).date()
    Ddeal = datetime.strptime(deal_date_str, DATE_FMT).date()
    start = D0 + timedelta(days=1)
    end = D0 + timedelta(days=days)
    return start <= Ddeal <= end
