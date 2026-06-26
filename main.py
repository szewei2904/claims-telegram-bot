import os, json, logging, time, uuid
from datetime import datetime, timezone
from dotenv import load_dotenv
from groq import Groq
import httpx

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL")
MANAGER_IDS = [int(x) for x in os.getenv("MANAGER_CHAT_IDS","").split(",") if x.strip()]
CURRENCY = os.getenv("CURRENCY","MYR")
MODEL = "llama-3.3-70b-versatile"

BASE = f"https://api.telegram.org/bot{TOKEN}"
groq_client = Groq(api_key=GROQ_API_KEY)
sessions = {}
submitted_ids = set()

def tg(method, **kwargs):
    r = httpx.post(f"{BASE}/{method}", json=kwargs, timeout=30)
    return r.json()

def send(chat_id, text):
    for chunk in [text[i:i+4000] for i in range(0,len(text),4000)]:
        tg("sendMessage", chat_id=chat_id, text=chunk)

def notify_managers(text):
    for mid in MANAGER_IDS:
        try: tg("sendMessage", chat_id=mid, text=f"[Claims Bot]\n{text}")
        except: pass

def apps(action, payload={}):
    try:
        r = httpx.post(APPS_SCRIPT_URL, json={"action":action,**payload}, timeout=15)
        return r.json()
    except Exception as e:
        return {"success":False,"error":str(e)}

def make_claim_id():
    import uuid as _uuid
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    rand = _uuid.uuid4().hex[:4].upper()
    return f"CLM-{ts}-{rand}"

