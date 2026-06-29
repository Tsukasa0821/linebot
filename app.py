import os
import re
import json
import hashlib
import hmac
import base64
import datetime
import requests
import threading
from flask import Flask, request, abort

app = Flask(__name__)

# Pending delete confirmations per user
pending_delete = {}  # {user_id: {"name": str, "args": dict}}
DELETE_TOOLS = {"clear_expenses", "clear_todos", "delete_expense", "delete_todo"}

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


def _notion_query_all(database_id: str, payload: dict):
    """Fetch all pages from Notion database with automatic pagination."""
    results = []
    start_cursor = None
    while True:
        body = dict(payload)
        if start_cursor:
            body["start_cursor"] = start_cursor
        res = requests.post(
            f"https://api.notion.com/v1/databases/{database_id}/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        if res.status_code != 200:
            return None, res.text
        data = res.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
    return results, None


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


def _tw_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def add_expense(amount: int, category: str, note: str, date: str = None) -> str:
    expense_date = date if date else _tw_now().strftime("%Y-%m-%d")
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


def query_expenses(period: str = "month", date: str = None, year_month: str = None) -> str:
    today = _tw_now().date()
    if date:
        date = date.replace("/", "-").replace(".", "-")
        filter_obj = {"property": "日期", "date": {"equals": date}}
        label = date
        payload = {"filter": filter_obj}
    elif year_month:
        year_month = year_month.replace("/", "-").replace(".", "-")
        yr, mo = map(int, year_month.split("-"))
        start = f"{yr:04d}-{mo:02d}-01"
        if mo == 12:
            end_dt = datetime.date(yr + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            end_dt = datetime.date(yr, mo + 1, 1) - datetime.timedelta(days=1)
        filter_obj = {"and": [{"property": "日期", "date": {"on_or_after": start}}, {"property": "日期", "date": {"on_or_before": end_dt.isoformat()}}]}
        label = f"{yr}年{mo}月"
        payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
    elif period == "last_month":
        first_of_this_month = today.replace(day=1)
        lm_end = first_of_this_month - datetime.timedelta(days=1)
        lm_start = lm_end.replace(day=1)
        filter_obj = {"and": [{"property": "日期", "date": {"on_or_after": lm_start.isoformat()}}, {"property": "日期", "date": {"on_or_before": lm_end.isoformat()}}]}
        label = f"{lm_start.year}年{lm_start.month}月"
        payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
    elif period == "today":
        start = today.isoformat()
        filter_obj = {"property": "日期", "date": {"on_or_after": start}}
        label = "今天"
        payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
    elif period == "week":
        start = (today - datetime.timedelta(days=today.weekday())).isoformat()
        filter_obj = {"property": "日期", "date": {"on_or_after": start}}
        label = "本週"
        payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
    else:
        start = today.replace(day=1).isoformat()
        filter_obj = {"property": "日期", "date": {"on_or_after": start}}
        label = "本月"
        payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
    results, qerr = _notion_query_all(NOTION_EXPENSE_DB_ID, payload)
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    if not results:
        return f"💭 {label}沒有記帳紀錄"
    lines = []
    total = 0
    for r in results:
        props = r["properties"]
        name = props["名稱"]["title"][0]["plain_text"] if props["名稱"]["title"] else "（無）"
        amount = props["金額"]["number"] or 0
        category = props["分類"]["select"]["name"] if props["分類"]["select"] else "其他"
        d = props["日期"]["date"]["start"] if props["日期"]["date"] else ""
        total += amount
        lines.append(f"  {d}  [{category}] {name}  ${amount}")
    return f"📊 {label}花費\n" + "\n".join(lines) + f"\n\n💰 合計：${total}"

def add_todo(title: str, note: str = "") -> str:
    data = {
        "parent": {"database_id": NOTION_TODO_DB_ID},
        "properties": {
            "名稱": {"title": [{"text": {"content": title}}]},
            "備註": {"rich_text": [{"text": {"content": note}}]},
            "狀態": {"select": {"name": "待辦"}},
            "建立日期": {"date": {"start": _tw_now().strftime("%Y-%m-%d")}},
        },
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=data)
    return "✅ 待辦已新增" if res.status_code == 200 else f"❌ 新增失敗：{res.text}"


def query_todos() -> str:
    todo_payload = {"filter": {"property": "狀態", "select": {"equals": "待辦"}}, "sorts": [{"property": "建立日期", "direction": "ascending"}]}
    results, qerr = _notion_query_all(NOTION_TODO_DB_ID, todo_payload)
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
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
    results, qerr = _notion_query_all(NOTION_EXPENSE_DB_ID, {})
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    if not results:
        return "✅ 記帳本來就是空的"
    for r in results:
        requests.patch(f"https://api.notion.com/v1/pages/{r['id']}", headers=NOTION_HEADERS, json={"archived": True})
    return f"✅ 已清空 {len(results)} 筆記帳記錄"


def clear_todos() -> str:
    results, qerr = _notion_query_all(NOTION_TODO_DB_ID, {})
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    if not results:
        return "✅ 待辦本來就是空的"
    for r in results:
        requests.patch(f"https://api.notion.com/v1/pages/{r['id']}", headers=NOTION_HEADERS, json={"archived": True})
    return f"✅ 已清空 {len(results)} 筆待辦事項"


def delete_expense(keyword: str, date: str = None, amount: int = None) -> str:
    results, qerr = _notion_query_all(NOTION_EXPENSE_DB_ID, {})
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    matched = []
    for r in results:
        props = r["properties"]
        name = props["名稱"]["title"][0]["plain_text"] if props["名稱"]["title"] else ""
        if keyword and keyword not in name:
            continue
        if date:
            r_date = props["日期"]["date"]["start"] if props["日期"]["date"] else ""
            if r_date != date:
                continue
        if amount is not None:
            r_amount = props["金額"]["number"] or 0
            if r_amount != amount:
                continue
        matched.append(r)
    if not matched:
        return "❌ 找不到符合條件的記帳記錄"
    for r in matched:
        requests.patch(f"https://api.notion.com/v1/pages/{r['id']}", headers=NOTION_HEADERS, json={"archived": True})
    return f"✅ 已刪除 {len(matched)} 筆記帳記錄"


def delete_todo(keyword: str) -> str:
    results, qerr = _notion_query_all(NOTION_TODO_DB_ID, {})
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    matched = [r for r in results if keyword in (r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else "")]
    if not matched:
        return f"❌ 找不到含「{keyword}」的待辦事項"
    for r in matched:
        requests.patch(f"https://api.notion.com/v1/pages/{r['id']}", headers=NOTION_HEADERS, json={"archived": True})
    return f"✅ 已刪除 {len(matched)} 筆含「{keyword}」的待辦事項"


TOOLS = [
    {"type": "function", "function": {"name": "add_expense", "description": "記錄一筆消費", "parameters": {"type": "object", "properties": {
        "amount": {"type": "integer", "description": "消費金額"},
        "category": {"type": "string", "description": "消費分類"},
        "note": {"type": "string", "description": "消費品項名稱，不可包含日期時間詞（昨天、前天、上週五等），只寫消費品項本身"},
        "date": {"type": "string", "description": "消費日期 YYYY-MM-DD。若用戶提到任何時間表達（上週五、昨天、前天、六月二十七日、2026/06/27），必須根據系統提示中的今天日期計算後填入。若未提到日期則省略。"}
    }, "required": ["amount", "category", "note"]}}},
    {"type": "function", "function": {"name": "query_expenses", "description": "查詢花費紀錄", "parameters": {"type": "object", "properties": {
        "period": {"type": "string", "enum": ["today", "week", "month", "last_month"], "description": "today=今天, week=本週, month=本月, last_month=上個月"},
        "date": {"type": "string", "description": "查詢指定日期花費（YYYY-MM-DD）。任何時間表達都先算出日期再填入。有此參數時忽略其他。"},
        "year_month": {"type": "string", "description": "查詢指定月份全部花費（YYYY-MM）。如「6月」→ 2026-06，「5月明細」→ 2026-05，「2026年3月」→ 2026-03。有此參數時忽略 period。"}
    }, "required": []}}},
    {"type": "function", "function": {"name": "add_todo", "description": "新增待辦事項", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "note": {"type": "string"}}, "required": ["title"]}}},
    {"type": "function", "function": {"name": "query_todos", "description": "查詢待辦清單", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "clear_expenses", "description": "清除記帳花費紀錄，可指定時間範圍；不指定刪全部。", "parameters": {"type": "object", "properties": {
        "period": {"type": "string", "enum": ["today", "week", "month", "last_month", "all"], "description": "today=今天, week=本週, month=本月, last_month=上個月, all=全部"},
        "date": {"type": "string", "description": "指定日期 YYYY-MM-DD"},
        "year_month": {"type": "string", "description": "指定月份 YYYY-MM"}
    }}}},
    {"type": "function", "function": {"name": "clear_todos", "description": "清空刪除所有待辦事項", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "delete_expense", "description": "刪除指定記帳花費。用戶貼上記錄行（如：2026-06-27  [電影] 電影  $260）時，從中提取 keyword=品項名稱、date=日期、amount=金額。", "parameters": {"type": "object", "properties": {
        "keyword": {"type": "string", "description": "記帳品項名稱關鍵字"},
        "date": {"type": "string", "description": "日期 YYYY-MM-DD（可選，用於精確匹配）"},
        "amount": {"type": "integer", "description": "金額（可選，用於精確匹配）"}
    }, "required": ["keyword"]}}},
    {"type": "function", "function": {"name": "delete_todo", "description": "刪除指定的某筆待辦事項（依關鍵字搜尋）", "parameters": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]}}},
]

