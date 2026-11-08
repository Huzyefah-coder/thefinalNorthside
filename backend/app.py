import json
import os
import re
import secrets
import postgres_compat as sqlite3
import threading
import time

from datetime import datetime, timedelta, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask, request, jsonify, render_template,
    redirect, url_for, session as flask_session
)

from clinic_backend import ClinicConversation, DB_PATH


load_dotenv()

app = Flask(__name__)

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")

if not DASHBOARD_PASSWORD or not FLASK_SECRET_KEY:
    raise RuntimeError(
        "Set DASHBOARD_PASSWORD and FLASK_SECRET_KEY in .env"
    )

app.secret_key = FLASK_SECRET_KEY

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8)
)

conversations = {}
conversations_lock = threading.RLock()

SESSION_TIMEOUT = 3600


def conversation_owner():
    """Bind an AI conversation to the current login or anonymous browser session."""
    if flask_session.get("site_user_id") is not None:
        return ("user", str(flask_session["site_user_id"]), flask_session.get("site_csrf"))
    if "ai_guest_token" not in flask_session:
        flask_session["ai_guest_token"] = secrets.token_urlsafe(32)
    return ("guest", flask_session["ai_guest_token"])
PKT = timezone(timedelta(hours=5))


def timestamp():
    return datetime.now(PKT).isoformat(timespec="seconds")


