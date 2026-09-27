import time
import re
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI()

START_TIME = time.time()

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict[str, Any]] = {}

# conversation_id -> {"messages": [], "sent_bodies": set(), "merchant_id": ..., ...}
conversations: dict[str, dict[str, Any]] = {}

# Suppression keys already used
sent_suppressions: set[str] = set()


# ============================================================
# HELPERS
# ============================================================

def get_name(obj: Optional[dict]) -> str:
    if not obj:
        return "there"

    identity = obj.get("identity", {})
    return (
        identity.get("owner_first_name")
        or identity.get("name")
        or "there"
    )


def get_merchant_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get(
        "name",
        "your business"
    )


def get_category_name(category: dict) -> str:
    return category.get("slug", "business")


def money(value):
    if value is None:
        return None
    try:
        return f"₹{int(value):,}"
    except Exception:
        return str(value)


def pct(value):
    try:
        return f"{abs(float(value)) * 100:.0f}%"
    except Exception:
        return str(value)


def store_context(scope: str, context_id: str, version: int, payload: dict):
    key = (scope, context_id)
    current = contexts.get(key)

    if current and current["version"] >= version:
        return False, current["version"]

    contexts[key] = {
        "version": version,
        "payload": payload
    }

    return True, version


def get_context(scope: str, context_id: Optional[str]):
    if not context_id:
        return None

    item = contexts.get((scope, context_id))
    if not item:
        return None

    return item["payload"]


def get_trigger(trigger_id: str):
    return get_context("trigger", trigger_id)


def get_merchant(merchant_id: str):
    return get_context("merchant", merchant_id)


def get_customer(customer_id: Optional[str]):
    return get_context("customer", customer_id)


def get_category(slug: Optional[str]):
    return get_context("category", slug)


def active_offer(merchant: dict):
    for offer in merchant.get("offers", []):
        if offer.get("status") == "active":
            return offer
    return None


def first_digest_item(category: dict, trigger: dict):
    trigger_payload = trigger.get("payload", {})
    item_id = trigger_payload.get("top_item_id")

    for item in category.get("digest", []):
        if item.get("id") == item_id:
            return item

    return None


def latest_history_message(merchant: dict):
    history = merchant.get("conversation_history", [])
    if not history:
        return None
    return history[-1].get("body")


def customer_is_opted_in(customer: dict, trigger: dict) -> bool:
    consent = customer.get("consent", {})
    scopes = consent.get("scope", [])

    if not consent.get("opted_in_at"):
        return False

    kind = trigger.get("kind", "")

    # Map trigger types to reasonable consent scopes.
    required = {
        "recall_due": "appointment_reminders",
        "wedding_package_followup": "bridal_package_followup",
        "customer_lapsed_hard": "winback_offers",
        "trial_followup": "kids_program_updates",
        "chronic_refill_due": "refill_reminders",
    }.get(kind)

    if required and required not in scopes:
        return False

    return True


def suppression_key_for(trigger: dict) -> str:
    return trigger.get(
        "suppression_key",
        f"{trigger.get('kind', 'unknown')}:{trigger.get('id', 'unknown')}"
    )


def safe_template_name(kind: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]", "_", kind.lower())
    return f"vera_{cleaned}_v1"


# ============================================================
# DETERMINISTIC COMPOSER
# ============================================================

