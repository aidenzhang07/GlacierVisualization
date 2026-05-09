from flask import Flask, jsonify, render_template
import sqlite3
import os

app = Flask(__name__)
DB_PATH = "glacier.db"

def init_db():
    """Create the database and seed it with sample glacier data."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS glacier_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            glacier_name TEXT NOT NULL,
            thickness_meters REAL NOT NULL
        )
    """)

    # Check if data already exists
    cursor.execute("SELECT COUNT(*) FROM glacier_data")
    if cursor.fetchone()[0] == 0:
        # Sample data: thickness of 3 famous glaciers over the decades
        sample_data = [
            # Athabasca Glacier (Canada)
            (1950, "Athabasca", 300.0),
            (1960, "Athabasca", 287.0),
            (1970, "Athabasca", 271.0),
            (1980, "Athabasca", 258.0),
            (1990, "Athabasca", 242.0),
            (2000, "Athabasca", 225.0),
            (2010, "Athabasca", 203.0),
            (2020, "Athabasca", 180.0),
            (2023, "Athabasca", 171.0),

            # Rhône Glacier (Switzerland)
            (1950, "Rhône",     250.0),
            (1960, "Rhône",     238.0),
            (1970, "Rhône",     224.0),
            (1980, "Rhône",     210.0),
            (1990, "Rhône",     194.0),
            (2000, "Rhône",     176.0),
            (2010, "Rhône",     154.0),
            (2020, "Rhône",     130.0),
            (2023, "Rhône",     121.0),

            # Perito Moreno (Argentina)
            (1950, "Perito Moreno", 700.0),
            (1960, "Perito Moreno", 695.0),
            (1970, "Perito Moreno", 688.0),
            (1980, "Perito Moreno", 680.0),
            (1990, "Perito Moreno", 673.0),
            (2000, "Perito Moreno", 665.0),
            (2010, "Perito Moreno", 652.0),
            (2020, "Perito Moreno", 640.0),
            (2023, "Perito Moreno", 635.0),
        ]
        cursor.executemany(
            "INSERT INTO glacier_data (year, glacier_name, thickness_meters) VALUES (?, ?, ?)",
            sample_data
        )
        print("✅ Database seeded with sample glacier data.")

    conn.commit()
    conn.close()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/glaciers")
def get_glaciers():
    """Return all glacier data from the database as JSON."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM glacier_data ORDER BY glacier_name, year")
    rows = cursor.fetchall()
    conn.close()

    data = [dict(row) for row in rows]
    return jsonify(data)


if __name__ == "__main__":
    init_db()
    print("🌍 Starting Glacier Tracker...")
    print("👉 Open your browser at: http://localhost:5000")
    app.run(debug=True)