SYSTEM_PROMPT = (
    "你是 LINE 記帳助理 Friday。強制規則："
    "1.訊息含具體金額數字才呼叫 add_expense，無數字禁止呼叫；"
    "若提到過去時間（上週五、昨天等）必須先計算出正確日期（YYYY-MM-DD）再填入 date 參數；"
    "note 只寫消費品項，不可包含日期時間詞；"
    "2.訊息含待辦提醒且無金額才呼叫 add_todo；"
    "3.查詢花費記帳支出記錄等詞呼叫 query_expenses，今天用 period=today，本週用 week，本月用 month，上個月用 last_month；"
    "指定月份（幾月、某月份、YYYY年M月）用 year_month=YYYY-MM；"
    "任何指定日期（無論是數字、中文、昨天前天上週五等）都先計算出 YYYY-MM-DD 再用 date 參數；"
    "4.查詢待辦呼叫 query_todos；"
    "5.清空刪除花費呼叫 clear_expenses；本月用 period=month，上個月用 period=last_month，指定月份用 year_month=YYYY-MM，指定日期用 date=YYYY-MM-DD，不指定時間為全部；"
    "6.清空刪除全部待辦呼叫 clear_todos；"
    "7.刪除指定花費呼叫 delete_expense；"
    "8.刪除指定待辦呼叫 delete_todo。"
    "永遠呼叫工具，不得自行回答。繁體中文，回覆簡短。"
)


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


