#!/usr/bin/env python3
import os
import re
import time
import json
import queue
import psutil
import signal
import subprocess
import threading
from datetime import datetime, timezone

# ======================
# CONFIG
# ======================
SERVER_PROCESS_HINT = "java"
MC_LOG_PATH = "./logs/latest.log"        # ถ้า docker → ไม่ใช้ก็ได้
POLL_SEC = 2
OUT_JSONL = "./monitor_out.jsonl"
ALERT_TPS_LT = 18.0

ENABLE_INGAME_HUD = True
HUD_INTERVAL_SEC = 2

DOCKER_CONTAINER = "mc-server"
SEND_TO_PLAYERS = "@a"   # หรือชื่อผู้เล่น

QUEUE_MAX = 200

# ======================
# Internal
# ======================
stop_event = threading.Event()
raw_q = queue.Queue(maxsize=QUEUE_MAX)
proc_q = queue.Queue(maxsize=QUEUE_MAX)

TPS_PATTERNS = [
    re.compile(r"TPS[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    re.compile(r"MSPT[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_server_process():
    candidates = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "cpu_percent", "memory_info"]):
        try:
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if SERVER_PROCESS_HINT in name or SERVER_PROCESS_HINT in cmd:
                candidates.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not candidates:
        return None

    for p in candidates:
        try:
            p.cpu_percent(interval=None)
        except Exception:
            pass

    time.sleep(0.2)

    scored = []
    for p in candidates:
        try:
            scored.append((p.cpu_percent(interval=None), p))
        except Exception:
            pass

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def collector_thread():
    proc = None

    while not stop_event.is_set():
        ts = now_iso()

        if proc is None or not proc.is_running():
            proc = find_server_process()

        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        load1, load5, load15 = os.getloadavg()

        server_pid = None
        server_cpu = None
        server_rss = None

        if proc:
            try:
                server_pid = proc.pid
                server_cpu = proc.cpu_percent(interval=None)
                server_rss = proc.memory_info().rss
            except Exception:
                proc = None

        payload = {
            "ts": ts,
            "host": {
                "cpu_percent": cpu,
                "mem_percent": mem.percent,
                "load1": load1,
            },
            "server": {
                "pid": server_pid,
                "cpu_percent": server_cpu,
                "rss": server_rss,
            },
            "mc": {
                "tps": None,
                "mspt": None,
            }
        }

        try:
            raw_q.put(payload, timeout=1)
        except queue.Full:
            pass

        time.sleep(POLL_SEC)


def processor_thread():
    tps_window = []
    WIN = 10

    while not stop_event.is_set():
        try:
            item = raw_q.get(timeout=1)
        except queue.Empty:
            continue

        tps = item["mc"]["tps"]

        if tps is not None:
            tps_window.append(tps)
            if len(tps_window) > WIN:
                tps_window.pop(0)

        tps_avg = sum(tps_window) / len(tps_window) if tps_window else None

        alert = None
        if tps_avg is not None and tps_avg < ALERT_TPS_LT:
            alert = f"LOW TPS {tps_avg:.2f}"

        item["summary"] = {
            "tps_avg": tps_avg,
            "alert": alert,
        }

        try:
            proc_q.put(item, timeout=1)
        except queue.Full:
            pass


def exporter_thread():
    os.makedirs(os.path.dirname(OUT_JSONL) or ".", exist_ok=True)

    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        last_hud = 0.0

        while not stop_event.is_set():
            try:
                item = proc_q.get(timeout=1)
            except queue.Empty:
                continue

            f.write(json.dumps(item) + "\n")
            f.flush()

            host = item["host"]
            srv = item["server"]
            sm = item["summary"]

            print(
                f"[{item['ts']}] "
                f"CPU {host['cpu_percent']:.1f}% | "
                f"MEM {host['mem_percent']:.1f}% | "
                f"LOAD {host['load1']:.2f} | "
                f"JAVA {srv['cpu_percent'] or 0:.1f}% | "
                f"RSS {(srv['rss'] or 0)/1024/1024:.0f}MB"
            )

            # ---- ActionBar HUD ----
            now = time.time()
            if ENABLE_INGAME_HUD and (now - last_hud) >= HUD_INTERVAL_SEC:
                last_hud = now

                rss_mb = (srv["rss"] or 0) / 1024 / 1024
                msg = (
                    f"CPU {host['cpu_percent']:.0f}% | "
                    f"MEM {host['mem_percent']:.0f}% | "
                    f"LOAD {host['load1']:.2f} | "
                    f"RSS {rss_mb:.0f}MB"
                )

                safe = msg.replace("\\", "\\\\").replace('"', '\\"')
                cmd = f'title {SEND_TO_PLAYERS} actionbar {{"text":"{safe}"}}'

                subprocess.run(
                    ["docker", "exec", DOCKER_CONTAINER, "rcon-cli", cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )


def handle_signal(sig, frame):
    stop_event.set()


def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threading.Thread(target=collector_thread, daemon=True).start()
    threading.Thread(target=processor_thread, daemon=True).start()
    threading.Thread(target=exporter_thread, daemon=True).start()

    while not stop_event.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
