import os
import json
import hashlib
import hmac
import base64
import datetime
import re
import requests
from flask import Flask, request, abort
import google.generativeai as genai

app = Flask(__name__)

# ─── 環境變數 ───────────────────────────────────────────
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_EXPENSE_DB_ID = os.environ.get("NOTION_EXPENSE_DB_ID", "")
NOTION_TODO_DB_ID = os.environ.get("NOTION_TODO_DB_ID", "")

# 初始化 Gemini
genai.configure(api_key=GEMINI_API_KEY)

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

# ─── LINE 簽名驗證 ──────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    hash_ = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    return hmac.compare_digest(
        base64.b64encode(hash_).decode("utf-8"), signature
    )

# ─── LINE 回覆 ──────────────────────────────────────────
def reply_message(reply_token: str, text: str):
    chunks = [text[i:i+4999] for i in range(0, len(text), 4999)]
    messages = [{"type": "text", "text": chunk} for chunk in chunks[:5]]
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        },
        json={"replyToken": reply_token, "messages": messages},
    )

# ─── Notion 工具函式 ────────────────────────────────────
def add_expense(amount: int, category: str, note: str) -> str:
    data = {
        "parent": {"database_id": NOTION_EXPENSE_DB_ID},
        "properties": {
            "名稱": {"title": [{"text": {"content": note}}]},
            "金額": {"number": amount},
            "分類": {"select": {"name": category}},
            "日期": {"date": {"start": datetime.date.today().isoformat()}},
        },
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=data)
    return "✅ 已記帳" if res.status_code == 200 else f"❌ 記帳失敗：{res.text}"


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
        json={
            "filter": {"property": "日期", "date": {"on_or_after": start}},
            "sorts": [{"property": "日期", "direction": "ascending"}],
        },
    )
    if res.status_code != 200:
        return f"❌ 查詢失敗：{res.text}"

    results = res.json().get("results", [])
    if not results:
        return "📭 這段期間沒有記帳紀錄"

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
        json={
            "filter": {"property": "狀態", "select": {"equals": "待辦"}},
            "sorts": [{"property": "建立日期", "direction": "ascending"}],
        },
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


# ─── Gemini 工具定義 ────────────────────────────────────
TOOLS_SCHEMA = [
    {
        "function_declarations": [
            {
                "name": "add_expense",
                "description": "記錄一筆消費。當使用者說花了多少錢、買了什麼時使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "integer", "description": "金額（台幣整數）"},
                        "category": {
                            "type": "string",
                            "description": "分類：餐飲、交通、購物、娛樂、醫療、工作、其他 其中之一",
                        },
                        "note": {"type": "string", "description": "消費說明，例如午餐、計程車"},
                    },
                    "required": ["amount", "category", "note"],
                },
            },
            {
                "name": "query_expenses",
                "description": "查詢花費紀錄。當使用者問花了多少錢時使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "period": {
                            "type": "string",
                            "description": "查詢期間：today=今天, week=本週, month=本月",
                        }
                    },
                    "required": ["period"],
                },
            },
            {
                "name": "add_todo",
                "description": "新增待辦事項。當使用者說要記得做某事時使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "待辦事項標題"},
                        "note": {"type": "string", "description": "備註（可空白）"},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "query_todos",
                "description": "查詢未完成的待辦清單。",
                "parameters": {"type": "object", "properties": {}},
            },
        ]
    }
]

SYSTEM_PROMPT = """你是用戶的個人 LINE 助理，名字叫「小飛」。
你的工作：
1. 幫他記帳、查帳（用 add_expense / query_expenses 工具）
2. 幫他管理待辦（用 add_todo / query_todos 工具）
3. 回答任何工作或生活問題

規則：
- 用繁體中文回覆，語氣輕鬆自然
- 看到金額就直接記帳，不要多問
- 看到待辦需求就直接新增，不要多問
- 回覆簡短有力，執行完工具一句話確認就好"""

# ─── 執行工具 ───────────────────────────────────────────
def run_tool(name: str, args: dict) -> str:
    if name == "add_expense":
        return add_expense(**args)
    elif name == "query_expenses":
        return query_expenses(**args)
    elif name == "add_todo":
        return add_todo(**args)
    elif name == "query_todos":
        return query_todos()
    return "未知工具"

# ─── 對話處理 ───────────────────────────────────────────
def handle_message(user_text: str) -> str:
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=SYSTEM_PROMPT,
        tools=TOOLS_SCHEMA,
    )
    chat = model.start_chat()

    response = chat.send_message(user_text)

    # 最多跑 3 輪處理工具呼叫
    for _ in range(3):
        # 找出所有工具呼叫
        tool_calls = []
        for part in response.parts:
            if hasattr(part, "function_call") and part.function_call.name:
                tool_calls.append(part.function_call)

        if not tool_calls:
            # 沒有工具呼叫，直接回傳文字
            return response.text

        # 執行所有工具，收集結果
        tool_responses = []
        for fc in tool_calls:
            result = run_tool(fc.name, dict(fc.args))
            tool_responses.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            )

        response = chat.send_message(tool_responses)

    return response.text


# ─── Webhook 端點 ───────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        abort(400, "Invalid signature")

    events = json.loads(body).get("events", [])
    for event in events:
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_text = event["message"]["text"]
        reply_token = event["replyToken"]

        try:
            reply = handle_message(user_text)
        except Exception as e:
            reply = f"⚠️ 出錯了：{str(e)}"

        reply_message(reply_token, reply)

    return "OK"


@app.route("/", methods=["GET"])
def health():
    return "小飛在線上 ✅"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
