
import os
import re
import json
import postgres_compat as sqlite3
import threading
import calendar
from datetime import datetime, date, timedelta, timezone

from dotenv import load_dotenv
from google import genai
from google.genai import types


# ============================================
# CONFIGURATION
# ============================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is missing from your .env file."
    )

MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite"
)

client = genai.Client(api_key=API_KEY)

DB_PATH = os.path.join(BASE_DIR, "appointments.db")
KNOWLEDGE_PATH = os.path.join(
    BASE_DIR, "clinic_knowledge.json"
)

with open(KNOWLEDGE_PATH, encoding="utf-8") as file:
    KNOWLEDGE = json.load(file)

PKT = timezone(timedelta(hours=5))

def get_services():
    # Shared staff-managed list; keep AI validation in sync with patient booking.
    with sqlite3.connect(DB_PATH) as db:
        return tuple(row[0] for row in db.execute('SELECT name FROM clinic_services ORDER BY id').fetchall())


DB_LOCK = threading.RLock()


def now_pk():
    return datetime.now(PKT)


def display_date(value):
    return date.fromisoformat(value).strftime(
        "%A, %d %B %Y"
    )


def display_time(value):
    return datetime.strptime(
        value, "%H:%M"
    ).strftime("%I:%M %p").lstrip("0")


# ============================================
# STRICT CALENDAR DATE VALIDATION
# ============================================

MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12
}

MONTH_PATTERN = "|".join(
    sorted(MONTHS.keys(), key=len, reverse=True)
)


def parse_customer_date(message):
    """
    Return (ISO date, error) for an explicitly
    stated calendar date.

    A yearless date means the next occurrence
    of that month/day.

    A stated year is never silently replaced.
    """

    text = str(message or "").lower().strip()

    day_first = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({MONTH_PATTERN})"
        rf"(?:\s*,?\s*(\d{{4}}))?\b",
        text
    )

    month_first = re.search(
        rf"\b({MONTH_PATTERN})\s*(\d{{1,2}})(?:st|nd|rd|th)?"
        rf"(?:\s*,?\s*(\d{{4}}))?\b",
        text
    )

    iso_match = re.search(
        r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",
        text
    )

    if day_first:
        day = int(day_first.group(1))
        month = MONTHS[day_first.group(2)]
        year = (
            int(day_first.group(3))
            if day_first.group(3)
            else None
        )

    elif month_first:
        month = MONTHS[month_first.group(1)]
        day = int(month_first.group(2))
        year = (
            int(month_first.group(3))
            if month_first.group(3)
            else None
        )

    elif iso_match:
        year, month, day = map(
            int, iso_match.groups()
        )

    else:
        return None, None

    today = now_pk().date()

    if year is None:
        year = today.year

        for candidate_year in range(
            year, min(year + 9, 10000)
        ):
            try:
                candidate = date(
                    candidate_year, month, day
                )
            except ValueError:
                continue

            if candidate >= today:
                return candidate.isoformat(), None

        return None, (
            "That date is not valid. "
            "Please choose another date."
        )

    try:
        candidate = date(year, month, day)
    except ValueError:
        return None, (
            "That date is not valid. "
            "Please choose another date."
        )

    if candidate < today:
        return None, (
            f"{candidate.strftime('%d %B %Y')} "
            "has already passed. "
            "Please choose a future appointment date."
        )

    return candidate.isoformat(), None


def invalid_calendar_date(message):
    return parse_customer_date(message)[1]


# ============================================
# DATABASE
# ============================================

