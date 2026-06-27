import os, json, logging, time, uuid, base64, re
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from groq import Groq
import httpx

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)h

TOKEN         = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL")
MANAGER_IDS   = [int(x) for x in os.getenv("MANAGER_CHAT_IDS","").split(",") if x.strip()]
CURRENCY      = os.getenv("CURRENCY","MYR")
VISION_MODEL  = "meta-llama/llama-4-scout-17b-16e-instruct"

BASE = f"https://api.telegram.org/bot{TOKEN}"
groq_client = Groq(api_key=GROQ_API_KEY)
pending = {}
submitted_ids = set()

DEPARTMENTS = ["Management HQ", "Bukit Kota Kemuning", "TGCR", "YTSA", "TARH", "Bukit Jalil Berjaya", "Lakepoint Club", "360Club", "Setia Alam Club", "Canopy Club", "Sri KDU Subang", "Sri KDU Klang", "Sri KDU Kota Damansara"]
CATEGORIES  = ["Meals", "Transport", "Accommodation", "Office Supplies", "Travel", "Entertainment", "Utilities", "Others"]
MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,"january":1,"february":2,"march":3,"april":4,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12}

def tg(method, **kwargs):
    r = httpx.post(f"{BASE}/{method}", json=kwargs, timeout=30)
    return r.json()

def send(chat_id, text):
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        tg("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML")

def send_buttons(chat_id, text, buttons):
    keyboard = {"inline_keyboard": [[{"text": b["text"], "callback_data": b["data"]}] for b in buttons]}
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)

def send_grid_buttons(chat_id, text, buttons, cols=2):
    rows = [buttons[i:i+cols] for i in range(0, len(buttons), cols)]
    keyboard = {"inline_keyboard": [[{"text": b["text"], "callback_data": b["data"]} for b in row] for row in rows]}
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)

def notify_managers(text):
    for mid in MANAGER_IDS:
        try: tg("sendMessage", chat_id=mid, text=f"[Claims Bot]\n{text}")
        except: pass

def apps(action, payload={}):
    try:
        r = httpx.post(APPS_SCRIPT_URL, json={"action": action, **payload}, timeout=15, follow_redirects=True)
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}

def make_claim_id():
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    return f"CLM-{ts}-{uuid.uuid4().hex[:4].upper()}"

def get_tg_name(user):
    return (f"{user.get('first_name','')} {user.get('last_name','')}".strip() or user.get("username","Unknown"))

def parse_date(text):
    text = text.lower().strip()
    today = date.today()
    if "today" in text: return today.strftime('%Y-%m-%d')
    if "yesterday" in text: return (today - timedelta(days=1)).strftime('%Y-%m-%d')
    m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', text)
    if m:
        d2,mo,yr = int(m.group(1)),int(m.group(2)),int(m.group(3))
        if yr < 100: yr += 2000
        try: return date(yr, mo, d2).strftime('%Y-%m-%d')
        except: pass
    m = re.search(r'(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?', text)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2)[:3]
        yr2 = int(m.group(3)) if m.group(3) else today.year
        mon = MONTHS.get(mon_str) or MONTHS.get(m.group(2).lower())
        if mon:
            try: return date(yr2, mon, day).strftime('%Y-%m-%d')
            except: pass
    m = re.search(r'([a-z]+)\s+(\d{1,2})(?:\s+(\d{4}))?', text)
    if m:
        mon_str = m.group(1)[:3]
        day = int(m.group(2))
        yr2 = int(m.group(3)) if m.group(3) else today.year
        mon = MONTHS.get(mon_str) or MONTHS.get(m.group(1).lower())
        if mon:
            try: return date(yr2, mon, day).strftime('%Y-%m-%d')
            except: pass
    m = re.fullmatch(r'(\d{1,2})', text.strip())
    if m:
        day = int(m.group(1))
        try: return date(today.year, today.month, day).strftime('%Y-%m-%d')
        except: pass
    return None

