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
    expense_date = date if date else datetime.date.today().isoformat()
    data = {
        "parent": {"database_id": NOTION_EXPENSE_DB_ID},
        "properties": {
            "名稱": {"title": [{"text": {"content": note}}]},
            "金額": {"number": amount},
            "分類": {"select": {"name": category}},
            "日期": {"date": {"start": expense_date}},
        },
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=data)
    return f"✅ 已記帳（{expense_date}）" if res.status_code == 200 else f"❌ 記帳失敗：{res.text}"

def query_expenses(period: str = "month") -> str:
    today = datetime.date.today()
    if period == "today":
        start = today.isoformat()
    elif period == "week":
        start = (today - datetime.timedelta(days=today.weekday())).isoformat()
    else:
        start = today.replace(day=1).isoformat()
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "日期", "date": {"on_or_after": start}}, "sorts": [{"property": "日期", "direction": "ascending"}]},
    )
    if res.status_code != 200:
        return f"❌ 查詢失敗：{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "὎d 這段期間沒有記帳紀錄"
    lines = []
    total = 0
    for r in results:
        props = r["properties"]
        name = props["名稱"]["title"][0]["plain_text"] if props["名稱"]["title"] else "（無）"
        amount = props["金額"]["number"] or 0
        category = props["分類"]["select"]["name"] if props["分類"]["select"] else "其他"
        date = props["日期"]["date"]["start"] if props["日期"]["date"] else ""
        total += amount
        lines.append(f"  {date}  [{category}] {name}  ${amount}")
    label = {"today": "今天", "week": "本週", "month": "本月"}.get(period, "本月")
    return f"📊 {label}花費\n" + "\n".join(lines) + f"\n\n💰 合計：${total}"

def add_todo(title: str, note: str = "") -> str:
    data = {
        "parent": {"database_id": NOTION_TODO_DB_ID},
        "properties": {
            "名稱": {"title": [{"text": {"content": title}}]},
            "備註": {"rich_text": [{"text": {"content": note}}]},
            "狀態": {"select": {"name": "待辦"}},
            "建立日期": {"date": {"start": datetime.date.today().isoformat()}},
        },
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=data)
    return "✅ 待辦已新增" if res.status_code == 200 else f"❌ 新增失敗：{res.text}"

def query_todos() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "狀態", "select": {"equals": "待辦"}}, "sorts": [{"property": "建立日期", "direction": "ascending"}]},
    )
    if res.status_code != 200:
        return f"❌ 查詢失敗：{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "🎉 沒有待辦事項！"
    lines = []
    for i, r in enumerate(results, 1):
        props = r["properties"]
        name = props["名稱"]["title"][0]["plain_text"] if props["名稱"]["title"] else "（無）"
        note = ""
        if props.get("備註") and props["備註"]["rich_text"]:
            note = f"\n   └ {props['備註']['rich_text'][0]['plain_text']}"
        lines.append(f"{i}. {name}{note}")
    return "📋 待辦清單\n" + "\n".join(lines)

def clear_expenses() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"❌ 查詢失敗：{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "✅ 記帳本來就是空的"
    for r in results:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"✅ 已清空 {len(results)} 筆記帳記錄"

def clear_todos() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"❌ 查詢失敗：{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "✅ 待辦本來就是空的"
    for r in results:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"✅ 已清空 {len(results)} 筆待辦事項"

def delete_expense(keyword: str) -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"❌ 查詢失敗：{res.text}"
    results = res.json().get("results", [])
    matched = [r for r in results if keyword in (r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else "")]
    if not matched:
        return f"❌ 找不到含「{keyword}」的記帳記錄"
    for r in matched:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"✅ 已刪除 {len(matched)} 筆含「{keyword}」的記帳記錄"

def delete_todo(keyword: str) -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"❌ 查詢失敗：{res.text}"
    results = res.json().get("results", [])
    matched = [r for r in results if keyword in (r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else "")]
    if not matched:
        return f"❌ 找不到含「{keyword}」的待辦事項"
    for r in matched:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"✅ 已刪除 {len(matched)} 筆含「{keyword}」的待辦事項"

TOOLS = [
    {"type": "function", "function": {"name": "add_expense", "description": "記錄一筆消費", "parameters": {"type": "object", "properties": {"amount": {"type": "integer"}, "category": {"type": "string"}, "note": {"type": "string"}, "date": {"type": "string", "description": "消費日期 YYYY-MM-DD，若用戶指定過去日期請填入，否則省略"}}, "required": ["amount", "category", "note"]}}},
    {"type": "function", "function": {"name": "query_expenses", "description": "查詢花費紀錄", "parameters": {"type": "object", "properties": {"period": {"type": "string", "enum": ["today", "week", "month"], "description": "today=今天, week=本週, month=本月"}}, "required": []}}},
    {"type": "function", "function": {"name": "add_todo", "description": "新增待辦事項", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "note": {"type": "string"}}, "required": ["title"]}}},
    {"type": "function", "function": {"name": "query_todos", "description": "查詢待辦清單", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "clear_expenses", "description": "清空刪除所有記帳花費紀錄", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "clear_todos", "description": "清空刪除所有待辦事項", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "delete_expense", "description": "刪除指定的某筆記帳花費（依關鍵字搜尋）", "parameters": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}}},
    {"type": "function", "function": {"name": "delete_todo", "description": "刪除指定的某筆待辦事項（依關鍵字搜尋）", "parameters": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}}},
]

SYSTEM_PROMPT = "你是 LINE 記帳助理 Friday。強制規則：1.訊息含具體金額數字才呼叫 add_expense，無數字禁止呼叫；2.訊息含待辦提醒且無金額才呼叫 add_todo；3.查詢花費記帳支出記錄等詞呼叫 query_expenses，今天用 period=today，本週用 week，其餘用 month；4.查詢待辦呼叫 query_todos；5.清空刪除全部花費呼叫 clear_expenses；6.清空刪除全部待辦呼叫 clear_todos；7.刪除指定花費呼叫 delete_expense；8.刪除指定待辦呼叫 delete_todo。永遠呼叫工具，不得自行回答。繁體中文，回覆簡短。"

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
    return "未知工具"

def handle_message(user_text: str) -> str:
    today = datetime.date.today().isoformat()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f" 今天日期：{today}。"},
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
                return run_tool(name, json.loads(json_str))
            except Exception:
                pass
        return f"Groq錯誤：{data}"
    msg = data["choices"][0]["message"]
    tool_calls = msg.get("tool_calls")
    if not tool_calls:
        return msg.get("content") or "（無法理解指令）"
    results = []
    for tc in tool_calls:
        args = json.loads(tc["function"]["arguments"])
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
                reply = f"⚠️ 出錯了：{str(e)}"
            push_message(user_id, reply)
    threading.Thread(target=process_events, daemon=True).start()
    return "OK"

@app.route("/", methods=["GET"])
def health():
    return "小飛在線上 ✅"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
