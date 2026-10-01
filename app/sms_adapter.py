"""短信适配器：真实环境替换为实现 send() 的网关即可，这里只落库 + 控制台回显。"""
from __future__ import annotations

import sqlite3

from .timeutil import new_id, now_ts


class SmsAdapter:
    def __init__(self, conn: sqlite3.Connection, echo: bool = True):
        self.conn = conn
        self.echo = echo

    def send(self, phone: str, content: str,
             related_type: str = "", related_id: str = "") -> dict:
        sms_id = new_id("sms")
        ts = now_ts()
        self.conn.execute(
            "INSERT INTO sms_outbox VALUES (?,?,?,?,?,?)",
            (sms_id, phone, content, ts, related_type, related_id),
        )
        # 模拟发送：写库即发出。真实适配器在此调用短信网关。
        if self.echo:
            print(f"[SMS→{phone}] {content}")
        return {"sms_id": sms_id, "phone": phone, "content": content, "sent_at": ts}

    def list_outbox(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sms_outbox ORDER BY sent_at DESC, sms_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
