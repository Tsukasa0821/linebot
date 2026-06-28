import os
import json
import hashlib
import hmac
import base64
import datetime
import requests
import threading
from flask import Flask, request, abort

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_EXPENSE_DB_ID = os.environ.get("NOTION_EXPENSE_DB_ID", "")
NOTION_TODO_DB_ID = os.environ.get("NOTION_TODO_DB_ID", "")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

def verify_signature(body: bytes, signature: str) -> bool:
    hash_ = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(hash_).decode("utf-8"), signature)

def push_message(user_id: str, text: str):
    chunks = [text[i:i+4999] for i in range(0, len(text), 4999)]
    messages = [{"type": "text", "text": chunk} for chunk in chunks[:5]]
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        json={"to": user_id, "messages": messages},
    )

def add_expense(amount: int, category: str, note: str, date: str = None) -> str:
    expense_date = date if date else (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")
    data = {
        "parent": {"database_id": NOTION_EXPENSE_DB_ID},
        "properties": {
            "åç¨±": {"title": [{"text": {"content": note}}]},
            "éé¡": {"number": amount},
            "åé¡": {"select": {"name": category}},
            "æ¥æ": {"date": {"start": expense_date}},
        },
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=data)
    return f"â å·²è¨å¸³ï¼{expense_date}ï¼" if res.status_code == 200 else f"â è¨å¸³å¤±æï¼{res.text}"

def query_expenses(period: str = "month", date: str = None) -> str:
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
    if date:
        date = date.replace("/", "-").replace(".", "-")
        filter_obj = {"property": "æ¥æ", "date": {"equals": date}}
        label = date
    elif period == "today":
        start = today.isoformat()
        filter_obj = {"property": "æ¥æ", "date": {"on_or_after": start}}
        label = "ä»å¤©"
    elif period == "week":
        start = (today - datetime.timedelta(days=today.weekday())).isoformat()
        filter_obj = {"property": "æ¥æ", "date": {"on_or_after": start}}
        label = "æ¬é±"
    else:
        start = today.replace(day=1).isoformat()
        filter_obj = {"property": "æ¥æ", "date": {"on_or_after": start}}
        label = "æ¬æ"
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={"filter": filter_obj, "sorts": [{"property": "æ¥æ", "direction": "ascending"}]},
    )
    if res.status_code != 200:
        return f"â æ¥è©¢å¤±æï¼{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "ð­ éæ®µæéæ²æè¨å¸³ç´é"
    lines = []
    total = 0
    for r in results:
        props = r["properties"]
        name = props["åç¨±"]["title"][0]["plain_text"] if props["åç¨±"]["title"] else "ï¼ç¡ï¼"
        amount = props["éé¡"]["number"] or 0
        category = props["åé¡"]["select"]["name"] if props["åé¡"]["select"] else "å¶ä»"
        date_val = props["æ¥æ"]["date"]["start"] if props["æ¥æ"]["date"] else ""
        total += amount
        lines.append(f"  {date_val}  [{category}] {name}  ${amount}")
    return f"ð {label}è±è²»\n" + "\n".join(lines) + f"\n\nð° åè¨ï¼${total}"

def add_todo(title: str, note: str = "") -> str:
    data = {
        "parent": {"database_id": NOTION_TODO_DB_ID},
        "properties": {
            "åç¨±": {"title": [{"text": {"content": title}}]},
            "åè¨»": {"rich_text": [{"text": {"content": note}}]},
            "çæ": {"select": {"name": "å¾è¾¦"}},
            "å»ºç«æ¥æ": {"date": {"start": (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")}},
        },
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=data)
    return "â å¾è¾¦å·²æ°å¢" if res.status_code == 200 else f"â æ°å¢å¤±æï¼{res.text}"

def query_todos() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "çæ", "select": {"equals": "å¾è¾¦"}}, "sorts": [{"property": "å»ºç«æ¥æ", "direction": "ascending"}]},
    )
    if res.status_code != 200:
        return f"â æ¥è©¢å¤±æï¼{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "ð æ²æå¾è¾¦äºé ï¼"
    lines = []
    for i, r in enumerate(results, 1):
        props = r["properties"]
        name = props["åç¨±"]["title"][0]["plain_text"] if props["åç¨±"]["title"] else "ï¼ç¡ï¼"
        note = ""
        if props.get("åè¨»") and props["åè¨»"]["rich_text"]:
            note = f"\n   â {props['åè¨»']['rich_text'][0]['plain_text']}"
        lines.append(f"{i}. {name}{note}")
    return "ð å¾è¾¦æ¸å®\n" + "\n".join(lines)

