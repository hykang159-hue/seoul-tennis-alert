from http.server import BaseHTTPRequestHandler
import os
import json
import urllib.parse
from datetime import datetime, timezone, timedelta
import requests

# 환경변수 설정 (Vercel Environment Variables)
SEOUL_API_KEY = os.environ.get("SEOUL_API_KEY", "5364436a77796d733931694d494558")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")  # 선택 사항

TARGET_AREAS = {"강서구", "구로구", "마포구"}
CACHE_FILE = "/tmp/seoul_tennis_state.json"

# Warm 컨테이너 유지 시 중복 알림 방지용 메모리 세트
MEMORY_CACHE = set()


def load_cache() -> set:
    global MEMORY_CACHE
    if MEMORY_CACHE:
        return MEMORY_CACHE
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                MEMORY_CACHE = set(json.load(f))
        except Exception:
            pass
    return MEMORY_CACHE


def save_cache(active_keys: set):
    global MEMORY_CACHE
    MEMORY_CACHE = active_keys
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(active_keys), f, ensure_ascii=False)
    except Exception:
        pass


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("Telegram Send Error:", e)


def is_target_time(svcnm: str, v_max: str) -> bool:
    """
    필터링 조건: 주말(전시간) 또는 평일(18시 이후 운영 코트)
    - 주말/휴일 키워드 포함 시 통과
    - 평일 전용이면서 '주간' 키워드가 있거나 종료시간(V_MAX)이 18시 이하인 경우 제외
    """
    is_weekend = any(kw in svcnm for kw in ["주말", "토/일", "공휴일", "휴일", "토요일", "일요일"])
    is_weekday_only = "평일" in svcnm and not is_weekend

    if is_weekend:
        return True
    if is_weekday_only and "주간" in svcnm:
        return False

    if v_max and ":" in v_max:
        try:
            end_hour = int(v_max.split(":")[0])
            if end_hour <= 18 and is_weekday_only:
                return False
        except ValueError:
            pass
    return True


def check_seoul_api() -> list:
    service_type = urllib.parse.quote("테니스장")
    url = f"http://openAPI.seoul.go.kr:8088/{SEOUL_API_KEY}/json/ListPublicReservationSport/1/1000/{service_type}"
    
    available_items = []
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        rows = data.get("ListPublicReservationSport", {}).get("row", [])

        for row in rows:
            area = row.get("AREANM", "").strip()
            stat = row.get("SVCSTATNM", "").strip()
            svcnm = row.get("SVCNM", "").strip()
            v_min = row.get("V_MIN", "").strip()
            v_max = row.get("V_MAX", "").strip()
            svcid = row.get("SVCID", "").strip()
            svcurl = row.get("SVCURL", "").strip()
            place = row.get("PLACENM", "").strip()

            # 강서구, 구로구, 마포구 중 '접수중' 상태이면서 시간대 조건 부합
            if area in TARGET_AREAS and stat == "접수중":
                if is_target_time(svcnm, v_max):
                    available_items.append({
                        "key": svcid,
                        "area": area,
                        "place": place,
                        "title": svcnm,
                        "hours": f"{v_min}~{v_max}",
                        "url": svcurl,
                    })
    except Exception as e:
        print("Seoul API Error:", e)

    return available_items


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if CRON_SECRET and f"key={CRON_SECRET}" not in self.path:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        prev_keys = load_cache()
        current_items = check_seoul_api()
        current_keys = {item["key"] for item in current_items}

        # 이전에 '접수중'이 아니었다가 새로 '접수중'으로 바뀐 코트만 필터링
        new_items = [item for item in current_items if item["key"] not in prev_keys]

        if new_items:
            lines = ["🎾 <b>[서울시 공공예약] 테니스장 접수중 감지!</b>\n"]
            for item in new_items[:10]:
                lines.append(
                    f"• <b>[{item['area']} | {item['place']}]</b>\n"
                    f"  └ {item['title']}\n"
                    f"  └ 운영시간: {item['hours']}\n"
                    f"  └ <a href='{item['url']}'>👉 바로 예약하러 가기</a>\n"
                )
            send_telegram("\n".join(lines))

        save_cache(current_keys)

        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.end_headers()
        response_body = {
            "status": "ok",
            "available_count": len(current_items),
            "new_alerts_sent": len(new_items),
            "available_courts": [f"[{i['area']}] {i['title']}" for i in current_items],
            "timestamp": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        }
        self.wfile.write(json.dumps(response_body, ensure_ascii=False).encode("utf-8"))
