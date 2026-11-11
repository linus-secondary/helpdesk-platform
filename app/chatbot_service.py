"""chatbot service: AI ticket triage + self-service help assistant.

1. Triage (internal, called by the tickets service for every new ticket):
   Claude on Bedrock reads the ticket and returns {category, priority, suggestion} as schema-validated
   JSON (structured outputs). Invalid or missing AI output falls back to keyword rules, so triage
   never blocks ticket creation.
2. Help chat (employees): RAG over the knowledge base. Matching KB articles are retrieved with
   PostgreSQL full-text search and Claude answers only from them; if nothing helps, it suggests
   raising a ticket.
"""
from pathlib import Path

from fastapi import Depends
from pydantic import BaseModel, Field

import rules
from common import create_app, current_user, get_pool, log, require_internal
from llm import ask_claude, ask_claude_json

app = create_app("chatbot", schema=Path(__file__).with_name("schema.sql"))

TRIAGE_SYSTEM = f"""You are an IT service desk triage assistant.
Classify the ticket and suggest a first fix the employee can try.
Categories: {", ".join(rules.CATEGORIES)}.
Priorities: P1 = security incident, outage, or many people unable to work; P2 = one person fully
blocked or a deadline at risk; P3 = degraded but a workaround exists; P4 = request or question.
The suggestion must be 2-4 short, safe steps an employee can do without admin rights. Never ask for
passwords. If the ticket describes a possible security incident, the suggestion must tell the user to
disconnect from the network and wait for the security team."""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": rules.CATEGORIES},
        "priority": {"type": "string", "enum": rules.PRIORITIES},
        "suggestion": {"type": "string"},
    },
    "required": ["category", "priority", "suggestion"],
    "additionalProperties": False,
}

HELP_SYSTEM = """You are the IT help assistant for company employees.
Answer only from the KNOWLEDGE BASE articles in the user's message. Give short numbered steps.
If the articles do not cover the problem, say so and tell the user to raise a ticket from the
"New ticket" page. Never ask for or accept passwords or MFA codes."""


class TriageIn(BaseModel):
    title: str = Field(max_length=200)
    description: str = Field(max_length=5000)


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=2000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=10)


def search_kb(text: str, limit: int = 3) -> list[dict]:
    words = ["".join(ch for ch in w if ch.isalnum()) for w in text.split()]
    query = " | ".join(w for w in words if len(w) > 2)
    if not query:
        return []
    with get_pool().connection() as conn:
        return conn.execute(
            """SELECT slug, title, category, body FROM kb_articles
               WHERE search @@ to_tsquery('english', %s)
               ORDER BY ts_rank(search, to_tsquery('english', %s)) DESC LIMIT %s""",
            (query, query, limit)).fetchall()


def format_kb(articles: list[dict]) -> str:
    if not articles:
        return "KNOWLEDGE BASE:\n- (no matching articles)"
    return "KNOWLEDGE BASE:\n" + "\n".join(f"- [{a['title']}] {a['body']}" for a in articles)


def triage(title: str, description: str) -> dict:
    prompt = f"TICKET TITLE: {title}\nTICKET DESCRIPTION: {description}"
    ai = rules.validate_triage(ask_claude_json(TRIAGE_SYSTEM, prompt, TRIAGE_SCHEMA))
    if ai:
        return {**ai, "triaged_by": "ai"}
    result = rules.rule_based_triage(title, description)
    articles = search_kb(f"{title} {description}", limit=1)
    if articles:
        result["suggestion"] = f"While you wait, try: {articles[0]['title']}. {articles[0]['body']}"
    return {**result, "triaged_by": "rules"}


@app.post("/internal/triage", dependencies=[Depends(require_internal)])
def internal_triage(body: TriageIn):
    result = triage(body.title, body.description)
    log.info(f"triaged as {result['category']}/{result['priority']} by {result['triaged_by']}")
    return result


@app.post("/chat")
def chat(body: ChatIn, _: dict = Depends(current_user)):
    articles = search_kb(body.message)
    messages = [{"role": t.role, "content": t.content} for t in body.history]
    messages.append({"role": "user", "content": f"{format_kb(articles)}\n\nQUESTION: {body.message}"})
    reply = ask_claude(HELP_SYSTEM, messages)
    if not reply:
        if articles:
            reply = f"{articles[0]['title']}:\n{articles[0]['body']}"
        else:
            reply = "I couldn't find a help article for that. Please raise a ticket from the New ticket page."
    return {"reply": reply, "sources": [a["title"] for a in articles]}


@app.get("/kb")
def list_articles(q: str = "", _: dict = Depends(current_user)):
    if q:
        return search_kb(q, limit=10)
    with get_pool().connection() as conn:
        return conn.execute("SELECT slug, title, category, body FROM kb_articles ORDER BY category, title").fetchall()
