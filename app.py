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
            "Ã¥ÂÂÃ§Â¨Â±": {"title": [{"text": {"content": note}}]},
            "Ã©ÂÂÃ©Â¡Â": {"number": amount},
            "Ã¥ÂÂÃ©Â¡Â": {"select": {"name": category}},
            "Ã¦ÂÂ¥Ã¦ÂÂ": {"date": {"start": expense_date}},
        },
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=data)
    return f"Ã¢ÂÂ Ã¥Â·Â²Ã¨Â¨ÂÃ¥Â¸Â³Ã¯Â¼Â{expense_date}Ã¯Â¼Â" if res.status_code == 200 else f"Ã¢ÂÂ Ã¨Â¨ÂÃ¥Â¸Â³Ã¥Â¤Â±Ã¦ÂÂÃ¯Â¼Â{res.text}"

def query_expenses(period: str = "month", date: str = None) -> str:
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date()
    if date:
        date = date.replace("/", "-").replace(".", "-")
        filter_obj = {"property": "Ã¦ÂÂ¥Ã¦ÂÂ", "date": {"equals": date}}
        label = date
    elif period == "today":
        start = today.isoformat()
        filter_obj = {"property": "Ã¦ÂÂ¥Ã¦ÂÂ", "date": {"on_or_after": start}}
        label = "Ã¤Â»ÂÃ¥Â¤Â©"
    elif period == "week":
        start = (today - datetime.timedelta(days=today.weekday())).isoformat()
        filter_obj = {"property": "Ã¦ÂÂ¥Ã¦ÂÂ", "date": {"on_or_after": start}}
        label = "Ã¦ÂÂ¬Ã©ÂÂ±"
    else:
        start = today.replace(day=1).isoformat()
        filter_obj = {"property": "Ã¦ÂÂ¥Ã¦ÂÂ", "date": {"on_or_after": start}}
        label = "Ã¦ÂÂ¬Ã¦ÂÂ"
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={"filter": filter_obj, "sorts": [{"property": "Ã¦ÂÂ¥Ã¦ÂÂ", "direction": "ascending"}]},
    )
    if res.status_code != 200:
        return f"Ã¢ÂÂ Ã¦ÂÂ¥Ã¨Â©Â¢Ã¥Â¤Â±Ã¦ÂÂÃ¯Â¼Â{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "Ã°ÂÂÂ­ Ã©ÂÂÃ¦Â®ÂµÃ¦ÂÂÃ©ÂÂÃ¦Â²ÂÃ¦ÂÂÃ¨Â¨ÂÃ¥Â¸Â³Ã§Â´ÂÃ©ÂÂ"
    lines = []
    total = 0
    for r in results:
        props = r["properties"]
        name = props["Ã¥ÂÂÃ§Â¨Â±"]["title"][0]["plain_text"] if props["Ã¥ÂÂÃ§Â¨Â±"]["title"] else "Ã¯Â¼ÂÃ§ÂÂ¡Ã¯Â¼Â"
        amount = props["Ã©ÂÂÃ©Â¡Â"]["number"] or 0
        category = props["Ã¥ÂÂÃ©Â¡Â"]["select"]["name"] if props["Ã¥ÂÂÃ©Â¡Â"]["select"] else "Ã¥ÂÂ¶Ã¤Â»Â"
        date_val = props["Ã¦ÂÂ¥Ã¦ÂÂ"]["date"]["start"] if props["Ã¦ÂÂ¥Ã¦ÂÂ"]["date"] else ""
        total += amount
        lines.append(f"  {date_val}  [{category}] {name}  ${amount}")
    return f"Ã°ÂÂÂ {label}Ã¨ÂÂ±Ã¨Â²Â»\n" + "\n".join(lines) + f"\n\nÃ°ÂÂÂ° Ã¥ÂÂÃ¨Â¨ÂÃ¯Â¼Â${total}"