def _prepare_delete(user_id: str, fname: str, args: dict) -> str:
    if fname == "clear_expenses":
        period = args.get("period", "all")
        date_arg = args.get("date")
        year_month = args.get("year_month")
        today_d = _tw_now().date()
        if date_arg:
            date_arg = date_arg.replace("/", "-").replace(".", "-")
            filter_obj = {"property": "日期", "date": {"equals": date_arg}}
            label = date_arg
            payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
        elif year_month:
            year_month = year_month.replace("/", "-").replace(".", "-")
            yr, mo = map(int, year_month.split("-"))
            start = f"{yr:04d}-{mo:02d}-01"
            if mo == 12:
                end_dt = datetime.date(yr + 1, 1, 1) - datetime.timedelta(days=1)
            else:
                end_dt = datetime.date(yr, mo + 1, 1) - datetime.timedelta(days=1)
            filter_obj = {"and": [{"property": "日期", "date": {"on_or_after": start}}, {"property": "日期", "date": {"on_or_before": end_dt.isoformat()}}]}
            label = f"{yr}年{mo}月"
            payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
        elif period == "last_month":
            first_of_this_month = today_d.replace(day=1)
            lm_end = first_of_this_month - datetime.timedelta(days=1)
            lm_start = lm_end.replace(day=1)
            filter_obj = {"and": [{"property": "日期", "date": {"on_or_after": lm_start.isoformat()}}, {"property": "日期", "date": {"on_or_before": lm_end.isoformat()}}]}
            label = f"{lm_start.year}年{lm_start.month}月"
            payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
        elif period == "today":
            filter_obj = {"property": "日期", "date": {"equals": today_d.isoformat()}}
            label = "今天"
            payload = {"filter": filter_obj}
        elif period == "week":
            start = (today_d - datetime.timedelta(days=today_d.weekday())).isoformat()
            filter_obj = {"property": "日期", "date": {"on_or_after": start}}
            label = "本週"
            payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
        elif period == "month":
            start = today_d.replace(day=1).isoformat()
            filter_obj = {"property": "日期", "date": {"on_or_after": start}}
            label = "本月"
            payload = {"filter": filter_obj, "sorts": [{"property": "日期", "direction": "ascending"}]}
        else:
            payload = {}
            label = "全部"
        results, qerr = _notion_query_all(NOTION_EXPENSE_DB_ID, payload)
        if qerr:
            return f"❌ 查詢失敗：{qerr}"
        if not results:
            return f"💭 {label}記帳本來就是空的"
        lines = []
        total = 0
        for r in results:
            props = r["properties"]
            n = props["名稱"]["title"][0]["plain_text"] if props["名稱"]["title"] else "（無）"
            amt = props["金額"]["number"] or 0
            cat = props["分類"]["select"]["name"] if props["分類"]["select"] else "其他"
            d = props["日期"]["date"]["start"] if props["日期"]["date"] else ""
            total += amt
            lines.append(f"  {d}  [{cat}] {n}  ${amt}")
        ids = [r["id"] for r in results]
        pending_delete[user_id] = {"page_ids": ids}
        desc = "\n".join(lines)
        return f"⚠️ 確認要刪除{label}以下 {len(ids)} 筆記帳紀錄嗎？\n\n{desc}\n💰 合計：${total}\n\n請回覆【確認刪除】確認，或回覆【取消】取消。"
    elif fname == "clear_todos":
        results, qerr = _notion_query_all(NOTION_TODO_DB_ID, {})
        if qerr:
            return f"❌ 查詢失敗：{qerr}"
        if not results:
            return "💭 待辦本來就是空的"
        lines = ["  • " + (r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else "（無）") for r in results]
        ids = [r["id"] for r in results]
        pending_delete[user_id] = {"page_ids": ids}
        desc = "\n".join(lines)
        return f"⚠️ 確認要刪除所有 {len(ids)} 筆待辦事項嗎？\n\n{desc}\n\n請回覆【確認刪除】確認，或回覆【取消】取消。"
    elif fname == "delete_expense":
        keyword = args.get("keyword", "")
        date = args.get("date")
        amount = args.get("amount")
        results, qerr = _notion_query_all(NOTION_EXPENSE_DB_ID, {})
        if qerr:
            return f"❌ 查詢失敗：{qerr}"
        matched = []
        for r in results:
            props = r["properties"]
            name = props["名稱"]["title"][0]["plain_text"] if props["名稱"]["title"] else ""
            if keyword and keyword not in name:
                continue
            if date:
                r_date = props["日期"]["date"]["start"] if props["日期"]["date"] else ""
                if r_date != date:
                    continue
            if amount is not None:
                r_amount = props["金額"]["number"] or 0
                if r_amount != amount:
                    continue
            matched.append(r)
        if not matched:
            return "❌ 找不到符合條件的記帳記錄"
        lines = []
        for r in matched:
            props = r["properties"]
            n = props["名稱"]["title"][0]["plain_text"] if props["名稱"]["title"] else "（無）"
            amt = props["金額"]["number"] or 0
            cat = props["分類"]["select"]["name"] if props["分類"]["select"] else "其他"
            d = props["日期"]["date"]["start"] if props["日期"]["date"] else ""
            lines.append(f"  {d}  [{cat}] {n}  ${amt}")
        ids = [r["id"] for r in matched]
        pending_delete[user_id] = {"page_ids": ids}
        desc = "\n".join(lines)
        return f"⚠️ 確認要刪除以下 {len(ids)} 筆記帳紀錄嗎？\n\n{desc}\n\n請回覆【確認刪除】確認，或回覆【取消】取消。"
    elif fname == "delete_todo":
        keyword = args.get("keyword", "")
        results, qerr = _notion_query_all(NOTION_TODO_DB_ID, {})
        if qerr:
            return f"❌ 查詢失敗：{qerr}"
        matched = [r for r in results if keyword in (r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else "")]
        if not matched:
            return f"❌ 找不到含「{keyword}」的待辦事項"
        lines = ["  • " + (r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else "（無）") for r in matched]
        ids = [r["id"] for r in matched]
        pending_delete[user_id] = {"page_ids": ids}
        desc = "\n".join(lines)
        return f"⚠️ 確認要刪除以下 {len(ids)} 筆待辦事項嗎？\n\n{desc}\n\n請回覆【確認刪除】確認，或回覆【取消】取消。"
    return "❌ 未知操作"