def clear_expenses() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"â æ¥è©¢å¤±æï¼{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "â è¨å¸³æ¬ä¾å°±æ¯ç©ºç"
    for r in results:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"â å·²æ¸ç©º {len(results)} ç­è¨å¸³è¨é"

def clear_todos() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"â æ¥è©¢å¤±æï¼{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "â å¾è¾¦æ¬ä¾å°±æ¯ç©ºç"
    for r in results:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"â å·²æ¸ç©º {len(results)} ç­å¾è¾¦äºé "

def delete_expense(keyword: str) -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"â æ¥è©¢å¤±æï¼{res.text}"
    results = res.json().get("results", [])
    matched = [r for r in results if keyword in (r["properties"]["åç¨±"]["title"][0]["plain_text"] if r["properties"]["åç¨±"]["title"] else "")]
    if not matched:
        return f"â æ¾ä¸å°å«ã{keyword}ãçè¨å¸³è¨é"
    for r in matched:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"â å·²åªé¤ {len(matched)} ç­å«ã{keyword}ãçè¨å¸³è¨é"

def delete_todo(keyword: str) -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"â æ¥è©¢å¤±æï¼{res.text}"
    results = res.json().get("results", [])
    matched = [r for r in results if keyword in (r["properties"]["åç¨±"]["title"][0]["plain_text"] if r["properties"]["åç¨±"]["title"] else "")]
    if not matched:
        return f"â æ¾ä¸å°å«ã{keyword}ãçå¾è¾¦äºé "
    for r in matched:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"â å·²åªé¤ {len(matched)} ç­å«ã{keyword}ãçå¾è¾¦äºé "

TOOLS = [
    {"type": "function", "function": {"name": "add_expense", "description": "è¨éä¸ç­æ¶è²»", "parameters": {"type": "object", "properties": {"amount": {"type": "integer"}, "category": {"type": "string"}, "note": {"type": "string", "description": "消費品項名稱，不可包含日期時間詞（昨天、前天、上週五等），只寫消費品項本身"}, "date": {"type": "string", "description": "æ¶è²»æ¥æï¼æ ¼å¼YYYY-MM-DDãè¥ç¨æ¶æå°éå»æéï¼å¦ä¸é±äºãæ¨å¤©ãä¸å¤©åãä¸åæ15èï¼ï¼å¿é æ ¹æç³»çµ±æç¤ºä¸­çä»å¤©æ¥æè¨ç®åºæ­£ç¢ºæ¥æå¾å¡«å¥ãè¥æªæå°ç¹å®æ¥æåä¸å¡«ã"}}, "required": ["amount", "category", "note"]}}},
    {"type": "function", "function": {"name": "query_expenses", "description": "æ¥è©¢è±è²»ç´é", "parameters": {"type": "object", "properties": {"period": {"type": "string", "enum": ["today", "week", "month"], "description": "today=ä»å¤©, week=æ¬é±, month=æ¬æ"}, "date": {"type": "string", "description": "æ¥è©¢æå®æ¥æè±è²»ï¼ç¡è«ç¨æ¶ç¨ä½ç¨®è¡¨éï¼ä¸é±äºãæ¨å¤©ãåå¤©ãå­æäºåä¸æ¥ã2026/06/27ï¼ï¼é½å¿é æ ¹æä»å¤©æ¥æè¨ç®ä¸¦è½æçº YYYY-MM-DD æ ¼å¼å¡«å¥æ­¤æ¬ä½"}}, "required": []}}},
    {"type": "function", "function": {"name": "add_todo", "description": "æ°å¢å¾è¾¦äºé ", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "note": {"type": "string"}}, "required": ["title"]}}},
    {"type": "function", "function": {"name": "query_todos", "description": "æ¥è©¢å¾è¾¦æ¸å®", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "clear_expenses", "description": "æ¸ç©ºåªé¤ææè¨å¸³è±è²»ç´é", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "clear_todos", "description": "æ¸ç©ºåªé¤ææå¾è¾¦äºé ", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "delete_expense", "description": "åªé¤æå®çæç­è¨å¸³è±è²»ï¼ä¾ééµå­æå°ï¼", "parameters": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}}},
    {"type": "function", "function": {"name": "delete_todo", "description": "åªé¤æå®çæç­å¾è¾¦äºé ï¼ä¾ééµå­æå°ï¼", "parameters": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}}},
]