def initialize_database():
    with DB_LOCK:
        with sqlite3.connect(DB_PATH) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_name TEXT NOT NULL,
                    service TEXT NOT NULL,
                    appointment_date TEXT NOT NULL,
                    appointment_time TEXT,
                    status TEXT DEFAULT 'Pending'
                )
            """)

            columns = {
                row[1]
                for row in db.execute(
                    "PRAGMA table_info(appointments)"
                )
            }

            if "appointment_time" not in columns:
                db.execute("""
                    ALTER TABLE appointments
                    ADD COLUMN appointment_time TEXT
                """)


initialize_database()


def get_appointment(appointment_id, name):
    with sqlite3.connect(DB_PATH) as db:
        return db.execute("""
            SELECT id, customer_name, service,
                   appointment_date,
                   appointment_time, status
            FROM appointments
            WHERE id = ?
            AND LOWER(TRIM(customer_name)) =
                LOWER(TRIM(?))
        """, (appointment_id, name)).fetchone()


# ============================================
# DOUBLE-BOOKING PREVENTION
# ============================================

def occupied(
    db, day, hour,
    exclude_id=None,
    service=None
):
    """
    Check whether a service is already booked
    on the requested date and time.

    Pending and Accepted appointments block slots.
    Cancelled appointments do not block slots.
    """

    sql = """
        SELECT 1
        FROM appointments
        WHERE appointment_date = ?
        AND appointment_time = ?
        AND status IN ('Pending', 'Accepted')
    """

    params = [day, hour]

    if service is not None:
        sql += " AND service = ?"
        params.append(service)

    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)

    return db.execute(
        sql + " LIMIT 1", params
    ).fetchone() is not None


def booked_slot_message(service, day, hour):
    return (
        "I'm sorry, we're already booked for "
        f"{service} on {display_date(day)} "
        f"at {display_time(hour)}. "
        "Could you please choose another time?"
    )


# ============================================
# APPOINTMENT VALIDATION
# ============================================

def validate_slot(day, hour):
    try:
        requested_date = date.fromisoformat(day)

        requested_time = datetime.strptime(
            hour, "%H:%M"
        ).time()

        requested = datetime.combine(
            requested_date,
            requested_time,
            tzinfo=PKT
        )

    except (ValueError, TypeError):
        return (
            "The appointment date or time is invalid. "
            "Please provide a valid date and time."
        )

    if requested <= now_pk():
        return (
            "That date or time has already passed. "
            "Please choose a future appointment."
        )

    if requested_date.weekday() == 6:
        return (
            "The clinic is closed on Sundays. "
            "Please choose Monday to Saturday."
        )

    if not 9 <= requested_time.hour < 18:
        return (
            "Appointments are available between "
            "9 AM and 5:30 PM."
        )

    if requested_time.minute not in (0, 30):
        return (
            "Please select a 30-minute appointment "
            "slot, such as 2:00 PM or 2:30 PM."
        )

    return None


# ============================================
# GEMINI RESPONSE FORMAT
# ============================================

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {
            "type": "STRING",
            "enum": [
                "book",
                "cancel",
                "reschedule",
                "status",
                "faq",
                "greeting",
                "other"
            ]
        },
        "name": {
            "type": "STRING",
            "nullable": True
        },
        "service": {
            "type": "STRING",
            "nullable": True
        },
        "date": {
            "type": "STRING",
            "nullable": True
        },
        "time": {
            "type": "STRING",
            "nullable": True
        },
        "appointment_id": {
            "type": "INTEGER",
            "nullable": True
        },
        "confirmation": {
            "type": "STRING",
            "enum": [
                "yes",
                "no",
                "unclear"
            ]
        },
        "question": {
            "type": "STRING",
            "nullable": True
        }
    },
    "required": [
        "intent",
        "name",
        "service",
        "date",
        "time",
        "appointment_id",
        "confirmation",
        "question"
    ]
}


# ============================================
# CONVERSATION
# ============================================

class ClinicConversation:

    def __init__(self):
        self.mode = None
        self.details = {}
        self.pending = None
        self.history = []

    def reset(self):
        self.mode = None
        self.details = {}
        self.pending = None
        self.history = []

    def understand(self, message):
        recent = self.history[-10:]

        prompt = f"""
You are the natural-language understanding
system for Northside Clinic's AI receptionist.

Current Pakistan date and time:
{now_pk().isoformat()}

CURRENT WORKFLOW:
{self.mode or "none"}

COLLECTED DETAILS:
{json.dumps(self.details)}

AWAITING FINAL CONFIRMATION:
{json.dumps(self.pending)}

RECENT CONVERSATION:
{json.dumps(recent, ensure_ascii=False)}

LATEST CUSTOMER MESSAGE:
{message}

Your task is to UNDERSTAND the customer's
meaning and return structured JSON.

IMPORTANT RULES:

1. Understand natural English, Roman Urdu,
   Urdu-English mixtures, spelling errors,
   and speech-recognition mistakes.

2. Interpret the message in conversation
   context. Do not depend on specific
   words such as "appointment" or "book".

3. If the receptionist asked which service
   the customer wants, "general" means
   General Consultation.

4. Understand booking requests such as:
   "I need to see a doctor"
   "Mujhe doctor ko dikhana hai"
   "I want dental tomorrow"
   "Can you arrange a checkup?"

5. Do not interpret a question about whether
   a service exists as a definite booking
   request unless booking intent is clear.

6. Extract only information explicitly
   provided by the customer, or unambiguously
   established by the conversation.

7. Never invent a customer name, service,
   appointment ID, date or time.

8. Recognize corrections. If the customer
   says "actually dental", extract Dental
   Care as the corrected service.

