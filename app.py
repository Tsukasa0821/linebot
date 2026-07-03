import os
import re
import json
import hashlib
import hmac
import base64
import datetime
import requests
import threading
import time
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Pending delete confirmations per user
pending_delete = {}  # {user_id: {"name": str, "args": dict}}
_PENDING_EXPENSE_MSG = {}  # {user_id: original_message} for rule 17
DELETE_TOOLS = {"clear_expenses", "clear_todos", "clear_work_tasks", "delete_expense", "delete_todo"}

_STATE_FILE = "/tmp/friday_state.json"
_STARTUP_TIME = time.time()


def _save_user_state(user_id: str):
    """Save last active user ID and timestamp for wake-up notification."""
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"uid": user_id, "ts": time.time()}, f)
    except Exception:
        pass


def _startup_wakeup():
    """On startup, notify last active user if service was sleeping (>18 min inactive)."""
    time.sleep(4)
    try:
        if not os.path.exists(_STATE_FILE):
            return
        with open(_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        uid = state.get("uid", "")
        last_ts = state.get("ts", 0)
        if uid and (time.time() - last_ts) > 1080:  # 18+ minutes → was sleeping
            push_message(uid, "⚡ 我剛從睡眠中醒來！\n如果剛才有傳訊息沒收到回覆，請再說一次 😊")
    except Exception:
        pass


threading.Thread(target=_startup_wakeup, daemon=True).start()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_EXPENSE_DB_ID = os.environ.get("NOTION_EXPENSE_DB_ID", "")
NOTION_TODO_DB_ID = os.environ.get("NOTION_TODO_DB_ID", "")
NOTION_WORK_DB_ID = os.environ.get("NOTION_WORK_DB_ID", "")
NOTION_MEMO_DB_ID = os.environ.get("NOTION_MEMO_DB_ID", "")
MORNING_TOKEN = os.environ.get("MORNING_TOKEN", "friday2026")
_memo_db_id_cache = NOTION_MEMO_DB_ID

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



# ─── Work task functions ──────────────────────────────────────────────────────

def _get_or_create_memo_db() -> str:
    global _memo_db_id_cache
    if _memo_db_id_cache:
        return _memo_db_id_cache
    try:
        r = requests.post("https://api.notion.com/v1/search", headers=NOTION_HEADERS,
                          json={"query": "備忘錄", "filter": {"property": "object", "value": "database"}})
        if r.status_code == 200:
            for result in r.json().get('results', []):
                title = ''.join(t.get('plain_text', '') for t in result.get('title', []))
                if '備忘錄' in title:
                    _memo_db_id_cache = result['id'].replace('-', '')
                    return _memo_db_id_cache
        rw = requests.get(f"https://api.notion.com/v1/databases/{NOTION_WORK_DB_ID}", headers=NOTION_HEADERS)
        if rw.status_code != 200:
            return ''
        parent = rw.json().get('parent', {})
        if parent.get('type') == 'workspace':
            r2 = requests.post("https://api.notion.com/v1/search", headers=NOTION_HEADERS,
                               json={"filter": {"property": "object", "value": "page"}, "page_size": 1})
            pages = r2.json().get('results', []) if r2.status_code == 200 else []
            if not pages:
                return ''
            parent = {"type": "page_id", "page_id": pages[0]['id']}
        rc = requests.post("https://api.notion.com/v1/databases", headers=NOTION_HEADERS,
                           json={
                               "parent": parent,
                               "title": [{"type": "text", "text": {"content": "備忘錄"}}],
                               "properties": {
                                   "標籤": {"title": {}},
                                   "內容": {"rich_text": {}}
                               }
                           })
        if rc.status_code == 200:
            _memo_db_id_cache = rc.json()['id'].replace('-', '')
            return _memo_db_id_cache
    except Exception as e:
        print(f"_get_or_create_memo_db error: {e}")
    return ''


def add_work_task(description: str, deadline: str = None) -> str:
    """Add work task(s). Detects [M/D~M/D] range and creates one Notion entry per date."""
    range_match = re.search(r'\[(\d{1,2}/\d{1,2})[~\uff5e\u301c](\d{1,2}/\d{1,2})\]', description)
    if range_match:
        clean_desc = (description[:range_match.start()] + description[range_match.end():]).strip()
        year = _tw_now().year
        try:
            sm, sd = map(int, range_match.group(1).split('/'))
            em, ed = map(int, range_match.group(2).split('/'))
            start_dt = datetime.date(year, sm, sd)
            end_dt = datetime.date(year, em, ed)
            if end_dt < start_dt:
                end_dt = datetime.date(year + 1, em, ed)
        except (ValueError, IndexError):
            return "❌ 日期區間格式錯誤"
        dates, d = [], start_dt
        while d <= end_dt:
            dates.append(d)
            d += datetime.timedelta(days=1)
        for dt in dates:
            props = {
                "名稱": {"title": [{"text": {"content": clean_desc}}]},
                "狀態": {"select": {"name": "待處理"}},
                "截止日期": {"date": {"start": str(dt)}},
            }
            res = requests.post("https://api.notion.com/v1/pages",
                                headers=NOTION_HEADERS,
                                json={"parent": {"database_id": NOTION_WORK_DB_ID}, "properties": props})
            if res.status_code != 200:
                return f"❌ 新增失敗（{dt.strftime('%m/%d')}）：{res.text}"
        date_list = "、".join(dt.strftime("%m/%d") for dt in dates)
        return f"✅ 已新增 {len(dates)} 筆任務：{clean_desc}（{date_list}）"
    props = {
        "名稱": {"title": [{"text": {"content": description}}]},
        "狀態": {"select": {"name": "待處理"}},
    }
    if deadline:
        deadline = deadline.replace("/", "-").replace(".", "-")
        props["截止日期"] = {"date": {"start": deadline}}
    res = requests.post("https://api.notion.com/v1/pages",
                        headers=NOTION_HEADERS,
                        json={"parent": {"database_id": NOTION_WORK_DB_ID}, "properties": props})
    deadline_str = f"，截止：{deadline}" if deadline else ""
    return f"✅ 已新增工作任務：{description}{deadline_str}" if res.status_code == 200 else f"❌ 新增失敗：{res.text}"


def batch_add_work_tasks(content: str) -> str:
    """Parse multi-line work schedule and create Notion entries. Handles M/D~M/D: and M/D: formats."""
    year = _tw_now().year
    created, errors = [], []
    for line in content.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if (stripped.startswith('[') or '花費' in stripped or stripped.startswith('(')):
            continue
        rm = re.match(r'^(\d{1,2}/\d{1,2})\s*[~\uff5e\u301c]\s*(\d{1,2}/\d{1,2})\s*[\uff1a:]\s*(.+)', stripped)
        sm2 = re.match(r'^(\d{1,2}/\d{1,2})\s*[\uff1a:]\s*(.+)', stripped) if not rm else None
        if rm:
            task = rm.group(3).split('(')[0].split('（')[0].rstrip('。').strip()
            try:
                s_m, s_d = map(int, rm.group(1).split('/'))
                e_m, e_d = map(int, rm.group(2).split('/'))
                s_dt = datetime.date(year, s_m, s_d)
                e_dt = datetime.date(year, e_m, e_d)
                if e_dt < s_dt: e_dt = datetime.date(year+1, e_m, e_d)
            except ValueError:
                errors.append(f"日期錯誤：{stripped[:20]}"); continue
            if s_dt == e_dt:
                _date_prop = {"start": str(s_dt)}
                label = f"{s_dt.strftime('%m/%d')} {task}"
            else:
                _date_prop = {"start": str(s_dt), "end": str(e_dt)}
                label = f"{s_dt.strftime('%m/%d')}~{e_dt.strftime('%m/%d')} {task}"
            props = {"名稱": {"title": [{"text": {"content": task}}]},
                     "狀態": {"select": {"name": "待處理"}},
                     "截止日期": {"date": _date_prop}}
            r2 = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS,
                               json={"parent": {"database_id": NOTION_WORK_DB_ID}, "properties": props})
            (created if r2.status_code == 200 else errors).append(label)
        elif sm2:
            task = sm2.group(2).split('(')[0].split('（')[0].rstrip('。').strip()
            try:
                t_m, t_d = map(int, sm2.group(1).split('/'))
                dt2 = datetime.date(year, t_m, t_d)
            except ValueError:
                errors.append(f"日期錯誤：{stripped[:20]}"); continue
            props = {"名稱": {"title": [{"text": {"content": task}}]},
                     "狀態": {"select": {"name": "待處理"}},
                     "截止日期": {"date": {"start": str(dt2)}}}
            r2 = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS,
                               json={"parent": {"database_id": NOTION_WORK_DB_ID}, "properties": props})
            label = f"{dt2.strftime('%m/%d')} {task}"
            (created if r2.status_code == 200 else errors).append(label)
    if not created and not errors:
        return "⚠️ 未找到可解析的工作任務（需格式 M/D : 工作 或 M/D~M/D : 工作）"
    result_parts = []
    if created: result_parts.append(f"✅ 已新增 {len(created)} 筆：\n" + "\n".join(f"  • {c}" for c in created))
    if errors: result_parts.append("❌ 失敗：\n" + "\n".join(f"  • {e}" for e in errors))
    # Auto-save memo if first line is [tag] or 「」 tag
    _fl = content.strip().split('\n')[0].strip()
    _at = re.match(r'^[\u3010\[](.+)[\u3011\]]$', _fl)
    if _at and created:
        save_memo(_at.group(1), content)
    return "\n".join(result_parts)

