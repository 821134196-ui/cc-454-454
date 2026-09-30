#!/usr/bin/env python3
"""SCBA 气瓶充装与检测系统 —— 本地启动入口（零第三方依赖）。

用法：
    python3 app.py            # 初始化数据库 + 训练/样例数据 + 启动 Web
    python3 app.py --port 8080
    python3 app.py --no-seed  # 不写入种子数据

启动后访问 http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scba import config, repository as repo, service
from scba.engine import Engine
from scba.errors import SCBAError
from scba.schema import connect, init_db
from scba.seed import seed

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

engine: Engine = Engine()


# --------------------------------------------------------------------------- #
# 处理器
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "SCBA/1.0"

    # ---- 基础收发 ---- #
    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise SCBAError("请求体不是合法 JSON", code="bad_json")

    def _actor(self, conn, data: dict):
        """根据 X-Actor-Id 头或请求体 person_id 解析当前操作人。"""
        pid = self.headers.get("X-Actor-Id") or data.get("person_id")
        if not pid:
            return None
        row = conn.execute("SELECT * FROM people WHERE id=?", (int(pid),)).fetchone()
        return row

    def _require_actor(self, conn, data: dict, role: str | None = None):
        actor = self._actor(conn, data)
        if not actor:
            raise SCBAError("未选择当前操作人员", code="no_actor")
        if role and actor["role"] != role:
            from scba.errors import PermissionDenied
            raise PermissionDenied(
                f"该操作仅允许{config.ROLE_NAMES[role]}执行（当前："
                f"{config.ROLE_NAMES.get(actor['role'], actor['role'])}）")
        return actor

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    # ---- 路由 ---- #
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._api_get(path, qs)
            else:
                self._serve_static(path)
        except SCBAError as e:
            self._send_json({"error": str(e), "code": e.code}, 404 if e.code == "not_found"
                            else 400)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"服务器错误：{e}", "code": "server_error"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()
        try:
            self._api_post(path, data)
        except SCBAError as e:
            status = 403 if e.code == "permission_denied" else 404 if e.code == "not_found" else 400
            self._send_json({"error": str(e), "code": e.code}, status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"服务器错误：{e}", "code": "server_error"}, 500)

    # ---------------- GET API ---------------- #
    def _api_get(self, path: str, qs: dict):
        conn = connect()
        try:
            if path == "/api/dashboard":
                self._send_json(repo.dashboard(conn))
            elif path == "/api/meta":
                self._send_json({
                    "rated": config.RATED_PRESSURE,
                    "return_low": config.RETURN_LOW_PRESSURE,
                    "fill_final_min": config.FILL_FINAL_MIN,
                    "drop_limit": config.RETEST_DROP_LIMIT,
                    "settle_seconds": config.RETEST_SETTLE_SECONDS,
                    "warn_days": config.EXPIRY_WARN_DAYS,
                    "ports": config.FILL_STATION_PORTS,
                    "roles": config.ROLE_NAMES,
                    "status_names": config.CYL_STATUS_NAMES,
                })
            elif path == "/api/people":
                self._send_json(repo.people(conn, qs.get("role", [None])[0]))
            elif path == "/api/cylinders":
                self._send_json(repo.ledger(conn, qs.get("status", [None])[0],
                                            qs.get("q", [None])[0]))
            elif path.startswith("/api/cylinders/"):
                cid = int(path.rsplit("/", 1)[-1])
                d = repo.cylinder_detail(conn, cid)
                if not d:
                    raise SCBAError("气瓶不存在", code="not_found")
                self._send_json(d)
            elif path == "/api/tasks":
                self._send_json(repo.tasks_view(conn))
            elif path == "/api/returns":
                self._send_json(repo.returns_view(conn))
            elif path == "/api/batches":
                self._send_json(repo.batches_view(
                    conn, active_only=qs.get("active", ["0"])[0] == "1"))
            elif path.startswith("/api/batches/"):
                self._send_json(repo.batch_detail(conn, int(path.rsplit("/", 1)[-1])))
            elif path == "/api/retest":
                self._send_json(repo.retest_view(conn))
            elif path == "/api/pairing":
                self._send_json({"rows": repo.pairing_view(conn),
                                 "harnesses": repo.harnesses(conn)})
            elif path == "/api/expiry":
                self._send_json(repo.expiry_view(conn))
            elif path == "/api/reminders":
                self._send_json(repo.reminders(conn))
            elif path == "/api/notifications":
                after = int(qs.get("after", ["0"])[0])
                self._send_json({"items": engine.drain_notifications(after)})
            elif path == "/api/events":
                self._send_json(repo.events_feed(conn, int(qs.get("limit", ["60"])[0])))
            elif path == "/api/harnesses":
                self._send_json(repo.harnesses(conn))
            elif path.startswith("/api/attachments/"):
                att = conn.execute("SELECT * FROM attachments WHERE id=?",
                                   (int(path.rsplit("/", 1)[-1]),)).fetchone()
                if not att:
                    raise SCBAError("附件不存在", code="not_found")
                fpath = (config.DATA_DIR / att["relpath"]).resolve()
                if not str(fpath).startswith(str(config.DATA_DIR.resolve())) or not fpath.is_file():
                    raise SCBAError("附件文件缺失", code="not_found")
                body = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{att["filename"]}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json({"error": "未知接口", "code": "not_found"}, 404)
        finally:
            conn.close()

    # ---------------- POST API ---------------- #
    def _api_post(self, path: str, d: dict):
        conn = connect()
        try:
            F, O, S = config.ROLE_FIGHTER, config.ROLE_OPERATOR, config.ROLE_SUPERVISOR

            if path == "/api/issue":
                actor = self._require_actor(conn, d, F)
                r = service.issue_cylinder(conn, int(d["cylinder_id"]), int(d["task_id"]),
                                           actor["id"])
            elif path == "/api/returns":
                actor = self._require_actor(conn, d, F)
                r = service.return_cylinder(conn, int(d["cylinder_id"]),
                                            float(d["return_pressure"]), actor["id"],
                                            note=d.get("note", ""))
            elif path == "/api/inspections":
                actor = self._require_actor(conn, d, O)
                r = service.inspect_appearance(
                    conn, int(d["return_id"]),
                    appearance_ok=bool(d.get("appearance_ok")),
                    valve_ok=bool(d.get("valve_ok")),
                    label_ok=bool(d.get("label_ok", True)),
                    findings=d.get("findings", ""), inspector_id=actor["id"],
                    attachment_id=d.get("attachment_id"))
            elif path == "/api/batches":
                actor = self._require_actor(conn, d, O)
                r = service.create_batch(conn, actor["id"])
            elif path.endswith("/enqueue"):
                actor = self._require_actor(conn, d, O)
                bid = int(path.split("/")[3])
                r = service.add_to_batch(
                    conn, bid, int(d["cylinder_id"]),
                    position=d.get("position"), scenario=d.get("scenario", ""))
            elif path.endswith("/start"):
                actor = self._require_actor(conn, d, O)
                bid = int(path.split("/")[3])
                r = service.start_fill(conn, bid, actor["id"])
            elif path.endswith("/complete"):
                actor = self._require_actor(conn, d, O)
                bid = int(path.split("/")[3])
                r = service.complete_fill(conn, bid, actor["id"])
            elif path.endswith("/interrupt"):
                actor = self._require_actor(conn, d, O)
                bid = int(path.split("/")[3])
                r = service.interrupt_batch(conn, bid, d.get("reason", "未说明"), actor["id"])
            elif path == "/api/retest":
                actor = self._require_actor(conn, d, O)
                r = service.run_retest(conn, int(d["position_id"]), actor["id"])
            elif path == "/api/pair":
                actor = self._require_actor(conn, d, O)
                r = service.pair_cylinder(conn, int(d["cylinder_id"]),
                                          str(d["harness_code"]).strip(), actor["id"])
            elif path == "/api/repaint":
                actor = self._require_actor(conn, d, S)
                r = service.repaint_pair(conn, int(d["cylinder_id"]),
                                         str(d["harness_code"]).strip(), actor["id"])
            elif path.endswith("/unseal"):
                actor = self._require_actor(conn, d, S)
                cid = int(path.split("/")[3])
                r = service.unseal(conn, cid, actor["id"])
            elif path.endswith("/scrap"):
                actor = self._require_actor(conn, d, S)
                cid = int(path.split("/")[3])
                r = service.confirm_scrap(conn, cid, actor["id"], d.get("reason", ""))
            elif path == "/api/loans":
                actor = self._require_actor(conn, d, S)
                r = service.emergency_loan(conn, int(d["cylinder_id"]),
                                           d.get("borrower_unit", "").strip(), actor["id"])
            elif path.startswith("/api/cylinders/") and path.endswith("/loan-return"):
                actor = self._require_actor(conn, d, F)
                cid = int(path.split("/")[3])
                r = service.loan_return_by_cylinder(conn, cid, float(d["pressure_back"]),
                                                    actor["id"])
            elif path.startswith("/api/loans/") and path.endswith("/return"):
                actor = self._require_actor(conn, d, F)
                loan_id = int(path.split("/")[3])
                r = service.loan_return(conn, loan_id, float(d["pressure_back"]), actor["id"])
            elif path == "/api/attachments":
                actor = self._require_actor(conn, d)
                r = self._save_attachment(conn, d, actor["id"])
            else:
                self._send_json({"error": "未知接口", "code": "not_found"}, 404)
                return
            conn.commit()
            self._send_json({"ok": True, **r})
        finally:
            conn.close()

    def _save_attachment(self, conn, d: dict, actor_id: int) -> dict:
        cid = int(d["cylinder_id"])
        filename = Path(d.get("filename", "note.txt")).name
        kind = d.get("kind", "appearance")
        config.ATTACH_DIR.mkdir(parents=True, exist_ok=True)
        rel_dir = Path("attachments") / str(cid)
        abs_dir = config.ATTACH_DIR / str(cid)
        abs_dir.mkdir(parents=True, exist_ok=True)
        dest = abs_dir / filename
        content = base64.b64decode(d.get("content_base64", "")) if d.get("content_base64") \
            else d.get("content", "").encode("utf-8")
        dest.write_bytes(content)
        cur = conn.execute(
            "INSERT INTO attachments(cylinder_id,kind,filename,relpath,note) VALUES(?,?,?,?,?)",
            (cid, kind, filename, f"{rel_dir.as_posix()}/{filename}", d.get("note", "")))
        service.log_event(conn, "attachment", cylinder_id=cid, actor_id=actor_id,
                          detail={"file": filename, "kind": kind})
        return {"attachment_id": cur.lastrowid, "relpath": f"{rel_dir.as_posix()}/{filename}"}

    # ---------------- 静态文件 ---------------- #
    def _serve_static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self.send_error(404, "Not found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
            ".png": "image/png", ".json": "application/json",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SCBA 气瓶充装与检测系统")
    parser.add_argument("--port", type=int, default=int(__import__("os").environ.get("PORT", 8000)))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-seed", action="store_true")
    parser.add_argument("--no-engine", action="store_true", help="不启动后台复测/提醒线程")
    args = parser.parse_args(argv)

    init_db()
    if not args.no_seed:
        created = seed()
        print(f"[seed] {'已写入训练数据与泄漏/借调/报废样例' if created else '库内已有数据，跳过'}")

    if not args.no_engine:
        engine.db_path = config.DB_PATH
        engine.start()
        print("[engine] 静置复测与检验到期提醒已启动")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[web] 消防 SCBA 系统已启动: http://{args.host}:{args.port}")
    print("       Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 正在停止…")
    finally:
        engine.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