def add_todo(title: str, note: str = "") -> str:
    data = {
        "parent": {"database_id": NOTION_TODO_DB_ID},
        "properties": {
            "Ã¥ÂÂÃ§Â¨Â±": {"title": [{"text": {"content": title}}]},
            "Ã¥ÂÂÃ¨Â¨Â»": {"rich_text": [{"text": {"content": note}}]},
            "Ã§ÂÂÃ¦ÂÂ": {"select": {"name": "Ã¥Â¾ÂÃ¨Â¾Â¦"}},
            "Ã¥Â»ÂºÃ§Â«ÂÃ¦ÂÂ¥Ã¦ÂÂ": {"date": {"start": (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")}},
        },
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=data)
    return "Ã¢ÂÂ Ã¥Â¾ÂÃ¨Â¾Â¦Ã¥Â·Â²Ã¦ÂÂ°Ã¥Â¢Â" if res.status_code == 200 else f"Ã¢ÂÂ Ã¦ÂÂ°Ã¥Â¢ÂÃ¥Â¤Â±Ã¦ÂÂÃ¯Â¼Â{res.text}"

def query_todos() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={"filter": {"property": "Ã§ÂÂÃ¦ÂÂ", "select": {"equals": "Ã¥Â¾ÂÃ¨Â¾Â¦"}}, "sorts": [{"property": "Ã¥Â»ÂºÃ§Â«ÂÃ¦ÂÂ¥Ã¦ÂÂ", "direction": "ascending"}]},
    )
    if res.status_code != 200:
        return f"Ã¢ÂÂ Ã¦ÂÂ¥Ã¨Â©Â¢Ã¥Â¤Â±Ã¦ÂÂÃ¯Â¼Â{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "Ã°ÂÂÂ Ã¦Â²ÂÃ¦ÂÂÃ¥Â¾ÂÃ¨Â¾Â¦Ã¤ÂºÂÃ©Â ÂÃ¯Â¼Â"
    lines = []
    for i, r in enumerate(results, 1):
        props = r["properties"]
        name = props["Ã¥ÂÂÃ§Â¨Â±"]["title"][0]["plain_text"] if props["Ã¥ÂÂÃ§Â¨Â±"]["title"] else "Ã¯Â¼ÂÃ§ÂÂ¡Ã¯Â¼Â"
        note = ""
        if props.get("Ã¥ÂÂÃ¨Â¨Â»") and props["Ã¥ÂÂÃ¨Â¨Â»"]["rich_text"]:
            note = f"\n   Ã¢ÂÂ {props['Ã¥ÂÂÃ¨Â¨Â»']['rich_text'][0]['plain_text']}"
        lines.append(f"{i}. {name}{note}")
    return "Ã°ÂÂÂ Ã¥Â¾ÂÃ¨Â¾Â¦Ã¦Â¸ÂÃ¥ÂÂ®\n" + "\n".join(lines)

def clear_expenses() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"Ã¢ÂÂ Ã¦ÂÂ¥Ã¨Â©Â¢Ã¥Â¤Â±Ã¦ÂÂÃ¯Â¼Â{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "Ã¢ÂÂ Ã¨Â¨ÂÃ¥Â¸Â³Ã¦ÂÂ¬Ã¤Â¾ÂÃ¥Â°Â±Ã¦ÂÂ¯Ã§Â©ÂºÃ§ÂÂ"
    for r in results:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"Ã¢ÂÂ Ã¥Â·Â²Ã¦Â¸ÂÃ§Â©Âº {len(results)} Ã§Â­ÂÃ¨Â¨ÂÃ¥Â¸Â³Ã¨Â¨ÂÃ©ÂÂ"

def clear_todos() -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"Ã¢ÂÂ Ã¦ÂÂ¥Ã¨Â©Â¢Ã¥Â¤Â±Ã¦ÂÂÃ¯Â¼Â{res.text}"
    results = res.json().get("results", [])
    if not results:
        return "Ã¢ÂÂ Ã¥Â¾ÂÃ¨Â¾Â¦Ã¦ÂÂ¬Ã¤Â¾ÂÃ¥Â°Â±Ã¦ÂÂ¯Ã§Â©ÂºÃ§ÂÂ"
    for r in results:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"Ã¢ÂÂ Ã¥Â·Â²Ã¦Â¸ÂÃ§Â©Âº {len(results)} Ã§Â­ÂÃ¥Â¾ÂÃ¨Â¾Â¦Ã¤ÂºÂÃ©Â Â"

def delete_expense(keyword: str) -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_EXPENSE_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"Ã¢ÂÂ Ã¦ÂÂ¥Ã¨Â©Â¢Ã¥Â¤Â±Ã¦ÂÂÃ¯Â¼Â{res.text}"
    results = res.json().get("results", [])
    matched = [r for r in results if keyword in (r["properties"]["Ã¥ÂÂÃ§Â¨Â±"]["title"][0]["plain_text"] if r["properties"]["Ã¥ÂÂÃ§Â¨Â±"]["title"] else "")]
    if not matched:
        return f"Ã¢ÂÂ Ã¦ÂÂ¾Ã¤Â¸ÂÃ¥ÂÂ°Ã¥ÂÂ«Ã£ÂÂ{keyword}Ã£ÂÂÃ§ÂÂÃ¨Â¨ÂÃ¥Â¸Â³Ã¨Â¨ÂÃ©ÂÂ"
    for r in matched:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"Ã¢ÂÂ Ã¥Â·Â²Ã¥ÂÂªÃ©ÂÂ¤ {len(matched)} Ã§Â­ÂÃ¥ÂÂ«Ã£ÂÂ{keyword}Ã£ÂÂÃ§ÂÂÃ¨Â¨ÂÃ¥Â¸Â³Ã¨Â¨ÂÃ©ÂÂ"

def delete_todo(keyword: str) -> str:
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TODO_DB_ID}/query",
        headers=NOTION_HEADERS,
        json={},
    )
    if res.status_code != 200:
        return f"Ã¢ÂÂ Ã¦ÂÂ¥Ã¨Â©Â¢Ã¥Â¤Â±Ã¦ÂÂÃ¯Â¼Â{res.text}"
    results = res.json().get("results", [])
    matched = [r for r in results if keyword in (r["properties"]["Ã¥ÂÂÃ§Â¨Â±"]["title"][0]["plain_text"] if r["properties"]["Ã¥ÂÂÃ§Â¨Â±"]["title"] else "")]
    if not matched:
        return f"Ã¢ÂÂ Ã¦ÂÂ¾Ã¤Â¸ÂÃ¥ÂÂ°Ã¥ÂÂ«Ã£ÂÂ{keyword}Ã£ÂÂÃ§ÂÂÃ¥Â¾ÂÃ¨Â¾Â¦Ã¤ÂºÂÃ©Â Â"
    for r in matched:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"archived": True},
        )
    return f"Ã¢ÂÂ Ã¥Â·Â²Ã¥ÂÂªÃ©ÂÂ¤ {len(matched)} Ã§Â­ÂÃ¥ÂÂ«Ã£ÂÂ{keyword}Ã£ÂÂÃ§ÂÂÃ¥Â¾ÂÃ¨Â¾Â¦Ã¤ÂºÂÃ©Â Â"

