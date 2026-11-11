"""sla service: watches SLA deadlines and reports compliance.

A background thread checks every SLA_CHECK_SECONDS for tickets that missed their first-response or
resolution deadline, flags them once, and alerts agents (and the requester's assignee).
Only one pod does the check at a time (PostgreSQL advisory lock), so scaling out is safe.
"""
import os
import threading
import time
from pathlib import Path

import httpx
from fastapi import Depends
from prometheus_client import Gauge

import rules
from common import create_app, get_pool, internal_client, log, require_roles

NOTIFICATIONS_URL = os.environ.get("NOTIFICATIONS_URL", "http://notifications:8000")
CHECK_SECONDS = int(os.environ.get("SLA_CHECK_SECONDS", "60"))
OPEN_STATES = ("OPEN", "IN_PROGRESS", "WAITING_ON_USER")

OPEN_TICKETS = Gauge("helpdesk_open_tickets", "Open tickets", ["priority"])
BREACHED = Gauge("helpdesk_sla_breached_open_tickets", "Open tickets that breached SLA", ["priority"])


def find_and_flag_breaches() -> list[dict]:
    with get_pool().connection() as conn, conn.transaction():
        got_lock = conn.execute("SELECT pg_try_advisory_xact_lock(626262) AS ok").fetchone()["ok"]
        if not got_lock:
            return []
        response = conn.execute(
            """UPDATE tickets SET response_breached = true
               WHERE status = ANY(%s) AND first_response_at IS NULL AND response_due_at < now()
                 AND response_breached = false
               RETURNING ticket_id, priority, title, assignee_id, 'first response' AS kind""",
            (list(OPEN_STATES),)).fetchall()
        resolve = conn.execute(
            """UPDATE tickets SET resolve_breached = true
               WHERE status = ANY(%s) AND resolve_due_at < now() AND resolve_breached = false
               RETURNING ticket_id, priority, title, assignee_id, 'resolution' AS kind""",
            (list(OPEN_STATES),)).fetchall()
        for row in response + resolve:
            conn.execute("INSERT INTO ticket_events (ticket_id, actor, action, detail) VALUES (%s, 'sla-bot', "
                         "'sla_breach', %s)", (row["ticket_id"], f"{row['kind']} SLA missed"))
    return response + resolve


def update_gauges() -> None:
    with get_pool().connection() as conn:
        rows = conn.execute(
            """SELECT priority, count(*) AS open,
                      count(*) FILTER (WHERE response_breached OR resolve_breached) AS breached
               FROM tickets WHERE status = ANY(%s) GROUP BY priority""", (list(OPEN_STATES),)).fetchall()
    seen = {r["priority"]: r for r in rows}
    for p in rules.PRIORITIES:
        OPEN_TICKETS.labels(p).set(seen.get(p, {}).get("open", 0))
        BREACHED.labels(p).set(seen.get(p, {}).get("breached", 0))


def check_loop() -> None:
    while True:
        try:
            for b in find_and_flag_breaches():
                message = f"SLA BREACH: {b['priority']} ticket #{b['ticket_id']} missed its {b['kind']} target"
                log.warning(message)
                with internal_client(timeout=5) as http:
                    http.post(f"{NOTIFICATIONS_URL}/internal/notify",
                              json={"message": message, "ticket_id": b["ticket_id"], "recipient_role": "agent",
                                    "alert": b["priority"] in ("P1", "P2")})
            update_gauges()
        except (httpx.HTTPError, Exception) as exc:  # keep the watcher alive
            log.error(f"sla check failed: {exc}")
        time.sleep(CHECK_SECONDS)


def start_watcher() -> None:
    threading.Thread(target=check_loop, name="sla-watcher", daemon=True).start()


app = create_app("sla", schema=Path(__file__).with_name("schema.sql"), on_startup=[start_watcher])


@app.get("/sla/summary")
def summary(days: int = 30, _: dict = Depends(require_roles("agent", "admin"))):
    with get_pool().connection() as conn:
        rows = conn.execute(
            """SELECT priority,
                      count(*) AS total,
                      count(*) FILTER (WHERE response_breached OR resolve_breached) AS breached,
                      count(*) FILTER (WHERE status IN ('OPEN','IN_PROGRESS','WAITING_ON_USER')) AS open,
                      round(avg(extract(epoch FROM resolved_at - created_at) / 3600)
                            FILTER (WHERE resolved_at IS NOT NULL)::numeric, 1) AS avg_resolve_hours,
                      count(*) FILTER (WHERE triaged_by = 'ai') AS ai_triaged
               FROM tickets WHERE created_at > now() - make_interval(days => %s)
               GROUP BY priority ORDER BY priority""", (max(1, min(days, 365)),)).fetchall()
    for r in rows:
        r["compliance_pct"] = rules.sla_compliance(r["total"], r["breached"])
        r["targets_hours"] = rules.SLA_HOURS[r["priority"]]
    total = sum(r["total"] for r in rows)
    breached = sum(r["breached"] for r in rows)
    return {"days": days, "overall_compliance_pct": rules.sla_compliance(total, breached),
            "total": total, "by_priority": rows}


@app.get("/sla/breaches")
def breaches(_: dict = Depends(require_roles("agent", "admin"))):
    with get_pool().connection() as conn:
        return conn.execute(
            """SELECT ticket_id, title, priority, status, assignee_name, response_breached, resolve_breached,
                      response_due_at, resolve_due_at FROM tickets
               WHERE (response_breached OR resolve_breached) AND status = ANY(%s)
               ORDER BY priority, resolve_due_at""", (list(OPEN_STATES),)).fetchall()
