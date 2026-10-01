"""HTTP 层：仅做参数解析与 JSON 编解码，业务判定全部在 domain.Service。

启动：python -m app.server  （或 ./run.sh）
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db
from .domain import (ConflictError, DomainError, Service, WALKIN)
from .sms_adapter import SmsAdapter
from .timeutil import new_id, now_ts

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.environ.get("APP_DATA", os.path.join(BASE_DIR, "..", "data"))
SIGN_DIR = os.path.join(DATA_DIR, "signatures")
DB_PATH = os.environ.get("APP_DB", os.path.join(DATA_DIR, "brokerage.db"))

_singleton_lock = threading.Lock()
_state: dict = {}


def get_service() -> Service:
    """进程级单例；线程局部连接 + BEGIN IMMEDIATE 保证多线程写入串行化。"""
    if "svc" not in _state:
        with _singleton_lock:
            if "svc" not in _state:
                os.makedirs(SIGN_DIR, exist_ok=True)
                database = db.Database(DB_PATH)
                _state["svc"] = Service(database)
                _state["db"] = database
    return _state["svc"]


def reset_service_for_tests(path: str) -> Service:
    database = db.Database(path)
    svc = Service(database)
    _state["svc"] = svc
    _state["db"] = database
    return svc


def _safe(fn):
    try:
        return fn()
    except DomainError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "BrokerageGuard/1.0"

    # ------------------------------------------------------------ plumbing
    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str, ctype: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise DomainError("请求体不是合法 JSON")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")

    # --------------------------------------------------------------- routing
    def do_GET(self):
        try:
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            svc = get_service()
            if p in ("/", "/index.html"):
                return self._file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            if p.startswith("/static/"):
                ctypes = {".css": "text/css; charset=utf-8",
                          ".js": "application/javascript; charset=utf-8",
                          ".svg": "image/svg+xml"}
                ext = os.path.splitext(p)[1]
                return self._file(os.path.join(STATIC_DIR, p[len("/static/"):]),
                                  ctypes.get(ext, "application/octet-stream"))
            m = re.fullmatch(r"/signatures/([\w.\-]+\.png)", p)
            if m:
                return self._file(os.path.join(SIGN_DIR, m.group(1)), "image/png")

            if p == "/api/meta":
                return self._json({
                    "stores": [dict(r) for r in svc.rows("SELECT * FROM stores ORDER BY store_id")],
                    "agents": [dict(r) for r in svc.rows("SELECT * FROM agents ORDER BY store_id,agent_id")],
                    "active_rule": _safe(svc.active_rule),
                    "walkin": WALKIN,
                })
            if p == "/api/rules":
                return self._json(svc.list_rules())
            if p == "/api/properties":
                return self._json(svc.search_properties(q.get("q", [""])[0]))
            m = re.fullmatch(r"/api/properties/(\w+)", p)
            if m:
                prop = svc.get_property(m.group(1))
                prop["timeline"] = svc.list_viewings(property_id=m.group(1))
                return self._json(prop)
            if p == "/api/customers":
                return self._json(svc.search_customers(q.get("q", [""])[0]))
            m = re.fullmatch(r"/api/customers/(\w+)", p)
            if m:
                c = svc.get_customer(m.group(1))
                c["viewings"] = svc.list_viewings(customer_id=m.group(1))
                return self._json(c)
            m = re.fullmatch(r"/api/customers/check-dup", p)
            if m:
                return self._json({"duplicates": "use POST"})
            if p == "/api/viewings":
                return self._json(svc.list_viewings(
                    date_str=q.get("date", [""])[0],
                    property_id=q.get("property_id", [""])[0],
                    customer_id=q.get("customer_id", [""])[0]))
            if p == "/api/calendar":
                ym = q.get("ym", [""])[0]
                year, month = (int(x) for x in ym.split("-"))
                return self._json(svc.calendar_month(year, month))
            m = re.fullmatch(r"/api/viewings/(\w+)", p)
            if m:
                return self._json(svc.get_viewing(m.group(1)))
            if p == "/api/deals":
                return self._json(svc.list_deals(q.get("status", [""])[0]))
            m = re.fullmatch(r"/api/deals/(\w+)", p)
            if m:
                return self._json(svc.get_deal(m.group(1)))
            if p == "/api/rulings":
                return self._json(svc.list_rulings())
            if p == "/api/audit":
                return self._json({"events": svc.audit_feed(),
                                   "denied": svc.denied_feed()})
            if p == "/api/sms":
                return self._json(svc.sms.list_outbox())
            return self._json({"error": "not found"}, 404)
        except DomainError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"服务器错误: {e}"}, 500)

    def do_POST(self):
        try:
            p = urlparse(self.path).path
            body = self._body()
            svc = get_service()

            if p == "/api/customers":
                dup_only = body.pop("check_duplicates", False)
                if dup_only:
                    return self._json({"duplicates": svc.find_duplicates(
                        body.get("name", ""), body.get("phone", ""))})
                return self._json(svc.register_customer(
                    body.get("name", ""), body.get("phone", ""),
                    body.get("source_agent_id", ""), body.get("note", ""),
                    bool(body.get("force"))))

            if p == "/api/viewings":
                return self._json(svc.schedule_viewing(
                    body["property_id"], body["customer_id"], body["agent_id"],
                    body["appointment_time"], ide_key=body.get("ide_key", ""),
                    store_id_override=body.get("store_id", "")))

            m = re.fullmatch(r"/api/viewings/(\w+)/cancel", p)
            if m:
                return self._json(svc.cancel_viewing(
                    m.group(1), body.get("actor", ""), body.get("reason", "")))

            m = re.fullmatch(r"/api/viewings/(\w+)/checkin", p)
            if m:
                sign_path = self._save_signature(body.get("signature_data_url", ""),
                                                 m.group(1))
                return self._json(svc.check_in(
                    m.group(1), float(body.get("lat", 0) or 0),
                    float(body.get("lng", 0) or 0), sign_path,
                    body.get("actor", "")))

            m = re.fullmatch(r"/api/viewings/(\w+)/confirm", p)
            if m:
                return self._json(svc.customer_confirm(
                    m.group(1), body.get("code", "")))

            if p == "/api/collab-grants":
                return self._json(svc.grant_collab(
                    body["property_id"], body["agent_id"], body["granted_by"]))

            if p == "/api/deals":
                return self._json(svc.register_deal(
                    body["property_id"], body["customer_id"],
                    int(body["amount_cents"]), body["deal_date"],
                    body.get("actor", ""), body.get("note", "")))

            m = re.fullmatch(r"/api/deals/(\w+)/rule", p)
            if m:
                return self._json(svc.rule_deal(
                    m.group(1), body["decided_by"], body["split"],
                    body.get("rationale", "")))

            if p == "/api/merge-customers":
                return self._json(svc.merge_customers(
                    body["keep_id"], body["drop_id"], body.get("actor", "")))

            return self._json({"error": "not found"}, 404)
        except ConflictError as e:
            return self._json({"error": str(e), "conflict": True}, 409)
        except DomainError as e:
            return self._json({"error": str(e)}, 400)
        except KeyError as e:
            return self._json({"error": f"缺少字段: {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"服务器错误: {e}"}, 500)

    # ------------------------------------------------------------ signature
    def _save_signature(self, data_url: str, viewing_id: str) -> str:
        if not data_url or not data_url.startswith("data:image/png;base64,"):
            raise DomainError("签字图缺失（需要 PNG data URL）")
        raw = base64.b64decode(data_url.split(",", 1)[1])
        fname = f"{viewing_id}_{new_id('sign')}.png"
        fpath = os.path.join(SIGN_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(raw)
        rel = f"/signatures/{fname}"
        get_service()._emit("viewing", viewing_id, "signature.stored",
                            {"path": rel, "bytes": len(raw), "ts": now_ts()})
        return rel


def serve(host: str = "127.0.0.1", port: int = 8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"◈ 房产带看与佣金归属系统  http://{host}:{port}")
    print(f"  数据库: {os.path.abspath(DB_PATH)}")
    print(f"  签字图: {os.path.abspath(SIGN_DIR)}")
    httpd.serve_forever()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    serve(args.host, args.port)