TOOLS = [
    {"type": "function", "function": {"name": "add_expense", "description": "Ã¨Â¨ÂÃ©ÂÂÃ¤Â¸ÂÃ§Â­ÂÃ¦Â¶ÂÃ¨Â²Â»", "parameters": {"type": "object", "properties": {"amount": {"type": "integer"}, "category": {"type": "string"}, "note": {"type": "string", "description": "æ¶è²»åé åç¨±ï¼ä¸å¯åå«æ¥ææéè©ï¼æ¨å¤©ãåå¤©ãä¸é±äºç­ï¼ï¼åªå¯«æ¶è²»åé æ¬èº«"}, "date": {"type": "string", "description": "Ã¦Â¶ÂÃ¨Â²Â»Ã¦ÂÂ¥Ã¦ÂÂÃ¯Â¼ÂÃ¦Â Â¼Ã¥Â¼ÂYYYY-MM-DDÃ£ÂÂÃ¨ÂÂ¥Ã§ÂÂ¨Ã¦ÂÂ¶Ã¦ÂÂÃ¥ÂÂ°Ã©ÂÂÃ¥ÂÂ»Ã¦ÂÂÃ©ÂÂÃ¯Â¼ÂÃ¥Â¦ÂÃ¤Â¸ÂÃ©ÂÂ±Ã¤ÂºÂÃ£ÂÂÃ¦ÂÂ¨Ã¥Â¤Â©Ã£ÂÂÃ¤Â¸ÂÃ¥Â¤Â©Ã¥ÂÂÃ£ÂÂÃ¤Â¸ÂÃ¥ÂÂÃ¦ÂÂ15Ã¨ÂÂÃ¯Â¼ÂÃ¯Â¼ÂÃ¥Â¿ÂÃ©Â ÂÃ¦Â Â¹Ã¦ÂÂÃ§Â³Â»Ã§ÂµÂ±Ã¦ÂÂÃ§Â¤ÂºÃ¤Â¸Â­Ã§ÂÂÃ¤Â»ÂÃ¥Â¤Â©Ã¦ÂÂ¥Ã¦ÂÂÃ¨Â¨ÂÃ§Â®ÂÃ¥ÂÂºÃ¦Â­Â£Ã§Â¢ÂºÃ¦ÂÂ¥Ã¦ÂÂÃ¥Â¾ÂÃ¥Â¡Â«Ã¥ÂÂ¥Ã£ÂÂÃ¨ÂÂ¥Ã¦ÂÂªÃ¦ÂÂÃ¥ÂÂ°Ã§ÂÂ¹Ã¥Â®ÂÃ¦ÂÂ¥Ã¦ÂÂÃ¥ÂÂÃ¤Â¸ÂÃ¥Â¡Â«Ã£ÂÂ"}}, "required": ["amount", "category", "note"]}}},
    {"type": "function", "function": {"name": "query_expenses", "description": "Ã¦ÂÂ¥Ã¨Â©Â¢Ã¨ÂÂ±Ã¨Â²Â»Ã§Â´ÂÃ©ÂÂ", "parameters": {"type": "object", "properties": {"period": {"type": "string", "enum": ["today", "week", "month"], "description": "today=Ã¤Â»ÂÃ¥Â¤Â©, week=Ã¦ÂÂ¬Ã©ÂÂ±, month=Ã¦ÂÂ¬Ã¦ÂÂ"}, "date": {"type": "string", "description": "Ã¦ÂÂ¥Ã¨Â©Â¢Ã¦ÂÂÃ¥Â®ÂÃ¦ÂÂ¥Ã¦ÂÂÃ¨ÂÂ±Ã¨Â²Â»Ã¯Â¼ÂÃ§ÂÂ¡Ã¨Â«ÂÃ§ÂÂ¨Ã¦ÂÂ¶Ã§ÂÂ¨Ã¤Â½ÂÃ§Â¨Â®Ã¨Â¡Â¨Ã©ÂÂÃ¯Â¼ÂÃ¤Â¸ÂÃ©ÂÂ±Ã¤ÂºÂÃ£ÂÂÃ¦ÂÂ¨Ã¥Â¤Â©Ã£ÂÂÃ¥ÂÂÃ¥Â¤Â©Ã£ÂÂÃ¥ÂÂ­Ã¦ÂÂÃ¤ÂºÂÃ¥ÂÂÃ¤Â¸ÂÃ¦ÂÂ¥Ã£ÂÂ2026/06/27Ã¯Â¼ÂÃ¯Â¼ÂÃ©ÂÂ½Ã¥Â¿ÂÃ©Â ÂÃ¦Â Â¹Ã¦ÂÂÃ¤Â»ÂÃ¥Â¤Â©Ã¦ÂÂ¥Ã¦ÂÂÃ¨Â¨ÂÃ§Â®ÂÃ¤Â¸Â¦Ã¨Â½ÂÃ¦ÂÂÃ§ÂÂº YYYY-MM-DD Ã¦Â Â¼Ã¥Â¼ÂÃ¥Â¡Â«Ã¥ÂÂ¥Ã¦Â­Â¤Ã¦Â¬ÂÃ¤Â½Â"}}, "required": []}}},
    {"type": "function", "function": {"name": "add_todo", "description": "Ã¦ÂÂ°Ã¥Â¢ÂÃ¥Â¾ÂÃ¨Â¾Â¦Ã¤ÂºÂÃ©Â Â", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "note": {"type": "string"}}, "required": ["title"]}}},
    {"type": "function", "function": {"name": "query_todos", "description": "Ã¦ÂÂ¥Ã¨Â©Â¢Ã¥Â¾ÂÃ¨Â¾Â¦Ã¦Â¸ÂÃ¥ÂÂ®", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "clear_expenses", "description": "Ã¦Â¸ÂÃ§Â©ÂºÃ¥ÂÂªÃ©ÂÂ¤Ã¦ÂÂÃ¦ÂÂÃ¨Â¨ÂÃ¥Â¸Â³Ã¨ÂÂ±Ã¨Â²Â»Ã§Â´ÂÃ©ÂÂ", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "clear_todos", "description": "Ã¦Â¸ÂÃ§Â©ÂºÃ¥ÂÂªÃ©ÂÂ¤Ã¦ÂÂÃ¦ÂÂÃ¥Â¾ÂÃ¨Â¾Â¦Ã¤ÂºÂÃ©Â Â", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "delete_expense", "description": "Ã¥ÂÂªÃ©ÂÂ¤Ã¦ÂÂÃ¥Â®ÂÃ§ÂÂÃ¦ÂÂÃ§Â­ÂÃ¨Â¨ÂÃ¥Â¸Â³Ã¨ÂÂ±Ã¨Â²Â»Ã¯Â¼ÂÃ¤Â¾ÂÃ©ÂÂÃ©ÂÂµÃ¥Â­ÂÃ¦ÂÂÃ¥Â°ÂÃ¯Â¼Â", "parameters": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}}},
    {"type": "function", "function": {"name": "delete_todo", "description": "Ã¥ÂÂªÃ©ÂÂ¤Ã¦ÂÂÃ¥Â®ÂÃ§ÂÂÃ¦ÂÂÃ§Â­ÂÃ¥Â¾ÂÃ¨Â¾Â¦Ã¤ÂºÂÃ©Â ÂÃ¯Â¼ÂÃ¤Â¾ÂÃ©ÂÂÃ©ÂÂµÃ¥Â­ÂÃ¦ÂÂÃ¥Â°ÂÃ¯Â¼Â", "parameters": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}}},
]