TOOLS = [
    {"type":"function","function":{"name":"submit_claim","description":"Submit expense claim","parameters":{"type":"object","properties":{"employee_name":{"type":"string"},"date":{"type":"string"},"amount":{"type":"number"},"category":{"type":"string","enum":["Meals","Transport","Accommodation","Office Supplies","Travel","Entertainment","Utilities","Others"]},"merchant":{"type":"string"},"description":{"type":"string"},"department":{"type":"string"}},"required":["employee_name","date","amount","category","merchant","description","department"]}}},
    {"type":"function","function":{"name":"check_status","description":"Check claim status","parameters":{"type":"object","properties":{"employee_name":{"type":"string"}}}}},
    {"type":"function","function":{"name":"approve_claim","description":"Approve a claim","parameters":{"type":"object","properties":{"claim_id":{"type":"string"},"approver":{"type":"string"},"remarks":{"type":"string"}},"required":["claim_id","approver"]}}},
    {"type":"function","function":{"name":"reject_claim","description":"Reject a claim","parameters":{"type":"object","properties":{"claim_id":{"type":"string"},"approver":{"type":"string"},"reason":{"type":"string"}},"required":["claim_id","approver","reason"]}}},
    {"type":"function","function":{"name":"get_pending","description":"Get pending claims","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"get_summary","description":"Get summary stats","parameters":{"type":"object","properties":{}}}},
]

def run_tool(name, args, telegram_user):
    if name == "submit_claim":
        claim_id = make_claim_id()
        if claim_id in submitted_ids:
            return json.dumps({"success":False,"error":"Duplicate blocked"})
        tg_first = telegram_user.get("first_name","")
        tg_last = telegram_user.get("last_name","")
        tg_name = f"{tg_first} {tg_last}".strip() or telegram_user.get("username","Unknown")
        tg_username = telegram_user.get("username","")
        employee_id = f"TG-{telegram_user.get('id','')}"
        employee_name = args.get("employee_name") or tg_name
        payload = {**args,"employee_name":employee_name,"employee_id":employee_id,"claimId":claim_id,"status":"Pending"}
        result = apps("addClaim",{"claim":payload})
        if result.get("success"):
            submitted_ids.add(claim_id)
            notify_managers(f"New Claim!\nID: {claim_id}\nBy: {employee_name} (@{tg_username})\nAmount: {CURRENCY} {float(args.get('amount',0)):.2f}\nCategory: {args.get('category')} - {args.get('merchant')}\nDept: {args.get('department')}\nDesc: {args.get('description')}")
        return json.dumps(result)
    elif name == "check_status":
        claims = apps("getClaims",{}).get("claims",[])
        name_filter = args.get("employee_name","").lower()
        found = [c for c in claims if name_filter and name_filter in c.get("employeeName","").lower()]
        return json.dumps({"claims":found[:10]})
    elif name == "approve_claim":
        result = apps("updateStatus",{"claimId":args["claim_id"],"status":"Approved","approver":args["approver"],"remarks":args.get("remarks","")})
        if result.get("success"): notify_managers(f"Claim {args['claim_id']} APPROVED by {args['approver']}.")
        return json.dumps(result)
    elif name == "reject_claim":
        result = apps("updateStatus",{"claimId":args["claim_id"],"status":"Rejected","approver":args["approver"],"remarks":args.get("reason","")})
        if result.get("success"): notify_managers(f"Claim {args['claim_id']} REJECTED.\nReason: {args.get('reason','')}")
        return json.dumps(result)
    elif name == "get_pending":
        claims = apps("getClaims",{}).get("claims",[])
        pending = [c for c in claims if c.get("status","").lower()=="pending"]
        return json.dumps({"count":len(pending),"claims":pending[:15]})
    elif name == "get_summary":
        return json.dumps(apps("getSummary",{}))
    return json.dumps({"error":f"Unknown: {name}"})

SYSTEM = f"""You are a friendly expense claims assistant for Sailfish Swim Academy.
Help employees submit claims. You already know their name from Telegram - use it directly.
For each claim collect: date, amount ({CURRENCY}), category, merchant name, description, department.
Do NOT ask for employee ID - captured automatically from Telegram.
Be concise. Confirm details before submitting. Only submit ONCE.
Today: {datetime.now().strftime('%d %B %Y')}"""

def agent(user_id, text, telegram_user):
    hist = sessions.setdefault(user_id,[])
    hist.append({"role":"user","content":text})
    msgs = [{"role":"system","content":SYSTEM}]+hist[-20:]
    for _ in range(5):
        resp = groq_client.chat.completions.create(model=MODEL,messages=msgs,tools=TOOLS,tool_choice="auto",max_tokens=1024,temperature=0.3)
        msg = resp.choices[0].message
        if resp.choices[0].finish_reason=="stop" or not msg.tool_calls:
            reply = msg.content or "Done."
            hist.append({"role":"assistant","content":reply})
            return reply
        msgs.append({"role":"assistant","content":msg.content,"tool_calls":msg.tool_calls})
        for tc in msg.tool_calls:
            result = run_tool(tc.function.name,json.loads(tc.function.arguments),telegram_user)
            msgs.append({"role":"tool","tool_call_id":tc.id,"content":result})
    return "Could not complete. Please try again."

def handle(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from",{})
    first_name = user.get("first_name","there")
    text = msg.get("text","")
    log.info("Message from chat_id %s: %s", chat_id, text[:80])
    tg_name = f"{first_name} {user.get('last_name','')}".strip()
    if text.startswith("/start"):
        send(chat_id, f"Hi {first_name}! I am the Sailfish Claims Assistant.\n\nSend your claim details naturally.\n\nCommands:\n/submit - Submit expense claim\n/status - Check your claims\n/pending - Pending approvals\n/summary - Monthly summary\n/clear - Reset\n\nExample: I want to claim RM50 for lunch at Secret Recipe")
    elif text.startswith("/submit"):
        send(chat_id, agent(chat_id, f"I want to submit a claim. My name is {tg_name}.", user))
    elif text.startswith("/status"):
        send(chat_id, agent(chat_id, f"Show my recent claims. My name is {tg_name}.", user))
    elif text.startswith("/pending"):
        send(chat_id, agent(chat_id, "Show all pending claims.", user))
    elif text.startswith("/approve"):
        parts = text.split()
        if len(parts)>1: send(chat_id, agent(chat_id, f"Approve claim {parts[1].upper()}. My name is {first_name}.", user))
        else: send(chat_id, "Usage: /approve CLM-20260626-XXXX")
    elif text.startswith("/reject"):
        parts = text.split(None,2)
        if len(parts)>2: send(chat_id, agent(chat_id, f"Reject claim {parts[1].upper()} because: {parts[2]}. My name is {first_name}.", user))
        else: send(chat_id, "Usage: /reject CLM-20260626-XXXX reason")
    elif text.startswith("/summary"):
        send(chat_id, agent(chat_id, "Give full summary.", user))
    elif text.startswith("/clear"):
        sessions.pop(chat_id,None)
        send(chat_id, "Conversation cleared!")
    else:
        if chat_id not in sessions:
            text = f"My name is {tg_name}. " + text
        send(chat_id, agent(chat_id, text, user))

def main():
    log.info("Bot starting with token: ...%s", TOKEN[-6:] if TOKEN else "NONE")
    offset = 0
    log.info("Sailfish Claims Bot polling started!")
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
            for update in r.get("result",[]):
                offset = update["update_id"]+1
                if "message" in update:
                    handle(update["message"])
        except Exception as e:
            log.error("Polling error: %s", e)
            time.sleep(5)

if __name__=="__main__":
    main()