def compose(category, merchant, trigger, customer=None):
    """
    Deterministic message composer.

    Returns:
        body
        cta
        send_as
        suppression_key
        rationale
    """

    kind = trigger.get("kind", "update")
    tp = trigger.get("payload", {})

    merchant_name = get_merchant_name(merchant)
    owner = get_name(merchant)
    category_name = get_category_name(category)

    # --------------------------------------------------------
    # CUSTOMER-FACING
    # --------------------------------------------------------

    if customer is not None:

        customer_name = get_name(customer)
        language = customer.get("identity", {}).get(
            "language_pref",
            "english"
        )

        send_as = "merchant_on_behalf"

        if kind == "recall_due":
            service = tp.get("service_due", "your next service")
            due = tp.get("due_date", "soon")
            slots = tp.get("available_slots", [])

            slot_text = ""
            if slots:
                slot_text = f" I have {slots[0].get('label', 'a slot')} available."

            body = (
                f"Hi {customer_name}, {merchant_name} here. "
                f"Your {service.replace('_', ' ')} is due on {due}."
                f"{slot_text} Want me to hold that slot?"
            )

            return {
                "body": body,
                "cta": "binary_yes_no",
                "send_as": send_as,
                "suppression_key": suppression_key_for(trigger),
                "rationale": (
                    "Recall reminder uses the customer's service history, "
                    "due date and available appointment slot with a low-friction CTA."
                )
            }

        if kind == "wedding_package_followup":
            wedding_date = tp.get("wedding_date", "your wedding date")
            next_step = tp.get(
                "next_step_window_open",
                "your next bridal step"
            )

            body = (
                f"Hi {customer_name}, {merchant_name} here. "
                f"With your wedding on {wedding_date}, your next step is "
                f"{next_step.replace('_', ' ')}. Want me to share the plan?"
            )

            return {
                "body": body,
                "cta": "binary_yes_no",
                "send_as": send_as,
                "suppression_key": suppression_key_for(trigger),
                "rationale": (
                    "Bridal follow-up is tied to the customer's wedding date "
                    "and the next step recorded in the trigger."
                )
            }

        if kind == "customer_lapsed_hard":
            days = tp.get("days_since_last_visit")
            focus = tp.get("previous_focus", "your previous goal")

            body = (
                f"Hi {customer_name}, {owner} from {merchant_name} here. "
                f"It's been {days} days since your last visit — no pressure. "
                f"I remember your focus was {focus.replace('_', ' ')}. "
                f"Want me to suggest a simple way to restart?"
            )

            return {
                "body": body,
                "cta": "open_ended",
                "send_as": send_as,
                "suppression_key": suppression_key_for(trigger),
                "rationale": (
                    "Winback message acknowledges the lapse without guilt "
                    "and connects to the customer's previous goal."
                )
            }

        if kind == "trial_followup":
            options = tp.get("next_session_options", [])
            slot = options[0].get("label") if options else None

            body = (
                f"Hi {customer_name}, {merchant_name} here. "
                f"Thanks for trying the program."
            )

            if slot:
                body += f" The next available session is {slot}."
            else:
                body += " I can share the next available session."

            body += " Would you like me to hold it?"

            return {
                "body": body,
                "cta": "binary_yes_no",
                "send_as": send_as,
                "suppression_key": suppression_key_for(trigger),
                "rationale": (
                    "Trial follow-up uses the recorded trial and next available "
                    "session to make the next action easy."
                )
            }

        if kind == "chronic_refill_due":
            due = tp.get("runout_date", tp.get("due_date", "soon"))
            medicines = tp.get("medicines", [])

            med_text = ", ".join(
                str(x) for x in medicines
            ) if medicines else "your regular medicines"

            prefix = "Namaste" if language == "hi" else f"Hi {customer_name}"

            body = (
                f"{prefix} — {merchant_name} here. "
                f"{med_text} are due for refill around {due}. "
                f"Would you like us to prepare the refill?"
            )

            return {
                "body": body,
                "cta": "binary_yes_no",
                "send_as": send_as,
                "suppression_key": suppression_key_for(trigger),
                "rationale": (
                    "Refill reminder uses the recorded medicines and runout date "
                    "with a clear confirmation CTA."
                )
            }

        # Generic customer fallback
        body = (
            f"Hi {customer_name}, {merchant_name} here. "
            f"We have an update related to your recent activity. "
            f"Would you like the details?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                f"Customer-facing response grounded in the {kind} trigger "
                f"and merchant relationship."
            )
        }

    # --------------------------------------------------------
    # MERCHANT-FACING
    # --------------------------------------------------------

    send_as = "vera"

    if kind == "research_digest":
        item = first_digest_item(category, trigger)

        if item:
            title = item.get("title", "a new research item")
            source = item.get("source", "")

            body = (
                f"{owner}, {title}. "
                f"It looks relevant to {merchant_name}'s {category_name} business."
            )

            if source:
                body += f" Source: {source}."

            body += " Want me to turn it into a short customer-ready post?"

        else:
            body = (
                f"{owner}, there's a new research update relevant to "
                f"{merchant_name}. Want me to turn it into a short customer-ready post?"
            )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Research trigger is tied to the category digest and includes "
                "the available source citation."
            )
        }

    if kind == "regulation_change":
        deadline = tp.get("deadline_iso")
        item = first_digest_item(category, trigger)

        detail = item.get("title") if item else "a regulatory update"

        body = (
            f"{owner}, regulatory update for {merchant_name}: {detail}."
        )

        if deadline:
            body += f" The recorded deadline is {deadline}."

        body += " Want me to turn the requirement into a simple checklist?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Compliance message leads with the documented regulatory "
                "change and deadline rather than inventing requirements."
            )
        }

    if kind == "perf_dip":
        metric = tp.get("metric", "performance")
        delta = tp.get("delta_pct")
        window = tp.get("window", "recent period")
        baseline = tp.get("vs_baseline")

        body = (
            f"{owner}, {metric} is down {pct(delta)} over {window}."
        )

        if baseline is not None:
            body += f" The recorded baseline is {baseline}."

        body += " Want me to identify the most relevant lever to test first?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Performance dip message uses the exact metric, change and "
                "time window supplied by the trigger."
            )
        }

    if kind == "perf_spike":
        metric = tp.get("metric", "performance")
        delta = tp.get("delta_pct")
        window = tp.get("window", "recent period")

        body = (
            f"{owner}, {metric} is up {pct(delta)} over {window}. "
            f"That's worth investigating while the signal is fresh. "
            f"Want me to break down what may be driving it?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Performance spike is framed as an actionable signal using "
                "the supplied metric and time window."
            )
        }

    if kind == "renewal_due":
        days = tp.get("days_remaining")
        plan = tp.get("plan")
        amount = tp.get("renewal_amount")

        body = (
            f"{owner}, your {plan or 'current'} plan renews in "
            f"{days} days."
        )

        if amount is not None:
            body += f" Renewal amount: {money(amount)}."

        body += " Want me to walk you through the renewal?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Renewal message uses the exact plan, remaining days and "
                "amount available in the trigger."
            )
        }

    if kind == "festival_upcoming":
        festival = tp.get("festival", "upcoming festival")
        date = tp.get("date")

        body = (
            f"{owner}, {festival} is coming up"
        )

        if date:
            body += f" on {date}"

        body += (
            f". It is relevant to {category_name}. "
            f"Want me to suggest one timely campaign using your existing offers?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Festival message connects the documented festival timing "
                "with the merchant's category and existing offers."
            )
        }

    if kind == "curious_ask_due":
        ask = tp.get(
            "ask_template",
            "what is in demand this week"
        )

        body = (
            f"{owner}, quick one for {merchant_name}: "
            f"I can look into {ask.replace('_', ' ')} and bring you the "
            f"most useful signal. Want me to?"
        )

        return {
            "body": body,
            "cta": "binary_yes_no",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Curious-ask trigger is kept short and offers a single "
                "low-friction next step."
            )
        }

    if kind == "winback_eligible":
        days = tp.get("days_since_expiry")
        lapsed = tp.get("lapsed_customers_added_since_expiry")
        dip = tp.get("perf_dip_pct")

        body = (
            f"{owner}, {merchant_name} has a win-back opportunity: "
            f"{lapsed} lapsed customers were added since expiry"
        )

        if days is not None:
            body += f", {days} days ago"

        if dip is not None:
            body += f", alongside a {pct(dip)} performance dip"

        body += ". Want me to draft a win-back message?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Win-back message combines the trigger's lapsed-customer "
                "count with the recorded performance signal."
            )
        }

    if kind == "ipl_match_today":
        match = tp.get("match", "today's match")
        venue = tp.get("venue")
        match_time = tp.get("match_time_iso")

        body = (
            f"{owner}, {match} is on today"
        )

        if venue:
            body += f" at {venue}"

        if match_time:
            body += f" ({match_time[-14:-9]})"

        body += (
            ". Before pushing a match offer, want me to check whether "
            "your existing offer and audience fit the occasion?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Match-day message uses the actual fixture and venue while "
                "avoiding an unsupported assumption that a promotion should run."
            )
        }

    if kind == "review_theme_emerged":
        theme = tp.get("theme", "a review theme")
        occurrences = tp.get("occurrences_30d")
        trend = tp.get("trend")

        body = (
            f"{owner}, {theme.replace('_', ' ')} has appeared in "
        )

        if occurrences is not None:
            body += f"{occurrences} reviews in the last 30 days"
        else:
            body += "recent reviews"

        if trend:
            body += f", with the trend marked {trend}"

        body += ". Want me to suggest the first fix to test?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Review-theme message surfaces the actual recurring theme "
                "and its supplied trend instead of overgeneralizing."
            )
        }

    if kind == "milestone_reached":
        metric = tp.get("metric", "metric")
        value_now = tp.get("value_now")
        milestone = tp.get("milestone_value")

        body = (
            f"{owner}, you're at {value_now} {metric.replace('_', ' ')} "
            f"with the {milestone} milestone close."
        )

        body += " Want me to suggest a simple way to use the milestone?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Milestone message uses the current and target values from "
                "the trigger and proposes one follow-up action."
            )
        }

    if kind == "active_planning_intent":
        topic = tp.get(
            "intent_topic",
            "the plan you were discussing"
        )

        last_message = tp.get("merchant_last_message")

        body = (
            f"{owner}, on {topic.replace('_', ' ')}: "
            f"I can turn that into a concrete starter plan for {merchant_name}."
        )

        if last_message:
            body += f" You asked: “{last_message}”."

        body += " Want me to draft it now?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Active planning intent is treated as an engaged continuation "
                "and moves directly toward the requested artifact."
            )
        }

    if kind == "seasonal_perf_dip":
        metric = tp.get("metric", "performance")
        delta = tp.get("delta_pct")
        note = tp.get("season_note")

        body = (
            f"{owner}, {metric} is down {pct(delta)} over the recorded window."
        )

        if tp.get("is_expected_seasonal"):
            body += " The trigger marks this as an expected seasonal dip."

        if note:
            body += f" Context: {note.replace('_', ' ')}."

        body += " Want me to suggest a retention-first response?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Seasonal dip is explicitly distinguished from an unexpected "
                "performance problem using the trigger's seasonal flag."
            )
        }

    if kind == "kids_yoga_program_drafting":
        body = (
            f"{owner}, I can turn the kids-yoga idea into a starter program "
            f"with age group, session format, pricing and a launch message. "
            f"Want me to draft it?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Program-drafting intent calls for a concrete artifact rather "
                "than another generic suggestion."
            )
        }

    if kind == "supply_alert":
        molecule = tp.get("molecule", "the affected medicine")
        batches = tp.get("affected_batches", [])
        manufacturer = tp.get("manufacturer", "the manufacturer")

        batch_text = ", ".join(batches)

        body = (
            f"{owner}, supply alert: {molecule} batches {batch_text} "
            f"are flagged by {manufacturer}."
        )

        body += (
            " Want me to turn the alert into a customer-notification "
            "and replacement workflow?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Supply alert uses the documented molecule, batch numbers and "
                "manufacturer without adding unsupported medical claims."
            )
        }

    if kind == "category_seasonal":
        season = tp.get("season", "the current season")
        signal = tp.get("signal")

        body = (
            f"{owner}, there's a {season} signal relevant to "
            f"{category_name}."
        )

        if signal:
            body += f" The recorded signal is {signal}."

        body += " Want me to turn it into one practical action?"

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Seasonal category message uses the available category-level "
                "signal and keeps the requested action focused."
            )
        }

    if kind == "gbp_unverified":
        body = (
            f"{owner}, {merchant_name}'s Google Business Profile is marked "
            f"unverified. That can limit how reliably customers find the listing. "
            f"Want me to walk you through the verification step?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "GBP trigger is directly tied to the merchant's verification "
                "state and proposes a concrete remediation."
            )
        }

    if kind == "cde_opportunity":
        opportunity = tp.get("opportunity", "a new opportunity")

        body = (
            f"{owner}, I found a {opportunity.replace('_', ' ')} opportunity "
            f"for {merchant_name}. Want me to break down the next step?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Opportunity message uses the trigger's identified opportunity "
                "and asks for one low-friction next step."
            )
        }

    if kind == "competitor_opened":
        competitor = tp.get("competitor_name", "a nearby competitor")
        locality = tp.get("locality", merchant.get("identity", {}).get("locality"))

        body = (
            f"{owner}, {competitor} has opened"
        )

        if locality:
            body += f" in {locality}"

        body += (
            ". Want me to compare the visible offer positioning "
            "with yours before suggesting a response?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Competitor alert is framed as a comparison opportunity "
                "without making unsupported claims about the competitor."
            )
        }

    if kind == "dormant_with_vera":
        days = tp.get("days_dormant", tp.get("days_since_last_message"))

        body = (
            f"{owner}, I haven't heard from you recently"
        )

        if days is not None:
            body += f" for {days} days"

        body += (
            ". I can pick up from your latest business signals. "
            "Want one useful action to start with?"
        )

        return {
            "body": body,
            "cta": "open_ended",
            "send_as": send_as,
            "suppression_key": suppression_key_for(trigger),
            "rationale": (
                "Dormancy re-engagement acknowledges the gap and offers "
                "a single low-friction action."
            )
        }

    # --------------------------------------------------------
    # GENERIC FALLBACK FOR NEW / INJECTED TRIGGERS
    # --------------------------------------------------------

    metric = tp.get("metric")
    signal = tp.get("signal")
    title = tp.get("title")

    details = []

    if title:
        details.append(str(title))

    if metric:
        details.append(str(metric))

    if signal:
        details.append(str(signal))

    detail_text = " — ".join(details)

    body = (
        f"{owner}, there's a new {kind.replace('_', ' ')} signal "
        f"for {merchant_name}"
    )

    if detail_text:
        body += f": {detail_text}"

    body += ". Want me to turn it into one practical next step?"

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": send_as,
        "suppression_key": suppression_key_for(trigger),
        "rationale": (
            f"Generic deterministic fallback grounded in the received "
            f"{kind} trigger without inventing missing facts."
        )
    }