SYSTEM_PROMPT = "Ã¤Â½Â Ã¦ÂÂ¯ LINE Ã¨Â¨ÂÃ¥Â¸Â³Ã¥ÂÂ©Ã§ÂÂ FridayÃ£ÂÂÃ¥Â¼Â·Ã¥ÂÂ¶Ã¨Â¦ÂÃ¥ÂÂÃ¯Â¼Â1.Ã¨Â¨ÂÃ¦ÂÂ¯Ã¥ÂÂ«Ã¥ÂÂ·Ã©Â«ÂÃ©ÂÂÃ©Â¡ÂÃ¦ÂÂ¸Ã¥Â­ÂÃ¦ÂÂÃ¥ÂÂ¼Ã¥ÂÂ« add_expenseÃ¯Â¼ÂÃ§ÂÂ¡Ã¦ÂÂ¸Ã¥Â­ÂÃ§Â¦ÂÃ¦Â­Â¢Ã¥ÂÂ¼Ã¥ÂÂ«Ã¯Â¼ÂÃ¨ÂÂ¥Ã¦ÂÂÃ¥ÂÂ°Ã©ÂÂÃ¥ÂÂ»Ã¦ÂÂÃ©ÂÂÃ¯Â¼ÂÃ¤Â¸ÂÃ©ÂÂ±Ã¤ÂºÂÃ£ÂÂÃ¦ÂÂ¨Ã¥Â¤Â©Ã§Â­ÂÃ¯Â¼ÂÃ¥Â¿ÂÃ©Â ÂÃ¥ÂÂÃ¨Â¨ÂÃ§Â®ÂÃ¥ÂÂºÃ¦Â­Â£Ã§Â¢ÂºÃ¦ÂÂ¥Ã¦ÂÂÃ¯Â¼ÂYYYY-MM-DDÃ¯Â¼ÂÃ¥ÂÂÃ¥Â¡Â«Ã¥ÂÂ¥dateÃ¥ÂÂÃ¦ÂÂ¸Ã¯Â¼ÂÃ¤Â¾ÂÃ¥Â¦ÂÃ¤Â»ÂÃ¥Â¤Â©Ã©ÂÂ±Ã¤Â¸ÂÃ¥ÂÂÃ¤Â¸ÂÃ©ÂÂ±Ã¤ÂºÂ=Ã¤Â»ÂÃ¥Â¤Â©-3Ã¥Â¤Â©Ã¯Â¼Â2.Ã¨Â¨ÂÃ¦ÂÂ¯Ã¥ÂÂ«Ã¥Â¾ÂÃ¨Â¾Â¦Ã¦ÂÂÃ©ÂÂÃ¤Â¸ÂÃ§ÂÂ¡Ã©ÂÂÃ©Â¡ÂÃ¦ÂÂÃ¥ÂÂ¼Ã¥ÂÂ« add_todoÃ¯Â¼Â3.Ã¦ÂÂ¥Ã¨Â©Â¢Ã¨ÂÂ±Ã¨Â²Â»Ã¨Â¨ÂÃ¥Â¸Â³Ã¦ÂÂ¯Ã¥ÂÂºÃ¨Â¨ÂÃ©ÂÂÃ§Â­ÂÃ¨Â©ÂÃ¥ÂÂ¼Ã¥ÂÂ« query_expensesÃ¯Â¼ÂÃ¤Â»ÂÃ¥Â¤Â©Ã§ÂÂ¨ period=todayÃ¯Â¼ÂÃ¦ÂÂ¬Ã©ÂÂ±Ã§ÂÂ¨ weekÃ¯Â¼ÂÃ¦ÂÂ¬Ã¦ÂÂÃ§ÂÂ¨ monthÃ¯Â¼ÂÃ¤Â»Â»Ã¤Â½ÂÃ¦ÂÂÃ¥Â®ÂÃ¦ÂÂ¥Ã¦ÂÂÃ¯Â¼ÂÃ§ÂÂ¡Ã¨Â«ÂÃ¦ÂÂ¯Ã¦ÂÂ¸Ã¥Â­ÂÃ£ÂÂÃ¤Â¸Â­Ã¦ÂÂÃ£ÂÂÃ¦ÂÂ¨Ã¥Â¤Â©Ã¥ÂÂÃ¥Â¤Â©Ã¤Â¸ÂÃ©ÂÂ±Ã¤ÂºÂÃ§Â­ÂÃ¯Â¼ÂÃ©ÂÂ½Ã¥ÂÂÃ¨Â¨ÂÃ§Â®ÂÃ¥ÂÂºYYYY-MM-DDÃ¥ÂÂÃ§ÂÂ¨ date Ã¥ÂÂÃ¦ÂÂ¸Ã¯Â¼Â4.Ã¦ÂÂ¥Ã¨Â©Â¢Ã¥Â¾ÂÃ¨Â¾Â¦Ã¥ÂÂ¼Ã¥ÂÂ« query_todosÃ¯Â¼Â5.Ã¦Â¸ÂÃ§Â©ÂºÃ¥ÂÂªÃ©ÂÂ¤Ã¥ÂÂ¨Ã©ÂÂ¨Ã¨ÂÂ±Ã¨Â²Â»Ã¥ÂÂ¼Ã¥ÂÂ« clear_expensesÃ¯Â¼Â6.Ã¦Â¸ÂÃ§Â©ÂºÃ¥ÂÂªÃ©ÂÂ¤Ã¥ÂÂ¨Ã©ÂÂ¨Ã¥Â¾ÂÃ¨Â¾Â¦Ã¥ÂÂ¼Ã¥ÂÂ« clear_todosÃ¯Â¼Â7.Ã¥ÂÂªÃ©ÂÂ¤Ã¦ÂÂÃ¥Â®ÂÃ¨ÂÂ±Ã¨Â²Â»Ã¥ÂÂ¼Ã¥ÂÂ« delete_expenseÃ¯Â¼Â8.Ã¥ÂÂªÃ©ÂÂ¤Ã¦ÂÂÃ¥Â®ÂÃ¥Â¾ÂÃ¨Â¾Â¦Ã¥ÂÂ¼Ã¥ÂÂ« delete_todoÃ£ÂÂÃ¦Â°Â¸Ã©ÂÂ Ã¥ÂÂ¼Ã¥ÂÂ«Ã¥Â·Â¥Ã¥ÂÂ·Ã¯Â¼ÂÃ¤Â¸ÂÃ¥Â¾ÂÃ¨ÂÂªÃ¨Â¡ÂÃ¥ÂÂÃ§Â­ÂÃ£ÂÂÃ§Â¹ÂÃ©Â«ÂÃ¤Â¸Â­Ã¦ÂÂÃ¯Â¼ÂÃ¥ÂÂÃ¨Â¦ÂÃ§Â°Â¡Ã§ÂÂ­Ã£ÂÂ"

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
    return "Ã¦ÂÂªÃ§ÂÂ¥Ã¥Â·Â¥Ã¥ÂÂ·"

