import os
import re
import json
import time
import random
import logging
import anthropic
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Bookly Support Agent")
templates = Jinja2Templates(directory="templates")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-6"

# ─── Structured Logging ───────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)
logger = logging.getLogger("bookly")


def log(event_type: str, session_id: str, **kwargs):
    """Emit a structured JSON log line to stdout and store in-memory for /api/events."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "session": session_id,
        **kwargs,
    }
    logger.info(json.dumps(entry))
    EVENTS.setdefault(session_id, []).append(entry)


# ─── Mock Data ────────────────────────────────────────────────────────────────

ORDERS = {
    "BK-4821": {
        "order_id": "BK-4821",
        "customer_email": "sarah.chen@email.com",
        "status": "shipped",
        "items": [
            {"title": "The Great Gatsby", "qty": 1, "price": 12.99},
            {"title": "1984", "qty": 1, "price": 9.99},
        ],
        "tracking_number": "1Z999AA10123456784",
        "carrier": "UPS",
        "estimated_delivery": "April 12, 2026",
        "ordered_date": "April 3, 2026",
        "total": 22.98,
        "return_eligible": True,
    },
    "BK-5102": {
        "order_id": "BK-5102",
        "customer_email": "james.w@email.com",
        "status": "delivered",
        "items": [
            {"title": "Dune", "qty": 1, "price": 15.99},
        ],
        "tracking_number": "1Z999AA10198765432",
        "carrier": "UPS",
        "estimated_delivery": "April 1, 2026",
        "delivered_date": "March 31, 2026",
        "ordered_date": "March 27, 2026",
        "total": 15.99,
        "return_eligible": True,
    },
    "BK-3399": {
        "order_id": "BK-3399",
        "customer_email": "sarah.chen@email.com",
        "status": "processing",
        "items": [
            {"title": "Sapiens", "qty": 1, "price": 14.99},
            {"title": "Educated", "qty": 1, "price": 11.99},
            {"title": "Becoming", "qty": 1, "price": 13.99},
        ],
        "tracking_number": None,
        "carrier": None,
        "estimated_delivery": "April 15-18, 2026",
        "ordered_date": "April 6, 2026",
        "total": 40.97,
        "return_eligible": False,
    },
}

EMAIL_TO_ORDERS = {}
for oid, order in ORDERS.items():
    email = order["customer_email"]
    EMAIL_TO_ORDERS.setdefault(email, []).append(oid)

KNOWLEDGE_BASE = {
    "shipping_policy": "Bookly offers free standard shipping on orders over $25. Standard shipping takes 5-7 business days. Expedited shipping (2-3 business days) is available for $5.99. Express next-day shipping is $12.99. All orders are shipped via UPS or USPS.",
    "return_policy": "Items can be returned within 30 days of delivery for a full refund. Books must be in original condition (unread, no markings, no damage). Return shipping is free for defective items. For non-defective returns, a prepaid return label costs $4.99 which is deducted from the refund. Digital purchases (e-books, audiobooks) are non-refundable.",
    "password_reset": "To reset your password: (1) Go to bookly.com/login, (2) Click 'Forgot Password', (3) Enter the email associated with your account, (4) Check your inbox for a reset link (arrives within 5 minutes), (5) Click the link and create a new password. If you don't receive the email, check your spam folder or contact support.",
    "payment_methods": "Bookly accepts Visa, Mastercard, American Express, Discover, PayPal, Apple Pay, and Google Pay. Gift cards can also be used as payment. We do not accept cash on delivery or bank transfers.",
    "order_cancellation": "Orders can be cancelled within 1 hour of placement if they haven't entered processing. Once an order is in 'processing' or 'shipped' status, it cannot be cancelled — but you can return it after delivery under our return policy.",
}

RETURN_COUNTER = {"next_rma": 7001}

# ─── Tool Definitions ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "lookup_order",
        "description": "Look up an order by order ID (e.g. BK-4821) or customer email address. Returns order status, items, tracking information, and delivery estimates. Use this when a customer asks about their order status, tracking, or delivery.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID (e.g. BK-4821). Provide this OR email, not both.",
                },
                "email": {
                    "type": "string",
                    "description": "Customer email address to look up all orders. Provide this OR order_id, not both.",
                },
            },
        },
    },
    {
        "name": "initiate_return",
        "description": "Initiate a return/refund for an order. Only call this AFTER: (1) looking up the order to confirm it exists and is return-eligible, and (2) confirming the return details with the customer. Returns an RMA number and instructions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID to return",
                },
                "reason": {
                    "type": "string",
                    "description": "The customer's reason for the return",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of item titles to return. If not specified, all items in the order are returned.",
                },
            },
            "required": ["order_id", "reason"],
        },
    },
    {
        "name": "search_knowledge_base",
        "description": "Search Bookly's knowledge base for policy information. Use this for questions about shipping, returns, payment methods, password reset, order cancellation, or any general store policies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The topic to search for (e.g. 'return policy', 'shipping times', 'password reset')",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Escalate the conversation to a live human support agent. Call this when: (1) the customer is frustrated, angry, or upset, (2) the issue is too complex to resolve through standard flows, (3) the customer explicitly requests a human. This creates a support ticket and queues the customer for a live agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief summary of why escalation is needed (e.g. 'customer frustrated with delayed order', 'disputed charge not in system')",
                },
                "priority": {
                    "type": "string",
                    "enum": ["normal", "high"],
                    "description": "Use 'high' if the customer is very upset or has a time-sensitive issue.",
                },
            },
            "required": ["reason"],
        },
    },
]

# ─── System Prompt ────────────────────────────────────────────────────────────
# Each rule is structured as a named AOP (Agent Operating Procedure) —
# a deterministic decision procedure the agent must follow precisely.

SYSTEM_PROMPT = """You are Bookly's customer support assistant. You operate under the following Agent Operating Procedures (AOPs) — follow them precisely.

