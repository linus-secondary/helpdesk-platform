"""Claude on Amazon Bedrock, with an offline fallback.

Uses the Anthropic Python SDK's Bedrock client (AnthropicBedrockMantle), which signs requests with
your normal AWS credentials (aws configure locally, IAM Roles for Service Accounts on EKS).

BEDROCK_ENABLED=false (default for local development): functions return None and callers use
their rule-based fallback, so the platform runs for free without AWS.
"""
import json
import os

from common import log

BEDROCK_ENABLED = os.environ.get("BEDROCK_ENABLED", "false").lower() == "true"
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-opus-5-5")

_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import AnthropicBedrockMantle

        _client = AnthropicBedrockMantle(aws_region=BEDROCK_REGION)
    return _client


def _create(**kwargs):
    """Calls Bedrock; returns the response or None on any API failure (callers fall back)."""
    if not BEDROCK_ENABLED:
        return None
    import anthropic

    try:
        response = _get_client().messages.create(model=BEDROCK_MODEL_ID, **kwargs)
    except anthropic.RateLimitError:
        log.warning("bedrock throttled the request")
        return None
    except anthropic.APIStatusError as exc:
        log.error(f"bedrock error {exc.status_code}: {exc.message}")
        return None
    except anthropic.APIConnectionError as exc:
        log.error(f"bedrock unreachable: {exc}")
        return None
    if response.stop_reason == "refusal":
        log.warning("bedrock declined the request")
        return None
    return response


def _text(response) -> str:
    return "".join(block.text for block in response.content if block.type == "text").strip()


def ask_claude(system: str, messages: list[dict], max_tokens: int = 1024) -> str | None:
    response = _create(system=system, messages=messages, max_tokens=max_tokens,
                       output_config={"effort": "low"})
    return (_text(response) or None) if response else None


def ask_claude_json(system: str, prompt: str, schema: dict, max_tokens: int = 2048) -> dict | None:
    """Structured output: the reply is constrained to the JSON schema."""
    response = _create(system=system, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens,
                       output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}})
    if not response:
        return None
    try:
        return json.loads(_text(response))
    except json.JSONDecodeError:
        log.error("bedrock returned invalid JSON")
        return None
