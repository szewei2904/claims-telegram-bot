import os, json, logging, time, uuid, base64
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq
import httpx

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

TOKEN         = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL")
MANAGER_IDS   = [int(x) for x in os.getenv("MANAGER_CHAT_IDS","").split(",") if x.strip()]
CURRENCY      = os.getenv("CURRENCY","MYR")
VISION_MODEL  = "meta-llama/llama-4-scout-17b-16e-instruct"
CHAT_MODEL    = "llama-3.3-70b-versatile"

BASE = f"https://api.telegram.org/bot{TOKEN}"
groq_client = Groq(api_key=GROQ_API_KEY)

pending = {}
sessions = {}
submitted_ids = set()

def tg(method, **kwargs):
    r = httpx.post(f"{BASE}/{method}", json=kwargs, timeout=30)
    return r.json()

def send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        payload["text"] = chunk
        tg("sendMessage", **payload)

def send_buttons(chat_id, text, buttons):
    keyboard = {"inline_keyboard": [[{"text": b["text"], "callback_data": b["data"]}] for b in buttons]}
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)

def notify_managers(text):
    for mid in MANAGER_IDS:
        try: tg("sendMessage", chat_id=mid, text=f"[Claims Bot]\n{text}")
        except: pass

def apps(action, payload={}):
    try:
        r = httpx.post(APPS_SCRIPT_URL, json={"action": action, **payload}, timeout=15)
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}

def make_claim_id():
    import uuid as _u
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    return f"CLM-{ts}-{_u.uuid4().hex[:4].upper()}"

def get_tg_name(user):
    return (f"{user.get('first_name','')} {user.get('last_name','')}".strip() or user.get("username","Unknown"))

def scan_receipt_image(file_id):
    try:
        r = tg("getFile", file_id=file_id)
        file_path = r["result"]["file_path"]
        file_url  = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        img_resp  = httpx.get(file_url, timeout=30)
        img_b64   = base64.b64encode(img_resp.content).decode("utf-8")
        mime = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
        today = datetime.now().strftime('%Y-%m-%d')
        prompt = f"""Analyse this Malaysian receipt image. Return ONLY a JSON object with these exact keys:
{{"merchant": "name", "amount": 0.00, "currency": "MYR", "date": "YYYY-MM-DD", "category": "Meals|Transport|Accommodation|Office Supplies|Travel|Entertainment|Utilities|Others", "description": "brief description", "items": "items list"}}
Rules: amount is total paid (number). Date YYYY-MM-DD (use {today} if not visible). No markdown. JSON only."""
        resp = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}, {"type": "text", "text": prompt}]}],
            max_tokens=512, temperature=0.1
        )
        raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        return {"success": True, "data": json.loads(raw)}
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"Could not parse receipt: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def submit_claim(chat_id, user, receipt_data, department):
    claim_id = make_claim_id()
    if claim_id in submitted_ids:
        return False, "Duplicate blocked."
    employee_name = get_tg_name(user)
    employee_id   = f"TG-{user.get('id','')}"
    payload = {
        "claimId": claim_id, "employee_name": employee_name, "employee_id": employee_id,
        "date": receipt_data.get("date", datetime.now().strftime('%Y-%m-%d')),
        "amount": float(receipt_data.get("amount", 0)),
        "category": receipt_data.get("category", "Others"),
        "merchant": receipt_data.get("merchant", "Unknown"),
        "description": receipt_data.get("description", ""),
        "department": department, "status": "Pending"
    }
    result = apps("addClaim", {"claim": payload})
    if result.get("success"):
        submitted_ids.add(claim_id)
        notify_managers(f"New Claim! ID:{claim_id}\nBy:{employee_name}\nAmt:{CURRENCY} {float(receipt_data.get('amount',0)):.2f}\nCat:{receipt_data.get('category')} - {receipt_data.get('merchant')}\nDept:{department}")
        return True, claim_id
    return False, result.get("error", "Unknown error")

DEPARTMENTS = ["Swimming", "Admin", "Management", "Marketing", "Operations", "Others"]
CATEGORIES  = ["Meals", "Transport", "Accommodation", "Office Supplies", "Travel", "Entertainment", "Utilities", "Others"]

