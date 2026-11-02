import hashlib
import hmac
import io
import os
from functools import wraps

import psycopg2
import requests
from flask import (Flask, redirect, render_template_string, request,
                   send_file, session)
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "iace_verify_123")
WA_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID", "")
APP_SECRET = os.environ.get("APP_SECRET") or os.environ.get("META_APP_SECRET", "")  # signature check
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
COUNSELLOR_PHONE = os.environ.get("COUNSELLOR_PHONE", "")  # optional, e.g. 919876543210

# ---------------- COURSES: category -> [(name, fee)] ----------------
CATEGORIES = [
    ("Bank & SSC", [("Banking Life", 27000), ("SSC Life", 29500),
                    ("ITP (Bank+SSC) Life", 35500)]),
    ("RRB Railways", [("RRB NonTech Life", 28000), ("RRB NonTech 1 Year", 24000),
                      ("RRB Group D-1 Notification", 22000), ("RRB ALP (T+NT)", 29500),
                      ("RRB ALP CBT-2 (T)", 16000)]),
    ("Combo Courses", [("Bank+RRB (NT) Life", 35500), ("SSC+RRB (NT) Life", 35500),
                       ("Bank+SSC+RRB (NT) Life", 39000)]),
    ("JE Technical", [("RRB JE+SSC JE (T+NT)", 36000), ("RRB JE CBT-2 (T)", 27000),
                      ("RRB JE+SSC JE (T)", 27000)]),
    ("Police & Constable", [("SSC GD Constable 1 Year", 22000),
                            ("SSC GD Constable Life", 26000),
                            ("AP SI/PC-1 Notification", 24000),
                            ("TG SI/PC-1 Notification", 24000)]),
]
# flat list with a stable id for each course
COURSES = [(n, f, ci) for ci, (_, items) in enumerate(CATEGORIES) for n, f in items]


# ---------------- DATABASE ----------------
def q(sql, params=(), fetch=False):
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)  # never hang on a dead DB
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        conn.close()


def init_db():
    q("""CREATE TABLE IF NOT EXISTS messages(
            id SERIAL PRIMARY KEY, phone TEXT, name TEXT, direction TEXT,
            text TEXT, created_at TIMESTAMPTZ DEFAULT now())""")
    q("ALTER TABLE messages ADD COLUMN IF NOT EXISTS course TEXT")
    q("ALTER TABLE messages ADD COLUMN IF NOT EXISTS wamid TEXT")
    q("CREATE UNIQUE INDEX IF NOT EXISTS messages_wamid_idx ON messages(wamid)")
    q("""CREATE TABLE IF NOT EXISTS leads(
            phone TEXT PRIMARY KEY, name TEXT, last_course TEXT,
            status TEXT DEFAULT 'new', msg_count INT DEFAULT 0,
            first_seen TIMESTAMPTZ DEFAULT now(), last_seen TIMESTAMPTZ DEFAULT now())""")


def log(phone, name, direction, text, course=None, wamid=None):
    """Save a message. Returns False only for a duplicate (already stored).
    A database problem must NEVER stop the bot from replying, so errors return True."""
    try:
        n = q("""INSERT INTO messages(phone,name,direction,text,course,wamid)
                 VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (wamid) DO NOTHING""",
              (phone, name, direction, text, course, wamid))
        return n > 0
    except Exception as e:
        print("log failed:", e)
        return True


def touch_lead(phone, name):
    try:
        q("""INSERT INTO leads(phone,name,msg_count) VALUES (%s,%s,1)
             ON CONFLICT (phone) DO UPDATE SET last_seen=now(),
               name=COALESCE(EXCLUDED.name, leads.name), msg_count=leads.msg_count+1""",
          (phone, name))
    except Exception as e:
        print("touch_lead failed:", e)


# ---------------- WHATSAPP SENDING ----------------
def wa_post(payload):
    try:
        r = requests.post(
            f"https://graph.facebook.com/v21.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
            json={"messaging_product": "whatsapp", **payload}, timeout=15)
        if r.status_code >= 300:
            print("WA error:", r.status_code, r.text)
    except Exception as e:
        print("WA exception:", e)


def send_text(to, text):
    wa_post({"to": to, "type": "text", "text": {"body": text}})
    log(to, None, "out", text)


def send_list(to, body, button, rows, section="Options"):
    """rows = [(id, title, description)]; WhatsApp limits: 10 rows, title 24 chars."""
    rows = [{"id": i, "title": t[:24], "description": d[:72]} for i, t, d in rows]
    wa_post({"to": to, "type": "interactive", "interactive": {
        "type": "list", "body": {"text": body},
        "action": {"button": button[:20], "sections": [{"title": section, "rows": rows}]}}})
    log(to, None, "out", f"[list] {body[:80]}")


