"""HTTP/JSON API + 静态前端，全部基于 Python 标准库。"""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import domain as D
from . import db
from .pressure import MockPressureAdapter
from .reminder import ReminderWorker
from .service import Service
from . import seed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "scba.db")
ATTACH_DIR = os.path.join(DATA_DIR, "attachments")

CT = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
      ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
      ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json"}

# _run 出错（已自行写出错误响应）时返回的哨兵
_ERROR_HANDLED = object()


class AppState:
    def __init__(self, db_path=DB_PATH, attach_dir=ATTACH_DIR, settle_seconds=900,
                 do_seed=True, reminder_interval=10.0):
        os.makedirs(attach_dir := attach_dir, exist_ok=True)
        self.attach_dir = attach_dir
        self.conn = db.connect(db_path)
        self.adapter = MockPressureAdapter()
        self.svc = Service(self.conn, self.adapter, settle_seconds=settle_seconds)
        self.lock = threading.RLock()
        self.seeded = False
        if do_seed:
            with self.lock:
                self.seeded = seed.seed(self.conn, self.svc, self.adapter, attach_dir)
        self.worker = ReminderWorker(self.svc, interval=reminder_interval,
                                     lock=self.lock)

    def start_worker(self):
        self.worker.start()


def build_handler(state: AppState):
    svc = state.svc

    class Handler(BaseHTTPRequestHandler):
        server_version = "SCBAStation/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            print(f"[http] {self.address_string()} - {fmt % args}", flush=True)

        # ---------- 工具 ----------
        def _qs(self):
            u = urllib.parse.urlparse(self.path)
            return u.path, urllib.parse.parse_qs(u.query)

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, exc):
            self._json({"error": {"code": exc.code, "message": exc.message}}, exc.http)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise D.DomainError("BAD_JSON", "请求体不是合法 JSON", http=400)

        def _actor(self, qs, body):
            return (self.headers.get("X-Actor")
                    or body.get("actor_id")
                    or (qs.get("actor", [None])[0]))

        def _run(self, fn):
            """加锁执行业务并按请求提交。

            成功 -> 返回业务结果（可能为 None）；出错 -> 已写出错误响应并返回哨兵。
            """
            try:
                with state.lock:
                    result = fn()
                    state.conn.commit()
                return result
            except D.DomainError as e:
                with state.lock:
                    state.conn.rollback()
                self._err(e)
                return _ERROR_HANDLED
            except Exception as e:  # 兜底：未预期错误也返回 JSON 并回滚
                with state.lock:
                    state.conn.rollback()
                print(f"[http] server error: {type(e).__name__}: {e}", flush=True)
                self._json({"error": {"code": "INTERNAL", "message": str(e)}}, 500)
                return _ERROR_HANDLED

        def _respond(self, result):
            """_run 之后统一应答：None 表示成功但无数据。"""
            if result is not _ERROR_HANDLED:
                self._json(result if result is not None else {"ok": True})

        # ---------- 静态 ----------
        def do_GET(self):
            path, qs = self._qs()
            if path.startswith("/api/"):
                self._api_get(path, qs)
                return
            self._static(path)

        def do_POST(self):
            path, qs = self._qs()
            if path.startswith("/api/"):
                self._api_post(path, qs)
            else:
                self._json({"error": {"code": "NOT_FOUND", "message": path}}, 404)

        def _static(self, path):
            rel = path[1:] if path != "/" else "index.html"
            rel = urllib.parse.unquote(rel.split("?")[0])
            full = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(WEB_DIR) or not os.path.isfile(full):
                self.send_error(404, "Not found")
                return
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", CT.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # ---------- GET API ----------
        def _api_get(self, path, qs):
            def go():
                if path == "/api/personnel":
                    return svc.list_personnel()
                if path == "/api/tasks":
                    return svc.list_tasks(include_closed=qs.get("closed", ["0"])[0] == "1")
                if path == "/api/cylinders":
                    return svc.list_cylinders()
                if path.startswith("/api/cylinders/"):
                    return svc.cylinder_detail(urllib.parse.unquote(path.rsplit("/", 1)[1]))
                if path == "/api/issues":
                    return svc.view_issues()
                if path == "/api/checks":
                    return svc.view_return_checks()
                if path == "/api/batches":
                    if qs.get("active", ["0"])[0] == "1":
                        return svc.list_batches(active_only=True)
                    return svc.list_batches()
                if path.startswith("/api/batches/"):
                    return svc.batch_detail(urllib.parse.unquote(path.rsplit("/", 1)[1]))
                if path == "/api/retests":
                    return svc.view_retests()
                if path == "/api/pairs":
                    return svc.view_pairs()
                if path == "/api/due":
                    return svc.view_due()
                if path == "/api/reminders":
                    return svc.list_reminders(
                        include_acked=qs.get("acked", ["0"])[0] == "1")
                if path == "/api/events":
                    return svc.list_events(int(qs.get("limit", ["200"])[0]))
                if path == "/api/bootstrap":
                    return {
                        "personnel": svc.list_personnel(),
                        "tasks": svc.list_tasks(),
                        "cylinders": svc.list_cylinders(),
                        "reminders": svc.list_reminders(),
                        "thresholds": {
                            "rated": D.RATED_PRESSURE_MPA,
                            "final_min": D.FINAL_MIN_MPA,
                            "settle_drop_max": D.SETTLE_DROP_MAX_MPA,
                            "settle_seconds": svc.settle_seconds,
                            "inspection_months": D.INSPECTION_INTERVAL_MONTHS,
                        },
                        "adapter": svc.adapter.NAME,
                        "seeded": state.seeded,
                    }
                raise D.DomainError("NOT_FOUND", path, http=404)

            # 附件下载为字节流，不走 JSON 应答
            if path.startswith("/api/attachments/") and path.endswith("/download"):
                return self._download_attachment(path)
            result = self._run(go)
            self._respond(result)

        def _download_attachment(self, path):
            try:
                att_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._json({"error": {"code": "NOT_FOUND", "message": "附件"}}, 404)
            with state.lock:
                row = state.conn.execute(
                    "SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
                if row and os.path.isfile(row["stored_path"]):
                    with open(row["stored_path"], "rb") as f:
                        body = f.read()
                else:
                    row, body = None, None
            if not row or body is None:
                return self._json({"error": {"code": "NOT_FOUND", "message": "附件文件缺失"}}, 404)
            fname = urllib.parse.quote(row["filename"])
            self.send_response(200)
            self.send_header("Content-Type", row["content_type"] or "application/octet-stream")
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{fname}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- POST API ----------
        def _api_post(self, path, qs):
            # 附件原始字节上传
            if path.startswith("/api/cylinders/") and path.endswith("/attachments"):
                return self._upload_attachment(path)

            body = self._body()
            actor = self._actor(qs, body)
            if not actor:
                return self._err(D.AuthError("缺少操作人（X-Actor 头或 actor_id）"))
            body["actor_id"] = actor

            def go():
                p = path.strip("/").split("/")
                if path == "/api/issues":
                    return {"issue_id": svc.issue(
                        body["cyl_no"], body.get("person_id") or actor,
                        task_id=body.get("task_id"),
                        issue_type=body.get("issue_type", D.ISSUE_TASK),
                        target_unit=body.get("target_unit"),
                        issue_pressure=body.get("issue_pressure"), actor_id=actor)}
                if path == "/api/returns":
                    return svc.return_cylinder(body["cyl_no"], actor,
                                               float(body["remain_pressure"]))
                if len(p) == 4 and p[1] == "checks" and p[3] == "inspect":
                    return svc.inspect_return(int(p[2]), actor,
                                              bool(body.get("appearance_ok")),
                                              bool(body.get("valve_ok")),
                                              body.get("note"))
                if len(p) == 4 and p[1] == "cylinders":
                    no = urllib.parse.unquote(p[2])
                    action = p[3]
                    if action == "repair-done":
                        return svc.repair_done(no, actor, body.get("note"))
                    if action == "inspection":
                        return {"inspection_due": svc.update_inspection(
                            no, actor, body["last_inspection"], body.get("note"))}
                    if action == "seal":
                        return svc.seal(no, actor, body.get("reason", "装备封存"))
                    if action == "unseal":
                        return svc.unseal(no, actor, body.get("reason", "解除封存"))
                    if action == "condemn-propose":
                        return svc.propose_condemn(no, actor,
                                                   body.get("reason", "报废审议"))
                    if action == "scrap":
                        return svc.confirm_scrap(no, actor, body.get("reason", "报废确认"))
                if path == "/api/batches":
                    return {"batch_id": svc.create_batch(
                        actor, int(body.get("positions", 4)))}
                if len(p) == 4 and p[1] == "batches":
                    bid = urllib.parse.unquote(p[2])
                    action = p[3]
                    if action == "assign":
                        return svc.assign_port(bid, int(body["position"]),
                                               body["cyl_no"], actor)
                    if action == "unassign":
                        return svc.unassign_port(bid, int(body["position"]), actor)
                    if action == "start":
                        return svc.start_charging(bid, actor)
                    if action == "complete":
                        return svc.complete_charging(bid, actor)
                    if action == "abort":
                        return svc.abort_batch(bid, actor, body.get("reason", "充装中断"))
                if path.startswith("/api/ports/") and path.endswith("/retest"):
                    pid_ = int(path.split("/")[3])
                    return svc.retest_port(pid_, actor, force=bool(body.get("force")))
                if path == "/api/pair-checks":
                    return svc.pair_check(body["cyl_no"], body["frame_scanned"], actor)
                if path.startswith("/api/reminders/") and path.endswith("/ack"):
                    rid = int(path.split("/")[3])
                    return svc.ack_reminder(rid, actor) or {"ok": True}
                raise D.DomainError("NOT_FOUND", path, http=404)

            result = self._run(go)
            self._respond(result)

        def _upload_attachment(self, path):
            actor = self.headers.get("X-Actor")
            if not actor:
                return self._err(D.AuthError("缺少操作人 X-Actor 头"))
            parts = path.strip("/").split("/")
            cyl_no = urllib.parse.unquote(parts[2])
            fname = urllib.parse.unquote(self.headers.get("X-Filename", "attachment.bin"))
            safe = os.path.basename(fname).replace(os.sep, "_")
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type", "application/octet-stream")

            def go():
                svc.cyl(cyl_no)
                svc.actor(actor)
                os.makedirs(state.attach_dir, exist_ok=True)
                stored = os.path.join(state.attach_dir, f"{cyl_no}_{len(raw)}_{safe}")
                with open(stored, "wb") as f:
                    f.write(raw)
                cur = state.conn.execute(
                    """INSERT INTO attachments(cyl_no,filename,stored_path,content_type,
                       actor_id,ts) VALUES(?,?,?,?,?,?)""",
                    (cyl_no, safe, stored, ctype, actor, svc.clock()))
                from .db import append_event
                append_event(state.conn, "ATTACHMENT", cyl_no, actor,
                             "attachment", cur.lastrowid,
                             {"filename": safe, "bytes": len(raw)})
                return {"attachment_id": cur.lastrowid, "stored": stored, "bytes": len(raw)}

            result = self._run(go)
            self._respond(result)

    return Handler


def serve(host="127.0.0.1", port=8080, settle_seconds=900, db_path=DB_PATH,
          attach_dir=ATTACH_DIR, reminder_interval=10.0):
    state = AppState(db_path=db_path, attach_dir=attach_dir,
                     settle_seconds=settle_seconds,
                     reminder_interval=reminder_interval)
    state.start_worker()
    httpd = ThreadingHTTPServer((host, port), build_handler(state))
    print(f"SCBA 充装检测系统: http://{host}:{port}  (种子样例: {'已生成' if state.seeded else '已存在'})",
          flush=True)
    return httpd
