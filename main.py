import os, json, logging, asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from groq import Groq
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import requests

load_dotenv()
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
APPS_SCRIPT_URL  = os.getenv("APPS_SCRIPT_URL")
MANAGER_CHAT_IDS = [int(x) for x in os.getenv("MANAGER_CHAT_IDS", "").split(",") if x.strip()]
CURRENCY         = os.getenv("CURRENCY", "MYR")
GROQ_MODEL       = "llama-3.3-70b-versatile"

groq_client = Groq(api_key=GROQ_API_KEY)
user_sessions = {}

def _call_apps_script(action, payload):
    try:
        resp = requests.post(APPS_SCRIPT_URL, json={"action": action, **payload}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Apps Script error: %s", e)
        return {"success": False, "error": str(e)}

def get_all_claims():
    return _call_apps_script("getClaims", {}).get("claims", [])

def add_claim(data):
    return _call_apps_script("addClaim", {"claim": data})

def update_claim_status(claim_id, status, approver, remarks=""):
    return _call_apps_script("updateStatus", {
        "claimId": claim_id, "status": status,
        "approver": approver, "remarks": remarks
    })

TOOLS = [
    {"type":"function","function":{"name":"submit_claim","description":"Submit a new expense claim.","parameters":{"type":"object","properties":{"employee_name":{"type":"string"},"employee_id":{"type":"string"},"date":{"type":"string"},"amount":{"type":"number"},"category":{"type":"string","enum":["Meals","Transport","Accommodation","Office Supplies","Travel","Entertainment","Utilities","Others"]},"merchant":{"type":"string"},"description":{"type":"string"},"department":{"type":"string"}},"required":["employee_name","employee_id","date","amount","category","merchant","description","department"]}}},
    {"type":"function","function":{"name":"check_claim_status","description":"Check claim status.","parameters":{"type":"object","properties":{"claim_id":{"type":"string"},"employee_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"approve_claim","description":"Approve a pending claim.","parameters":{"type":"object","properties":{"claim_id":{"type":"string"},"approver":{"type":"string"},"remarks":{"type":"string"}},"required":["claim_id","approver"]}}},
    {"type":"function","function":{"name":"reject_claim","description":"Reject a claim with reason.","parameters":{"type":"object","properties":{"claim_id":{"type":"string"},"approver":{"type":"string"},"reason":{"type":"string"}},"required":["claim_id","approver","reason"]}}},
    {"type":"function","function":{"name":"get_pending_claims","description":"List all pending claims.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"get_summary","description":"Get claims summary stats.","parameters":{"type":"object","properties":{}}}},
]

_id_counter = 0
def _next_id():
    global _id_counter
    _id_counter += 1
    return str(_id_counter).zfill(3)

async def _notify_managers(message, context):
    for mgr_id in MANAGER_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=mgr_id, text=f"[Claims Bot]\n{message}")
        except Exception as e:
            log.warning("Could not notify manager %s: %s", mgr_id, e)

async def execute_tool(name, args, context):
    if name == "submit_claim":
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        claim_id = f"CLM-{datetime.now().strftime('%Y%m%d')}-{_next_id()}"
        result = add_claim({**args, "claimId": claim_id, "timestamp": now, "status": "Pending"})
        if result.get("success"):
            await _notify_managers(
                f"New claim!\nID: {claim_id}\nBy: {args['employee_name']} ({args['employee_id']})\n"
                f"Amount: {CURRENCY} {float(args['amount']):.2f}\nCategory: {args['category']} - {args['merchant']}\n"
                f"Dept: {args['department']}", context)
            return json.dumps({"success": True, "claim_id": claim_id})
        return json.dumps(result)
    elif name == "check_claim_status":
        claims = get_all_claims()
        if cid := args.get("claim_id"):
            found = [c for c in claims if c.get("claimId","").upper() == cid.upper()]
        elif eid := args.get("employee_id"):
            found = [c for c in claims if c.get("employeeId","").upper() == eid.upper()]
        else:
            return json.dumps({"error": "Provide claim_id or employee_id"})
        return json.dumps({"claims": found[:10]})
    elif name == "approve_claim":
        result = update_claim_status(args["claim_id"], "Approved", args["approver"], args.get("remarks",""))
        if result.get("success"):
            await _notify_managers(f"Claim {args['claim_id']} APPROVED by {args['approver']}.", context)
        return json.dumps(result)
    elif name == "reject_claim":
        result = update_claim_status(args["claim_id"], "Rejected", args["approver"], args.get("reason",""))
        if result.get("success"):
            await _notify_managers(f"Claim {args['claim_id']} REJECTED by {args['approver']}.\nReason: {args.get('reason','')}", context)
        return json.dumps(result)
    elif name == "get_pending_claims":
        pending = [c for c in get_all_claims() if c.get("status","").lower() == "pending"]
        return json.dumps({"pending_count": len(pending), "claims": pending[:15]})
    elif name == "get_summary":
        return json.dumps(_call_apps_script("getSummary", {}))
    return json.dumps({"error": f"Unknown tool: {name}"})

SYSTEM_PROMPT = f"""You are a smart, friendly company expense claims assistant for Sailfish Swim Academy.
Help employees submit claims and managers approve/reject them.
When submitting, collect: employee name, employee ID, date, amount ({CURRENCY}), category, merchant, description, department.
For approvals/rejections, confirm the user is a manager.
Be concise. Format amounts as {CURRENCY} X,XXX.XX.
Today: {datetime.now().strftime('%d %B %Y')}"""

async def run_agent(user_id, user_message, context):
    history = user_sessions.setdefault(user_id, [])
    history.append({"role": "user", "content": user_message})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history[-20:]
    for _ in range(5):
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL, messages=messages, tools=TOOLS,
            tool_choice="auto", max_tokens=1024, temperature=0.3)
        msg = response.choices[0].message
        if response.choices[0].finish_reason == "stop" or not msg.tool_calls:
            reply = msg.content or "Done."
            history.append({"role": "assistant", "content": reply})
            return reply
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            result = await execute_tool(tc.function.name, args, context)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return "I couldn't complete that. Please try again."

async def cmd_start(update, context):
    uid = update.effective_user.id
    log.info("User %s started bot (chat_id: %s)", update.effective_user.first_name, uid)
    await update.message.reply_text(
        f"Hi {update.effective_user.first_name}! I am your Claims Assistant powered by Groq AI.\n\n"
        "/submit - Submit a new expense claim\n"
        "/status - Check your claim status\n"
        "/pending - View pending approvals (managers)\n"
        "/approve CLM-ID - Approve a claim\n"
        "/reject CLM-ID reason - Reject a claim\n"
        "/summary - Monthly summary\n"
        "/clear - Reset conversation\n\n"
        "Or just chat naturally! Try: \"I want to submit a claim\"")

async def cmd_submit(update, context):
    await update.message.reply_text("Sure! Tell me the details: your name, employee ID, date, amount, category, merchant, description and department.")

async def cmd_status(update, context):
    reply = await run_agent(update.effective_user.id, "Show my recent claims status. Ask me for my employee ID if needed.", context)
    await update.message.reply_text(reply)

async def cmd_pending(update, context):
    reply = await run_agent(update.effective_user.id, "Show all pending claims needing approval.", context)
    await update.message.reply_text(reply)

async def cmd_approve(update, context):
    if context.args:
        reply = await run_agent(update.effective_user.id,
            f"Approve claim {context.args[0].upper()}. My name is {update.effective_user.full_name}.", context)
    else:
        reply = "Usage: /approve CLM-20260624-001"
    await update.message.reply_text(reply)

async def cmd_reject(update, context):
    if len(context.args) >= 2:
        reason = " ".join(context.args[1:])
        reply = await run_agent(update.effective_user.id,
            f"Reject claim {context.args[0].upper()} because: {reason}. My name is {update.effective_user.full_name}.", context)
    else:
        reply = "Usage: /reject CLM-20260624-001 [reason]"
    await update.message.reply_text(reply)

async def cmd_summary(update, context):
    reply = await run_agent(update.effective_user.id,
        "Give me a full summary: total amounts, counts by status, approval rate.", context)
    await update.message.reply_text(reply)

async def cmd_clear(update, context):
    user_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("Conversation cleared! Start fresh.")

async def handle_message(update, context):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    log.info("Message from chat_id %s: %s", uid, update.message.text[:80])
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        reply = await run_agent(uid, update.message.text, context)
    except Exception as e:
        log.exception("Agent error")
        reply = "Something went wrong. Please try again or /clear to reset."
    for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
        await update.message.reply_text(chunk)

async def main_async():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("submit",  cmd_submit))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject",  cmd_reject))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("clear",   cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot running with Groq (%s) — token: ...%s", GROQ_MODEL, TELEGRAM_TOKEN[-6:] if TELEGRAM_TOKEN else "NONE")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Bot is polling. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main_async())
