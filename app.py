import os
import hmac
import hashlib
from functools import wraps

import psycopg2
import requests
from flask import Flask, request, session, redirect, render_template_string

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "iace_verify_123")
WA_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")  # optional but recommended

# ---------------- COURSES (from the IACE fee sheet) ----------------
COURSES = [
    ("Banking Life", 27000),
    ("SSC Life", 29500),
    ("RRB NonTech Life", 28000),
    ("RRB NonTech 1 Year", 24000),
    ("ITP (Bank+SSC) Life", 35500),
    ("Bank+RRB (NT) Life", 35500),
    ("SSC+RRB (NT) Life", 35500),
    ("Bank+SSC+RRB (NT) Life", 39000),
    ("SSC GD Constable 1 Year", 22000),
    ("SSC GD Constable Life", 26000),
    ("RRB Group D-1 Notification", 22000),
    ("RRB JE+SSC JE (T+NT)", 36000),
    ("RRB ALP (T+NT)", 29500),
    ("RRB JE CBT-2 (T)", 27000),
    ("RRB JE+SSC JE (T)", 27000),
    ("AP SI/PC-1 Notification", 24000),
    ("TG SI/PC-1 Notification", 24000),
    ("RRB ALP CBT-2 (T)", 16000),
]


def menu_text():
    lines = ["*Welcome to IACE* \U0001F64F", "Reply with a number to know the fee:\n"]
    for i, (name, _) in enumerate(COURSES, 1):
        lines.append(f"{i}. {name}")
    lines.append("\nReply *menu* anytime to see this list again.")
    return "\n".join(lines)


def reply_for(text):
    t = (text or "").strip()
    if t.isdigit() and 1 <= int(t) <= len(COURSES):
        name, fee = COURSES[int(t) - 1]
        return (
            f"*{name}*\nFee: \u20B9{fee:,}\n\n"
            "Reply *menu* to see all courses."
        )
    return menu_text()


# ---------------- DATABASE ----------------
def q(sql, params=(), fetch=False):
    # connect_timeout stops a dead database from freezing the request
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
    finally:
        conn.close()


def init_db():
    q(
        """CREATE TABLE IF NOT EXISTS messages(
            id SERIAL PRIMARY KEY,
            phone TEXT, name TEXT, direction TEXT, text TEXT,
            created_at TIMESTAMPTZ DEFAULT now())"""
    )


def log(phone, name, direction, text):
    """Save a message. A database problem must NEVER stop the bot from replying."""
    try:
        q(
            "INSERT INTO messages(phone,name,direction,text) VALUES (%s,%s,%s,%s)",
            (phone, name, direction, text),
        )
    except Exception as e:
        print("log failed:", e)


# ---------------- WHATSAPP ----------------
def send(to, text):
    if not WA_TOKEN or not PHONE_ID:
        print("send skipped: WHATSAPP_TOKEN or PHONE_NUMBER_ID is not set")
        return
    try:
        r = requests.post(
            f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text[:4000]},
            },
            timeout=15,
        )
        if r.status_code >= 400:
            # shows expired token (401), recipient not allowed (131030), etc.
            print("send failed:", r.status_code, r.text[:300])
    except Exception as e:
        print("send exception:", type(e).__name__)
    log(to, None, "out", text)


def signature_ok(raw_body):
    """If META_APP_SECRET is set, only accept requests really signed by Meta."""
    if not META_APP_SECRET:
        return True
    header = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header, expected)


@app.route("/")
def home():
    return "IACE bot is running"


@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token", "")
    if request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(
        token.encode(), VERIFY_TOKEN.encode()
    ):
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data()
    if not signature_ok(raw):
        print("webhook rejected: bad signature")
        return "Forbidden", 403

    data = request.get_json(silent=True) or {}
    try:
        value = data["entry"][0]["changes"][0]["value"]
        msgs = value.get("messages")
        if msgs:
            m = msgs[0]
            phone = m["from"]
            name = (value.get("contacts") or [{}])[0].get("profile", {}).get("name")
            text = m.get("text", {}).get("body", "[non-text message]")
            send(phone, reply_for(text))   # reply FIRST
            log(phone, name, "in", text)   # then save
    except Exception as e:
        print("webhook error:", e)
    return "ok", 200


# ---------------- ADMIN DASHBOARD ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("ok"):
            return redirect("/login")
        return f(*a, **kw)

    return wrapper