SYSTEM_PROMPT = "ä½ æ¯ LINE è¨å¸³å©ç Fridayãå¼·å¶è¦åï¼1.è¨æ¯å«å·é«éé¡æ¸å­æå¼å« add_expenseï¼ç¡æ¸å­ç¦æ­¢å¼å«ï¼è¥æå°éå»æéï¼ä¸é±äºãæ¨å¤©ç­ï¼å¿é åè¨ç®åºæ­£ç¢ºæ¥æï¼YYYY-MM-DDï¼åå¡«å¥dateåæ¸ï¼ä¾å¦ä»å¤©é±ä¸åä¸é±äº=ä»å¤©-3å¤©ï¼2.è¨æ¯å«å¾è¾¦æéä¸ç¡éé¡æå¼å« add_todoï¼3.æ¥è©¢è±è²»è¨å¸³æ¯åºè¨éç­è©å¼å« query_expensesï¼ä»å¤©ç¨ period=todayï¼æ¬é±ç¨ weekï¼æ¬æç¨ monthï¼ä»»ä½æå®æ¥æï¼ç¡è«æ¯æ¸å­ãä¸­æãæ¨å¤©åå¤©ä¸é±äºç­ï¼é½åè¨ç®åºYYYY-MM-DDåç¨ date åæ¸ï¼4.æ¥è©¢å¾è¾¦å¼å« query_todosï¼5.æ¸ç©ºåªé¤å¨é¨è±è²»å¼å« clear_expensesï¼6.æ¸ç©ºåªé¤å¨é¨å¾è¾¦å¼å« clear_todosï¼7.åªé¤æå®è±è²»å¼å« delete_expenseï¼8.åªé¤æå®å¾è¾¦å¼å« delete_todoãæ°¸é å¼å«å·¥å·ï¼ä¸å¾èªè¡åç­ãç¹é«ä¸­æï¼åè¦ç°¡ç­ã"

def groq_chat(messages, tools=None):
    payload = {"model": "llama-3.3-70b-versatile", "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    res = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json=payload,
    )
    return res.json()

def run_tool(name: str, args: dict) -> str:
    if name == "add_expense":
        return add_expense(**args)
    elif name == "query_expenses":
        return query_expenses(**args)
    elif name == "add_todo":
        return add_todo(**args)
    elif name == "query_todos":
        return query_todos()
    elif name == "clear_expenses":
        return clear_expenses()
    elif name == "clear_todos":
        return clear_todos()
    elif name == "delete_expense":
        return delete_expense(**args)
    elif name == "delete_todo":
        return delete_todo(**args)
    return "æªç¥å·¥å·"

def handle_message(user_text: str) -> str:
    _now_tw = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today = _now_tw.strftime("%Y-%m-%d")
    weekday = ["é±ä¸","é±äº","é±ä¸","é±å","é±äº","é±å­","é±æ¥"][_now_tw.weekday()]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f" ä»å¤©ï¼{today}ï¼{weekday}ï¼ã"},
        {"role": "user", "content": user_text},
    ]
    data = groq_chat(messages, TOOLS)
    if "choices" not in data:
        err = data.get("error", {})
        if err.get("code") == "tool_use_failed":
            failed = err.get("failed_generation", "")
            try:
                start = failed.index("<function=") + len("<function=")
                rest = failed[start:]
                name = rest[:rest.index("=")]
                json_str = rest[rest.index("{"):rest.rindex("}")+1]
                return run_tool(name, json.loads(json_str) or {})
            except Exception:
                pass
        return f"Groqé¯èª¤ï¼{data}"
    msg = data["choices"][0]["message"]
    tool_calls = msg.get("tool_calls")
    if not tool_calls:
        return msg.get("content") or "ï¼ç¡æ³çè§£æä»¤ï¼"
    results = []
    for tc in tool_calls:
        args = json.loads(tc["function"]["arguments"]) or {}
        result = run_tool(tc["function"]["name"], args)
        results.append(result)
    return "\n".join(results)

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not verify_signature(body, signature):
        abort(400, "Invalid signature")
    events = json.loads(body).get("events", [])
    def process_events():
        for event in events:
            if event.get("type") != "message":
                continue
            if event["message"].get("type") != "text":
                continue
            user_text = event["message"]["text"]
            user_id = event["source"]["userId"]
            try:
                reply = handle_message(user_text)
            except Exception as e:
                reply = f"â ï¸ åºé¯äºï¼{str(e)}"
            push_message(user_id, reply)
    threading.Thread(target=process_events, daemon=True).start()
    return "OK"

@app.route("/", methods=["GET"])
def health():
    return "å°é£å¨ç·ä¸ â"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