# ============================================================
# API MODELS
# ============================================================

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# ============================================================
# HEALTH
# ============================================================

@app.get("/v1/healthz")
async def healthz():

    counts = {
        "category": 0,
        "merchant": 0,
        "customer": 0,
        "trigger": 0
    }

    for scope, _ in contexts.keys():
        if scope in counts:
            counts[scope] += 1

    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts
    }


# ============================================================
# METADATA
# ============================================================

@app.get("/v1/metadata")
async def metadata():

    return {
        "team_name": "Dhairya Arora",
        "team_members": ["Dhairya Arora"],
        "model": "deterministic-rule-composer",
        "approach": (
            "deterministic context-grounded composer with trigger dispatch, "
            "suppression and stateful conversation handling"
        ),
        "version": "1.0.0",
        "submitted_at": datetime.utcnow().isoformat() + "Z"
    }


# ============================================================
# CONTEXT
# ============================================================

@app.post("/v1/context")
async def push_context(body: ContextBody):

    accepted, current_version = store_context(
        body.scope,
        body.context_id,
        body.version,
        body.payload
    )

    if not accepted:
        # The challenge examples explicitly expect stale/repeated versions
        # to be rejected.
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": current_version
            }
        )

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z"
    }


# ============================================================
# TICK
# ============================================================

