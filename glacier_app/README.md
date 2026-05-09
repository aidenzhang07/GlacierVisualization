# 🧊 Glacier Thickness Tracker

A beginner-friendly web app with a Python/Flask backend, SQLite database, and a Chart.js line chart.

---

## 📁 Project Structure

```
glacier_app/
├── app.py               ← Backend server (Python + Flask)
├── requirements.txt     ← Python packages needed
├── glacier.db           ← SQLite database (auto-created on first run)
└── templates/
    └── index.html       ← Frontend (HTML + CSS + JavaScript)
```

---

## 🚀 How to Run (Step by Step)

### 1. Make sure Python is installed
Open a terminal and type:
```bash
python --version
```
You should see something like `Python 3.10.x`. If not, download it from https://python.org

### 2. Navigate to the project folder
```bash
cd path/to/glacier_app
```

### 3. Install Flask (one-time setup)
```bash
pip install -r requirements.txt
```

### 4. Run the app
```bash
python app.py
```

You should see:
```
✅ Database seeded with sample glacier data.
🌍 Starting Glacier Tracker...
👉 Open your browser at: http://localhost:5000
```

### 5. Open your browser
Go to: **http://localhost:5000**

---

## 🗄️ How the Database Works

- The database (`glacier.db`) is a **SQLite** file — just a single file on your computer, no installation needed.
- It has one table called `glacier_data` with columns: `year`, `glacier_name`, `thickness_meters`.
- The data is automatically seeded with sample records the first time you run the app.

### To view or edit the database directly:
Download **DB Browser for SQLite** (free): https://sqlitebrowser.org

---

## 🌐 How the Website Works

1. Your browser visits `http://localhost:5000`
2. Flask serves the `index.html` page
3. The page's JavaScript calls `/api/glaciers` (a JSON endpoint)
4. Flask reads from the SQLite database and returns the data as JSON
5. Chart.js draws the line chart using that data

---

## ✏️ How to Add Your Own Data

Open `app.py` and find the `sample_data` list. Add rows like:
```python
(2024, "Athabasca", 168.0),
```

Then delete `glacier.db` and restart the app to reload with fresh data.

---

## 🛠️ Tech Stack

| Layer      | Technology         |
|------------|--------------------|
| Backend    | Python + Flask     |
| Database   | SQLite             |
| Frontend   | HTML + CSS + JS    |
| Chart      | Chart.js           |
| Fonts      | Google Fonts       |