def show_receipt_summary(chat_id, data, prompt="Is this correct? Select department or edit any field:"):
    amount = float(data.get("amount", 0))
    msg = (
        f"✅ <b>Receipt details:</b>\n\n"
        f"🏪 Merchant: <b>{data.get('merchant','?')}</b>\n"
        f"💰 Amount: <b>{CURRENCY} {amount:.2f}</b>\n"
        f"📅 Date: <b>{data.get('date','?')}</b>\n"
        f"🏷 Category: <b>{data.get('category','?')}</b>\n"
        f"📝 Description: {data.get('description','?')}\n\n"
        f"{prompt}"
    )
    buttons = [{"text": d, "data": f"dept:{d}"} for d in DEPARTMENTS]
    buttons.append({"text": "✏️ Edit date", "data": "edit:date"})
    buttons.append({"text": "✏️ Edit amount", "data": "edit:amount"})
    buttons.append({"text": "✏️ Edit merchant", "data": "edit:merchant"})
    buttons.append({"text": "✏️ Edit category", "data": "edit:category"})
    buttons.append({"text": "✏️ Edit description", "data": "edit:description"})
    send_grid_buttons(chat_id, msg, buttons, cols=2)
def scan_receipt_image(file_id):
    try:
        r = tg("getFile", file_id=file_id)
        file_path = r["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        img_resp = httpx.get(file_url, timeout=30)
        img_b64 = base64.b64encode(img_resp.content).decode("utf-8")
        mime = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
        today = datetime.now().strftime('%Y-%m-%d')
        prompt = f"""Analyse this Malaysian receipt. Return ONLY valid JSON with keys: merchant, amount (number), currency (MYR), date (YYYY-MM-DD, use {today} if unclear), category (one of: Meals/Transport/Accommodation/Office Supplies/Travel/Entertainment/Utilities/Others), description, items. No markdown."""
        resp = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}}, {"type": "text", "text": prompt}]}],
            max_tokens=512, temperature=0.1
        )
        raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        return {"success": True, "data": json.loads(raw)}
    except Exception as e:
        return {"success": False, "error": str(e)}

def do_submit(chat_id, user):
    rd = pending[chat_id]["receipt"]
    dept = pending[chat_id].get("department", "Others")
    claim_id = make_claim_id()
    if claim_id in submitted_ids:
        send(chat_id, "⚠️ Duplicate blocked. Please try again."); return
    employee_name = get_tg_name(user)
    employee_id = f"TG-{user.get('id','')}"
    payload = {"claimId": claim_id, "employee_name": employee_name, "employee_id": employee_id,
               "date": rd.get("date", datetime.now().strftime('%Y-%m-%d')),
               "amount": float(rd.get("amount", 0)),
               "category": rd.get("category", "Others"),
               "merchant": rd.get("merchant", "Unknown"),
               "description": rd.get("description", ""),
               "department": dept, "status": "Pending"}
    result = apps("addClaim", {"claim": payload})
    if result.get("success"):
        submitted_ids.add(claim_id)
        notify_managers(f"New Claim!\nID:{claim_id}\nBy:{employee_name}\nAmt:{CURRENCY} {float(rd.get('amount',0)):.2f}\nCat:{rd.get('category')} - {rd.get('merchant')}\nDept:{dept}")
        # Upload receipt image to Google Drive
        file_id = pending[chat_id].get("file_id")
        del pending[chat_id]
        if file_id:
            try:
                r2 = tg("getFile", file_id=file_id)
                file_path2 = r2["result"]["file_path"]
                file_url2 = f"https://api.telegram.org/file/bot{TOKEN}/{file_path2}"
                img_resp2 = httpx.get(file_url2, timeout=30)
                img_b64_2 = base64.b64encode(img_resp2.content).decode("utf-8")
                ext2 = "png" if file_path2.lower().endswith(".png") else "jpg"
                safe_name2 = employee_name.replace(" ", "_")
                filename2 = f"{claim_id}-{safe_name2}.{ext2}"
                                import urllib.parse as _ul
                                upload_payload = {"action": "uploadReceipt", "claimId": claim_id, "filename": filename2, "imageBase64": img_b64_2, "mimeType": f"image/{ext2}"}
                                _body = "payload=" + _ul.quote(json.dumps(upload_payload))
                                _r3 = httpx.post(APPS_SCRIPT_URL, content=_body.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=60, follow_redirects=True)
                                log.info("uploadReceipt result: %s", _r3.text[:200])
            except Exception as drive_err:
                log.error("Drive upload error: %s", drive_err)
        send(chat_id, f"🎉 <b>Submitted!</b>\n• ID: <code>{claim_id}</code>\n• Amount: {CURRENCY} {float(rd.get('amount',0)):.2f}\n• Merchant: {rd.get('merchant')}\n• Date: {rd.get('date')}\n\nManager notified! Keep your receipt 📎")
    else:
        send(chat_id, f"❌ Submit failed: {result.get('error','Unknown')}. Please try again.")