def save_memo(tag: str, content: str) -> str:
    """Save a memo with a tag for later retrieval."""
    db_id = _get_or_create_memo_db()
    if not db_id:
        return "❌ 無法建立備忘錄資料庫，請在 Render 設定 NOTION_MEMO_DB_ID 環境變數"
    chunks = [content[i:i+2000] for i in range(0, min(len(content), 10000), 2000)]
    rich_text = [{"type": "text", "text": {"content": c}} for c in chunks]
    props = {
        "標籤": {"title": [{"text": {"content": tag[:100]}}]},
        "內容": {"rich_text": rich_text}
    }
    res = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS,
                        json={"parent": {"database_id": db_id}, "properties": props})
    return f"✅ 已儲存備忘錄「{tag}」" if res.status_code == 200 else f"❌ 儲存失敗：{res.text[:100]}"


def get_memo(keyword: str) -> str:
    """Retrieve memo by keyword."""
    db_id = _get_or_create_memo_db()
    if not db_id:
        return "❌ 備忘錄資料庫未設定"
    results, err = _notion_query_all(db_id,
        {"filter": {"property": "標籤", "title": {"contains": keyword}}})
    if err:
        return f"❌ 查詢失敗：{err}"
    if not results:
        return f"找不到含「{keyword}」的備忘錄"
    page = results[0]
    tag_parts = page["properties"]["標籤"]["title"]
    tag = "".join(t["plain_text"] for t in tag_parts) if tag_parts else ""
    rt = page["properties"]["內容"]["rich_text"]
    content = "".join(b["plain_text"] for b in rt) if rt else ""
    return f"📋 [{tag}]\n\n{content}"


