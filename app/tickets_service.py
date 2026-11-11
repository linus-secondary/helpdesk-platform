"""tickets service: create, view, comment on and work tickets.

Roles (from Keycloak):
  user  - employees: raise tickets, see and comment on their own tickets, reopen/close them
  agent - IT staff: see the whole queue, assign, change status/priority, write internal notes
  admin - everything an agent can do, plus SLA reports
Every change is written to ticket_events (audit trail).
"""
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

import rules
from common import create_app, current_user, get_pool, internal_client, log

app = create_app("tickets", schema=Path(__file__).with_name("schema.sql"))
CHATBOT_URL = os.environ.get("CHATBOT_URL", "http://chatbot:8000")
NOTIFICATIONS_URL = os.environ.get("NOTIFICATIONS_URL", "http://notifications:8000")
STAFF = {"agent", "admin"}


class TicketIn(BaseModel):
    title: str = Field(min_length=5, max_length=200)
    description: str = Field(min_length=10, max_length=5000)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    internal: bool = False


class TicketUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(OPEN|IN_PROGRESS|WAITING_ON_USER|RESOLVED|CLOSED)$")
    priority: str | None = Field(default=None, pattern="^P[1-4]$")
    category: str | None = None
    assign_to_me: bool = False


def is_staff(user: dict) -> bool:
    return bool(user["roles"] & STAFF)


def display_name(user: dict) -> str:
    return user["preferred_username"] or user["email"] or user["sub"][:8]


def notify(message: str, ticket_id: int, recipient_id: str | None = None, recipient_role: str | None = None):
    """Best effort: a notification failure must never fail the ticket operation."""
    try:
        with internal_client(timeout=5) as http:
            http.post(f"{NOTIFICATIONS_URL}/internal/notify", json={
                "message": message, "ticket_id": ticket_id,
                "recipient_id": recipient_id, "recipient_role": recipient_role})
    except httpx.HTTPError as exc:
        log.warning(f"notification failed: {exc}")


def audit(conn, ticket_id: int, actor: str, action: str, detail: str = "") -> None:
    conn.execute("INSERT INTO ticket_events (ticket_id, actor, action, detail) VALUES (%s, %s, %s, %s)",
                 (ticket_id, actor, action, detail))


def load_ticket(ticket_id: int, user: dict) -> dict:
    with get_pool().connection() as conn:
        ticket = conn.execute("SELECT * FROM tickets WHERE ticket_id = %s", (ticket_id,)).fetchone()
    if not ticket or (ticket["requester_id"] != user["sub"] and not is_staff(user)):
        raise HTTPException(404, "Ticket not found")  # 404, not 403: don't reveal other people's tickets
    return ticket