def handle_photo(chat_id, user, photo_list, caption=""):
    file_id = photo_list[-1]["file_id"]
    send(chat_id, "📸 Got your receipt! Scanning with AI... please wait ⏳")
    result = scan_receipt_image(file_id)
    if not result["success"]:
        send(chat_id, f"❌ Could not read receipt: {result['error']}\nPlease try a clearer photo or type your claim.")
        return
    data = result["data"]
    pending[chat_id] = {"receipt": data, "user": user}
    amount = float(data.get("amount", 0))
    msg = (f"✅ <b>Receipt scanned!</b>\n\n• Merchant: <b>{data.get('merchant','?')}</b>\n• Amount: <b>{CURRENCY} {amount:.2f}</b>\n• Date: {data.get('date','?')}\n• Category: {data.get('category','?')}\n• Description: {data.get('description','?')}\n\nIs this correct? If anything is wrong, just tell me.\nOtherwise, which <b>department</b> is this for?")
    send_buttons(chat_id, msg, [{"text": d, "data": f"dept:{d}"} for d in DEPARTMENTS])

def handle_callback(update):
    cb = update["callback_query"]
    chat_id = cb["message"]["chat"]["id"]
    user = cb["from"]
    data = cb["data"]
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    if data.startswith("dept:"):
        dept = data[5:]
        if chat_id not in pending:
            send(chat_id, "Session expired. Please send your receipt again."); return
        pending[chat_id]["department"] = dept
        rd = pending[chat_id]["receipt"]
        msg = (f"📋 <b>Confirm your claim:</b>\n\n• Merchant: {rd.get('merchant')}\n• Amount: {CURRENCY} {float(rd.get('amount',0)):.2f}\n• Date: {rd.get('date')}\n• Category: {rd.get('category')}\n• Description: {rd.get('description')}\n• Department: {dept}\n\nSubmit?")
        send_buttons(chat_id, msg, [{"text": "✅ Yes, Submit!", "data": "confirm:yes"},{"text": "❌ Cancel", "data": "confirm:no"}])
    elif data.startswith("confirm:"):
        if data == "confirm:yes":
            if chat_id not in pending:
                send(chat_id, "Session expired. Please send your receipt again."); return
            rd   = pending[chat_id]["receipt"]
            dept = pending[chat_id].get("department", "Others")
            send(chat_id, "⏳ Submitting...")
            ok, res = submit_claim(chat_id, user, rd, dept)
            if ok:
                del pending[chat_id]
                send(chat_id, f"🎉 <b>Submitted!</b>\n• Claim ID: <code>{res}</code>\n• Amount: {CURRENCY} {float(rd.get('amount',0)):.2f}\n• Merchant: {rd.get('merchant')}\n\nManager notified! Keep your receipt 📎")
            else:
                send(chat_id, f"❌ Failed: {res}. Try again or contact admin.")
        else:
            pending.pop(chat_id, None)
            send(chat_id, "Cancelled. Send a new receipt when ready! 😊")

