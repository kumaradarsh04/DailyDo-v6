"""
Untangle backend
------------------
A tiny Flask server that takes raw brain-dump text and asks the Gemini API
to turn it into a hierarchical task list (categories + sub-tasks). User
accounts, usage, and premium status now live in Supabase Postgres — see
db.py for that layer.

Payments use a Razorpay Payment Link you create once in the Razorpay
dashboard (no code needed for that part — see DEPLOYMENT.md). This backend
only needs to verify the webhook Razorpay sends after that link gets paid.

Setup
-----
    pip install -r requirements.txt

    export GEMINI_API_KEY="your-gemini-key"
    export DATABASE_URL="postgresql://...supabase connection string..."
    # OR, instead of DATABASE_URL, the five separate pieces also work:
    #   SUPABASE_DB_HOST, SUPABASE_DB_PORT, SUPABASE_DB_NAME,
    #   SUPABASE_DB_USER, SUPABASE_DB_PASSWORD
    export RAZORPAY_WEBHOOK_SECRET="..."   # the secret you set when creating the webhook
    export ADMIN_KEY="pick-any-secret-string-only-you-know"  # lets you manually unlock a user if the webhook misfires

Run
---
    python backend.py

The server listens on http://localhost:5000 and exposes:

    POST /organize
        body: {"text": "raw brain dump ...", "email": "user@example.com"}
        returns: {"nodes": [...]}  OR  402 + {"error": "free_limit_reached"} once a
        free (non-premium) email has used its FREE_WEEKLY_LIMIT (see db.py)

    GET /status?email=user@example.com
        returns: {"is_premium": bool, "attempts": int, "weekly_attempts": int,
                   "weekly_remaining": int|null, "end_date": "YYYY-MM-DD"|null, "days_left": int|null}
        The frontend calls this to show plan info without needing to attempt
        an organize first.

    POST /webhook
        Razorpay calls this automatically after your shared Payment Link is paid.
        This is what actually marks an email as premium — never trust the
        frontend for this.

    POST /admin/grant-premium
        Manual override — use this to unlock someone by hand if the webhook
        didn't fire for some reason. Requires header X-Admin-Key: <ADMIN_KEY>.
        body: {"email": "user@example.com"}

    GET /health
        liveness + config check (does NOT touch the database)

See DEPLOYMENT.md for setting up Supabase, the Payment Link, and the webhook.
"""

import hashlib
import hmac
import json
import os
import re

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

import db

app = Flask(__name__)
CORS(app)  # allow index.html (opened as a local file or served separately) to call this

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

db.init_pool()
db.init_db()

# Pick whichever current Gemini model fits your budget/latency needs.
# "gemini-flash-latest" is an alias Google keeps pointed at their current
# recommended flash-tier model, so you're less likely to get caught out by
# a model being retired (which is what happened with a pinned version before).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

PROMPT_TEMPLATE = """Convert this messy brain dump into a well-organized hierarchical task list. Group related tasks under short category headings when it makes sense (2-6 word headings), and break down vague or large tasks into 2-4 concrete sub-steps only when genuinely useful. Keep item text short and action-oriented. Do not invent unrelated tasks.

Respond with ONLY a raw JSON array, no markdown fences, no commentary, in exactly this shape:
[{{"text": "Category or task", "children": [{{"text": "sub task", "children": []}}]}}]

If a top-level item has no natural sub-items, use an empty children array.

Brain dump:
\"\"\"
{dump}
\"\"\"
"""


def strip_code_fences(text: str) -> str:
    """Gemini sometimes wraps JSON in ```json ... ``` fences — strip them if present."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def normalize_nodes(nodes):
    """Recursively validate/clean the shape returned by the model."""
    clean = []
    if not isinstance(nodes, list):
        return clean
    for item in nodes:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        children = normalize_nodes(item.get("children", []))
        clean.append({"text": text, "children": children})
    return clean


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "model": GEMINI_MODEL,
        "key_configured": bool(GEMINI_API_KEY),
        "db_configured": bool(db.DATABASE_URL),
    })


@app.route("/status", methods=["GET"])
def status():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "No email provided"}), 400
    try:
        return jsonify(db.get_status(email))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/organize", methods=["POST"])
def organize():
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY is not set on the server"}), 500

    payload = request.get_json(silent=True) or {}
    dump_text = (payload.get("text") or "").strip()
    email = (payload.get("email") or "").strip().lower()

    if not dump_text:
        return jsonify({"error": "No text provided"}), 400
    if not email:
        return jsonify({"error": "No email provided"}), 400

    try:
        if not db.is_allowed_to_organize(email):
            return jsonify({
                "error": "free_limit_reached",
                "message": f"You've used all {db.FREE_WEEKLY_LIMIT} free organizes for this week.",
            }), 402
    except Exception as e:
        return jsonify({"error": f"Database error: {e}"}), 500

    prompt = PROMPT_TEMPLATE.format(dump=dump_text)

    request_body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = requests.post(
            GEMINI_URL,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=request_body,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        candidates = data.get("candidates", [])
        if not candidates:
            return jsonify({"error": "Gemini returned no candidates", "raw": data}), 502

        parts = candidates[0].get("content", {}).get("parts", [])
        raw_text = "".join(p.get("text", "") for p in parts)
        raw_text = strip_code_fences(raw_text)

        parsed = json.loads(raw_text)
        nodes = normalize_nodes(parsed)

        # Only count it as a used attempt once we know it actually worked —
        # a failed Gemini call shouldn't cost the user part of their quota.
        db.record_attempt(email)

        return jsonify({"nodes": nodes})

    except requests.exceptions.HTTPError as e:
        return jsonify({"error": f"Gemini API error: {e}", "details": response.text}), 502
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Could not parse Gemini's response as JSON: {e}", "raw": raw_text}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Request to Gemini failed: {e}"}), 502


@app.route("/admin/grant-premium", methods=["POST"])
def admin_grant_premium():
    """Manual escape hatch: unlock a user by hand if the Razorpay webhook
    didn't fire (e.g. wrong mode, misconfigured URL, etc). Protects itself
    with a simple shared-secret header — not fancy, but enough for an MVP
    with one operator (you)."""
    if not ADMIN_KEY:
        return jsonify({"error": "ADMIN_KEY is not set on the server"}), 500

    provided_key = request.headers.get("X-Admin-Key", "")
    if not hmac.compare_digest(provided_key, ADMIN_KEY):
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "No email provided"}), 400

    db.activate_premium(email)
    return jsonify({"status": "ok", "email": email, "premium": True})


@app.route("/webhook", methods=["POST"])
def razorpay_webhook():
    """Razorpay calls this automatically after your shared Payment Link is
    paid. This is what actually unlocks unlimited use — never trust the
    frontend alone for this, since anyone could fake a browser event."""
    payload = request.data
    signature = request.headers.get("X-Razorpay-Signature", "")

    expected_signature = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        return jsonify({"error": "Invalid webhook signature"}), 400

    event = json.loads(payload)
    event_type = event.get("event", "")

    # Triggered when your Payment Link is paid. The customer's own email —
    # the one they typed into Razorpay's checkout page — comes back here.
    if event_type == "payment_link.paid":
        payment_entity = (
            event.get("payload", {}).get("payment", {}).get("entity", {})
        )
        email = (payment_entity.get("email") or "").strip().lower()
        if email:
            db.activate_premium(email)

    return jsonify({"received": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