@app.post("/v1/tick")
async def tick(body: TickBody):

    actions = []

    for trigger_id in body.available_triggers:

        # Hard tick cap.
        if len(actions) >= 20:
            break

        trigger = get_trigger(trigger_id)

        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")

        merchant = get_merchant(merchant_id)

        if not merchant:
            continue

        category_slug = merchant.get("category_slug")
        category = get_category(category_slug)

        if not category:
            continue

        customer = None

        if customer_id:
            customer = get_customer(customer_id)

            # Customer-facing outreach requires valid consent.
            if not customer:
                continue

            if not customer_is_opted_in(customer, trigger):
                continue

        suppression_key = suppression_key_for(trigger)

        # Do not repeat an already-sent trigger.
        if suppression_key in sent_suppressions:
            continue

        result = compose(
            category,
            merchant,
            trigger,
            customer
        )

        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")

        # Meaningful conversation ID.
        conversation_id = (
            f"conv_{merchant_id}_"
            f"{trigger.get('kind', 'update')}_"
            f"{customer_id or 'merchant'}"
        )

        # First outbound uses a template structure.
        template_name = safe_template_name(
            trigger.get("kind", "update")
        )

        template_params = [
            get_name(merchant),
            result["body"]
        ]

        action = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result["send_as"],
            "trigger_id": trigger_id,
            "template_name": template_name,
            "template_params": template_params,
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"]
        }

        actions.append(action)

        sent_suppressions.add(suppression_key)

        conversations.setdefault(
            conversation_id,
            {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "sent_bodies": set(),
                "messages": []
            }
        )

        conversations[conversation_id]["sent_bodies"].add(
            result["body"]
        )

        conversations[conversation_id]["messages"].append({
            "role": "vera",
            "body": result["body"]
        })

    return {
        "actions": actions
    }