AOP-1 [GROUNDING]: Only answer using data returned by your tools. Never invent order IDs, tracking numbers, prices, dates, or policies. If the information isn't in a tool result, say so.

AOP-2 [CLARIFICATION]: If a customer asks about an order but has not provided an order ID or email address, ask for one before calling lookup_order. Do not guess or assume.

AOP-3 [RETURN CONFIRMATION]: Before calling initiate_return you must:
   (a) call lookup_order to confirm the order exists and is return-eligible
   (b) tell the customer which items will be returned and ask them to confirm
   (c) call initiate_return only after the customer explicitly says yes

AOP-4 [SCOPE BOUNDARY]: You handle Bookly customer support only. Politely decline unrelated requests (e.g. "write me a poem", "what's the weather") and redirect to what you can help with.

AOP-5 [ESCALATION TRIGGER]: If the customer expresses frustration, anger, or uses language like "unacceptable", "ridiculous", "terrible", "worst", "no one helps", or asks for a human — call escalate_to_human immediately. Do not make them ask twice. Use priority="high" if they seem very upset.

AOP-6 [INVALID INPUT]: If a lookup returns no order found, explain clearly, suggest the customer check their confirmation email, and offer to search by email address instead.

AOP-7 [BRAND VOICE]: Warm, concise, professional. Use the customer's name when known. Customers want answers, not essays — be direct."""

# ─── Guardrails ───────────────────────────────────────────────────────────────

# Pre-flight check for prompt injection — runs before every Claude API call
_INJECTION_PATTERNS = re.compile(
    r"(ignore (previous|prior|all) instructions?|"
    r"disregard (your|all|the) (instructions?|rules?|aops?)|"
    r"you are now|forget (your|all) instructions?|"
    r"override (your )?(instructions?|aops?|rules?)|"
    r"new persona|act as (a |an )?(different|unrestricted)|"
    r"bypass (your )?(safety|guardrails?|filters?)|"
    r"reveal (your )?(system prompt|instructions?))",
    re.IGNORECASE,
)


def check_injection(text: str) -> bool:
    """Return True if the message looks like a prompt injection attempt."""
    return bool(_INJECTION_PATTERNS.search(text))


