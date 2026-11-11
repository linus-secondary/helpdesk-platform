"""End-to-end smoke test against a running deployment.

    python scripts/smoke_test.py http://localhost:8000

Signs in as employee1 (Keycloak password grant on the helpdesk-cli test client), raises a ticket,
checks AI/rule triage, SLA deadlines, comments, role-based access control, the knowledge-base
assistant, notifications and gateway security. Agent accounts require MFA, so agent actions are
tested in the browser (see README).
"""
import json
import re
import sys

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
passed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if not condition:
        print(f"FAIL  {name} {detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


http = httpx.Client(timeout=60)

resp = http.get(f"{BASE}/config.js")
cfg = json.loads(re.search(r"=\s*(\{.*\});", resp.text).group(1))
check("frontend config served", "keycloakUrl" in cfg)
check("security headers set", resp.headers.get("x-frame-options") == "DENY"
      and "content-security-policy" in resp.headers)
check("API requires login", http.get(f"{BASE}/api/tickets/tickets").status_code == 401)
check("internal route blocked at gateway",
      http.post(f"{BASE}/api/chat/internal/triage", json={}).status_code == 403)

token = http.post(f"{cfg['keycloakUrl']}/realms/{cfg['realm']}/protocol/openid-connect/token",
                  data={"grant_type": "password", "client_id": "helpdesk-cli",
                        "username": "employee1", "password": "Employee@1234"})
check("keycloak login (employee1)", token.status_code == 200, token.text[:200])
auth = {"Authorization": f"Bearer {token.json()['access_token']}"}

ticket = http.post(f"{BASE}/api/tickets/tickets", headers=auth, json={
    "title": "Clicked a link in a phishing email",
    "description": "I clicked a link in a suspicious email and typed my password. The whole team got it."}).json()
check("ticket created", ticket.get("status") == "OPEN", str(ticket)[:300])
check("triaged as Security", ticket["category"] == "Security", ticket["category"])
check("triaged as P1", ticket["priority"] == "P1", ticket["priority"])
check("SLA deadlines set", ticket["response_due_at"] < ticket["resolve_due_at"])
check("suggested fix provided", len(ticket["ai_suggestion"]) > 10)
tid = ticket["ticket_id"]

mine = http.get(f"{BASE}/api/tickets/tickets", headers=auth).json()
check("employee sees own tickets only", all(t["requester_id"] == ticket["requester_id"] for t in mine))

comment = http.post(f"{BASE}/api/tickets/tickets/{tid}/comments", headers=auth, json={"body": "Still happening"})
check("employee can comment", comment.status_code == 201)
check("employee cannot write internal notes", http.post(
    f"{BASE}/api/tickets/tickets/{tid}/comments", headers=auth,
    json={"body": "x", "internal": True}).status_code == 403)
check("employee cannot change priority", http.patch(
    f"{BASE}/api/tickets/tickets/{tid}", headers=auth, json={"priority": "P4"}).status_code == 403)
check("employee cannot see SLA reports", http.get(f"{BASE}/api/sla/sla/summary", headers=auth).status_code == 403)
check("other tickets are hidden", http.get(f"{BASE}/api/tickets/tickets/999999", headers=auth).status_code == 404)

detail = http.get(f"{BASE}/api/tickets/tickets/{tid}", headers=auth).json()
check("ticket detail has comments", len(detail["comments"]) == 1 and "events" not in detail)

kb = http.get(f"{BASE}/api/chat/kb", headers=auth, params={"q": "vpn"}).json()
check("knowledge base search", any("VPN" in a["title"] for a in kb))
reply = http.post(f"{BASE}/api/chat/chat", headers=auth, json={"message": "my vpn will not connect"}).json()
check("help assistant answers with sources", bool(reply["reply"]) and len(reply["sources"]) > 0)

notes = http.get(f"{BASE}/api/notifications/notifications", headers=auth)
check("notifications endpoint", notes.status_code == 200)

print(f"\nAll {passed} checks passed")
