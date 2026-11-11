"""One image, many services: SERVICE=<name> picks which microservice this container runs."""
import importlib
import os

SERVICES = {
    "gateway": "gateway",
    "tickets": "tickets_service",
    "sla": "sla_service",
    "notifications": "notifications_service",
    "chatbot": "chatbot_service",
}

name = os.environ.get("SERVICE", "gateway")
if name not in SERVICES:
    raise SystemExit(f"Unknown SERVICE '{name}'. Choose one of: {', '.join(SERVICES)}")
app = importlib.import_module(SERVICES[name]).app
