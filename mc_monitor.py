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
# CONFIG (ปรับได้)
# ======================
SERVER_PROCESS_HINT = "java"              # ถ้า server รันด้วย Java ปกติใช้ "java"
MC_LOG_PATH = "./logs/latest.log"        # ปรับ path ให้ตรง (Paper/Spigot มักอยู่ logs/latest.log)
POLL_SEC = 2                              # เก็บค่าทุกกี่วินาที
OUT_JSONL = "./monitor_out.jsonl"         # ไฟล์ output แบบบรรทัดละ JSON
ALERT_TPS_LT = 18.0                       # threshold แจ้งเตือน
QUEUE_MAX = 200
ENABLE_INGAME_HUD = True
DOCKER_CONTAINER = "mc-server"
SEND_TO_PLAYERS = "@a"   # หรือใส่ชื่อคน เช่น "sominxt"
HUD_INTERVAL_SEC = 2     # ส่งทุกกี่วินาที (แนะนำ 2-5)

# ======================
# Internal
# ======================
stop_event = threading.Event()
raw_q: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAX)
proc_q: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAX)

# ตัวอย่าง pattern บาง plugin/ระบบจะเขียนค่า MSPT/TPS ลง log
# (ไม่รับประกันว่าทุกเซิร์ฟเวอร์จะมี)
TPS_PATTERNS = [
    re.compile(r"TPS[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    re.compile(r"MSPT[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
]

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_server_process():
    """
    หา process ของ server แบบง่าย ๆ: เลือก java ที่ CPU ใช้สูงสุด (หรือชื่อที่ hint)
    ถ้ามีหลาย java ในเครื่องเดียวอาจต้องปรับ logic เพิ่ม
    """
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

    # อัปเดต cpu_percent ให้ meaningful
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
    return scored[0][1] if scored else candidates[0]

def tail_log_nonblocking(path, last_pos):
    """
    อ่าน log เพิ่มเติมจากตำแหน่งล่าสุด (เหมือน tail -f แบบเบา ๆ)
    คืนค่า (new_pos, lines)
    """
    if not os.path.exists(path):
        return last_pos, []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(last_pos)
            data = f.read()
            new_pos = f.tell()
    except Exception:
        return last_pos, []

    lines = [ln for ln in data.splitlines() if ln.strip()]
    return new_pos, lines

def extract_metrics_from_lines(lines):
    """
    พยายามจับ TPS/MSPT จาก log lines (ถ้าไม่มี ก็คืน None)
    """
    tps = None
    mspt = None

    for ln in reversed(lines[-50:]):  # ดูท้าย ๆ ก่อน
        for pat in TPS_PATTERNS:
            m = pat.search(ln)
            if not m:
                continue
            val = float(m.group(1))
            if "mspt" in pat.pattern.lower():
                mspt = val
            else:
                # บางแพทเทิร์นจับ TPS
                tps = val

        if tps is not None or mspt is not None:
            break

    return tps, mspt

def collector_thread():
    """
    Thread 1: อ่านข้อมูลระบบ + อ่าน log
    """
    proc = find_server_process()
    log_pos = 0

    # prime CPU percent
    if proc:
        try:
            proc.cpu_percent(interval=None)
        except Exception:
            proc = None

    while not stop_event.is_set():
        ts = now_iso()

        # หา process ใหม่เป็นระยะ เผื่อรีสตาร์ท server
        if proc is None or not proc.is_running():
            proc = find_server_process()

        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        load1, load5, load15 = os.getloadavg() if hasattr(os, "getloadavg") else (None, None, None)

        server_cpu = None
        server_rss = None
        server_pid = None

        if proc and proc.is_running():
            try:
                server_pid = proc.pid
                server_cpu = proc.cpu_percent(interval=None)  # % ของ 1 core*? (psutil นิยาม)
                server_rss = proc.memory_info().rss
            except Exception:
                proc = None

        # อ่าน log
        log_lines = []
        log_pos, log_lines = tail_log_nonblocking(MC_LOG_PATH, log_pos)
        tps, mspt = extract_metrics_from_lines(log_lines) if log_lines else (None, None)

        payload = {
            "ts": ts,
            "host": {
                "cpu_percent": cpu,
                "mem_used": mem.used,
                "mem_total": mem.total,
                "mem_percent": mem.percent,
                "load1": load1, "load5": load5, "load15": load15,
            },
            "server": {
                "pid": server_pid,
                "cpu_percent": server_cpu,
                "rss": server_rss,
            },
            "mc": {
                "tps": tps,
                "mspt": mspt,
            }
        }

        try:
            raw_q.put(payload, timeout=1)
        except queue.Full:
            # ถ้าเต็ม แปลว่า downstream ช้า -> ทิ้งข้อมูลเก่าไป (เลือกทิ้งรอบนี้)
            pass

        time.sleep(POLL_SEC)

def processor_thread():
    """
    Thread 2: ประมวลผล + สรุป + ทำ alert logic
    """
    # ทำ rolling average แบบง่าย
    tps_window = []
    mspt_window = []
    WIN = 10  # 10 samples

    while not stop_event.is_set():
        try:
            item = raw_q.get(timeout=1)
        except queue.Empty:
            continue

        tps = item["mc"]["tps"]
        mspt = item["mc"]["mspt"]

        if tps is not None:
            tps_window.append(tps)
            if len(tps_window) > WIN:
                tps_window.pop(0)

        if mspt is not None:
            mspt_window.append(mspt)
            if len(mspt_window) > WIN:
                mspt_window.pop(0)

        tps_avg = (sum(tps_window) / len(tps_window)) if tps_window else None
        mspt_avg = (sum(mspt_window) / len(mspt_window)) if mspt_window else None

        alert = None
        if tps_avg is not None and tps_avg < ALERT_TPS_LT:
            alert = {
                "type": "LOW_TPS",
                "message": f"Average TPS low: {tps_avg:.2f} < {ALERT_TPS_LT}",
            }

        enriched = {
            **item,
            "summary": {
                "tps_avg": tps_avg,
                "mspt_avg": mspt_avg,
                "alert": alert,
            }
        }

        try:
            proc_q.put(enriched, timeout=1)
        except queue.Full:
            pass

def exporter_thread():
    """
    Thread 3: แสดงผล + เขียนไฟล์ (JSONL)
    """
    os.makedirs(os.path.dirname(OUT_JSONL) or ".", exist_ok=True)

    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        last_hud = 0.0

        while not stop_event.is_set():
            try:
                item = proc_q.get(timeout=1)
            except queue.Empty:
                continue

            # log to file
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            f.flush()

            # print a compact "F3-ish" line
            host = item["host"]
            srv = item["server"]
            mc = item["mc"]
            sm = item["summary"]
            load1 = host["load1"] if host["load1"] is not None else 0.0
            tps_avg_str = f"{sm['tps_avg']:.2f}" if sm["tps_avg"] is not None else "N/A"


            line = (
                f"[{item['ts']}] "
                f"CPU:{host['cpu_percent']:>5.1f}% MEM:{host['mem_percent']:>5.1f}% "
                f"LOAD:{load1:.2f} "
                f"JAVA(pid={srv['pid']}) CPU:{(srv['cpu_percent'] if srv['cpu_percent'] is not None else 0):>5.1f}% "
                f"RSS:{(srv['rss'] or 0)/1024/1024:>6.1f}MB "
                f"TPS:{mc['tps'] if mc['tps'] is not None else 'N/A'} "
                f"MSPT:{mc['mspt'] if mc['mspt'] is not None else 'N/A'} "
                f"AVG_TPS:{tps_avg_str}"
            )

            print(line)

            if sm["alert"]:
                print(f"  !!! ALERT: {sm['alert']['message']}")

            # --- Send to Minecraft ActionBar ---
            if ENABLE_INGAME_HUD:
                now = time.time()
                if ENABLE_INGAME_HUD and (now - last_hud) >= HUD_INTERVAL_SEC:
                    last_hud = now
                host = item["host"]
                srv = item["server"]
                sm = item["summary"]

                load1 = host["load1"] if host["load1"] is not None else 0.0
                rss_mb = ((srv["rss"] or 0) / 1024 / 1024)
                tps_avg = sm["tps_avg"]

                msg = (
                    f"CPU {host['cpu_percent']:.0f}% | MEM {host['mem_percent']:.0f}% | "
                    f"LOAD {load1:.2f} | RSS {rss_mb:.0f}MB | "
                    f"AVG_TPS {tps_avg:.2f}"
                    if tps_avg is not None else
                    f"CPU {host['cpu_percent']:.0f}% | MEM {host['mem_percent']:.0f}% | "
                    f"LOAD {load1:.2f} | RSS {rss_mb:.0f}MB | AVG_TPS N/A"
                )

                # escape JSON
                safe = msg.replace("\\", "\\\\").replace('"', '\\"')

                cmd = (
                    f'title {SEND_TO_PLAYERS} actionbar '
                    f'{{"text":"{safe}"}}'
                )

                try:
                    subprocess.run(
                        ["sudo", "docker", "exec", DOCKER_CONTAINER, "rcon-cli", cmd],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                except Exception:
                    pass


def handle_sigint(sig, frame):
    stop_event.set()

def main():
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    t1 = threading.Thread(target=collector_thread, name="collector", daemon=True)
    t2 = threading.Thread(target=processor_thread, name="processor", daemon=True)
    t3 = threading.Thread(target=exporter_thread, name="exporter", daemon=True)

    t1.start(); t2.start(); t3.start()

    # main loop
    while not stop_event.is_set():
        time.sleep(0.5)

    print("Shutting down...")

if __name__ == "__main__":
    main()