def handle_photo(chat_id, user, photo_list):
    file_id = photo_list[-1]["file_id"]
    send(chat_id, "📸 Scanning your receipt with AI... ⏳")
    result = scan_receipt_image(file_id)
    if not result["success"]:
        send(chat_id, f"❌ Could not read receipt.\nError: {result['error']}\n\nPlease try a clearer photo.")
        return
    pending[chat_id] = {"receipt": result["data"], "user": user, "file_id": file_id}
    show_receipt_summary(chat_id, result["data"])

def handle_callback(update):
    cb = update["callback_query"]
    chat_id = cb["message"]["chat"]["id"]
    user = cb["from"]
    data = cb["data"]
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    if data.startswith("dept:"):
        if chat_id not in pending:
            send(chat_id, "⚠️ Session expired. Please send your receipt again."); return
        dept = data[5:]
        pending[chat_id]["department"] = dept
        rd = pending[chat_id]["receipt"]
        # Always use stored user from when photo was sent
        stored_user = pending[chat_id].get("user", user)
        tg_n = get_tg_name(stored_user)
        emp_i = f"TG-{stored_user.get('id','')}"
        msg = (f"📋 <b>Confirm your claim:</b>\n\n"
               f"👤 Employee: <b>{tg_n}</b>\n"
               f"🆔 ID: {emp_i}\n"
               f"🏪 Merchant: {rd.get('merchant')}\n"
               f"💰 Amount: <b>{CURRENCY} {float(rd.get('amount',0)):.2f}</b>\n"
               f"📅 Date: {rd.get('date')}\n"
               f"🏷 Category: {rd.get('category')}\n"
               f"📝 Description: {rd.get('description')}\n"
               f"🏢 Department: {dept}\n\nAll correct? Tap Submit!")
        send_buttons(chat_id, msg, [{"text": "✅ Yes, Submit!", "data": "confirm:yes"}, {"text": "✏️ Edit more", "data": "back:edit"}, {"text": "❌ Cancel", "data": "confirm:no"}])
    elif data == "confirm:yes":
        if chat_id not in pending:
            send(chat_id, "⚠️ Session expired."); return
        send(chat_id, "⏳ Submitting...")
        stored_user = pending[chat_id].get("user", user)
        do_submit(chat_id, stored_user)
    elif data == "confirm:no":
        pending.pop(chat_id, None)
        send(chat_id, "Cancelled. Send a new receipt when ready! 😊")
    elif data == "back:edit":
        if chat_id not in pending:
            send(chat_id, "⚠️ Session expired."); return
        show_receipt_summary(chat_id, pending[chat_id]["receipt"])
    elif data.startswith("edit:"):
        if chat_id not in pending:
            send(chat_id, "⚠️ Session expired. Please send your receipt again."); return
        field = data[5:]
        pending[chat_id]["editing"] = field
        prompts = {
            "date": f"📅 What is the correct date?\n\nExamples: <i>26 June</i>, <i>today</i>, <i>yesterday</i>, <i>25/06/2026</i>, <i>26</i> (day of this month)",
            "amount": f"💰 What is the correct amount? (numbers only, e.g. <i>85.50</i>)",
            "merchant": f"🏪 What is the correct merchant/store name?",
            "category": f"🏷 Choose the correct category: {', '.join(CATEGORIES)}",
            "description": f"📝 What is the correct description?",
        }
        send(chat_id, prompts.get(field, f"Type the new value for {field}:"))