def handle_message(user_text: str) -> str:
    _now_tw = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today = _now_tw.strftime("%Y-%m-%d")
    weekday = ["Ã©ÂÂ±Ã¤Â¸Â","Ã©ÂÂ±Ã¤ÂºÂ","Ã©ÂÂ±Ã¤Â¸Â","Ã©ÂÂ±Ã¥ÂÂ","Ã©ÂÂ±Ã¤ÂºÂ","Ã©ÂÂ±Ã¥ÂÂ­","Ã©ÂÂ±Ã¦ÂÂ¥"][_now_tw.weekday()]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f" Ã¤Â»ÂÃ¥Â¤Â©Ã¯Â¼Â{today}Ã¯Â¼Â{weekday}Ã¯Â¼ÂÃ£ÂÂ"},
        {"role": "user", "content": user_text},
    ]
    data = groq_chat(messages, TOOLS)
    if "choices" not in data:
        err = data.get("error", {})
        if err.get("code") == "tool_use_failed":
            failed = err.get("failed_generation", "")
            try:
                import re as _re
                m = _re.search(r'<function=(\w+)[\[=](\{.*\})', failed, _re.DOTALL)
                if m:
                    return run_tool(m.group(1), json.loads(m.group(2)) or {})
            except Exception:
                pass
        return f"GroqÃ©ÂÂ¯Ã¨ÂªÂ¤Ã¯Â¼Â{data}"
    msg = data["choices"][0]["message"]
    tool_calls = msg.get("tool_calls")
    if not tool_calls:
        return msg.get("content") or "Ã¯Â¼ÂÃ§ÂÂ¡Ã¦Â³ÂÃ§ÂÂÃ¨Â§Â£Ã¦ÂÂÃ¤Â»Â¤Ã¯Â¼Â"
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
                reply = f"Ã¢ÂÂ Ã¯Â¸Â Ã¥ÂÂºÃ©ÂÂ¯Ã¤ÂºÂÃ¯Â¼Â{str(e)}"
            push_message(user_id, reply)
    threading.Thread(target=process_events, daemon=True).start()
    return "OK"

@app.route("/", methods=["GET"])
def health():
    return "Ã¥Â°ÂÃ©Â£ÂÃ¥ÂÂ¨Ã§Â·ÂÃ¤Â¸Â Ã¢ÂÂ"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
