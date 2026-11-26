"""
CV Pro Yemen - Telegram bot (webhook mode, Flask).
Free stack: Render (hosting) + Google Gemini (free API tier) + fpdf2 (PDF).
Env vars: BOT_TOKEN, GEMINI_API_KEY  (optional: GEMINI_MODEL)
"""
import os, io, json, re, hashlib, threading, time, tempfile
import requests
from flask import Flask, request
from fpdf import FPDF

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL", "")

app = Flask(__name__)
STATE = {}  # chat_id -> {"lang","step","data"}
LOCK = threading.Lock()

# ---------------------------------------------------------------- questions
STEPS = ["name", "title", "phone", "email", "city",
         "summary", "experience", "education", "skills", "languages"]

Q = {
    "ar": {
        "name": "١/١٠ - ما اسمك الكامل؟",
        "title": "٢/١٠ - ما المسمى الوظيفي الذي تستهدفه؟ (مثال: محاسب، مهندس مدني)",
        "phone": "٣/١٠ - رقم هاتفك؟",
        "email": "٤/١٠ - بريدك الإلكتروني؟ (أو أرسل - للتخطي)",
        "city": "٥/١٠ - المدينة والبلد؟ (مثال: صنعاء، اليمن)",
        "summary": "٦/١٠ - اكتب نبذة قصيرة عن نفسك وأهدافك (أو أرسل - ليكتبها الذكاء الاصطناعي).",
        "experience": "٧/١٠ - خبراتك العملية: الوظيفة، الجهة، المدة، وماذا أنجزت. اكتبها بحرية (أو - إن لم تكن لديك خبرة).",
        "education": "٨/١٠ - مؤهلاتك الدراسية: التخصص، الجامعة/المعهد، السنة.",
        "skills": "٩/١٠ - مهاراتك (افصل بينها بفاصلة).",
        "languages": "١٠/١٠ - اللغات التي تتقنها ومستواك فيها.",
    },
    "en": {
        "name": "1/10 - What is your full name?",
        "title": "2/10 - Which job title are you targeting? (e.g. Accountant, Civil Engineer)",
        "phone": "3/10 - Your phone number?",
        "email": "4/10 - Your email? (or send - to skip)",
        "city": "5/10 - City and country? (e.g. Sana'a, Yemen)",
        "summary": "6/10 - Short intro about you and your goals (or send - and AI will write it).",
        "experience": "7/10 - Work experience: job, employer, period, achievements. Write freely (or - if none).",
        "education": "8/10 - Education: major, university/institute, year.",
        "skills": "9/10 - Your skills (comma separated).",
        "languages": "10/10 - Languages you speak and your level.",
    },
}

MSG = {
    "ar": {
        "start": "أهلاً بك في CV Pro Yemen 📄\nسأنشئ لك سيرة ذاتية احترافية بصيغة PDF خلال دقيقتين.\nاختر لغة السيرة الذاتية:",
        "working": "⏳ جارٍ كتابة سيرتك الذاتية وتصميم الملف... قد يستغرق ذلك نصف دقيقة.",
        "done": "✅ تم! هذه سيرتك الذاتية. لإنشاء سيرة جديدة أرسل /start",
        "fail": "❌ حدث خطأ أثناء الإنشاء. أرسل /start وحاول مرة أخرى.",
        "none": "أرسل /start لبدء إنشاء سيرة ذاتية.",
        "cancel": "تم الإلغاء. أرسل /start للبدء من جديد.",
    },
    "en": {
        "start": "",
        "working": "⏳ Writing your CV and designing the PDF... this may take ~30 seconds.",
        "done": "✅ Done! Here is your CV. Send /start to create a new one.",
        "fail": "❌ Something went wrong. Send /start and try again.",
        "none": "Send /start to create a CV.",
        "cancel": "Cancelled. Send /start to begin again.",
    },
}

# ---------------------------------------------------------------- telegram
def tg(method, **kw):
    try:
        return requests.post(f"{API}/{method}", json=kw, timeout=30).json()
    except Exception as e:
        print("tg error", method, e)


def send(chat_id, text, **kw):
    return tg("sendMessage", chat_id=chat_id, text=text, **kw)


def send_pdf(chat_id, path, filename, caption):
    with open(path, "rb") as f:
        requests.post(f"{API}/sendDocument",
                      data={"chat_id": chat_id, "caption": caption},
                      files={"document": (filename, f, "application/pdf")},
                      timeout=120)

