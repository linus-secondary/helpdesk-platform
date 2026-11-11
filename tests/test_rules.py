"""Unit tests for helpdesk business rules (no database, Redis, Keycloak or network needed)."""
from datetime import UTC, datetime, timedelta

import rules
from chatbot_service import format_kb
from gateway import is_internal_path
from notifications_service import NotifyIn, should_alert

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


# ---------------- SLA
def test_sla_targets_per_priority():
    response, resolve = rules.sla_due("P1", NOW)
    assert response == NOW + timedelta(minutes=15)
    assert resolve == NOW + timedelta(hours=4)
    assert rules.sla_due("P4", NOW)[1] == NOW + timedelta(hours=72)


def test_sla_compliance():
    assert rules.sla_compliance(0, 0) == 100.0
    assert rules.sla_compliance(20, 1) == 95.0
    assert rules.sla_compliance(3, 1) == 66.7


# ---------------- ticket lifecycle
def test_valid_transitions():
    assert rules.can_transition("OPEN", "IN_PROGRESS")
    assert rules.can_transition("RESOLVED", "CLOSED")
    assert rules.can_transition("RESOLVED", "IN_PROGRESS")  # reopen


def test_invalid_transitions():
    assert not rules.can_transition("CLOSED", "OPEN")
    assert not rules.can_transition("IN_PROGRESS", "OPEN")
    assert not rules.can_transition("OPEN", "UNKNOWN")


# ---------------- rule-based triage (fallback when AI is off)
def test_security_incident_is_p1():
    t = rules.rule_based_triage("Clicked a link in a phishing email", "I entered my password on the page")
    assert t["category"] == "Security" and t["priority"] == "P1"


def test_outage_is_p1():
    t = rules.rule_based_triage("VPN down", "The VPN is down for the whole team, nobody can work")
    assert t["category"] == "Network" and t["priority"] == "P1"


def test_locked_account_is_access():
    t = rules.rule_based_triage("Account locked", "My account is locked after password change")
    assert t["category"] == "Access" and t["priority"] in ("P2", "P3")


def test_request_is_low_priority():
    t = rules.rule_based_triage("New keyboard please", "Would like a new keyboard for my desk")
    assert t == {**t, "category": "Hardware", "priority": "P4"}


def test_unknown_text_is_other():
    assert rules.rule_based_triage("Question", "Who orders the team lunch?")["category"] == "Other"


# ---------------- AI output validation
def test_validate_triage_accepts_valid_output():
    out = rules.validate_triage({"category": "Email", "priority": "P3", "suggestion": "Restart Outlook."})
    assert out == {"category": "Email", "priority": "P3", "suggestion": "Restart Outlook."}


def test_validate_triage_rejects_invalid_output():
    assert rules.validate_triage(None) is None
    assert rules.validate_triage({"category": "Plumbing", "priority": "P3"}) is None
    assert rules.validate_triage({"category": "Email", "priority": "P0"}) is None


# ---------------- misc
def test_kb_context_formatting():
    assert "no matching articles" in format_kb([])
    assert "[VPN will not connect] Check" in format_kb([{"title": "VPN will not connect", "body": "Check"}])


def test_urgent_alerts_go_to_sns():
    assert should_alert(NotifyIn(message="SLA BREACH: P1 ticket #4 missed", recipient_role="agent"))
    assert should_alert(NotifyIn(message="anything", recipient_role="agent", alert=True))
    assert not should_alert(NotifyIn(message="An agent replied on ticket #4", recipient_id="u1"))


def test_gateway_blocks_internal_paths():
    assert is_internal_path("internal/triage")
    assert not is_internal_path("tickets")