def send_buttons(to, body, buttons):
    """buttons = [(id, title)]; WhatsApp limits: 3 buttons, title 20 chars."""
    wa_post({"to": to, "type": "interactive", "interactive": {
        "type": "button", "body": {"text": body},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}}
                               for i, t in buttons]}}})
    log(to, None, "out", f"[buttons] {body[:80]}")


# ---------------- BOT FLOW ----------------
def show_main_menu(to):
    rows = [(f"cat_{i}", c, f"{len(items)} courses") for i, (c, items) in enumerate(CATEGORIES)]
    rows.append(("counsellor", "Talk to counsellor", "Get a call from our team"))
    send_list(to, "*Welcome to IACE* \U0001F64F\nChoose a category to see course fees.",
              "View Categories", rows, "Categories")


def show_category(to, ci):
    name, items = CATEGORIES[ci]
    rows = []
    for n, f in items:
        idx = next(k for k, c in enumerate(COURSES) if c[0] == n)
        rows.append((f"course_{idx}", n, f"Fee: \u20B9{f:,}"))
    send_list(to, f"*{name}* - pick a course:", "View Courses", rows, name)


def show_course(to, idx):
    n, f, _ = COURSES[idx]
    send_buttons(to, f"*{n}*\nFee: \u20B9{f:,}\n\nWhat next?",
                 [(f"enroll_{idx}", "I'm interested"), ("menu", "Other courses")])


def mark_interested(phone, course):
    try:
        _mark_interested(phone, course)
    except Exception as e:
        print("mark_interested failed:", e)


def _mark_interested(phone, course):
    q("""UPDATE leads SET status='interested', last_course=COALESCE(%s, last_course)
         WHERE phone=%s AND status='new'""", (course, phone))
    q("UPDATE leads SET last_course=COALESCE(%s,last_course) WHERE phone=%s", (course, phone))


def handle(phone, name, value):
    v = (value or "").strip()
    low = v.lower()
    if v.startswith("cat_") and v[4:].isdigit() and int(v[4:]) < len(CATEGORIES):
        show_category(phone, int(v[4:]))
    elif v.startswith("course_") and v[7:].isdigit() and int(v[7:]) < len(COURSES):
        show_course(phone, int(v[7:]))
    elif v.startswith("enroll_") and v[7:].isdigit() and int(v[7:]) < len(COURSES):
        course = COURSES[int(v[7:])][0]
        mark_interested(phone, course)
        send_text(phone, f"Thank you! \U0001F64F Our counsellor will contact you soon about *{course}*.\n\nReply *menu* for more courses.")
        notify_counsellor(phone, name, course)
    elif v == "counsellor":
        mark_interested(phone, None)
        send_text(phone, "Thank you! \U0001F64F Our counsellor will call you shortly.\n\nReply *menu* anytime.")
        notify_counsellor(phone, name, "General enquiry")
    else:
        show_main_menu(phone)  # menu, hi, anything unknown


def notify_counsellor(phone, name, course):
    if COUNSELLOR_PHONE:
        send_text(COUNSELLOR_PHONE, f"New lead: {name or 'Unknown'} ({phone})\nInterest: {course}")


def course_from_value(v):
    if v and v.startswith("course_") and v[7:].isdigit() and int(v[7:]) < len(COURSES):
        return COURSES[int(v[7:])][0]
    return None


# ---------------- WEBHOOK ----------------
def valid_signature():
    if not APP_SECRET:
        return True
    sent = request.headers.get("X-Hub-Signature-256", "")
    good = "sha256=" + hmac.new(APP_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, good)


@app.route("/")
def home():
    return "IACE bot is running"


@app.route("/webhook", methods=["GET"])
def verify():
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    if not valid_signature():
        return "Bad signature", 403
    data = request.get_json(silent=True) or {}
    try:
        value = data["entry"][0]["changes"][0]["value"]
        msgs = value.get("messages")
        if msgs:
            m = msgs[0]
            phone = m["from"]
            name = (value.get("contacts") or [{}])[0].get("profile", {}).get("name")
            kind = m.get("type")
            if kind == "text":
                val = m["text"]["body"]
            elif kind == "interactive":
                it = m["interactive"]
                val = (it.get("list_reply") or it.get("button_reply") or {}).get("id", "")
            else:
                val = ""
            shown = val if kind != "interactive" else f"[tap] {val}"
            fresh = log(phone, name, "in", shown or f"[{kind}]",
                        course_from_value(val), m.get("id"))
            if fresh:                      # ignore Meta's duplicate retries
                touch_lead(phone, name)
                handle(phone, name, val)
    except Exception as e:
        print("webhook error:", e)
    return "ok", 200