def handle_text(chat_id, user, text):
    import re
    tg_name = get_tg_name(user)
    first_name = user.get("first_name","there")
    if chat_id in pending and pending[chat_id].get("fixing"):
        field = pending[chat_id].pop("fixing")
        pending[chat_id]["receipt"][field] = text
        rd = pending[chat_id]["receipt"]
        send(chat_id, f"✅ Updated {field}! Which department is this for?")
        send_buttons(chat_id, "Select department:", [{"text": d, "data": f"dept:{d}"} for d in DEPARTMENTS])
        return
    if chat_id in pending and "receipt" in pending[chat_id]:
        lower = text.lower()
        data  = pending[chat_id]["receipt"]
        m = re.search(r'(?:rm|myr)?\s*(\d+\.?\d*)', lower)
        if any(w in lower for w in ['amount','rm','myr','price','total']) and m:
            data["amount"] = float(m.group(1))
            send(chat_id, f"✏️ Amount updated to {CURRENCY} {data['amount']:.2f}")
            send_buttons(chat_id, "Which department?", [{"text": d, "data": f"dept:{d}"} for d in DEPARTMENTS]); return
        for cat in CATEGORIES:
            if cat.lower() in lower:
                data["category"] = cat
                send(chat_id, f"✏️ Category updated to {cat}")
                send_buttons(chat_id, "Which department?", [{"text": d, "data": f"dept:{d}"} for d in DEPARTMENTS]); return
    if text.startswith("/start"):
        send(chat_id, f"Hi {first_name}! 👋 <b>Sailfish Claims Bot</b>\n\n📸 <b>Send a photo of your receipt</b> - I'll read it with AI!\n\nCommands:\n/status - Your claims\n/pending - Pending approvals\n/clear - Reset\n\n<b>Tip:</b> Good lighting = better scan 💡")
    elif text.startswith("/status"):
        result = apps("getClaims", {})
        claims = result.get("claims", [])
        mine = [c for c in claims if tg_name.lower() in c.get("employeeName","").lower()]
        if mine:
            msg = "📋 <b>Your claims:</b>\n\n"
            STATUS_MAP = {"Pending":"\u23f3","Approved":"\u2705","Rejected":"\u274c"}
            for c in mine[-5:]:
                em = STATUS_MAP.get(c.get('status',''),'?')
                msg += f"{em} {c.get('claimId','?')} - {CURRENCY} {float(c.get('amount',0)):.2f} - {c.get('status')}\n"
            send(chat_id, msg)
        else: send(chat_id, "No claims yet. Send a receipt photo!")
    elif text.startswith("/pending"):
        result = apps("getClaims", {})
        plist = [c for c in result.get("claims",[]) if c.get("status","").lower()=="pending"]
        if plist:
            msg = f"⏳ <b>{len(plist)} pending:</b>\n\n" + "\n".join(f"• {c.get('claimId','?')} - {c.get('employeeName','?')} - {CURRENCY}{float(c.get('amount',0)):.2f}" for c in plist[:10])
            msg += "\n\n/approve CLM-XXX or /reject CLM-XXX reason"
            send(chat_id, msg)
        else: send(chat_id, "✅ No pending claims!")
    elif text.startswith("/approve"):
        parts = text.split()
        if len(parts)>1:
            r = apps("updateStatus",{"claimId":parts[1].upper(),"status":"Approved","approver":tg_name,"remarks":""})
            send(chat_id, f"✅ Approved {parts[1].upper()}!" if r.get("success") else f"❌ Error: {r.get('error')}")
            if r.get("success"): notify_managers(f"✅ {parts[1].upper()} APPROVED by {tg_name}.")
        else: send(chat_id, "Usage: /approve CLM-XXXX")
    elif text.startswith("/reject"):
        parts = text.split(None,2)
        if len(parts)>2:
            r = apps("updateStatus",{"claimId":parts[1].upper(),"status":"Rejected","approver":tg_name,"remarks":parts[2]})
            send(chat_id, f"❌ Rejected {parts[1].upper()}." if r.get("success") else f"❌ Error: {r.get('error')}")
            if r.get("success"): notify_managers(f"❌ {parts[1].upper()} REJECTED by {tg_name}. Reason: {parts[2]}")
        else: send(chat_id, "Usage: /reject CLM-XXXX reason")
    elif text.startswith("/clear"):
        sessions.pop(chat_id,None); pending.pop(chat_id,None)
        send(chat_id, "✨ Cleared! Send a receipt photo to start.")
    else:
        send(chat_id, f"📸 Send a <b>photo of your receipt</b> and I'll read it automatically!\n\nOr type /start to see all commands.")

def handle(update):
    try:
        if "callback_query" in update: handle_callback(update); return
        msg = update.get("message",{})
        if not msg: return
        chat_id = msg["chat"]["id"]
        user = msg.get("from",{})
        if "photo" in msg: handle_photo(chat_id, user, msg["photo"], msg.get("caption",""))
        elif "text" in msg: handle_text(chat_id, user, msg["text"])
        else: send(chat_id, "Please send a photo of your receipt.")
    except Exception as e: log.error("Handle error: %s", e, exc_info=True)

def main():
    log.info("Sailfish Claims Bot v3 - AI Receipt Scanning")
    offset = 0
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message","callback_query"])
            for u in r.get("result",[]):
                offset = u["update_id"]+1
                handle(u)
        except Exception as e: log.error("Poll error: %s", e); time.sleep(5)

if __name__=="__main__": main()
