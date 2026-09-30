"""进程内后台任务：周期生成静置复测到期与检验到期提醒。"""
import threading


class ReminderWorker(threading.Thread):
    daemon = True

    def __init__(self, service, interval=10.0, lock=None):
        super().__init__(name="reminder-worker")
        self.service = service
        self.interval = interval
        # 与 HTTP 请求共享同一把锁，串行化对同一 SQLite 连接的访问
        self.lock = lock or threading.RLock()
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                with self.lock:
                    self.service.scan_reminders()
            except Exception as exc:  # 提醒任务不得拖垮主进程
                print(f"[reminder] scan error: {exc}", flush=True)

    def stop(self):
        self._stop.set()
