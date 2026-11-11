"""notifications service: in-app notifications + optional Amazon SNS alerts.

- Every notification is stored and shown in the web app (bell icon).
- If SNS_TOPIC_ARN is set, urgent alerts (P1/P2 SLA breaches, security tickets) are also published to
  Amazon SNS, which can email or SMS the on-call team. On EKS the pod gets sns:Publish via IRSA.
"""
import os
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from common import create_app, current_user, get_pool, log, require_internal

app = create_app("notifications", schema=Path(__file__).with_name("schema.sql"))
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
_sns = None


class NotifyIn(BaseModel):
    message: str = Field(max_length=500)
    ticket_id: int | None = None
    recipient_id: str | None = None
    recipient_role: str | None = Field(default=None, pattern="^(user|agent|admin)$")
    alert: bool = False


def should_alert(body: NotifyIn) -> bool:
    return body.alert or body.message.startswith(("New P1", "SLA BREACH: P1"))


def publish_sns(subject: str, message: str) -> None:
    global _sns
    if not SNS_TOPIC_ARN:
        return
    try:
        if _sns is None:
            import boto3

            _sns = boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        _sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:99], Message=message)
    except Exception as exc:  # never fail the request because the alert channel failed
        log.error(f"SNS publish failed: {exc}")


@app.post("/internal/notify", dependencies=[Depends(require_internal)], status_code=201)
def notify(body: NotifyIn):
    if not body.recipient_id and not body.recipient_role:
        raise HTTPException(422, "recipient_id or recipient_role is required")
    with get_pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO notifications (recipient_id, recipient_role, ticket_id, message) VALUES (%s, %s, %s, %s) "
            "RETURNING notification_id", (body.recipient_id, body.recipient_role, body.ticket_id, body.message)
        ).fetchone()
    if should_alert(body):
        publish_sns("IT Helpdesk alert", body.message)
    return row


@app.get("/notifications")
def my_notifications(unread_only: bool = False, user: dict = Depends(current_user)):
    sql = """SELECT n.* FROM notifications n
             WHERE (n.recipient_id = %s OR n.recipient_role = ANY(%s))"""
    if unread_only:
        sql += " AND n.read = false"
    with get_pool().connection() as conn:
        return conn.execute(sql + " ORDER BY n.created_at DESC LIMIT 50",
                            (user["sub"], list(user["roles"]))).fetchall()


@app.post("/notifications/{notification_id}/read", status_code=204)
def mark_read(notification_id: int, user: dict = Depends(current_user)):
    with get_pool().connection() as conn:
        updated = conn.execute(
            "UPDATE notifications SET read = true WHERE notification_id = %s "
            "AND (recipient_id = %s OR recipient_role = ANY(%s))",
            (notification_id, user["sub"], list(user["roles"]))).rowcount
    if not updated:
        raise HTTPException(404, "Notification not found")
