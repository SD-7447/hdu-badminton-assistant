"""The public personal-booking notice, retrieved independently of booking sessions."""
from datetime import datetime
import http.client
import json

from venue_api import CHINA, HOST, classify


def fetch_notice():
    connection = http.client.HTTPSConnection(HOST, timeout=5)
    try:
        connection.request("POST", "/book/client/show_notice", b'{"user_type":2}',
                           {"Content-Type": "application/json", "Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("公告请求失败")
        raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("公告响应过大")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("code", 200) not in (0, 200, "0", "200"):
            raise ValueError("公告响应异常")
        message = data.get("message")
        if not isinstance(message, str) or classify(message) == "AUTH":
            raise ValueError("未取得场馆公告")
        published = data.get("published_at")
        return {"body": message if message.strip() else "", "synced": datetime.now(CHINA).isoformat(),
                "published": published if isinstance(published, str) else ""}
    finally:
        connection.close()