# ─── Tool Execution ───────────────────────────────────────────────────────────

def normalize_order_id(raw: str) -> str:
    """Normalize messy order IDs: 'bk4821', 'BK 4821', '4821' → 'BK-4821'."""
    raw = raw.strip().upper()
    if re.match(r"^BK-\d+$", raw):
        return raw
    m = re.match(r"^BK[\s\-]?(\d+)$", raw)
    if m:
        return f"BK-{m.group(1)}"
    m = re.match(r"^(\d{3,})$", raw)
    if m:
        return f"BK-{m.group(1)}"
    return raw


def execute_tool(name: str, input_data: dict) -> str:
    if name == "lookup_order":
        raw_id = input_data.get("order_id", "")
        order_id = normalize_order_id(raw_id) if raw_id else ""
        email = input_data.get("email", "").lower()

        if order_id:
            order = ORDERS.get(order_id)
            if order:
                return json.dumps(order, indent=2)
            close = [oid for oid in ORDERS if oid.replace("BK-", "") in order_id or order_id.replace("BK-", "") in oid]
            hint = f" Did you mean {close[0]}?" if close else " Please double-check your confirmation email."
            return json.dumps({"error": f"No order found with ID '{order_id}'.{hint} You can also search by email address."})

        if email:
            order_ids = EMAIL_TO_ORDERS.get(email)
            if order_ids:
                orders = [ORDERS[oid] for oid in order_ids]
                return json.dumps({"email": email, "orders": orders}, indent=2)
            return json.dumps({"error": f"No orders found for email '{email}'. Please verify the email address."})

        return json.dumps({"error": "Please provide either an order_id or email to look up."})

    elif name == "initiate_return":
        order_id = input_data.get("order_id", "").upper()
        reason = input_data.get("reason", "No reason provided")
        items = input_data.get("items")

        order = ORDERS.get(order_id)
        if not order:
            return json.dumps({"error": f"Order '{order_id}' not found."})
        if not order.get("return_eligible"):
            return json.dumps({"error": f"Order '{order_id}' is not eligible for return. It may still be processing or past the return window."})

        rma = f"RMA-{RETURN_COUNTER['next_rma']}"
        RETURN_COUNTER["next_rma"] += 1

        return_items = items if items else [item["title"] for item in order["items"]]
        refund_amount = sum(
            item["price"] * item["qty"]
            for item in order["items"]
            if item["title"] in return_items
        )

        return json.dumps({
            "success": True,
            "rma_number": rma,
            "order_id": order_id,
            "items_returned": return_items,
            "reason": reason,
            "refund_amount": f"${refund_amount:.2f}",
            "instructions": (
                f"Return authorized! Your RMA number is {rma}. "
                f"Please pack the book(s) securely and ship to: Bookly Returns Center, "
                f"123 Book Lane, Portland, OR 97201. Include the RMA number on the outside of the package. "
                f"A prepaid return shipping label has been emailed to {order['customer_email']}. "
                f"Your refund of ${refund_amount:.2f} will be processed within 5-7 business days after we receive the return."
            ),
        })

    elif name == "search_knowledge_base":
        query = input_data.get("query", "").lower()
        results = []
        for topic, content in KNOWLEDGE_BASE.items():
            topic_words = topic.replace("_", " ")
            if any(word in query for word in topic_words.split()) or any(word in topic_words for word in query.split()):
                results.append({"topic": topic_words, "content": content})

        if results:
            return json.dumps({"results": results})
        return json.dumps({"results": [], "message": "No relevant policies found. Available topics: shipping, returns, password reset, payment methods, order cancellation."})

    elif name == "escalate_to_human":
        reason = input_data.get("reason", "Not specified")
        priority = input_data.get("priority", "normal")
        ticket_id = f"TKT-{random.randint(10000, 99999)}"
        wait = "under 2 minutes" if priority == "high" else "3–5 minutes"
        return json.dumps({
            "success": True,
            "ticket_id": ticket_id,
            "priority": priority,
            "estimated_wait": wait,
            "reason": reason,
            "message": f"Escalation successful. Ticket {ticket_id} created ({priority} priority). A human agent will join the chat in approximately {wait}.",
        })

    return json.dumps({"error": f"Unknown tool: {name}"})


