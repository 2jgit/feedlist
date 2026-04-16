import os
import socket
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, g, redirect, render_template, request, url_for, jsonify, Response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data.db"))
UPDATE_INTERVAL_MINUTES = int(os.environ.get("UPDATE_INTERVAL_MINUTES", "60"))
HOST = os.environ.get("APP_HOST", "0.0.0.0")
PORT = int(os.environ.get("APP_PORT", "8080"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
lock = threading.Lock()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                hostname TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS resolved_ips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_id INTEGER NOT NULL,
                ip_address TEXT NOT NULL,
                resolved_at TEXT NOT NULL,
                UNIQUE(url_id, ip_address),
                FOREIGN KEY(url_id) REFERENCES urls(id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_url(raw_url: str) -> tuple[str, str]:
    value = raw_url.strip()
    if not value:
        raise ValueError("URL is required")

    parsed = urlparse(value)
    if not parsed.scheme:
        value = f"http://{value}"
        parsed = urlparse(value)

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Could not determine a hostname from the URL")

    return value, hostname.lower()


def resolve_hostname(hostname: str) -> list[str]:
    ip_set = set()
    try:
        for family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
            hostname, None, family=socket.AF_INET
        ):
            if family == socket.AF_INET:
                ip_set.add(sockaddr[0])
    except socket.gaierror:
        return []
    return sorted(ip_set)


def update_all_ips():
    with lock:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            urls = cur.execute("SELECT id, hostname FROM urls ORDER BY hostname ASC").fetchall()
            now = utc_now()

            for row in urls:
                url_id = row["id"]
                hostname = row["hostname"]
                ips = resolve_hostname(hostname)

                cur.execute("DELETE FROM resolved_ips WHERE url_id = ?", (url_id,))
                for ip in ips:
                    cur.execute(
                        "INSERT OR IGNORE INTO resolved_ips (url_id, ip_address, resolved_at) VALUES (?, ?, ?)",
                        (url_id, ip, now),
                    )
                cur.execute("UPDATE urls SET updated_at = ? WHERE id = ?", (now, url_id))

            conn.commit()


def get_urls_with_ips():
    db = get_db()
    rows = db.execute(
        """
        SELECT u.id, u.url, u.hostname, u.created_at, u.updated_at,
               GROUP_CONCAT(r.ip_address, ', ') AS ip_list,
               MAX(r.resolved_at) AS last_resolved
        FROM urls u
        LEFT JOIN resolved_ips r ON u.id = r.url_id
        GROUP BY u.id, u.url, u.hostname, u.created_at, u.updated_at
        ORDER BY u.hostname ASC
        """
    ).fetchall()
    return rows


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", items=get_urls_with_ips(), interval=UPDATE_INTERVAL_MINUTES)


@app.route("/add", methods=["POST"])
def add_url():
    raw_url = request.form.get("url", "")
    try:
        normalized_url, hostname = normalize_url(raw_url)
    except ValueError as exc:
        return render_template(
            "index.html",
            items=get_urls_with_ips(),
            interval=UPDATE_INTERVAL_MINUTES,
            error=str(exc),
        ), 400

    db = get_db()
    now = utc_now()
    try:
        db.execute(
            "INSERT INTO urls (url, hostname, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (normalized_url, hostname, now, now),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return render_template(
            "index.html",
            items=get_urls_with_ips(),
            interval=UPDATE_INTERVAL_MINUTES,
            error="That URL is already in the list.",
        ), 409

    update_all_ips()
    return redirect(url_for("index"))


@app.route("/delete/<int:url_id>", methods=["POST"])
def delete_url(url_id: int):
    db = get_db()
    db.execute("DELETE FROM resolved_ips WHERE url_id = ?", (url_id,))
    db.execute("DELETE FROM urls WHERE id = ?", (url_id,))
    db.commit()
    return redirect(url_for("index"))


@app.route("/refresh", methods=["POST"])
def refresh_now():
    update_all_ips()
    return redirect(url_for("index"))


@app.route("/ips", methods=["GET"])
def list_ips():
    db = get_db()
    rows = db.execute(
        """
        SELECT DISTINCT r.ip_address
        FROM resolved_ips r
        JOIN urls u ON u.id = r.url_id
        ORDER BY r.ip_address ASC
        """
    ).fetchall()
    ips = [row["ip_address"] for row in rows]
    body = "\n".join(ips)
    if body:
        body += "\n"
    return Response(body, mimetype="text/plain")


@app.route("/api/ips", methods=["GET"])
def api_ips():
    db = get_db()
    rows = db.execute(
        """
        SELECT u.url, u.hostname, r.ip_address, r.resolved_at
        FROM urls u
        LEFT JOIN resolved_ips r ON u.id = r.url_id
        ORDER BY u.hostname ASC, r.ip_address ASC
        """
    ).fetchall()
    return jsonify([
        {
            "url": row["url"],
            "hostname": row["hostname"],
            "ip_address": row["ip_address"],
            "resolved_at": row["resolved_at"],
        }
        for row in rows
    ])


def start_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        update_all_ips,
        "interval",
        minutes=UPDATE_INTERVAL_MINUTES,
        id="refresh_ips",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler


if __name__ == "__main__":
    init_db()
    update_all_ips()
    start_scheduler()
    app.run(host=HOST, port=PORT)
else:
    init_db()
    update_all_ips()
    start_scheduler()