def complete_work_task(keyword: str) -> str:
    """Mark a work task as completed."""
    results, qerr = _notion_query_all(
        NOTION_WORK_DB_ID,
        {"filter": {"property": "狀態", "select": {"equals": "待處理"}}}
    )
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    matched = [r for r in results if keyword in (
        r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else ""
    )]
    if not matched:
        return f"❌ 找不到含「{keyword}」的工作任務"
    for r in matched:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"properties": {"狀態": {"select": {"name": "已完成"}}}}
        )
    names = [r["properties"]["名稱"]["title"][0]["plain_text"] for r in matched if r["properties"]["名稱"]["title"]]
    return f"✅ 已完成：{'、'.join(names)}"


def postpone_work_task(keyword: str, new_deadline: str) -> str:
    """Postpone a work task deadline."""
    new_deadline = new_deadline.replace("/", "-").replace(".", "-")
    results, qerr = _notion_query_all(
        NOTION_WORK_DB_ID,
        {"filter": {"property": "狀態", "select": {"equals": "待處理"}}}
    )
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    matched = [r for r in results if keyword in (
        r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else ""
    )]
    if not matched:
        return f"❌ 找不到含「{keyword}」的工作任務"
    for r in matched:
        requests.patch(
            f"https://api.notion.com/v1/pages/{r['id']}",
            headers=NOTION_HEADERS,
            json={"properties": {"截止日期": {"date": {"start": new_deadline}}}}
        )
    names = [r["properties"]["名稱"]["title"][0]["plain_text"] for r in matched if r["properties"]["名稱"]["title"]]
    return f"✅ 已將「{'、'.join(names)}」延期至 {new_deadline}"



def clear_work_tasks() -> str:
    results, qerr = _notion_query_all(NOTION_WORK_DB_ID, {})
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    if not results:
        return "✅ 工作任務本來就是空的"
    for r in results:
        requests.patch(f"https://api.notion.com/v1/pages/{r['id']}", headers=NOTION_HEADERS, json={"archived": True})
    return f"✅ 已清空 {len(results)} 筆工作任務"


