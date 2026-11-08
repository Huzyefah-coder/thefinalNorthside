
import sqlite3

DB_NAME = "appointments.db"

def create_database():
    with sqlite3.connect(DB_NAME) as connection:
        connection.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            service TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            status TEXT DEFAULT 'Pending'
        )
        """)

def save_appointment(name, service, date):
    with sqlite3.connect(DB_NAME) as connection:
        cursor = connection.execute("""
        INSERT INTO appointments
        (customer_name, service, appointment_date)
        VALUES (?, ?, ?)
        """, (name, service, date))
        return cursor.lastrowid

if __name__ == "__main__":
    create_database()
    print("Appointment database created successfully!")
