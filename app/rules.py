"""Business rules shared by the helpdesk services. Pure functions, unit tested, no I/O."""
from datetime import datetime, timedelta

CATEGORIES = ["Network", "Hardware", "Software", "Access", "Email", "Security", "Other"]
PRIORITIES = ["P1", "P2", "P3", "P4"]

# SLA policy: hours to first response / to resolution
SLA_HOURS = {
    "P1": {"response": 0.25, "resolve": 4},     # critical: 15 min / 4 h
    "P2": {"response": 1, "resolve": 8},
    "P3": {"response": 4, "resolve": 24},
    "P4": {"response": 8, "resolve": 72},
}

# Ticket lifecycle
TRANSITIONS = {
    "OPEN": {"IN_PROGRESS", "WAITING_ON_USER", "RESOLVED", "CLOSED"},
    "IN_PROGRESS": {"WAITING_ON_USER", "RESOLVED"},
    "WAITING_ON_USER": {"IN_PROGRESS", "RESOLVED"},
    "RESOLVED": {"CLOSED", "IN_PROGRESS"},          # requester can reopen
    "CLOSED": set(),
}


def sla_due(priority: str, created_at: datetime) -> tuple[datetime, datetime]:
    policy = SLA_HOURS[priority]
    return (created_at + timedelta(hours=policy["response"]),
            created_at + timedelta(hours=policy["resolve"]))


def can_transition(current: str, new: str) -> bool:
    return new in TRANSITIONS.get(current, set())


# Keyword rules: used when Bedrock is disabled or fails, so triage always works.
KEYWORDS = {
    "Security": ["phishing", "hacked", "virus", "malware", "suspicious", "ransomware", "breach", "clicked a link"],
    "Access": ["password", "locked", "login", "log in", "sign in", "mfa", "permission", "access", "account"],
    "Network": ["vpn", "wifi", "wi-fi", "internet", "network", "dns", "connection"],
    "Email": ["outlook", "email", "e-mail", "mailbox", "calendar"],
    "Hardware": ["laptop", "printer", "monitor", "keyboard", "mouse", "battery", "screen", "slow"],
    "Software": ["install", "software", "application", "app", "licence", "license", "update", "crash"],
}
URGENT = ["everyone", "all users", "whole team", "entire", "down", "outage", "production", "cannot work",
          "can't work", "ransomware", "hacked", "breach", "clicked a link"]
HIGH = ["urgent", "asap", "blocked", "deadline", "cannot", "can't", "unable"]


def rule_based_triage(title: str, description: str) -> dict:
    text = f"{title} {description}".lower()
    scores = {cat: sum(word in text for word in words) for cat, words in KEYWORDS.items()}
    category = max(scores, key=scores.get) if max(scores.values()) > 0 else "Other"
    if any(w in text for w in URGENT) or category == "Security" and scores["Security"] > 0:
        priority = "P1" if any(w in text for w in URGENT) else "P2"
    elif any(w in text for w in HIGH):
        priority = "P2"
    elif category in ("Access", "Network", "Email"):
        priority = "P3"
    else:
        priority = "P4"
    return {"category": category, "priority": priority,
            "suggestion": "See the knowledge base articles for this category while an agent reviews your ticket."}


def validate_triage(data: dict) -> dict | None:
    """Accept an AI triage result only if every field is valid; otherwise the caller falls back to rules."""
    if not isinstance(data, dict):
        return None
    if data.get("category") not in CATEGORIES or data.get("priority") not in PRIORITIES:
        return None
    suggestion = str(data.get("suggestion", "")).strip()[:1500]
    return {"category": data["category"], "priority": data["priority"], "suggestion": suggestion}


def sla_compliance(total: int, breached: int) -> float:
    return 100.0 if total == 0 else round(100.0 * (total - breached) / total, 1)