def init_session_database():
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS call_sessions (
                session_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                transcript TEXT NOT NULL DEFAULT '[]',
                detected_intent TEXT NOT NULL DEFAULT 'unknown',
                outcome TEXT NOT NULL DEFAULT 'In progress',
                follow_up_status TEXT NOT NULL DEFAULT 'None',
                summary TEXT NOT NULL DEFAULT ''
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS human_handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                phone_number TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL
            )
        """)


init_session_database()


def save_session(session_id, session):
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute("""
            INSERT INTO call_sessions
            (session_id, started_at, ended_at, transcript,
             detected_intent, outcome, follow_up_status, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                ended_at=excluded.ended_at,
                transcript=excluded.transcript,
                detected_intent=excluded.detected_intent,
                outcome=excluded.outcome,
                follow_up_status=excluded.follow_up_status,
                summary=excluded.summary
        """, (
            session_id,
            session["started_at"],
            session.get("ended_at"),
            json.dumps(
                session["transcript"],
                ensure_ascii=False
            ),
            session["intent"],
            session["outcome"],
            session["follow_up_status"],
            session["summary"]
        ))


def summarize(session):
    turns = [
        entry for entry in session["transcript"]
        if entry["role"] == "customer"
    ]

    if not turns:
        return "No customer messages recorded."

    latest = turns[-1]["text"].replace("\n", " ")[:180]

    return (
        f"{len(turns)} customer message(s). "
        f"Last intent: {session['intent']}. "
        f"Outcome: {session['outcome']}. "
        f"Last customer message: {latest}"
    )


def infer_intent(agent, message):
    for item in reversed(agent.history):
        if (
            item.get("customer") == message
            and isinstance(
                item.get("interpretation"), dict
            )
        ):
            intent = item["interpretation"].get(
                "intent", "other"
            )

            return (
                intent
                if isinstance(intent, str)
                else "other"
            )

    if agent.mode in (
        "book", "cancel", "reschedule", "status"
    ):
        return agent.mode

    return "unknown"


def infer_outcome(reply, previous_intent):
    low = reply.lower()

    if (
        "saved successfully" in low
        and "appointment" in low
    ):
        return "Appointment request saved"

    if "has been cancelled" in low:
        return "Appointment cancelled"

    if "has been rescheduled" in low:
        return "Appointment rescheduled"

    if (
        "status:" in low
        and "appointment id:" in low
    ):
        return "Appointment status provided"

    if (
        "not valid" in low
        or "already passed" in low
        or "closed on sundays" in low
    ):
        return "Awaiting valid appointment details"

    if (
        "couldn't" in low
        or "trouble understanding" in low
        or "unavailable" in low
    ):
        return "Unresolved"

    if previous_intent == "faq":
        return "FAQ answered (not independently verified)"

    return "In progress"


def end_session(session_id, session, outcome=None):
    session["ended_at"] = timestamp()

    if session["follow_up_status"] == "Pending":
        session["outcome"] = "Human handoff requested"

    elif outcome:
        session["outcome"] = outcome

    elif session["outcome"] == "In progress":
        session["outcome"] = (
            "Ended without completed action"
        )

    session["summary"] = summarize(session)
    save_session(session_id, session)


# ============================================
# HUMAN HANDOFF
# ============================================

def wants_human(message):
    """
    Recognise different ways customers ask
    to speak with clinic staff or a human.
    """

    text = re.sub(
        r"\s+", " ", message.lower()
    ).strip()

    patterns = [
        r"\b(human|real person|real human)\b",

        r"\b(speak|talk|connect|transfer|put)\s+(?:me\s+)?(?:to|with|through)\s+(?:(?:a|an|the|your|some)\s+)?(?:real\s+)?(?:person|human|staff|receptionist|representative|agent|manager|someone)\b",

        r"\b(?:i\s+)?(?:want|need|would like|wish)\s+to\s+(?:speak|talk|chat|connect|contact|reach)\s+(?:to|with)?\s*(?:(?:a|an|the|your|some)\s+)?(?:staff|person|human|receptionist|representative|agent|manager|someone)\b",

        r"\b(?:contact|reach|call|speak to|talk to|talk with|speak with)\s+(?:(?:the|your|a|an|some)\s+)?(?:clinic\s+)?(?:staff|receptionist|representative|manager|agent|human|person)\b",

        r"\b(staff member|human assistance|human support)\b",

        r"\b(?:i\s+)?(?:need|want)\s+(?:a|the|your)\s+(?:manager|representative|human|person|staff member)\b",

        r"\b(call me back|callback|call back)\b",

        r"\b(customer support|customer service)\b",

        r"\b(?:connect me|transfer me|put me through)\s+to\s+someone\b"
    ]

    return any(
        re.search(pattern, text)
        for pattern in patterns
    )


def is_staff_complaint(message):
    """
    Recognise complaints and offer staff
    assistance without automatically
    submitting a handoff.
    """

    text = re.sub(
        r"\s+", " ", message.lower()
    ).strip()

    patterns = [
        r"\bcomplain(?:t|ing)?\b",

        r"\bfile\s+(?:a\s+)?complaint\b",

        r"\breport\s+(?:a\s+)?(?:doctor|staff|nurse|receptionist)\b",

        r"\b(?:rude|unprofessional|bad|poor|disrespectful|inappropriate|shitty)\s+(?:behavio(?:u)?r|treatment|service)\b",

        r"\b(?:doctor|staff|nurse|receptionist)\s+(?:was|were|is|are)\s+(?:rude|unprofessional|disrespectful)\b"
    ]

    return any(
        re.search(pattern, text)
        for pattern in patterns
    )


def urgent_medical_request(message):
    text = message.lower()

    patterns = [
        r"\b(chest pain|heart attack|stroke)\b",
        r"\b(can't breathe|cannot breathe|difficulty breathing)\b",
        r"\b(unconscious|severe bleeding)\b",
        r"\b(suicidal|kill myself)\b"
    ]

    return any(
        re.search(pattern, text)
        for pattern in patterns
    )


def valid_name(name):
    name = name.strip()

    return (
        3 <= len(name) <= 100
        and not any(
            char.isdigit() for char in name
        )
        and "[" not in name
        and "]" not in name
    )


def normalize_phone(phone):
    return re.sub(
        r"[\s\-()]",
        "",
        phone.strip()
    )


def valid_phone(phone):
    return bool(
        re.fullmatch(r"03\d{9}", phone)
        or re.fullmatch(r"\+923\d{9}", phone)
        or re.fullmatch(r"00923\d{9}", phone)
        or re.fullmatch(r"\+\d{8,15}", phone)
    )


def handoff_reply(session, message):
    handoff = session["handoff"]
    stage = handoff["stage"]

    value = message.strip()
    lower = value.lower()

    # ========================================
    # NEW: COMPLAINT HANDOFF OFFER
    # ========================================

    if stage == "offer":

        if (
            lower in (
                "yes",
                "yes please",
                "sure",
                "okay",
                "ok",
                "please",
                "go ahead",
                "i do",
                "yes i do"
            )
            or wants_human(value)
        ):
            handoff["stage"] = "name"

            session["intent"] = "human_handoff"
            session["outcome"] = (
                "Collecting handoff details"
            )

            return (
                "Certainly. I can record a request "
                "for clinic staff to follow up "
                "with you.\n\n"
                "May I have your full name, please?"
            )

        if lower in (
            "no",
            "no thanks",
            "not now",
            "cancel",
            "never mind",
            "nevermind"
        ):
            session["handoff"] = None
            session["follow_up_status"] = "None"
            session["outcome"] = (
                "Handoff offer declined"
            )

            return (
                "Understood. How else may I help you?"
            )

        return (
            "Would you like me to record a "
            "staff follow-up request? "
            "Please say Yes or No."
        )

    # ========================================
    # EXISTING HANDOFF CANCELLATION
    # ========================================

    if lower in (
        "stop",
        "never mind",
        "nevermind",
        "cancel request",
        "cancel handoff"
    ):
        session["handoff"] = None
        session["follow_up_status"] = "None"
        session["outcome"] = (
            "Human handoff cancelled"
        )

        return (
            "No problem. I haven't submitted "
            "a staff follow-up request. "
            "How else may I help you?"
        )

    # ========================================
    # COLLECT CUSTOMER NAME
    # ========================================

    if stage == "name":

        if not valid_name(value):
            return (
                "Please provide your full name "
                "using letters, for example "
                "Ahmed Ali."
            )

        handoff["name"] = value
        handoff["stage"] = "phone"

        return (
            f"Thank you, {value}. "
            "What phone number should clinic "
            "staff use to contact you?"
        )

    # ========================================
    # COLLECT PHONE NUMBER
    # ========================================

    if stage == "phone":

        phone = normalize_phone(value)

        if not valid_phone(phone):
            return (
                "Please provide a valid contact "
                "number, for example 03001234567."
            )

        handoff["phone"] = phone
        handoff["stage"] = "reason"

        return (
            "Could you briefly tell me why "
            "you would like to speak with "
            "clinic staff?"
        )

    # ========================================
    # COLLECT HANDOFF REASON
    # ========================================

    if stage == "reason":

        if len(value) < 5:
            return (
                "Please briefly describe what "
                "you need assistance with."
            )

        if len(value) > 1000:
            return (
                "Please keep your reason "
                "under 1,000 characters."
            )

        handoff["reason"] = value
        handoff["stage"] = "confirm"

        return (
            "Please confirm your staff "
            "follow-up request:\n"
            f"Name: {handoff['name']}\n"
            f"Phone: {handoff['phone']}\n"
            f"Reason: {handoff['reason']}\n\n"
            "Shall I submit this request? "
            "Please say Yes or No."
        )

    # ========================================
    # CONFIRM AND SAVE HUMAN HANDOFF
    # ========================================

    if stage == "confirm":

        yes_answers = (
            "yes",
            "yes confirm",
            "yes i confirm",
            "yes i accept",
            "confirm",
            "sure",
            "go ahead",
            "submit",
            "please submit"
        )

        no_answers = (
            "no",
            "no thanks",
            "don't submit",
            "do not submit"
        )

        if lower in no_answers:

            session["handoff"] = None
            session["follow_up_status"] = "None"
            session["outcome"] = (
                "Human handoff cancelled"
            )

            return (
                "Okay. I haven't submitted "
                "the request. "
                "How else may I help you?"
            )

        if lower not in yes_answers:
            return (
                "Please say Yes to submit "
                "the request or No to cancel it."
            )

        with sqlite3.connect(
            DB_PATH, timeout=15
        ) as db:

            cursor = db.execute("""
                INSERT INTO human_handoffs
                (
                    session_id,
                    customer_name,
                    phone_number,
                    reason,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, 'Pending', ?)
            """, (
                session["session_id"],
                handoff["name"],
                handoff["phone"],
                handoff["reason"],
                timestamp()
            ))

            handoff_id = cursor.lastrowid

        session["handoff"] = None
        session["intent"] = "human_handoff"
        session["outcome"] = (
            "Human handoff requested"
        )
        session["follow_up_status"] = "Pending"

        return (
            "Your request has been recorded "
            "successfully!\n"
            f"Handoff Reference: {handoff_id}\n"
            "Status: Pending staff review.\n\n"
            "Clinic staff can review your "
            "request and use the contact "
            "number you provided. "
            "A callback is not yet confirmed."
        )

    return "How may I help you?"


# ============================================
# MAIN MESSAGE PROCESSING
# ============================================

def process_message(session, message):

    # Continue existing handoff workflow.
    if session["handoff"] is not None:
        return handoff_reply(session, message)

    # Existing urgent medical safety response.
    if urgent_medical_request(message):

        session["intent"] = "urgent_medical"
        session["outcome"] = (
            "Urgent medical guidance provided"
        )

        return (
            "Your symptoms may require urgent "
            "medical attention. Please seek "
            "immediate emergency medical care "
            "or contact local emergency services. "
            "Do not wait for a clinic callback."
        )

    # ========================================
    # IMPROVED HUMAN REQUEST DETECTION
    # ========================================

    if wants_human(message):

        session["handoff"] = {
            "stage": "name",
            "name": None,
            "phone": None,
            "reason": None
        }

        session["intent"] = "human_handoff"
        session["outcome"] = (
            "Collecting handoff details"
        )

        return (
            "Certainly. I can record a request "
            "for clinic staff to follow up "
            "with you.\n\n"
            "May I have your full name, please?"
        )

    # ========================================
    # NEW: OFFER HANDOFF FOR COMPLAINTS
    # ========================================

    if is_staff_complaint(message):

        session["handoff"] = {
            "stage": "offer",
            "name": None,
            "phone": None,
            "reason": None
        }

        session["intent"] = "human_handoff"
        session["outcome"] = (
            "Staff handoff offered"
        )

        return (
            "I'm sorry to hear about your "
            "experience. Would you like me "
            "to record a request for clinic "
            "staff to follow up with you? "
            "Please say Yes or No."
        )

    # ========================================
    # EXISTING GEMINI CONVERSATION
    # ========================================

    reply = session["agent"].send_message(message)

    detected = infer_intent(
        session["agent"], message
    )

    if detected not in (
        "unknown", "other", "greeting"
    ):
        session["intent"] = detected

    outcome = infer_outcome(
        reply, session["intent"]
    )

    if outcome != "In progress":
        session["outcome"] = outcome

    return reply


# ============================================
# EXISTING CUSTOMER ROUTES
# ============================================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "status": "success",
        "message": (
            "Flask and clinic backend connected!"
        )
    })


@app.route("/api/chat", methods=["POST"])
def chat_with_agent():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "error": "Invalid request."
        }), 400

    message = data.get("message", "")
    session_id = data.get("session_id")

    if (
        not isinstance(message, str)
        or not message.strip()
    ):
        return jsonify({
            "error": "Message is required."
        }), 400

    if len(message) > 4000:
        return jsonify({
            "error": "Message is too long."
        }), 400

    if not isinstance(session_id, str):
        session_id = None

    owner = conversation_owner()

    try:
        with conversations_lock:

            now = time.monotonic()

            expired = [
                key
                for key, value in conversations.items()
                if now - value["last_active"]
                > SESSION_TIMEOUT
            ]

            for key in expired:
                end_session(
                    key,
                    conversations[key],
                    "Timed out"
                )

                del conversations[key]

            if session_id and session_id in conversations and conversations[session_id].get("owner") != owner:
                return jsonify({"error": "This conversation does not belong to the current user."}), 403

            if (
                not session_id
                or session_id not in conversations
            ):
                session_id = secrets.token_urlsafe(32)

                conversations[session_id] = {
                    "session_id": session_id,
                    "owner": owner,
                    "agent": ClinicConversation(),
                    "handoff": None,
                    "last_active": now,
                    "started_at": timestamp(),
                    "ended_at": None,
                    "transcript": [],
                    "intent": "unknown",
                    "outcome": "In progress",
                    "follow_up_status": "None",
                    "summary": ""
                }

                save_session(
                    session_id,
                    conversations[session_id]
                )

            session = conversations[session_id]

            session["last_active"] = now

            clean_message = message.strip()

            reply = process_message(
                session,
                clean_message
            )

            session["transcript"].extend([
                {
                    "role": "customer",
                    "text": clean_message,
                    "at": timestamp()
                },
                {
                    "role": "assistant",
                    "text": reply,
                    "at": timestamp()
                }
            ])

            session["summary"] = summarize(session)

            save_session(session_id, session)

        return jsonify({
            "reply": reply,
            "session_id": session_id
        })

    except Exception:
        app.logger.exception(
            "AI chat or session logging error"
        )

        return jsonify({
            "error": (
                "AI service temporarily unavailable."
            )
        }), 500


@app.route("/api/end-session", methods=["POST"])
def close_session():

    data = request.get_json(silent=True)

    if (
        not isinstance(data, dict)
        or not isinstance(
            data.get("session_id"), str
        )
    ):
        return jsonify({
            "error": (
                "A valid session ID is required."
            )
        }), 400

    session_id = data["session_id"]
    owner = conversation_owner()

    with conversations_lock:

        session = conversations.get(session_id)

        if session is None:
            return jsonify({
                "error": (
                    "Session not found or already ended."
                )
            }), 404

        if session.get("owner") != owner:
            return jsonify({"error": "This conversation does not belong to the current user."}), 403

        try:
            end_session(session_id, session)

        except sqlite3.Error:
            app.logger.exception(
                "Could not close session"
            )

            return jsonify({
                "error": "Could not save session."
            }), 500

        del conversations[session_id]

    return jsonify({
        "status": "success",
        "message": "Session ended and saved."
    })


# ============================================
# STAFF DASHBOARD AUTHENTICATION
# ============================================

def staff_required(function):

    @wraps(function)
    def protected(*args, **kwargs):

        if not flask_session.get(
            "staff_logged_in"
        ):
            if request.path.startswith(
                "/api/staff/"
            ):
                return jsonify({
                    "error": "Staff login required."
                }), 401

            return redirect(
                url_for("dashboard_login")
            )

        return function(*args, **kwargs)

    return protected


def check_csrf():

    supplied = request.headers.get(
        "X-CSRF-Token", ""
    )

    expected = flask_session.get(
        "csrf_token", ""
    )

    return bool(
        expected
        and secrets.compare_digest(
            supplied, expected
        )
    )


@app.route(
    "/dashboard/login",
    methods=["GET", "POST"]
)
def dashboard_login():

    error = None

    if request.method == "POST":

        supplied = request.form.get(
            "password", ""
        )

        if secrets.compare_digest(
            supplied.encode("utf-8"),
            DASHBOARD_PASSWORD.encode("utf-8")
        ):
            flask_session.clear()

            flask_session.permanent = True

            flask_session["staff_logged_in"] = True

            flask_session["csrf_token"] = (
                secrets.token_urlsafe(32)
            )

            return redirect(
                url_for("dashboard")
            )

        error = "Incorrect password."

    return render_template(
        "dashboard.html",
        login_page=True,
        error=error
    )


@app.route("/dashboard")
@staff_required
def dashboard():

    return render_template(
        "dashboard.html",
        login_page=False,
        csrf_token=flask_session["csrf_token"]
    )


@app.route(
    "/dashboard/logout",
    methods=["POST"]
)
@staff_required
def dashboard_logout():

    if not check_csrf():
        return jsonify({
            "error": "Invalid security token."
        }), 403

    flask_session.clear()

    return redirect(
        url_for("dashboard_login")
    )


# ============================================
# STAFF DASHBOARD DATA
# ============================================

@app.route("/api/staff/dashboard")
@staff_required
def staff_dashboard_data():

    with sqlite3.connect(
        DB_PATH, timeout=15
    ) as db:

        db.row_factory = sqlite3.Row

        appointments = [
            dict(row)
            for row in db.execute("""
                SELECT
                    id,
                    customer_name,
                    service,
                    appointment_date,
                    appointment_time,
                    status
                FROM appointments
                ORDER BY
                    appointment_date DESC,
                    id DESC
                LIMIT 200
            """)
        ]

        handoffs = [
            dict(row)
            for row in db.execute("""
                SELECT
                    id,
                    customer_name,
                    phone_number,
                    reason,
                    status,
                    created_at
                FROM human_handoffs
                ORDER BY id DESC
                LIMIT 200
            """)
        ]

        sessions = [
            dict(row)
            for row in db.execute("""
                SELECT
                    session_id,
                    started_at,
                    ended_at,
                    detected_intent,
                    outcome,
                    follow_up_status,
                    summary
                FROM call_sessions
                ORDER BY started_at DESC
                LIMIT 100
            """)
        ]

        counts = {
            "appointments": db.execute(
                "SELECT COUNT(*) FROM appointments"
            ).fetchone()[0],

            "pending_appointments": db.execute(
                "SELECT COUNT(*) "
                "FROM appointments "
                "WHERE status='Pending'"
            ).fetchone()[0],

            "handoffs": db.execute(
                "SELECT COUNT(*) "
                "FROM human_handoffs"
            ).fetchone()[0],

            "pending_handoffs": db.execute(
                "SELECT COUNT(*) "
                "FROM human_handoffs "
                "WHERE status='Pending'"
            ).fetchone()[0]
        }

    return jsonify({
        "appointments": appointments,
        "handoffs": handoffs,
        "sessions": sessions,
        "counts": counts
    })


# ============================================
# ACCEPT OR REJECT APPOINTMENTS
# ============================================

@app.route(
    "/api/staff/appointments/<int:appointment_id>/status",
    methods=["POST"]
)
@staff_required
def update_appointment_status(appointment_id):

    if not check_csrf():
        return jsonify({
            "error": "Invalid security token."
        }), 403

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "error": "Invalid request."
        }), 400

    new_status = data.get("status")

    if new_status not in (
        "Accepted", "Cancelled"
    ):
        return jsonify({
            "error": (
                "Status must be Accepted or Cancelled."
            )
        }), 400

    try:
        with sqlite3.connect(
            DB_PATH, timeout=15
        ) as db:

            # Update only Pending appointments.
            # Protect existing status changes.

            cursor = db.execute("""
                UPDATE appointments
                SET status = ?
                WHERE id = ?
                AND status = 'Pending'
            """, (
                new_status,
                appointment_id
            ))

            if cursor.rowcount == 0:

                existing = db.execute("""
                    SELECT status
                    FROM appointments
                    WHERE id = ?
                """, (
                    appointment_id,
                )).fetchone()

                if existing is None:
                    return jsonify({
                        "error": (
                            "Appointment not found."
                        )
                    }), 404

                return jsonify({
                    "error": (
                        "This appointment is already "
                        f"{existing[0]} and cannot "
                        "be changed using these buttons."
                    )
                }), 409

    except sqlite3.Error:
        app.logger.exception(
            "Appointment status update failed"
        )

        return jsonify({
            "error": (
                "Could not update appointment status."
            )
        }), 500

    return jsonify({
        "status": "success",
        "appointment_id": appointment_id,
        "appointment_status": new_status,
        "message": (
            f"Appointment #{appointment_id} "
            f"{new_status.lower()}."
        )
    })


# ============================================
# EXISTING HUMAN HANDOFF MANAGEMENT
# ============================================

@app.route(
    "/api/staff/handoffs/<int:handoff_id>/complete",
    methods=["POST"]
)
@staff_required
def complete_handoff(handoff_id):

    if not check_csrf():
        return jsonify({
            "error": "Invalid security token."
        }), 403

    with sqlite3.connect(
        DB_PATH, timeout=15
    ) as db:

        row = db.execute("""
            SELECT session_id, status
            FROM human_handoffs
            WHERE id=?
        """, (
            handoff_id,
        )).fetchone()

        if not row:
            return jsonify({
                "error": (
                    "Handoff request not found."
                )
            }), 404

        if row[1] == "Completed":
            return jsonify({
                "status": "success",
                "message": "Already completed."
            })

        db.execute("""
            UPDATE human_handoffs
            SET status='Completed'
            WHERE id=?
        """, (
            handoff_id,
        ))

        remaining = db.execute("""
            SELECT COUNT(*)
            FROM human_handoffs
            WHERE session_id=?
            AND status='Pending'
        """, (
            row[0],
        )).fetchone()[0]

        if remaining == 0:
            db.execute("""
                UPDATE call_sessions
                SET follow_up_status='Completed'
                WHERE session_id=?
            """, (
                row[0],
            ))

    return jsonify({
        "status": "success",
        "message": (
            "Handoff marked as completed."
        )
    })


# ============================================
# EXISTING FULL CONVERSATION VIEWER
# ============================================

@app.route(
    "/api/staff/sessions/<session_id>/transcript"
)
@staff_required
def staff_session_transcript(session_id):

    with sqlite3.connect(
        DB_PATH, timeout=15
    ) as db:

        db.row_factory = sqlite3.Row

        row = db.execute("""
            SELECT
                session_id,
                started_at,
                ended_at,
                detected_intent,
                outcome,
                transcript
            FROM call_sessions
            WHERE session_id = ?
        """, (
            session_id,
        )).fetchone()

    if row is None:
        return jsonify({
            "error": (
                "Conversation not found."
            )
        }), 404

    try:
        transcript = json.loads(
            row["transcript"]
        )

        if not isinstance(
            transcript, list
        ):
            transcript = []

    except (ValueError, TypeError):
        transcript = []

    return jsonify({
        "session_id": row["session_id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "intent": row["detected_intent"],
        "outcome": row["outcome"],
        "transcript": transcript
    })



# Additional website routes (AI conversation logic unchanged).
from website_api import site, setup as setup_website
setup_website()
app.register_blueprint(site)

if __name__ == "__main__":
    app.run(
        debug=False,
        host="127.0.0.1",
        port=5000
    )