9. Convert relative dates using the current
   Pakistan date. Output dates as YYYY-MM-DD.

10. Convert unambiguous times to 24-hour
    HH:MM. For ambiguous times, use null.

11. Canonical services are exactly:
    {json.dumps(get_services(), ensure_ascii=False)}

12. A customer may supply multiple details
    in one sentence.

13. "yes, I accept", "sure, go ahead",
    and "yes, confirm" mean confirmation=yes
    when confirmation is pending.

14. "no, change the time" is a correction,
    not approval to save the current action.
    Return confirmation=unclear and extract
    the corrected information if present.

15. For a pure question about the clinic,
    use intent=faq and include the question.

16. When the customer is continuing an active
    workflow, keep its intent unless they
    clearly switch tasks.

17. For greetings and casual conversation,
    use greeting or other.

18. If information is not provided, return
    null. Never fill missing fields with
    guesses.

19. IMPORTANT CALENDAR RULE:
    Never silently convert an impossible
    date to another date.

    For example, February 31 is invalid.
    Do not convert it to March 3.

    Never invent a replacement date when
    the customer provides an invalid date.

20. When a customer corrects an invalid
    date, extract only the new date.

Return only the requested JSON structure.
"""

        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA
            )
        )

        if not response.text:
            raise ValueError(
                "Gemini returned an empty response."
            )

        result = json.loads(response.text)

        self.history.append({
            "customer": message,
            "interpretation": result
        })

        self.history = self.history[-12:]

        return result

    # ========================================
    # UPDATE DETAILS
    # ========================================

    def update_details(self, data):
        name = data.get("name")

        if name:
            name = name.strip()

            if (
                len(name) >= 3
                and len(name) <= 100
                and "[" not in name
                and "]" not in name
            ):
                self.details["name"] = name

        service = data.get("service")

        if service in get_services():
            self.details["service"] = service

        day = data.get("date")

        if day:
            try:
                date.fromisoformat(day)
                self.details["date"] = day
            except (ValueError, TypeError):
                pass

        hour = data.get("time")

        if hour:
            try:
                parsed = datetime.strptime(
                    hour, "%H:%M"
                )

                self.details["time"] = (
                    parsed.strftime("%H:%M")
                )

            except (ValueError, TypeError):
                pass

        appointment_id = data.get(
            "appointment_id"
        )

        if (
            isinstance(appointment_id, int)
            and not isinstance(appointment_id, bool)
            and appointment_id > 0
        ):
            self.details["id"] = appointment_id

    # ========================================
    # BOOKING
    # ========================================

    def booking(self):
        if not self.details.get("service"):
            choices = get_services()
            return ("Certainly! Which service would you like: "
                    + (", ".join(choices) if choices else "please contact clinic staff") + "?")

        if not self.details.get("name"):
            return (
                "Of course. May I have your "
                "full name, please?"
            )

        if not self.details.get("date"):
            return (
                "What date would you prefer "
                "for your appointment?"
            )

        if not self.details.get("time"):
            return (
                "What time would suit you? "
                "You can say something like "
                "2 PM or 3:30 PM."
            )

        error = validate_slot(
            self.details["date"],
            self.details["time"]
        )

        if error:
            if (
                "date" in error.lower()
                or "sunday" in error.lower()
            ):
                self.details.pop("date", None)
            else:
                self.details.pop("time", None)

            return error

        with sqlite3.connect(DB_PATH) as db:
            if occupied(
                db,
                self.details["date"],
                self.details["time"],
                service=self.details["service"]
            ):
                message = booked_slot_message(
                    self.details["service"],
                    self.details["date"],
                    self.details["time"]
                )

                self.details.pop("time", None)

                return message

        self.pending = {
            "type": "book",
            **self.details
        }

        return (
            "Let me confirm your appointment:\n"
            f"Name: {self.pending['name']}\n"
            f"Service: {self.pending['service']}\n"
            f"Date: {display_date(self.pending['date'])}\n"
            f"Time: {display_time(self.pending['time'])}\n"
            "Shall I submit this appointment request?"
        )

    # ========================================
    # EXISTING APPOINTMENTS
    # ========================================

    def existing(self):
        if not self.details.get("id"):
            return "May I have your appointment ID?"

        if not self.details.get("name"):
            return (
                "Please tell me the full name "
                "used for that appointment."
            )

        appointment = get_appointment(
            self.details["id"],
            self.details["name"]
        )

        if not appointment:
            self.reset()

            return (
                "I couldn't find an appointment "
                "matching that ID and name."
            )

        (
            appointment_id,
            name,
            service,
            day,
            hour,
            status
        ) = appointment

        if self.mode == "status":
            self.reset()

            return (
                f"Appointment ID: {appointment_id}\n"
                f"Name: {name}\n"
                f"Service: {service}\n"
                f"Date: {display_date(day)}\n"
                f"Time: "
                f"{display_time(hour) if hour else 'Not recorded'}\n"
                f"Status: {status}"
            )

        if status != "Pending":
            self.reset()

            return (
                "This appointment is not pending, "
                "so it cannot be changed here."
            )

        if self.mode == "cancel":
            self.pending = {
                "type": "cancel",
                "id": appointment_id,
                "name": name
            }

            return (
                f"Do you want to cancel appointment "
                f"#{appointment_id} for {name}?"
            )

        if not self.details.get("date"):
            return (
                "What new date would you "
                "like to reschedule to?"
            )

        if not self.details.get("time"):
            return (
                "And what new time would "
                "you prefer?"
            )

        error = validate_slot(
            self.details["date"],
            self.details["time"]
        )

        if error:
            if (
                "date" in error.lower()
                or "sunday" in error.lower()
            ):
                self.details.pop("date", None)
            else:
                self.details.pop("time", None)

            return error

        with sqlite3.connect(DB_PATH) as db:
            if occupied(
                db,
                self.details["date"],
                self.details["time"],
                exclude_id=appointment_id,
                service=service
            ):
                message = booked_slot_message(
                    service,
                    self.details["date"],
                    self.details["time"]
                )

                self.details.pop("time", None)
                return message

        self.pending = {
            "type": "reschedule",
            "id": appointment_id,
            "name": name,
            "date": self.details["date"],
            "time": self.details["time"]
        }

        return (
            f"Shall I reschedule appointment "
            f"#{appointment_id} to "
            f"{display_date(self.pending['date'])} "
            f"at {display_time(self.pending['time'])}?"
        )

    # ========================================
    # CONFIRMED DATABASE ACTION
    # ========================================

    def save_action(self):
        action = self.pending

        if not action:
            return "There is no action to confirm."

        try:
            with DB_LOCK:
                with sqlite3.connect(
                    DB_PATH, timeout=15
                ) as db:

                    db.execute("BEGIN IMMEDIATE")

                    if action["type"] == "book":
                        error = validate_slot(
                            action["date"],
                            action["time"]
                        )

                        if error:
                            self.pending = None
                            return error

                        if occupied(
                            db,
                            action["date"],
                            action["time"],
                            service=action["service"]
                        ):
                            self.pending = None
                            self.details.pop("time", None)

                            return booked_slot_message(
                                action["service"],
                                action["date"],
                                action["time"]
                            )

                        cursor = db.execute("""
                            INSERT INTO appointments
                            (
                                customer_name,
                                service,
                                appointment_date,
                                appointment_time,
                                status
                            )
                            VALUES (?, ?, ?, ?, 'Pending')
                        """, (
                            action["name"],
                            action["service"],
                            action["date"],
                            action["time"]
                        ))

                        new_id = cursor.lastrowid

                        result = (
                            "Your appointment request "
                            "has been saved successfully!\n"
                            f"Appointment ID: {new_id}\n"
                            f"Name: {action['name']}\n"
                            f"Service: {action['service']}\n"
                            f"Date: {display_date(action['date'])}\n"
                            f"Time: {display_time(action['time'])}\n"
                            "Status: Pending clinic confirmation."
                        )

                    else:
                        appointment = db.execute("""
                            SELECT status
                            FROM appointments
                            WHERE id = ?
                            AND LOWER(TRIM(customer_name)) =
                                LOWER(TRIM(?))
                        """, (
                            action["id"],
                            action["name"]
                        )).fetchone()

                        if (
                            not appointment
                            or appointment[0] != "Pending"
                        ):
                            self.pending = None

                            return (
                                "This appointment could "
                                "not be updated."
                            )

                        if action["type"] == "cancel":
                            db.execute("""
                                UPDATE appointments
                                SET status = 'Cancelled'
                                WHERE id = ?
                                AND status = 'Pending'
                            """, (action["id"],))

                            result = (
                                f"Appointment #{action['id']} "
                                "has been cancelled."
                            )

                        else:
                            error = validate_slot(
                                action["date"],
                                action["time"]
                            )

                            if error:
                                self.pending = None
                                return error

                            service_row = db.execute("""
                                SELECT service
                                FROM appointments
                                WHERE id = ?
                            """, (action["id"],)).fetchone()

                            service = service_row[0]

                            if occupied(
                                db,
                                action["date"],
                                action["time"],
                                exclude_id=action["id"],
                                service=service
                            ):
                                self.pending = None
                                self.details.pop("time", None)

                                return booked_slot_message(
                                    service,
                                    action["date"],
                                    action["time"]
                                )

                            db.execute("""
                                UPDATE appointments
                                SET appointment_date = ?,
                                    appointment_time = ?
                                WHERE id = ?
                                AND status = 'Pending'
                            """, (
                                action["date"],
                                action["time"],
                                action["id"]
                            ))

                            result = (
                                f"Appointment #{action['id']} "
                                "has been rescheduled to "
                                f"{display_date(action['date'])} "
                                f"at {display_time(action['time'])}."
                            )

            self.reset()
            return result

        except sqlite3.Error:
            return (
                "Sorry, I couldn't save that change "
                "right now. Please try again."
            )

    # ========================================
    # CLINIC QUESTIONS
    # ========================================

    def answer_faq(self, message):
        prompt = f"""