@app.post("/tickets", status_code=201)
def create_ticket(body: TicketIn, user: dict = Depends(current_user)):
    try:
        with internal_client(timeout=30) as http:
            resp = http.post(f"{CHATBOT_URL}/internal/triage", json=body.model_dump())
            resp.raise_for_status()
            triage = resp.json()
    except httpx.HTTPError as exc:
        log.warning(f"triage service unavailable, using rules: {exc}")
        triage = {**rules.rule_based_triage(body.title, body.description), "triaged_by": "rules"}

    now = datetime.now(UTC)
    response_due, resolve_due = rules.sla_due(triage["priority"], now)
    with get_pool().connection() as conn, conn.transaction():
        ticket = conn.execute(
            """INSERT INTO tickets (requester_id, requester_name, title, description, category, priority,
                   ai_suggestion, triaged_by, response_due_at, resolve_due_at, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (user["sub"], display_name(user), body.title, body.description, triage["category"],
             triage["priority"], triage["suggestion"], triage["triaged_by"], response_due, resolve_due, now)
        ).fetchone()
        audit(conn, ticket["ticket_id"], display_name(user), "created",
              f"{triage['category']}/{triage['priority']} ({triage['triaged_by']})")
    notify(f"New {ticket['priority']} {ticket['category']} ticket #{ticket['ticket_id']}: {ticket['title']}",
           ticket["ticket_id"], recipient_role="agent")
    log.info(f"ticket {ticket['ticket_id']} created ({ticket['priority']})")
    return ticket


@app.get("/tickets")
def list_tickets(status: str = "", priority: str = "", mine: bool = False, user: dict = Depends(current_user)):
    sql, params = "SELECT * FROM tickets WHERE true", []
    if not is_staff(user):
        sql += " AND requester_id = %s"
        params.append(user["sub"])
    elif mine:
        sql += " AND assignee_id = %s"
        params.append(user["sub"])
    if status:
        sql += " AND status = %s"
        params.append(status)
    if priority:
        sql += " AND priority = %s"
        params.append(priority)
    sql += " ORDER BY CASE WHEN status IN ('RESOLVED','CLOSED') THEN 1 ELSE 0 END, priority, created_at LIMIT 200"
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int, user: dict = Depends(current_user)):
    ticket = load_ticket(ticket_id, user)
    with get_pool().connection() as conn:
        comments_sql = "SELECT * FROM ticket_comments WHERE ticket_id = %s"
        if not is_staff(user):
            comments_sql += " AND internal = false"
        ticket["comments"] = conn.execute(comments_sql + " ORDER BY created_at", (ticket_id,)).fetchall()
        if is_staff(user):
            ticket["events"] = conn.execute(
                "SELECT actor, action, detail, created_at FROM ticket_events WHERE ticket_id = %s ORDER BY created_at",
                (ticket_id,)).fetchall()
    return ticket


@app.post("/tickets/{ticket_id}/comments", status_code=201)
def add_comment(ticket_id: int, body: CommentIn, user: dict = Depends(current_user)):
    ticket = load_ticket(ticket_id, user)
    if body.internal and not is_staff(user):
        raise HTTPException(403, "Only agents can write internal notes")
    with get_pool().connection() as conn, conn.transaction():
        comment = conn.execute(
            "INSERT INTO ticket_comments (ticket_id, author_id, author_name, body, internal) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (ticket_id, user["sub"], display_name(user), body.body, body.internal)).fetchone()
        if is_staff(user) and not body.internal and ticket["first_response_at"] is None:
            conn.execute("UPDATE tickets SET first_response_at = now(), updated_at = now() WHERE ticket_id = %s",
                         (ticket_id,))
            audit(conn, ticket_id, display_name(user), "first_response")
        audit(conn, ticket_id, display_name(user), "internal_note" if body.internal else "comment")
    if is_staff(user) and not body.internal:
        notify(f"An agent replied on ticket #{ticket_id}", ticket_id, recipient_id=ticket["requester_id"])
    elif not is_staff(user) and ticket["assignee_id"]:
        notify(f"{display_name(user)} replied on ticket #{ticket_id}", ticket_id, recipient_id=ticket["assignee_id"])
    return comment


@app.patch("/tickets/{ticket_id}")
def update_ticket(ticket_id: int, body: TicketUpdate, user: dict = Depends(current_user)):
    ticket = load_ticket(ticket_id, user)
    staff = is_staff(user)
    if not staff:
        # Requesters may only close a resolved ticket or reopen it
        if body.priority or body.category or body.assign_to_me or body.status not in ("CLOSED", "IN_PROGRESS"):
            raise HTTPException(403, "Only agents can change this")
        if ticket["status"] != "RESOLVED":
            raise HTTPException(409, "You can only close or reopen a resolved ticket")
    if body.category and body.category not in rules.CATEGORIES:
        raise HTTPException(422, "Unknown category")
    if body.status and body.status != ticket["status"] and not rules.can_transition(ticket["status"], body.status):
        raise HTTPException(409, f"Cannot move ticket from {ticket['status']} to {body.status}")

    changes, params, notes = [], [], []
    if body.status and body.status != ticket["status"]:
        changes.append("status = %s")
        params.append(body.status)
        notes.append(f"status {ticket['status']} -> {body.status}")
        if body.status == "RESOLVED":
            changes.append("resolved_at = now()")
        if body.status == "IN_PROGRESS" and ticket["status"] == "RESOLVED":
            changes.append("resolved_at = NULL")
    if body.priority and body.priority != ticket["priority"]:
        response_due, resolve_due = rules.sla_due(body.priority, ticket["created_at"])
        changes += ["priority = %s", "response_due_at = %s", "resolve_due_at = %s", "triaged_by = 'agent'"]
        params += [body.priority, response_due, resolve_due]
        notes.append(f"priority {ticket['priority']} -> {body.priority}")
    if body.category and body.category != ticket["category"]:
        changes += ["category = %s", "triaged_by = 'agent'"]
        params.append(body.category)
        notes.append(f"category {ticket['category']} -> {body.category}")
    if body.assign_to_me:
        changes += ["assignee_id = %s", "assignee_name = %s"]
        params += [user["sub"], display_name(user)]
        notes.append(f"assigned to {display_name(user)}")
    if not changes:
        return get_ticket(ticket_id, user)

    with get_pool().connection() as conn, conn.transaction():
        conn.execute(f"UPDATE tickets SET {', '.join(changes)}, updated_at = now() WHERE ticket_id = %s",
                     (*params, ticket_id))
        audit(conn, ticket_id, display_name(user), "updated", "; ".join(notes))
    if staff and body.status and body.status != ticket["status"]:
        notify(f"Ticket #{ticket_id} is now {body.status.replace('_', ' ').lower()}", ticket_id,
               recipient_id=ticket["requester_id"])
    return get_ticket(ticket_id, user)