LOGIN_HTML = """
<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<body style="font-family:sans-serif;max-width:320px;margin:80px auto">
<h2>IACE Admin</h2>
<form method=post>
<input type=password name=password placeholder=Password style="width:100%;padding:10px">
<button style="margin-top:10px;padding:10px;width:100%">Login</button>
</form>{{ err }}</body>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        sent = request.form.get("password", "")
        if hmac.compare_digest(sent.encode(), ADMIN_PASSWORD.encode()):
            session["ok"] = True
            return redirect("/admin")
        return render_template_string(LOGIN_HTML, err="Wrong password")
    return render_template_string(LOGIN_HTML, err="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


DASH_HTML = """
<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>IACE Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
body{font-family:sans-serif;margin:0;background:#f4f6f8}
.wrap{max-width:1000px;margin:auto;padding:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.card{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 3px #0002}
.card h1{margin:0;font-size:34px}.card p{margin:4px 0 0;color:#666}
.box{background:#fff;border-radius:10px;padding:16px;margin-top:16px;box-shadow:0 1px 3px #0002;overflow-x:auto}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px;border-bottom:1px solid #eee;font-size:14px}
</style>
<div class=wrap>
<div style="display:flex;justify-content:space-between"><h2>IACE WhatsApp Dashboard</h2><a href="/logout">Logout</a></div>
<div class=cards>
 <div class=card><h1>{{ today_msgs }}</h1><p>Messages today</p></div>
 <div class=card><h1>{{ today_users }}</h1><p>People messaged today</p></div>
 <div class=card><h1>{{ total_users }}</h1><p>Total people ever</p></div>
 <div class=card><h1>{{ total_msgs }}</h1><p>Total messages received</p></div>
</div>
<div class=box><h3>Messages - last 7 days</h3><canvas id=c height=100></canvas></div>
<div class=box><h3>Most requested courses</h3>
<table>{% for n,c in top %}<tr><td>{{ n }}</td><td>{{ c }}</td></tr>{% endfor %}</table></div>
<div class=box><h3>Recent messages</h3>
<table><tr><th>Time (IST)</th><th>Phone</th><th>Name</th><th>Message</th></tr>
{% for r in recent %}<tr><td>{{ r[0] }}</td><td>{{ r[1] }}</td><td>{{ r[2] or '' }}</td><td>{{ r[3] }}</td></tr>{% endfor %}
</table></div>
</div>
<script>
new Chart(document.getElementById('c'),{type:'bar',
data:{labels:{{ labels|tojson }},datasets:[{label:'Messages',data:{{ values|tojson }},backgroundColor:'#25D366'}]},
options:{plugins:{legend:{display:false}}}});
</script>
"""

DB_ERROR_HTML = """
<!doctype html><body style="font-family:sans-serif;max-width:420px;margin:80px auto">
<h3>Database not reachable</h3>
<p>Check the DATABASE_URL setting in Render, then refresh.</p>
<p><a href="/logout">Logout</a></p></body>
"""

IST = "(created_at AT TIME ZONE 'Asia/Kolkata')"


@app.route("/admin")
@login_required
def admin():
    try:
        today_msgs = q(
            f"SELECT count(*) FROM messages WHERE direction='in' AND {IST}::date=(now() AT TIME ZONE 'Asia/Kolkata')::date",
            fetch=True,
        )[0][0]
        today_users = q(
            f"SELECT count(DISTINCT phone) FROM messages WHERE direction='in' AND {IST}::date=(now() AT TIME ZONE 'Asia/Kolkata')::date",
            fetch=True,
        )[0][0]
        total_users = q(
            "SELECT count(DISTINCT phone) FROM messages WHERE direction='in'",
            fetch=True,
        )[0][0]
        total_msgs = q(
            "SELECT count(*) FROM messages WHERE direction='in'", fetch=True
        )[0][0]
        days = q(
            f"""SELECT to_char({IST}::date,'DD Mon'), count(*) FROM messages
                WHERE direction='in' AND created_at > now() - interval '7 days'
                GROUP BY {IST}::date, 1 ORDER BY {IST}::date""",
            fetch=True,
        )
        top_raw = q(
            """SELECT text, count(*) FROM messages
               WHERE direction='in' AND text ~ '^[0-9]{1,2}$'
               GROUP BY text ORDER BY 2 DESC LIMIT 5""",
            fetch=True,
        )
        top = []
        for t, c in top_raw:
            i = int(t)
            if 1 <= i <= len(COURSES):
                top.append((COURSES[i - 1][0], c))
        recent = q(
            f"""SELECT to_char({IST},'DD Mon HH24:MI'), phone, name, text
                FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 20""",
            fetch=True,
        )
    except Exception as e:
        print("admin db error:", e)
        return render_template_string(DB_ERROR_HTML), 503

    return render_template_string(
        DASH_HTML,
        today_msgs=today_msgs,
        today_users=today_users,
        total_users=total_users,
        total_msgs=total_msgs,
        labels=[d[0] for d in days],
        values=[d[1] for d in days],
        top=top,
        recent=recent,
    )


try:
    init_db()
except Exception as e:
    print("DB init failed:", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