# ─── Conversation + Event Storage (in-memory for prototype) ──────────────────

conversations: dict[str, list] = {}
EVENTS: dict[str, list] = {}  # Observability event log per session


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    user_message = body.get("message", "")
    session_id = body.get("session_id", "default")

    if session_id not in conversations:
        conversations[session_id] = []
        log("session_start", session_id)

    # ── Guardrail: prompt injection check ────────────────────────────────────
    if check_injection(user_message):
        log("guardrail_blocked", session_id, reason="prompt_injection", input=user_message[:120])
        return {
            "response": "I'm only able to help with Bookly customer support. How can I assist you with an order, return, or policy question?",
            "tool_calls": [],
            "flagged": True,
        }

    turn = len(conversations[session_id]) // 2 + 1
    log("user_message", session_id, turn=turn, text=user_message[:200])

    conversations[session_id].append({"role": "user", "content": user_message})
    messages = conversations[session_id].copy()

    # ── Agentic tool-use loop ─────────────────────────────────────────────────
    tool_calls_made = []
    request_start = time.perf_counter()
    max_iterations = 5

    for _ in range(max_iterations):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_start = time.perf_counter()
                    result = execute_tool(block.name, block.input)
                    latency_ms = round((time.perf_counter() - tool_start) * 1000)
                    parsed = json.loads(result)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                    # Capture full round-trip for frontend + observability log
                    tool_calls_made.append({
                        "tool": block.name,
                        "input": block.input,
                        "output": parsed,
                        "latency_ms": latency_ms,
                    })
                    log("tool_call", session_id,
                        tool=block.name,
                        input=block.input,
                        success="error" not in parsed,
                        latency_ms=latency_ms)

            messages.append({"role": "user", "content": tool_results})
        else:
            break

    total_ms = round((time.perf_counter() - request_start) * 1000)

    # Extract final text response
    assistant_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            assistant_text += block.text

    # Classify resolution type for observability
    resolution = "resolved"
    if any(tc["tool"] == "escalate_to_human" for tc in tool_calls_made):
        resolution = "escalated"
    elif not tool_calls_made:
        resolution = "deflected" if any(
            phrase in assistant_text.lower()
            for phrase in ["only help with", "can't help with", "outside of what"]
        ) else "resolved_no_tool"

    # Derive which AOP fired — included in the response for observability
    aop_triggered = None
    tool_names = [tc["tool"] for tc in tool_calls_made]
    if "escalate_to_human" in tool_names:
        aop_triggered = "AOP-5"
    elif resolution == "deflected":
        aop_triggered = "AOP-4"
    elif any(tc["tool"] == "lookup_order" and tc["output"].get("error") for tc in tool_calls_made):
        aop_triggered = "AOP-6"
    elif "initiate_return" in tool_names:
        aop_triggered = "AOP-3"
    elif not tool_calls_made and "?" in assistant_text:
        aop_triggered = "AOP-2"
    elif tool_calls_made:
        aop_triggered = "AOP-1"

    log("assistant_response", session_id,
        turn=turn,
        resolution=resolution,
        tools_used=[tc["tool"] for tc in tool_calls_made],
        aop_triggered=aop_triggered,
        total_latency_ms=total_ms)

    conversations[session_id].append({"role": "assistant", "content": assistant_text})

    return {
        "response": assistant_text,
        "tool_calls": tool_calls_made,
        "resolution": resolution,
        "aop_triggered": aop_triggered,
    }


@app.post("/api/reset")
async def reset(request: Request):
    body = await request.json()
    session_id = body.get("session_id", "default")
    conversations.pop(session_id, None)
    EVENTS.pop(session_id, None)
    return {"status": "ok"}


@app.get("/api/events/{session_id}")
async def get_events(session_id: str):
    """Observability endpoint — returns the structured event log for a session."""
    return {"session_id": session_id, "events": EVENTS.get(session_id, [])}