def list_work_tasks(period: str = "all", date: str = None) -> str:
    """List pending work tasks, optionally filtered by period."""
    results, qerr = _notion_query_all(
        NOTION_WORK_DB_ID,
        {"filter": {"property": "狀態", "select": {"equals": "待處理"}}}
    )
    if qerr:
        return f"❌ 查詢失敗：{qerr}"
    if not results:
        return "🎉 目前沒有待處理的工作任務！"

    today = _tw_now().date()
    week_start = today - datetime.timedelta(days=today.weekday())  # 本週一
    week_end = week_start + datetime.timedelta(days=6)             # 本週日
    next_week_start = week_start + datetime.timedelta(days=7)
    next_week_end = week_start + datetime.timedelta(days=13)
    next_next_week_start = week_start + datetime.timedelta(days=14)
    next_next_week_end = week_start + datetime.timedelta(days=20)
    month_start = today.replace(day=1)
    if today.month == 12:
        next_month_start = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_month_start = today.replace(month=today.month + 1, day=1)
    month_end = next_month_start - datetime.timedelta(days=1)
    next_month_end = (next_month_start.replace(month=next_month_start.month + 1, day=1)
                      if next_month_start.month < 12
                      else next_month_start.replace(year=next_month_start.year + 1, month=1, day=1)
                      ) - datetime.timedelta(days=1)

    overdue, today_tasks, upcoming, no_deadline = [], [], [], []
    for r in results:
        props = r["properties"]
        name = props["名稱"]["title"][0]["plain_text"] if props["名稱"]["title"] else "（無）"
        dl_prop = props.get("截止日期", {}).get("date")
        if dl_prop and dl_prop.get("start"):
            _start_d = datetime.date.fromisoformat(dl_prop["start"])
            _end_d = datetime.date.fromisoformat(dl_prop["end"]) if dl_prop.get("end") else _start_d
            if _end_d < today:
                overdue.append((_start_d, _end_d, name))
            elif _start_d <= today <= _end_d:
                today_tasks.append((_start_d, _end_d, name))
            else:
                upcoming.append((_start_d, _end_d, name))
        else:
            no_deadline.append(name)

    # 精確日期查詢（today/明天/後天/任意日期）
    if date:
        date = date.replace("/", "-").replace(".", "-")
        try:
            target = datetime.date.fromisoformat(date)
        except ValueError:
            return f"❌ 日期格式錯誤：{date}"
        label = target.strftime("%m/%d") if target != today else "今天"
        if target == today:
            overdue_f, today_f = overdue, today_tasks
            upcoming_f, no_deadline_f = [], []
            upcoming_label = "今天到期"
        elif target < today:
            overdue_f = [(s, e, n) for s, e, n in overdue if e == target]
            today_f, upcoming_f, no_deadline_f = [], [], []
            upcoming_label = f"{label}到期"
        else:
            overdue_f, today_f = [], []
            upcoming_f = [(s, e, n) for s, e, n in upcoming if e == target]
            no_deadline_f = []
            upcoming_label = f"{label}到期"
    # 週期查詢
    elif period == "this_week":
        overdue_f, today_f = overdue, today_tasks
        upcoming_f = [(s, e, n) for s, e, n in upcoming if e <= week_end]
        no_deadline_f = []
        label, upcoming_label = "本週", "本週到期"
    elif period == "next_week":
        overdue_f, today_f = [], []
        upcoming_f = [(s, e, n) for s, e, n in upcoming if next_week_start <= e <= next_week_end]
        no_deadline_f = []
        label, upcoming_label = "下禮拜", "下禮拜到期"
    elif period == "this_month":
        overdue_f, today_f = overdue, today_tasks
        upcoming_f = [(s, e, n) for s, e, n in upcoming if e <= month_end]
        no_deadline_f = []
        label, upcoming_label = "本月", "本月到期"
    elif period == "next_month":
        overdue_f, today_f = [], []
        upcoming_f = [(s, e, n) for s, e, n in upcoming if next_month_start <= e <= next_month_end]
        no_deadline_f = []
        label, upcoming_label = "下個月", "下個月到期"
    elif period == "next_next_week":
        overdue_f, today_f = [], []
        upcoming_f = [(s, e, n) for s, e, n in upcoming if next_next_week_start <= e <= next_next_week_end]
        no_deadline_f = []
        label, upcoming_label = "下下禮拜", "下下禮拜到期"
    elif period == "overdue":
        overdue_f, today_f = overdue, []
        upcoming_f, no_deadline_f = [], []
        label, upcoming_label = "逾期", "逾期"
    else:  # all
        overdue_f, today_f = overdue, today_tasks
        upcoming_f, no_deadline_f = upcoming, no_deadline
        label, upcoming_label = "全部", "即將到期"

    for lst in (overdue_f, today_f, upcoming_f):
        lst.sort(key=lambda x: x[0])

    if not (overdue_f or today_f or upcoming_f or no_deadline_f):
        return f"🎉 {label}沒有待處理的工作任務！"

    _wm = period in ("this_week", "next_week", "next_month")

    def _ds(s, e):
        if s == e:
            return f"（{e.strftime('%m/%d')}）"
        return f"（{s.strftime('%m/%d')}~{e.strftime('%m/%d')}）"

    lines = [f"📋 工作任務清單（{label}）"]
    if overdue_f:
        lines.append("\n⚠️ 逾期：")
        for s, e, n in overdue_f:
            lines.append(f"  • {n}{_ds(s, e)}")
    if today_f:
        lines.append("\n🚨 今天截止：")
        for s, e, n in today_f:
            if _wm:
                lines.append(f"  • {n}{_ds(s, e)}")
            elif s < e:
                lines.append(f"  • {n}（{e.month}/{e.day}截止）")
            else:
                lines.append(f"  • {n}")
    if upcoming_f:
        if not _wm:
            lines.append(f"\n📅 {upcoming_label}：")
        for s, e, n in upcoming_f:
            lines.append(f"  • {n}{_ds(s, e)}")
    if no_deadline_f:
        lines.append("\n📌 無截止日期：")
        for n in no_deadline_f:
            lines.append(f"  • {n}")
    return "\n".join(lines)