You are Northside Clinic's automated
AI receptionist.

Answer the customer naturally and
professionally, using ONLY the approved
clinic information below.

APPROVED CLINIC INFORMATION:
{json.dumps({**KNOWLEDGE, "services": list(get_services()), "faq": [q for q in KNOWLEDGE.get("faq", []) if q.get("question") != "What services do you provide?"]}, ensure_ascii=False)}

CUSTOMER:
{message}

RULES:
- Do not invent prices, doctors, addresses,
  contact details, or policies.
- Do not give medical diagnoses.
- If the information is unavailable,
  say so and refer to clinic staff.
- For emergencies, recommend immediate
  emergency medical care.
- Reply in the customer's language
  when practical.
- Keep your response concise.
"""

        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3
            )
        )

        return (
            response.text
            or "Please contact clinic staff "
               "for more information."
        )

    # ========================================
    # MAIN CONVERSATION
    # ========================================

    def send_message(self, message):
        message = str(message or "").strip()

        if not message:
            return "How can I help you today?"

        # ====================================
        # INVALID DATE RECOVERY
        # ====================================

        explicit_date = None

        if self.mode in (None, "book", "reschedule"):
            explicit_date, date_error = parse_customer_date(
                message
            )

            if date_error:
                if self.pending:
                    if self.pending.get("type") in (
                        "book",
                        "reschedule"
                    ):
                        self.pending = None

                if self.mode is None:
                    self.mode = "book"

                self.details.pop("date", None)

                self.history.append({
                    "customer": message,
                    "response": date_error
                })

                self.history = self.history[-12:]

                return date_error

        # ====================================
        # EXISTING GEMINI WORKFLOW
        # ====================================

        try:
            data = self.understand(message)

        except Exception as error:
            print(
                "Gemini understanding error:",
                repr(error)
            )

            return (
                "I'm sorry, I had trouble "
                "understanding that just now. "
                "Could you please repeat it?"
            )

        # Explicit dates take priority over
        # Gemini's interpretation.

        if explicit_date is not None and (
            self.mode in ("book", "reschedule")
            or data.get("intent") in ("book", "reschedule")
        ):
            data["date"] = explicit_date

        intent = data.get("intent", "other")
        confirmation = data.get(
            "confirmation", "unclear"
        )

        # Gemini interprets the message.
        # Python controls database actions.

        if self.pending:
            if confirmation == "yes":
                return self.save_action()

            if confirmation == "no":
                self.pending = None
                self.reset()

                return (
                    "Of course. I haven't made "
                    "any changes."
                )

            if self.pending["type"] == "book":
                self.pending = None
                self.mode = "book"

            elif self.pending["type"] == "reschedule":
                self.pending = None
                self.mode = "reschedule"

            else:
                return (
                    "Would you like to confirm "
                    "or cancel this request?"
                )

        # Switch workflow only when Gemini
        # identifies a different task.

        if intent in (
            "book",
            "cancel",
            "reschedule",
            "status"
        ):
            if intent != self.mode:
                self.mode = intent
                self.details = {}

        self.update_details(data)

        if self.mode == "book":
            return self.booking()

        if self.mode in (
            "cancel",
            "reschedule",
            "status"
        ):
            return self.existing()

        if intent == "greeting":
            return (
                "Hello! I'm Northside Clinic's "
                "AI receptionist. How may "
                "I assist you today?"
            )

        try:
            return self.answer_faq(message)

        except Exception as error:
            print(
                "Gemini FAQ error:",
                repr(error)
            )

            return (
                "Sorry, I couldn't retrieve "
                "that information right now."
            )
