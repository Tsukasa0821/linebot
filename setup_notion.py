"""
執行這個腳本來自動在 Notion 建立「記帳」和「待辦」兩個資料庫
用法：NOTION_API_KEY=xxx NOTION_PAGE_ID=xxx python setup_notion.py
"""
import os
import requests

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
# 你的 Notion 頁面 ID（從頁面網址取得，例如 https://notion.so/xxxxx 的 xxxxx 部分）
NOTION_PAGE_ID = os.environ["NOTION_PAGE_ID"]

HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

def create_expense_db():
    data = {
        "parent": {"type": "page_id", "page_id": NOTION_PAGE_ID},
        "title": [{"type": "text", "text": {"content": "💰 記帳"}}],
        "properties": {
            "名稱": {"title": {}},
            "金額": {"number": {"format": "number"}},
            "分類": {
                "select": {
                    "options": [
                        {"name": "餐飲", "color": "orange"},
                        {"name": "交通", "color": "blue"},
                        {"name": "購物", "color": "pink"},
                        {"name": "娛樂", "color": "purple"},
                        {"name": "醫療", "color": "red"},
                        {"name": "工作", "color": "green"},
                        {"name": "其他", "color": "gray"},
                    ]
                }
            },
            "日期": {"date": {}},
        },
    }
    res = requests.post("https://api.notion.com/v1/databases", headers=HEADERS, json=data)
    if res.status_code == 200:
        db_id = res.json()["id"]
        print(f"✅ 記帳資料庫建立成功！")
        print(f"   NOTION_EXPENSE_DB_ID={db_id}")
        return db_id
    else:
        print(f"❌ 失敗：{res.text}")
        return None


def create_todo_db():
    data = {
        "parent": {"type": "page_id", "page_id": NOTION_PAGE_ID},
        "title": [{"type": "text", "text": {"content": "📋 待辦"}}],
        "properties": {
            "名稱": {"title": {}},
            "備註": {"rich_text": {}},
            "狀態": {
                "select": {
                    "options": [
                        {"name": "待辦", "color": "red"},
                        {"name": "進行中", "color": "yellow"},
                        {"name": "完成", "color": "green"},
                    ]
                }
            },
            "建立日期": {"date": {}},
        },
    }
    res = requests.post("https://api.notion.com/v1/databases", headers=HEADERS, json=data)
    if res.status_code == 200:
        db_id = res.json()["id"]
        print(f"✅ 待辦資料庫建立成功！")
        print(f"   NOTION_TODO_DB_ID={db_id}")
        return db_id
    else:
        print(f"❌ 失敗：{res.text}")
        return None


if __name__ == "__main__":
    print("🚀 開始建立 Notion 資料庫...\n")
    expense_id = create_expense_db()
    todo_id = create_todo_db()

    if expense_id and todo_id:
        print("\n✅ 全部完成！把以下環境變數加到 Railway：")
        print(f"NOTION_EXPENSE_DB_ID={expense_id}")
        print(f"NOTION_TODO_DB_ID={todo_id}")
