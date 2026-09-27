import time
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI()

START = time.time()

contexts = {}
conversations = {}


def compose(category, merchant, trigger, customer=None):
    merchant_name = merchant.get("identity", {}).get("name", "your business")
    category_name = category.get("slug", "business")
    kind = trigger.get("kind", "update")

    if customer:
        customer_name = customer.get("identity", {}).get("name", "there")

        body = (
            f"Hi {customer_name}, {merchant_name} has an update "
            f"that may be relevant to you."
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": "merchant_on_behalf",
            "suppression_key": trigger.get(
                "suppression_key",
                f"{kind}:{merchant_name}:{customer_name}"
            ),
            "rationale": (
                f"Customer-facing message based on the {kind} "
                f"trigger for {merchant_name}."
            )
        }

    body = (
        f"Hi {merchant_name}, there's a new {kind} update "
        f"relevant to your {category_name} business."
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": trigger.get(
            "suppression_key",
            f"{kind}:{merchant_name}"
        ),
        "rationale": (
            f"Merchant-facing message based on the {kind} "
            f"trigger and the {category_name} category."
        )
    }


@app.get("/v1/healthz")
async def healthz():
    counts = {
        "category": 0,
        "merchant": 0,
        "customer": 0,
        "trigger": 0
    }

    for scope, _ in contexts:
        counts[scope] = counts.get(scope, 0) + 1

    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Dhairya Arora",
        "team_members": ["Dhairya Arora"],
        "model": "deterministic-composer",
        "approach": "context-grounded deterministic composer",
        "version": "1.0.0"
    }


class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: ContextBody):
    key = (body.scope, body.context_id)

    current = contexts.get(key)

    if current and current["version"] >= body.version:
        return {
            "accepted": False,
            "reason": "stale_version",
            "current_version": current["version"]
        }

    contexts[key] = {
        "version": body.version,
        "payload": body.payload
    }

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z"
    }


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    for trigger_id in body.available_triggers:

        trigger_data = contexts.get(
            ("trigger", trigger_id),
            {}
        ).get("payload")

        if not trigger_data:
            continue

        merchant_id = trigger_data.get("merchant_id")

        if not merchant_id:
            continue

        merchant = contexts.get(
            ("merchant", merchant_id),
            {}
        ).get("payload")

        if not merchant:
            continue

        category_slug = merchant.get("category_slug")

        category = contexts.get(
            ("category", category_slug),
            {}
        ).get("payload")

        if not category:
            continue

        result = compose(
            category,
            merchant,
            trigger_data
        )

        actions.append({
            "conversation_id": f"conv_{merchant_id}_{trigger_id}",
            "merchant_id": merchant_id,
            "customer_id": None,
            "send_as": result["send_as"],
            "trigger_id": trigger_id,
            "template_name": "vera_context_v1",
            "template_params": [],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"]
        })

    return {
        "actions": actions
    }


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):

    conversations.setdefault(
        body.conversation_id,
        []
    ).append({
        "from": body.from_role,
        "message": body.message
    })

    return {
        "action": "send",
        "body": "Got it. How would you like to proceed?",
        "cta": "open_ended",
        "rationale": "Acknowledged the reply and kept the conversation moving."
    }