def _simulate_morning_reminder(weekday_override: int, uid: str) -> str:
    """Build morning reminder content for a specific weekday (for testing)."""
    tw_now = _tw_now()
    today = tw_now.date()
    day_names = ["（週一）", "（週二）", "（週三）", "（週四）", "（週五）", "（週六）", "（週日）"]
    name = _get_line_display_name(uid) or "你"
    if weekday_override >= 5:  # 週六/週日
        content = list_work_tasks(period="next_week")
        greeting = f"🧪 [測試-{day_names[weekday_override].strip('（）')}] 早安！{name}\n以下是下週工作預覽：\n\n"
    elif weekday_override == 4:  # 週五
        _p = [x for x in [list_work_tasks(date=str(today)), list_work_tasks(period="this_week"), list_work_tasks(period="next_week")] if "沒有待處理的工作任務" not in x]
        content = "\n\n".join(_p) if _p else "🎉 目前沒有待處理的工作任務！"
        greeting = f"🧪 [測試-{day_names[weekday_override].strip('（）')}] 早安！{name}\n\n"
    else:  # 週一至週四 → 今天 + 本週
        _p = [x for x in [list_work_tasks(date=str(today)), list_work_tasks(period="this_week")] if "沒有待處理的工作任務" not in x]
        content = "\n\n".join(_p) if _p else "🎉 目前沒有待處理的工作任務！"
        greeting = f"🧪 [測試-{day_names[weekday_override].strip('（）')}] 早安！{name}\n\n"
    return greeting + content


def _get_line_display_name(uid: str) -> str:
    """Fetch LINE user display name via profile API."""
    try:
        res = requests.get(
            f"https://api.line.me/v2/bot/profile/{uid}",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=5,
        )
        if res.status_code == 200:
            return res.json().get("displayName", "")
    except Exception:
        pass
    return ""