def handle_message(user_text: str, user_id: str = "") -> str:
    _now_tw = _tw_now()
    today = _now_tw.strftime("%Y-%m-%d")
    weekday = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][_now_tw.weekday()]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f" 今天：{today}（{weekday}）。"},
        {"role": "user", "content": user_text},
    ]
    data = groq_chat(messages, TOOLS)
    if "choices" in data:
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content") or "（無法理解指令）"
        results = []
        for tc in tool_calls:
            fname = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"]) or {}
            if fname in DELETE_TOOLS and user_id:
                return _prepare_delete(user_id, fname, args)
            result = run_tool(fname, args)
            results.append(result)
        return "\n".join(results)
    err = data.get("error", {})
    if err.get("code") == "tool_use_failed":
        failed = err.get("failed_generation", "")
        fname_m = re.search(r"<function=(\w+)", failed)
        args_m = re.search(r"(\{[^{}]*\})", failed)
        if fname_m:
            fname = fname_m.group(1)
            args = json.loads(args_m.group(1)) if args_m else {}
            if fname in DELETE_TOOLS and user_id:
                return _prepare_delete(user_id, fname, args)
            return run_tool(fname, args)
    return f"Groq錯誤：{data}"


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
            user_text = event["message"]["text"].strip()
            user_id = event["source"]["userId"]
            # Handle pending delete confirmation
            if user_id in pending_delete:
                if user_text == "確認刪除":
                    op = pending_delete.pop(user_id)
                    page_ids = op.get("page_ids", [])
                    try:
                        for pid in page_ids:
                            requests.patch(
                                f"https://api.notion.com/v1/pages/{pid}",
                                headers=NOTION_HEADERS,
                                json={"archived": True}
                            )
                        result = f"✅ 已刪除 {len(page_ids)} 筆資料"
                    except Exception as e:
                        result = f"⚠️ 刪除失敗：{str(e)}"
                    push_message(user_id, result)
                    continue
                elif user_text == "取消":
                    pending_delete.pop(user_id)
                    push_message(user_id, "✅ 已取消，資料未刪除。")
                    continue
                elif user_text in ("是", "yes", "YES", "對", "ok", "OK", "好"):
                    push_message(user_id, "⚠️ 請輸入【確認刪除】才可執行刪除。")
                    continue
                else:
                    pending_delete.pop(user_id)
            try:
                reply = handle_message(user_text, user_id)
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

# redeploy
