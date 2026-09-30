"""启动入口：python -m app [--port 8080] [--host 127.0.0.1] [--settle-seconds 900] [--reset]"""
import argparse
import os
import sys

from .server import serve, DB_PATH, ATTACH_DIR

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="消防站空气呼吸器气瓶充装与检测系统")
    ap.add_argument("--host", default=os.environ.get("SCBA_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SCBA_PORT", "8080")))
    ap.add_argument("--settle-seconds", type=float,
                    default=int(os.environ.get("SCBA_SETTLE_SECONDS", "900")),
                    help="静置复测等待时长（默认 900 秒/15 分钟）")
    ap.add_argument("--reset", action="store_true", help="删除既有数据库与附件后重建样例")
    args = ap.parse_args()

    if args.reset and os.path.exists(DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.isdir(ATTACH_DIR):
            for f in os.listdir(ATTACH_DIR):
                os.remove(os.path.join(ATTACH_DIR, f))
        print("已重置数据库与附件目录", flush=True)

    try:
        httpd = serve(host=args.host, port=args.port, settle_seconds=args.settle_seconds)
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
