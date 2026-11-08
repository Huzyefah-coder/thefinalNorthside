# Northside Clinic — Integrated React + Flask + PostgreSQL

This is a **separate copy** of the original AI receptionist with a React patient/staff portal and PostgreSQL adapter. The original Gemini conversation implementation, model selection, prompts, FAQs, handoff rules, and confirmation flow remain in `backend/clinic_backend.py`; only the `sqlite3` import was replaced with the PostgreSQL compatibility adapter. The original Flask routes are retained. No `.env` secrets or real patient database are bundled.

## Prerequisites
- Python 3.11+, Node.js 20+, PostgreSQL 15+.
- A PostgreSQL database, e.g. `CREATE DATABASE northside_clinic;`.
- Your existing valid Gemini API key.

## Backend
```powershell
cd backend
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Edit .env: GEMINI_API_KEY, DATABASE_URL, FLASK_SECRET_KEY,
# DASHBOARD_PASSWORD, INITIAL_STAFF_EMAIL, INITIAL_STAFF_PASSWORD
python app.py
```
The Flask server listens on http://127.0.0.1:5000. It creates the PostgreSQL tables at startup. Staff login uses `INITIAL_STAFF_EMAIL` and `INITIAL_STAFF_PASSWORD` from `.env`; replace these before running.

## Frontend
Open another terminal:
```powershell
cd frontend
npm install
npm run dev
```
Open http://127.0.0.1:5173. The Vite proxy forwards `/api` calls to Flask. Use the shared **Login / Register** page; staff credentials route to the staff portal, while patients can self-register and use the patient portal. Patients can book manually or through the existing AI interface.

## Existing SQLite data — optional migration
Keep your original `appointments.db` safe and backed up. Once the PostgreSQL tables are created, run from the backend folder:
```powershell
python migrate_sqlite.py "C:\\path\\to\\a\\COPY\\of\\appointments.db"
```
This imports appointment, AI conversation, and handoff records with their existing IDs and does not modify the source database. Verify counts and behavior after import. Do **not** upload actual patient data to public GitHub.

## Design and included features
- Emerald / warm ivory / charcoal React landing page with existing voice/text AI assistant.
- Shared secure email/password login, patient registration, role-based staff and patient portals.
- Manual booking, appointment list, cancellation and patient rescheduling; staff approval/completion.
- Staff AI transcript viewer, human handoff management, activity overview.
- PostgreSQL storage, CSRF checks for portal mutations, password hashing, patient ownership checks.
- Existing legacy dashboard and API endpoints remain present for backward compatibility.

## Limitations and important verification
- **Not production-certified.** A running PostgreSQL service, Gemini API key, and dependencies are needed for live end-to-end tests. No live database or Gemini tests were possible in this build environment.
- Guest AI bookings are visible to staff but are **not automatically assigned** to a patient account. AI bookings initiated while signed in are linked to that account. Existing guest appointments require staff-verified ownership linking before appearing in a patient's dashboard.
- Appointment availability follows the original service-based scheduling logic. Clinic-wide doctor scheduling, notification emails, automated slot calendar, advanced reporting, and a staff account management UI are not implemented.
- The legacy dashboard password route remains for backward compatibility; secure or disable it before deployment. Use HTTPS, rate limiting, audit logging, and a secure deployment configuration before handling real patient data.
- The AI's existing spoken behavior is preserved at the backend; the new React voice interface uses browser speech APIs and may differ visually or in browser support.
- Test all original booking/cancel/reschedule/status/FAQ/handoff/voice flows with the PostgreSQL adapter before replacing the existing deployment.

## Staff-managed services
Sign in as staff and open **Manage Services** in the left dashboard menu. Add a service by name or delete an existing one. Changes are stored in PostgreSQL and reflected in the public homepage, manual patient booking and AI receptionist service selection. Existing appointment records are retained when a service is deleted. All existing AI confirmation, cancellation, scheduling and handoff rules remain unchanged.
