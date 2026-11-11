"""HTTP-level tests with FastAPI's TestClient (no database needed for these routes)."""
from fastapi.testclient import TestClient

import chatbot_service
import common
import gateway
import tickets_service

gw = TestClient(gateway.app)


def test_health_metrics_and_security_headers():
    resp = gw.get("/health")
    assert resp.json() == {"status": "ok", "service": "gateway"}
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert "http_requests_total" in gw.get("/metrics").text


def test_gateway_refuses_internal_routes():
    assert gw.post("/api/chat/internal/triage", json={}).status_code == 403


def test_gateway_rate_limit(monkeypatch):
    monkeypatch.setattr(gateway, "check_rate_limit", lambda key: 10)
    resp = gw.get("/api/tickets/tickets")
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "10"


def test_internal_endpoint_needs_internal_token():
    bot = TestClient(chatbot_service.app)
    resp = bot.post("/internal/triage", json={"title": "x", "description": "y"})
    assert resp.status_code == 403


def test_triage_falls_back_to_rules_without_bedrock(monkeypatch):
    monkeypatch.setattr(chatbot_service, "search_kb", lambda text, limit=3: [])
    result = chatbot_service.triage("Ransomware message on screen", "All files renamed, entire team affected")
    assert result["triaged_by"] == "rules"
    assert result["category"] == "Security" and result["priority"] == "P1"


def test_triage_uses_valid_ai_output(monkeypatch):
    monkeypatch.setattr(chatbot_service, "ask_claude_json",
                        lambda *a, **k: {"category": "Email", "priority": "P3", "suggestion": "Restart Outlook."})
    result = chatbot_service.triage("Outlook stuck", "Not syncing since morning")
    assert result == {"category": "Email", "priority": "P3", "suggestion": "Restart Outlook.", "triaged_by": "ai"}


def test_triage_rejects_invalid_ai_output(monkeypatch):
    monkeypatch.setattr(chatbot_service, "ask_claude_json", lambda *a, **k: {"category": "Pizza", "priority": "P9"})
    monkeypatch.setattr(chatbot_service, "search_kb", lambda text, limit=3: [])
    assert chatbot_service.triage("VPN broken", "cannot connect to vpn")["triaged_by"] == "rules"


def test_requester_cannot_be_staff_by_default():
    assert not tickets_service.is_staff({"roles": {"user"}})
    assert tickets_service.is_staff({"roles": {"user", "agent"}})


def test_invalid_token_rejected():
    try:
        common.current_user("Bearer not-a-jwt")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
    else:
        raise AssertionError("invalid token accepted")