# ---------------- ADMIN ----------------
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
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["ok"] = True
            return redirect("/admin")
        return render_template_string(LOGIN_HTML, err="Wrong password")
    return render_template_string(LOGIN_HTML, err="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.errorhandler(psycopg2.Error)
def db_down(e):
    print("admin db error:", e)
    return ("<h3>Database not reachable</h3><p>Check DATABASE_URL in Render, then refresh.</p>"
            '<p><a href="/logout">Logout</a></p>'), 503


IST = "(created_at AT TIME ZONE 'Asia/Kolkata')"
TODAY = "(now() AT TIME ZONE 'Asia/Kolkata')::date"
STATUSES = ["new", "interested", "contacted", "joined", "not interested"]

DASH_HTML = """
<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>IACE Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
body{font-family:sans-serif;margin:0;background:#f4f6f8}
.wrap{max-width:1100px;margin:auto;padding:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 3px #0002}
.card h1{margin:0;font-size:32px}.card p{margin:4px 0 0;color:#666}
.box{background:#fff;border-radius:10px;padding:16px;margin-top:16px;box-shadow:0 1px 3px #0002;overflow-x:auto}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px;border-bottom:1px solid #eee;font-size:14px}
.btn{background:#25D366;color:#fff;padding:8px 14px;border-radius:6px;text-decoration:none;font-size:14px;border:0;cursor:pointer}
.pill{padding:4px 10px;border-radius:14px;background:#e5e7eb;text-decoration:none;color:#111;font-size:13px}
.pill.on{background:#111;color:#fff}
.bar{background:#25D366;height:10px;border-radius:5px}
</style>
<div class=wrap>
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
 <h2>IACE WhatsApp Dashboard</h2>
 <div><a class=btn href="/admin/export.xlsx">Download Excel</a> &nbsp; <a href="/logout">Logout</a></div></div>
<div class=cards>
 <div class=card><h1>{{ s.today_msgs }}</h1><p>Messages today</p></div>
 <div class=card><h1>{{ s.today_users }}</h1><p>People today</p></div>
 <div class=card><h1>{{ s.week_msgs }}</h1><p>Messages (7 days)</p></div>
 <div class=card><h1>{{ s.week_users }}</h1><p>People (7 days)</p></div>
 <div class=card><h1>{{ s.total_users }}</h1><p>Total leads</p></div>
 <div class=card><h1>{{ s.hot }}</h1><p>Interested leads</p></div>
</div>
<div class=box><h3>Messages - last 7 days</h3><canvas id=c height=90></canvas></div>
<div class=box><h3>Most requested courses</h3>
 <p>Period:
 {% for d,l in [(1,'Today'),(7,'7 days'),(30,'30 days'),(0,'All time')] %}
  <a class="pill {{ 'on' if d==days }}" href="/admin?days={{ d }}">{{ l }}</a>
 {% endfor %}</p>
 <table>{% for n,c in top %}<tr><td style="width:40%">{{ n }}</td>
 <td><div class=bar style="width:{{ (c/top[0][1]*100)|int }}%"></div></td><td>{{ c }}</td></tr>
 {% else %}<tr><td>No data yet</td></tr>{% endfor %}</table></div>
<div class=box><h3>Leads</h3>
 <form method=get style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
  <input type=hidden name=days value="{{ days }}">
  <input name=search value="{{ search }}" placeholder="Search name / phone" style="padding:8px">
  <select name=status style="padding:8px"><option value="">All status</option>
   {% for st in statuses %}<option {{ 'selected' if st==status }}>{{ st }}</option>{% endfor %}</select>
  <button class=btn>Filter</button></form>
 <table><tr><th>Name</th><th>Phone</th><th>Last course</th><th>Msgs</th><th>Last seen (IST)</th><th>Status</th></tr>
 {% for l in leads %}<tr><td>{{ l[1] or '' }}</td><td>{{ l[0] }}</td><td>{{ l[2] or '' }}</td><td>{{ l[4] }}</td><td>{{ l[6] }}</td>
 <td><form method=post action="/admin/lead/{{ l[0] }}" style="margin:0">
  <select name=status onchange="this.form.submit()">
  {% for st in statuses %}<option {{ 'selected' if st==l[3] }}>{{ st }}</option>{% endfor %}</select></form></td></tr>{% endfor %}
 </table></div>
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


def top_courses(days):
    days = days if days > 0 else 36500
    return q("""SELECT course, count(*) FROM messages
                WHERE direction='in' AND course IS NOT NULL
                  AND created_at > now() - make_interval(days => %s)
                GROUP BY course ORDER BY 2 DESC LIMIT 10""", (days,), fetch=True)


@app.route("/admin")
@login_required
def admin():
    days = request.args.get("days", "7")
    days = int(days) if days.isdigit() else 7
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "")
    inn = "direction='in'"
    one = lambda sql, p=(): q(sql, p, fetch=True)[0][0]
    s = {
        "today_msgs": one(f"SELECT count(*) FROM messages WHERE {inn} AND {IST}::date={TODAY}"),
        "today_users": one(f"SELECT count(DISTINCT phone) FROM messages WHERE {inn} AND {IST}::date={TODAY}"),
        "week_msgs": one(f"SELECT count(*) FROM messages WHERE {inn} AND created_at > now()-interval '7 days'"),
        "week_users": one(f"SELECT count(DISTINCT phone) FROM messages WHERE {inn} AND created_at > now()-interval '7 days'"),
        "total_users": one("SELECT count(*) FROM leads"),
        "hot": one("SELECT count(*) FROM leads WHERE status='interested'"),
    }
    chart = q(f"""SELECT to_char(d::date,'DD Mon'), COALESCE(m.c,0)
                  FROM generate_series({TODAY}-6, {TODAY}, interval '1 day') d
                  LEFT JOIN (SELECT {IST}::date day, count(*) c FROM messages
                             WHERE {inn} GROUP BY 1) m ON m.day=d::date ORDER BY d""", fetch=True)
    like = f"%{search}%"
    leads = q("""SELECT phone,name,last_course,status,msg_count,
                        to_char(first_seen AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI'),
                        to_char(last_seen AT TIME ZONE 'Asia/Kolkata','DD Mon HH24:MI')
                 FROM leads WHERE (%s='' OR phone ILIKE %s OR name ILIKE %s)
                   AND (%s='' OR status=%s) ORDER BY last_seen DESC LIMIT 200""",
              (search, like, like, status, status), fetch=True)
    recent = q(f"""SELECT to_char({IST},'DD Mon HH24:MI'), phone, name, text
                   FROM messages WHERE {inn} ORDER BY id DESC LIMIT 20""", fetch=True)
    return render_template_string(
        DASH_HTML, s=s, days=days, top=top_courses(days), leads=leads, recent=recent,
        labels=[r[0] for r in chart], values=[r[1] for r in chart],
        search=search, status=status, statuses=STATUSES)


@app.route("/admin/lead/<phone>", methods=["POST"])
@login_required
def set_status(phone):
    st = request.form.get("status")
    if st in STATUSES:
        q("UPDATE leads SET status=%s WHERE phone=%s", (st, phone))
    return redirect(request.referrer or "/admin")


@app.route("/admin/export.xlsx")
@login_required
def export_excel():
    wb = Workbook()

    def sheet(ws, title, header, rows):
        ws.title = title
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="25D366")
        for r in rows:
            ws.append(list(r))
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(
                max(len(str(c.value or "")) for c in col) + 2, 50)
        ws.freeze_panes = "A2"

    sheet(wb.active, "Leads",
          ["Phone", "Name", "Last Course", "Status", "Messages", "First Seen (IST)", "Last Seen (IST)"],
          q("""SELECT phone,name,last_course,status,msg_count,
                      to_char(first_seen AT TIME ZONE 'Asia/Kolkata','YYYY-MM-DD HH24:MI'),
                      to_char(last_seen AT TIME ZONE 'Asia/Kolkata','YYYY-MM-DD HH24:MI')
               FROM leads ORDER BY last_seen DESC""", fetch=True))
    sheet(wb.create_sheet(), "Course Interest", ["Course", "Times viewed"],
          top_courses(0))
    sheet(wb.create_sheet(), "Messages",
          ["Time (IST)", "Phone", "Name", "Direction", "Message", "Course"],
          q(f"""SELECT to_char({IST},'YYYY-MM-DD HH24:MI'),phone,name,direction,text,course
                FROM messages ORDER BY id DESC LIMIT 20000""", fetch=True))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="iace_leads.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


try:
    init_db()
except Exception as e:
    print("DB init failed:", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