def handle_text(chat_id, user, text):
    tg_name = get_tg_name(user)
    first_name = user.get("first_name","there")
    if chat_id in pending and pending[chat_id].get("editing"):
        field = pending[chat_id].pop("editing")
        rd = pending[chat_id]["receipt"]
        ok = True
        if field == "date":
            parsed = parse_date(text)
            if parsed:
                rd["date"] = parsed
                send(chat_id, f"✅ Date updated to <b>{parsed}</b>")
            else:
                send(chat_id, f"❌ Could not understand date: <i>{text}</i>\nTry: <i>26 June</i>, <i>26/06/2026</i>, <i>today</i>, <i>yesterday</i>")
                ok = False
        elif field == "amount":
            m = re.search(r'[\d]+\.?[\d]*', text.replace(',','.'))
            if m:
                rd["amount"] = float(m.group())
                send(chat_id, f"✅ Amount updated to <b>{CURRENCY} {rd['amount']:.2f}</b>")
            else:
                send(chat_id, "❌ Please enter a valid number, e.g. <i>85.50</i>"); ok = False
        elif field == "merchant":
            rd["merchant"] = text.strip().title()
            send(chat_id, f"✅ Merchant updated to <b>{rd['merchant']}</b>")
        elif field == "category":
            matched = next((c for c in CATEGORIES if c.lower() in text.lower()), None)
            if matched:
                rd["category"] = matched
                send(chat_id, f"✅ Category updated to <b>{matched}</b>")
            else:
                send(chat_id, f"❌ Not recognised. Choose from: {', '.join(CATEGORIES)}"); ok = False
        elif field == "description":
            rd["description"] = text.strip()
            send(chat_id, f"✅ Description updated.")
        if ok:
            pending[chat_id]["receipt"] = rd
            show_receipt_summary(chat_id, rd, "Updated! Select department or edit more:")
        else:
            pending[chat_id]["editing"] = field
        return
    if chat_id in pending and "receipt" in pending[chat_id]:
        lower = text.lower()
        rd = pending[chat_id]["receipt"]
        fixed = False
        date_kw = any(w in lower for w in ['date','hari','tarikh'])
        if date_kw or re.search(r'\b(today|yesterday|\d{1,2}[/-]\d{1,2})\b', lower):
            after = re.split(r'(?:date|to|is|=|:)', lower, maxsplit=1)[-1].strip()
            parsed = parse_date(after) or parse_date(lower)
            if parsed:
                rd["date"] = parsed
                fixed = True
                send(chat_id, f"✅ Date updated to <b>{parsed}</b>")
        if not fixed and any(w in lower for w in ['amount','rm','myr','price','total','ringgit']):
            m = re.search(r'[\d]+\.?[\d]*', text.replace(',','.'))
            if m:
                rd["amount"] = float(m.group())
                fixed = True
                send(chat_id, f"✅ Amount updated to <b>{CURRENCY} {rd['amount']:.2f}</b>")
        if not fixed and any(w in lower for w in ['merchant','shop','store','restaurant','place','kedai']):
            after = re.split(r'(?:merchant|shop|store|restaurant|is|to|=|:)', lower, maxsplit=1)[-1].strip()
            if after:
                rd["merchant"] = after.title()
                fixed = True
                send(chat_id, f"✅ Merchant updated to <b>{rd['merchant']}</b>")
        if not fixed:
            for cat in CATEGORIES:
                if cat.lower() in lower:
                    rd["category"] = cat
                    fixed = True
                    send(chat_id, f"✅ Category updated to <b>{cat}</b>")
                    break
        if fixed:
            pending[chat_id]["receipt"] = rd
            show_receipt_summary(chat_id, rd, "Updated! Select department or edit more:")
            return
    if text.startswith("/start"):
        send(chat_id, f"Hi {first_name}! 👋 <b>Sailfish Claims Assistant</b>\n\n📸 <b>Send a photo of your receipt</b> - I'll read it with AI!\n\nAfter scanning, edit any detail before submitting.\n\nCommands:\n/status - Your recent claims\n/pending - Pending approvals\n/clear - Reset")
    elif text.startswith("/status"):
        result = apps("getClaims", {})
        claims = result.get("claims", [])
        mine = [c for c in claims if tg_name.lower() in c.get("employeeName","").lower()]
        STATUS_MAP = {"Pending":"⏳","Approved":"✅","Rejected":"❌"}
        if mine:
            msg = "📋 <b>Your recent claims:</b>\n\n"
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
            msg += "\n\n/approve CLM-XXXX or /reject CLM-XXXX reason"
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
        pending.pop(chat_id, None)
        send(chat_id, "✨ Cleared! Send a receipt photo to start.")
    else:
        send(chat_id, "📸 Send a <b>photo of your receipt</b> and I'll read it automatically!\n\nOr type /start to see all commands.")

def handle(update):
    try:
        if "callback_query" in update: handle_callback(update); return
        msg = update.get("message",{})
        if not msg: return
        chat_id = msg["chat"]["id"]
        user = msg.get("from",{})
        if "photo" in msg: handle_photo(chat_id, user, msg["photo"])
        elif "text" in msg: handle_text(chat_id, user, msg["text"])
        else: send(chat_id, "Please send a photo of your receipt.")
    except Exception as e: log.error("Handle error: %s", e, exc_info=True)

def main():
    log.info("Sailfish Claims Bot v4 - fixed user from pending")
    offset = 0
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message","callback_query"])
            for u in r.get("result",[]):
                offset = u["update_id"]+1
                handle(u)
        except Exception as e: log.error("Poll error: %s", e); time.sleep(5)

if __name__=="__main__": main()