def morning_reminder():
    """Send morning work task reminder at 9am Taiwan time (scheduled at 1am UTC)."""
    try:
        if not os.path.exists(_STATE_FILE):
            return
        with open(_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        uid = state.get("uid", "")
        if not uid or not NOTION_WORK_DB_ID:
            return

        tw_now = _tw_now()
        today = tw_now.date()
        weekday = today.weekday()  # 0=Mon ... 5=Sat 6=Sun
        day_names = ["（週一）", "（週二）", "（週三）", "（週四）", "（週五）", "（週六）", "（週日）"]

        name = _get_line_display_name(uid)
        if weekday >= 5:  # 週六/週日 → 只看下週
            content = list_work_tasks(period="next_week")
            greeting = f"🌅 早安！{name}，{today.strftime('%m/%d')}{day_names[weekday]}\n以下是下週工作預覽：\n\n"
        elif weekday == 4:  # 週五 → 今天 + 本週 + 下週
            _p = [x for x in [list_work_tasks(date=str(today)), list_work_tasks(period="this_week"), list_work_tasks(period="next_week")] if "沒有待處理的工作任務" not in x]
            content = "\n\n".join(_p) if _p else "🎉 目前沒有待處理的工作任務！"
            greeting = f"🌅 早安！{name}，{today.strftime('%m/%d')}{day_names[weekday]}\n\n"
        else:  # 週一至週四 → 今天 + 本週
            _p = [x for x in [list_work_tasks(date=str(today)), list_work_tasks(period="this_week")] if "沒有待處理的工作任務" not in x]
            content = "\n\n".join(_p) if _p else "🎉 目前沒有待處理的工作任務！"
            greeting = f"🌅 早安！{name}，{today.strftime('%m/%d')}{day_names[weekday]}\n\n"

        push_message(uid, greeting + content)
    except Exception as e:
        print(f"morning_reminder error: {e}")

# Schedule morning reminder at 9am Taiwan time = 1am UTC
_scheduler = BackgroundScheduler()
_scheduler.add_job(morning_reminder, 'cron', hour=1, minute=0)
_scheduler.start()


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
   {"type": "function", "function": {"name": "add_work_task", "description": "新增工作任務。說「X前要Y」或任何工作安排均呼叫此工具。description只填工作內容不含時間詞；若含日期區間如「燒機測試 [7/13~7/15]」，原樣保留在 description，deadline 留空，函式自動展開每日建立；否則 deadline 填截止日 YYYY-MM-DD。", "parameters": {"type": "object", "properties": {
        "description": {"type": "string", "description": "工作任務內容，不含日期時間詞。例：「生出LC-300測試SOP」而非「下禮拜二前生出LC-300測試SOP」"},
        "deadline": {"type": "string", "description": "截止日期 YYYY-MM-DD。任何時間表達（下禮拜二、下週五、月底、X號前等）都計算成具體日期後填入。"}
    }, "required": ["description"]}}},
    {"type": "function", "function": {"name": "batch_add_work_tasks", "description": "批量解析多行工作計畫並新增至Notion。當用戶傳入含日期前綴的多行工作安排（如：7/2~7/3 : 完成燒機架組裝\n7/13 : 測試品到貨）時呼叫，傳入完整文字。", "parameters": {"type": "object", "properties": {
        "content": {"type": "string", "description": "含日期工作安排的多行文字"}
    }, "required": ["content"]}}},
    {"type": "function", "function": {"name": "save_memo", "description": "儲存備忘錄。收到含[標籤]的完整訊息或文件時呼叫，tag填[...]裡的文字，content填完整原始訊息。", "parameters": {"type": "object", "properties": {
        "tag": {"type": "string", "description": "備忘錄標籤（從[...]提取）"},
        "content": {"type": "string", "description": "完整原始訊息"}
    }, "required": ["tag", "content"]}}},
    {"type": "function", "function": {"name": "get_memo", "description": "用關鍵字查詢備忘錄，回傳原始完整訊息。", "parameters": {"type": "object", "properties": {
        "keyword": {"type": "string", "description": "查詢關鍵字"}
    }, "required": ["keyword"]}}},
    {"type": "function", "function": {"name": "complete_work_task", "description": "標記工作任務為已完成", "parameters": {"type": "object", "properties": {
        "keyword": {"type": "string", "description": "工作任務關鍵字"}
    }, "required": ["keyword"]}}},
    {"type": "function", "function": {"name": "postpone_work_task", "description": "延期工作任務截止日期", "parameters": {"type": "object", "properties": {
        "keyword": {"type": "string", "description": "工作任務關鍵字"},
        "new_deadline": {"type": "string", "description": "新截止日期 YYYY-MM-DD"}
    }, "required": ["keyword", "new_deadline"]}}},
    {"type": "function", "function": {"name": "clear_work_tasks", "description": "清空工作任務清單（需確認）", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_work_tasks", "description": (
        "查詢工作任務清單。訊息含任何時間範圍時必須傳對應參數，不可用預設值！\n"
        "■ 精確日期（今天/明天/後天/大後天/X月X日/下週二等某一天）→ 計算 YYYY-MM-DD 傳 date\n"
        "■ 本週/這週/這禮拜 → period=this_week\n"
        "■ 下週/下禮拜/下個禮拜 → period=next_week\n"
        "■ 下下週/下下禮拜/下下個禮拜 → period=next_next_week\n"
        "■ 本月/這個月 → period=this_month\n"
        "■ 下個月/下月 → period=next_month\n"
        "■ 逾期/過期/已過截止 → period=overdue\n"
        "■ 完全沒說時間才用 period=all"
    ), "parameters": {"type": "object", "properties": {
        "period": {"type": "string", "enum": ["all", "this_week", "next_week", "next_next_week", "this_month", "next_month", "overdue"], "description": "this_week=本週/這週/這禮拜, next_week=下週/下禮拜/下個禮拜, next_next_week=下下週/下下禮拜, this_month=本月/這個月, next_month=下個月, overdue=逾期, all=全部（無時間限定時才用）"},
        "date": {"type": "string", "description": "精確單日 YYYY-MM-DD。今天/明天/後天/大後天/X月X日/下週二等均計算成具體日期。有此參數時 period 無效。"}
    }, "required": ["period"]}}},
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
    "8.刪除指定待辦呼叫 delete_todo；"
    "9.工作任務新增：訊息表達「X前要Y」或任何工作安排，呼叫 add_work_task；description只填工作內容不含時間詞；若描述含 [M/D~M/D] 日期區間（如 [7/13~7/15]），原樣保留在 description，deadline 留空；否則 deadline 填 YYYY-MM-DD；"
    "9b.批量工作計畫：若訊息含多行工作安排（行首有日期 M/D: 或 M/D~M/D: 格式），呼叫 batch_add_work_tasks 並傳入完整文字；"
    "9c.訊息第一行為[標籤]或《標籤》（如[燒機室]），後面有多行工作計畫（含 M/D 日期）：除呼叫 batch_add_work_tasks 外，同時呼叫 save_memo（tag=第一行括號內文字，content=完整訊息）；"
    "9d.用戶輸入單獨[關鍵字]格式（如[燒機室]），呼叫 get_memo(keyword=關鍵字)；"
    "10.查詢工作任務清單呼叫 list_work_tasks，訊息含時間範圍時必須傳對應參數（禁止用預設 all）："
    "今天→date=今天日期；明天→date=明天日期；後天/大後天→date=計算日期；X月X日→date=YYYY-MM-DD；"
    "本週/這週/這禮拜→period=this_week；下週/下禮拜/下個禮拜→period=next_week；下下週/下下禮拜→period=next_next_week；"
    "本月/這個月→period=this_month；下個月→period=next_month；逾期/過期→period=overdue；"
    "完全沒提時間才用 period=all；"
    "11.完成某工作任務（說完成了、做好了、搞定了、已處理）呼叫 complete_work_task；"
    "12.延期工作任務截止日期呼叫 postpone_work_task，計算新日期後填入 new_deadline。"
    "永遠呼叫工具，不得自行回答。繁體中文，回覆簡短。"
    "13.如果用戶抱怨早安提醒沒收到（例如「你沒提醒我」「早上沒收到提醒」），直接解釋是因為服務重啟可能導致排程失效，不要新增任何待辦事項或工作任務。"
    "14.只有在用戶訊息以「[工作待辦]」開頭時，才可以使用新增工作任務的工具（add_work_task、batch_add_work_tasks）；沒有此開頭詞，絕對不可新增工作任務。"
    "15.只有在用戶訊息以「[花費]」開頭時，才可以使用新增花費的工具（add_expense）；沒有此開頭詞，絕對不可記錄花費。"
    "16.用戶輸入日期加工作項目（例如「7/3工作項目」「明天的工作」「今天任務」），一律使用 list_work_tasks 查詢，不可呼叫 add_work_task 或 batch_add_work_tasks。"
    "17.若訊息以「[工作待辦]」開頭且同一則訊息含有「花費」區塊：不呼叫 add_expense 不記帳；詢問用戶「這些花費是已花費還是預計花費？」；用戶回覆後，把原訊息中的「花費：」替換為「已花費：」或「預計花費：」，並回傳完整修改後的訊息。"
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
    elif name == "save_memo":
        return save_memo(**args)
    elif name == "get_memo":
        return get_memo(**args)
    elif name == "batch_add_work_tasks":
        return batch_add_work_tasks(**args)
    elif name == "add_work_task":
        return add_work_task(**args)
    elif name == "complete_work_task":
        return complete_work_task(**args)
    elif name == "postpone_work_task":
        return postpone_work_task(**args)
    elif name == "clear_work_tasks":
        return clear_work_tasks()
    elif name == "list_work_tasks":
        return list_work_tasks(**args)
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
    elif fname == "clear_work_tasks":
        results, qerr = _notion_query_all(NOTION_WORK_DB_ID, {})
        if qerr:
            return f"❌ 查詢失敗：{qerr}"
        if not results:
            return "🔕 工作任務本來就是空的"
        lines_out = []
        for r in results:
            _n = r["properties"]["名稱"]["title"][0]["plain_text"] if r["properties"]["名稱"]["title"] else "（無）"
            _dp = r["properties"].get("截止日期", {}).get("date")
            if _dp and _dp.get("start"):
                _s = _dp["start"][5:].replace("-", "/")
                _e = (_dp.get("end") or _dp["start"])[5:].replace("-", "/")
                lines_out.append(f"  • {_n}（{_s}~{_e}）" if _s != _e else f"  • {_n}（{_s}）")
            else:
                lines_out.append(f"  • {_n}")
        ids = [r["id"] for r in results]
        pending_delete[user_id] = {"page_ids": ids}
        desc = "\n".join(lines_out)
        return f"⚠️ 確認要清空工作任務共 {len(ids)} 筆？\n\n{desc}\n\n回覆【確認刪除】確認，或回覆【取消】取消。"
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
    # ── 測試早安提醒指令 ──────────────────────────────────────────
    _TEST_DAY_MAP = {
        "測試週一早安提醒": 0, "測試週二早安提醒": 1, "測試週三早安提醒": 2,
        "測試週四早安提醒": 3, "測試週五早安提醒": 4,
        "測試週六早安提醒": 5, "測試週日早安提醒": 6,
    }
    memo_q = re.match(r'^[\u3010\[](.+)[\u3011\]]$', user_text.strip())
    if memo_q:
        return get_memo(memo_q.group(1))
    if user_text.strip() in _TEST_DAY_MAP:
        return _simulate_morning_reminder(_TEST_DAY_MAP[user_text.strip()], user_id)
    # Rule 17: handle pending expense type reply
    if user_id and user_id in _PENDING_EXPENSE_MSG:
        t = user_text.strip()
        if '已花費' in t:
            orig = _PENDING_EXPENSE_MSG.pop(user_id)
            return orig.replace('花費：', '已花費：').replace('花費:', '已花費:')
        elif '預計花費' in t:
            orig = _PENDING_EXPENSE_MSG.pop(user_id)
            return orig.replace('花費：', '預計花費：').replace('花費:', '預計花費:')
        else:
            _PENDING_EXPENSE_MSG.pop(user_id, None)

    data = groq_chat(messages, TOOLS)
    if "choices" in data:
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg.get("content") or "（無法理解指令）"
        results = []
        _work_expense = (
            '工作待辦' in user_text and
            ('花費：' in user_text or '花費:' in user_text)
        )
        for tc in tool_calls:
            fname = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"]) or {}
            if fname in DELETE_TOOLS and user_id:
                return _prepare_delete(user_id, fname, args)
            if fname == "add_expense" and not user_text.strip().startswith("[花費]"):
                continue  # Rule 15/17: block add_expense unless message starts with [花費]
            result = run_tool(fname, args)
            results.append(result)
        print(f"[R17] work_expense={_work_expense}, has_wo={'工作待辦' in user_text}, has_fe={'花費' in user_text}, start={user_text[:80]!r}", flush=True)
        if _work_expense and user_id:
            _PENDING_EXPENSE_MSG[user_id] = user_text
        return "\n".join(filter(None, results))
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
            user_text = user_text.replace('\u301c', '~').replace('\uff5e', '~')  # normalize tilde variants
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
            # Rule 17: ask expense type as separate message after work results
            if user_id in _PENDING_EXPENSE_MSG and _PENDING_EXPENSE_MSG[user_id] == user_text:
                push_message(user_id, "這些花費是已花費還是預計花費？")
            _save_user_state(user_id)
    threading.Thread(target=process_events, daemon=True).start()
    return "OK"


@app.route("/", methods=["GET"])
def health():
    # Update last-seen timestamp on ping to prevent false wake-up notifications
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            state["ts"] = time.time()
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f)
    except Exception:
        pass
    return "Friday在線上 ✅"



@app.route("/morning", methods=["GET", "POST"])
def morning_trigger():
    token = request.args.get("token", "")
    if token != MORNING_TOKEN:
        return "Unauthorized", 401
    morning_reminder()
    return "OK"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# redeploy
