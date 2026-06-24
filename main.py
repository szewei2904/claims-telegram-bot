import os, json, logging, time
from datetime import datetime, timezone
from dotenv import load_dotenv
from groq import Groq
import httpx

load_dotenv()
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

TOKEN           = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL")
MANAGER_IDS     = [int(x) for x in os.getenv("MANAGER_CHAT_IDS","").split(",") if x.strip()]
CURRENCY        = os.getenv("CURRENCY","MYR")
MODEL           = "llama-3.3-70b-versatile"

BASE = f"https://api.telegram.org/bot{TOKEN}"
groq = Groq(api_key=GROQ_API_KEY)
sessions = {}
_id = 0

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

TOOLS = [
    {"type":"function","function":{"name":"submit_claim","description":"Submit expense claim","parameters":{"type":"object","properties":{"employee_name":{"type":"string"},"employee_id":{"type":"string"},"date":{"type":"string"},"amount":{"type":"number"},"category":{"type":"string","enum":["Meals","Transport","Accommodation","Office Supplies","Travel","Entertainment","Utilities","Others"]},"merchant":{"type":"string"},"description":{"type":"string"},"department":{"type":"string"}},"required":["employee_name","employee_id","date","amount","category","merchant","description","department"]}}},
    {"type":"function","function":{"name":"check_status","description":"Check claim status","parameters":{"type":"object","properties":{"claim_id":{"type":"string"},"employee_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"approve_claim","description":"Approve a claim","parameters":{"type":"object","properties":{"claim_id":{"type":"string"},"approver":{"type":"string"},"remarks":{"type":"string"}},"required":["claim_id","approver"]}}},
    {"type":"function","function":{"name":"reject_claim","description":"Reject a claim","parameters":{"type":"object","properties":{"claim_id":{"type":"string"},"approver":{"type":"string"},"reason":{"type":"string"}},"required":["claim_id","approver","reason"]}}},
    {"type":"function","function":{"name":"get_pending","description":"Get pending claims","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"get_summary","description":"Get summary stats","parameters":{"type":"object","properties":{}}}},
]

def run_tool(name, args):
    global _id
    if name == "submit_claim":
        _id += 1
        claim_id = f"CLM-{datetime.now().strftime('%Y%m%d')}-{str(_id).zfill(3)}"
        result = apps("addClaim", {"claim":{**args,"claimId":claim_id,"status":"Pending"}})
        if result.get("success"):
            notify_managers(f"New claim!\nID: {claim_id}\nBy: {args['employee_name']} ({args['employee_id']})\nAmount: {CURRENCY} {float(args['amount']):.2f}\nCategory: {args['category']} - {args['merchant']}\nDept: {args['department']}")
        return json.dumps(result)
    elif name == "check_status":
        claims = apps("getClaims",{}).get("claims",[])
        cid = args.get("claim_id",""); eid = args.get("employee_id","")
        found = [c for c in claims if (cid and c.get("claimId","").upper()==cid.upper()) or (eid and c.get("employeeId","").upper()==eid.upper())]
        return json.dumps({"claims":found[:10]})
    elif name == "approve_claim":
        result = apps("updateStatus",{"claimId":args["claim_id"],"status":"Approved","approver":args["approver"],"remarks":args.get("remarks","")})
        if result.get("success"): notify_managers(f"Claim {args['claim_id']} APPROVED by {args['approver']}.")
        return json.dumps(result)
    elif name == "reject_claim":
        result = apps("updateStatus",{"claimId":args["claim_id"],"status":"Rejected","approver":args["approver"],"remarks":args.get("reason","")})
        if result.get("success"): notify_managers(f"Claim {args['claim_id']} REJECTED by {args['approver']}.\nReason: {args.get('reason','')}")
        return json.dumps(result)
    elif name == "get_pending":
        claims = apps("getClaims",{}).get("claims",[])
        pending = [c for c in claims if c.get("status","").lower()=="pending"]
        return json.dumps({"count":len(pending),"claims":pending[:15]})
    elif name == "get_summary":
        return json.dumps(apps("getSummary",{}))
    return json.dumps({"error":f"Unknown: {name}"})

SYSTEM = f"""You are a friendly expense claims assistant. Help employees submit claims and managers approve/reject them.
For submissions collect: employee name, employee ID, date, amount ({CURRENCY}), category, merchant, description, department.
Be concise. Today: {datetime.now().strftime('%d %B %Y')}"""

def agent(user_id, text):
    hist = sessions.setdefault(user_id,[])
    hist.append({"role":"user","content":text})
    msgs = [{"role":"system","content":SYSTEM}]+hist[-20:]
    for _ in range(5):
        resp = groq.chat.completions.create(model=MODEL,messages=msgs,tools=TOOLS,tool_choice="auto",max_tokens=1024,temperature=0.3)
        msg = resp.choices[0].message
        if resp.choices[0].finish_reason=="stop" or not msg.tool_calls:
            reply = msg.content or "Done."
            hist.append({"role":"assistant","content":reply})
            return reply
        msgs.append({"role":"assistant","content":msg.content,"tool_calls":msg.tool_calls})
        for tc in msg.tool_calls:
            result = run_tool(tc.function.name, json.loads(tc.function.arguments))
            msgs.append({"role":"tool","tool_call_id":tc.id,"content":result})
    return "Could not complete. Please try again."

def handle(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from",{})
    name = user.get("first_name","there")
    text = msg.get("text","")
    log.info("Message from chat_id %s: %s", chat_id, text[:80])
    if text.startswith("/start"):
        send(chat_id,
            f"Hi {name}! I am your Claims Assistant powered by Groq AI.\n\n"
            "/submit - Submit a new expense claim\n"
            "/status - Check your claim status\n"
            "/pending - View pending approvals\n"
            "/approve CLM-ID - Approve a claim\n"
            "/reject CLM-ID reason - Reject a claim\n"
            "/summary - Monthly summary\n"
            "/clear - Reset conversation\n\n"
            "Or just chat naturally! Try: I want to submit a claim")
    elif text.startswith("/submit"):
        send(chat_id,"Tell me the details: name, employee ID, date, amount, category, merchant, description, department.")
    elif text.startswith("/status"):
        send(chat_id, agent(chat_id,"Show my recent claims. Ask for my employee ID if needed."))
    elif text.startswith("/pending"):
        send(chat_id, agent(chat_id,"Show all pending claims needing approval."))
    elif text.startswith("/approve"):
        parts = text.split()
        if len(parts)>1:
            send(chat_id, agent(chat_id,f"Approve claim {parts[1].upper()}. My name is {name}."))
        else:
            send(chat_id,"Usage: /approve CLM-20260624-001")
    elif text.startswith("/reject"):
        parts = text.split(None,2)
        if len(parts)>2:
            send(chat_id, agent(chat_id,f"Reject claim {parts[1].upper()} because: {parts[2]}. My name is {name}."))
        else:
            send(chat_id,"Usage: /reject CLM-20260624-001 reason")
    elif text.startswith("/summary"):
        send(chat_id, agent(chat_id,"Give full summary: totals, counts by status, approval rate."))
    elif text.startswith("/clear"):
        sessions.pop(chat_id,None)
        send(chat_id,"Conversation cleared!")
    else:
        reply = agent(chat_id, text)
        send(chat_id, reply)

def main():
    log.info("Bot starting with token: ...%s", TOKEN[-6:] if TOKEN else "NONE")
    offset = 0
    log.info("Bot polling started!")
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