# ---------------------------------------------------------------- AI
def gemini_cv(d, lang):
    """Return structured CV dict written professionally in `lang`."""
    language = "Arabic (Modern Standard Arabic)" if lang == "ar" else "English"
    prompt = f"""You are an expert CV writer. Using the candidate data below, write a professional CV in {language}.
Rules: translate user text to {language} if needed; never invent employers, degrees, dates or numbers that were not provided;
you may improve wording and write action-oriented bullet points; if no summary is given write a concise 2-3 sentence one based on the data;
if experience is empty return an empty list. Keep names, phone and email unchanged.
Return ONLY valid JSON with this schema:
{{"summary": "string",
 "experience": [{{"role":"","company":"","period":"","bullets":["",""]}}],
 "education": [{{"degree":"","school":"","period":""}}],
 "skills": ["",""],
 "languages": ["",""]}}

Candidate data:
Name: {d.get('name')}
Target job title: {d.get('title')}
Intro: {d.get('summary')}
Experience: {d.get('experience')}
Education: {d.get('education')}
Skills: {d.get('skills')}
Languages: {d.get('languages')}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
    for attempt in range(3):
        try:
            r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=60)
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            return json.loads(text)
        except Exception as e:
            print("gemini error", attempt, e)
            time.sleep(2)
    return None


def fallback_cv(d):
    skip = lambda v: "" if (not v or v.strip() == "-") else v.strip()
    return {
        "summary": skip(d.get("summary")),
        "experience": [{"role": skip(d.get("experience")), "company": "", "period": "", "bullets": []}]
        if skip(d.get("experience")) else [],
        "education": [{"degree": skip(d.get("education")), "school": "", "period": ""}]
        if skip(d.get("education")) else [],
        "skills": [s.strip() for s in re.split(r"[,،\n]", skip(d.get("skills"))) if s.strip()],
        "languages": [s.strip() for s in re.split(r"[,،\n]", skip(d.get("languages"))) if s.strip()],
    }

# ---------------------------------------------------------------- fonts / PDF
FONT_DIR = os.path.join(tempfile.gettempdir(), "cvfonts")
FONTS = {
    "Amiri-Regular.ttf": "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf",
    "Amiri-Bold.ttf": "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Bold.ttf",
}


def ensure_fonts():
    os.makedirs(FONT_DIR, exist_ok=True)
    for name, url in FONTS.items():
        p = os.path.join(FONT_DIR, name)
        if not os.path.exists(p) or os.path.getsize(p) < 10000:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            with open(p, "wb") as f:
                f.write(r.content)
    return (os.path.join(FONT_DIR, "Amiri-Regular.ttf"),
            os.path.join(FONT_DIR, "Amiri-Bold.ttf"))


LABELS = {
    "ar": {"summary": "الملخص المهني", "experience": "الخبرات العملية",
           "education": "التعليم", "skills": "المهارات", "languages": "اللغات"},
    "en": {"summary": "Professional Summary", "experience": "Work Experience",
           "education": "Education", "skills": "Skills", "languages": "Languages"},
}
NAVY = (18, 62, 94)
GRAY = (100, 100, 100)
DARK = (30, 30, 30)


def build_pdf(cv, d, lang, out_path):
    reg, bold = ensure_fonts()
    rtl = lang == "ar"
    al = "R" if rtl else "L"
    L = LABELS[lang]
    pdf = FPDF(format="A4")
    pdf.set_margins(16, 14, 16)
    pdf.set_auto_page_break(True, 14)
    pdf.add_page()
    pdf.add_font("F", "", reg)
    pdf.add_font("F", "B", bold)
    pdf.set_font("F", "", 12)
    pdf.set_text_shaping(True)

    def line(h, text, size=12, style="", color=DARK):
        pdf.set_font("F", style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, h, text, align=al, new_x="LMARGIN", new_y="NEXT")

    # header band
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 44, "F")
    pdf.set_y(9)
    line(12, d["name"], 26, "B", (255, 255, 255))
    line(8, d["title"], 15, "", (210, 225, 240))
    contact = " | ".join(x for x in [d.get("phone"), d.get("email") if d.get("email", "-") != "-" else "", d.get("city")] if x)
    line(7, contact, 11, "", (255, 255, 255))
    pdf.set_y(52)

    def heading(text):
        pdf.ln(3)
        line(8, text, 15, "B", NAVY)
        y = pdf.get_y()
        pdf.set_draw_color(*NAVY)
        pdf.set_line_width(0.5)
        pdf.line(16, y, 194, y)
        pdf.ln(2)

    def bullets(items):
        for b in items:
            if b and str(b).strip():
                line(6, "• " + str(b).strip(), 12)

    if cv.get("summary"):
        heading(L["summary"])
        line(6.5, cv["summary"], 12)

    if cv.get("experience"):
        heading(L["experience"])
        for e in cv["experience"]:
            head = " - ".join(x for x in [e.get("role"), e.get("company")] if x)
            if head:
                line(7, head, 13, "B")
            if e.get("period"):
                line(6, e["period"], 11, "", GRAY)
            bullets(e.get("bullets") or [])
            pdf.ln(2)

    if cv.get("education"):
        heading(L["education"])
        for e in cv["education"]:
            head = " - ".join(x for x in [e.get("degree"), e.get("school")] if x)
            if head:
                line(7, head, 13, "B")
            if e.get("period"):
                line(6, e["period"], 11, "", GRAY)
            pdf.ln(1)

    if cv.get("skills"):
        heading(L["skills"])
        bullets(cv["skills"])

    if cv.get("languages"):
        heading(L["languages"])
        bullets(cv["languages"])

    pdf.output(out_path)

# ---------------------------------------------------------------- flow
def generate_and_send(chat_id, lang, d):
    try:
        cv = (gemini_cv(d, lang) if GEMINI_KEY else None) or fallback_cv(d)
        safe = re.sub(r"[^\w\-]+", "_", d["name"], flags=re.U)[:40] or "CV"
        path = os.path.join(tempfile.gettempdir(), f"CV_{chat_id}_{int(time.time())}.pdf")
        build_pdf(cv, d, lang, path)
        send_pdf(chat_id, path, f"CV_{safe}.pdf", MSG[lang]["done"])
        os.remove(path)
    except Exception as e:
        print("generate error", repr(e))
        send(chat_id, MSG[lang]["fail"])
    finally:
        with LOCK:
            STATE.pop(chat_id, None)


def ask(chat_id, lang, step):
    send(chat_id, Q[lang][STEPS[step]])


def handle_message(m):
    chat_id = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    if text.startswith("/start"):
        with LOCK:
            STATE[chat_id] = {"lang": None, "step": -1, "data": {}}
        send(chat_id, "أهلاً بك في CV Pro Yemen 📄\nWelcome to CV Pro Yemen 📄\n\n"
                      "اختر لغة السيرة الذاتية / Choose CV language:",
             reply_markup={"inline_keyboard": [[
                 {"text": "العربية 🇾🇪", "callback_data": "lang:ar"},
                 {"text": "English 🇬🇧", "callback_data": "lang:en"}]]})
        return
    st = STATE.get(chat_id)
    if text.startswith("/cancel"):
        lang = st["lang"] if st and st["lang"] else "ar"
        with LOCK:
            STATE.pop(chat_id, None)
        send(chat_id, MSG[lang]["cancel"])
        return
    if not st or st["step"] < 0:
        send(chat_id, MSG["ar"]["none"] + "\n" + MSG["en"]["none"])
        return
    if not text:
        return
    lang = st["lang"]
    st["data"][STEPS[st["step"]]] = text[:1500]
    st["step"] += 1
    if st["step"] >= len(STEPS):
        send(chat_id, MSG[lang]["working"])
        threading.Thread(target=generate_and_send,
                         args=(chat_id, lang, st["data"]), daemon=True).start()
    else:
        ask(chat_id, lang, st["step"])


def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    data = cb.get("data", "")
    if data.startswith("lang:"):
        lang = data.split(":")[1]
        with LOCK:
            STATE[chat_id] = {"lang": lang, "step": 0, "data": {}}
        ask(chat_id, lang, 0)

# ---------------------------------------------------------------- web
@app.route("/")
def health():
    return "CV Pro Yemen is running"


@app.route(f"/webhook/{SECRET}", methods=["POST"])
def webhook():
    upd = request.get_json(silent=True) or {}
    try:
        if "message" in upd:
            handle_message(upd["message"])
        elif "callback_query" in upd:
            handle_callback(upd["callback_query"])
    except Exception as e:
        print("update error", repr(e))
    return "ok"


def set_webhook():
    if not BASE_URL:
        print("No public URL; webhook not set")
        return
    for _ in range(5):
        r = tg("setWebhook", url=f"{BASE_URL}/webhook/{SECRET}",
               drop_pending_updates=False)
        print("setWebhook", r)
        if r and r.get("ok"):
            break
        time.sleep(3)
    try:
        ensure_fonts()  # warm up fonts
    except Exception as e:
        print("font warmup failed", e)


threading.Thread(target=set_webhook, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