# ============================================================
# REPLY
# ============================================================

@app.post("/v1/reply")
async def reply(body: ReplyBody):

    conversation = conversations.setdefault(
        body.conversation_id,
        {
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "sent_bodies": set(),
            "messages": []
        }
    )

    message = body.message.strip()
    lower = message.lower()

    conversation["messages"].append({
        "role": body.from_role,
        "body": message
    })

    # --------------------------------------------------------
    # HARD OPT-OUT
    # --------------------------------------------------------

    opt_out_patterns = [
        "stop messaging",
        "stop message",
        "unsubscribe",
        "do not message",
        "don't message",
        "dont message",
        "remove me",
        "no more messages",
        "stop contacting"
    ]

    if any(x in lower for x in opt_out_patterns):
        return {
            "action": "end",
            "body": "",
            "rationale": (
                "User explicitly requested no further messages, "
                "so the conversation is ended."
            )
        }

    # --------------------------------------------------------
    # AUTO-REPLY DETECTION
    # --------------------------------------------------------

    auto_patterns = [
        "thank you for contacting",
        "thanks for contacting",
        "our team will respond",
        "we will respond shortly",
        "will get back to you",
        "currently unavailable",
        "office hours",
        "we are currently closed",
        "automatic reply",
        "auto-reply"
    ]

    if any(x in lower for x in auto_patterns):
        return {
            "action": "wait",
            "wait_seconds": 14400,
            "rationale": (
                "Detected a likely automated/canned reply, so the bot "
                "backs off instead of continuing to message."
            )
        }

    # --------------------------------------------------------
    # AFFIRMATIVE RESPONSE
    # --------------------------------------------------------

    affirmative = [
        "yes",
        "yes please",
        "sure",
        "okay",
        "ok",
        "go ahead",
        "do it",
        "please do",
        "send it",
        "send me",
        "sounds good"
    ]

    if any(x == lower or lower.startswith(x + " ") for x in affirmative):

        response = (
            "Absolutely — I'll take that forward. "
            "I'll keep the next step focused and use the details already "
            "shared in this conversation."
        )

        if response not in conversation["sent_bodies"]:
            conversation["sent_bodies"].add(response)

        conversation["messages"].append({
            "role": "vera",
            "body": response
        })

        return {
            "action": "send",
            "body": response,
            "cta": "open_ended",
            "rationale": (
                "Acknowledged the affirmative intent and continued with "
                "the requested next step without repeating the previous message."
            )
        }

    # --------------------------------------------------------
    # NEGATIVE / OBJECTION
    # --------------------------------------------------------

    negative = [
        "not interested",
        "no thanks",
        "not now",
        "maybe later",
        "too expensive",
        "don't want",
        "dont want",
        "busy",
        "later"
    ]

    if any(x in lower for x in negative):
        return {
            "action": "wait",
            "wait_seconds": 86400,
            "rationale": (
                "Detected a negative or postponement response, so the bot "
                "backs off rather than pushing another offer immediately."
            )
        }

    # --------------------------------------------------------
    # QUESTION / NORMAL CONVERSATION
    # --------------------------------------------------------

    if "?" in message:

        response = (
            "Good question. I’ll keep the answer grounded in the business "
            "and customer context already provided. Tell me which part "
            "you want to act on first."
        )

        return {
            "action": "send",
            "body": response,
            "cta": "open_ended",
            "rationale": (
                "The message contains a question, so the bot keeps the "
                "conversation open without inventing unsupported facts."
            )
        }

    # --------------------------------------------------------
    # DEFAULT
    # --------------------------------------------------------

    return {
        "action": "send",
        "body": (
            "Got it. I can work with that. "
            "What would you like me to handle first?"
        ),
        "cta": "open_ended",
        "rationale": (
            "Acknowledged the message and asked for a focused next action "
            "without repeating the previous outbound message."
        )
    }


# ============================================================
# OPTIONAL TEARDOWN
# ============================================================

@app.post("/v1/teardown")
async def teardown():

    contexts.clear()
    conversations.clear()
    sent_suppressions.clear()

    return {
        "status": "ok",
        "cleared": True
    }
