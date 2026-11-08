from http.server import BaseHTTPRequestHandler
import os
import re
import json
import urllib.parse
from datetime import datetime, timezone, timedelta
import requests

SEOUL_API_KEY = os.environ.get("SEOUL_API_KEY", "5364436a77796d733931694d494558")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

TARGET_AREAS = {"강서구", "구로구", "마포구"}

CACHE_FILE = "/tmp/seoul_tennis_state.json"
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


def send_telegram(message: str) -> dict:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"ok": False, "error": "Token or Chat ID missing in environment variables"}
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def is_excluded(place: str, svcnm: str) -> bool:
    """
    서남센터(서남물재생센터) 테니스장 중 1~4번 코트만 제외하고 5번 이상 코트는 통과
    - 매칭 예시: '서남센터 테니스장 1번 코트', '서남물재생센터 2코트', '서남 테니스장3' 등
    """
    combined = f"{place} {svcnm}"
    if "서남" in combined:
        # '1번'~'4번', '1코트'~'4코트', '테니스장1'~'테니스장4', '1~4' 패턴 감지
        # (단, 10번 등 두 자리 숫자나 날짜 '11월', '2026년' 등과 겹치지 않게 정규식 처리)
        pattern = r"(?<![0-9])[1-4]\s*(번|코트|면)|테니스장\s*[1-4](?![0-9])|1\s*~\s*4"
        if re.search(pattern, combined):
            return True
    return False


def is_target_time(svcnm: str, v_max: str) -> bool:
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

            # 1. 지역 및 접수중 상태 확인
            if area in TARGET_AREAS and stat == "접수중":
                # 2. 서남센터 1~4번 코트만 핀셋 제외 (5번 이상 코트는 유지)
                if is_excluded(place, svcnm):
                    continue
                # 3. 시간대 조건 확인
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

        if "test=true" in self.path:
            test_msg = (
                "🎾 <b>[서울시 테니스 알리미] 텔레그램 연동 테스트 성공!</b>\n\n"
                "• 봇 토큰 및 Chat ID가 정상 작동 중입니다.\n"
                f"• 모니터링 대상: {', '.join(TARGET_AREAS)}\n"
                "• 제외 조건: 서남센터 테니스장 1~4번 코트만 제외 (5번 이상 코트 포함)\n"
                "• 기준: 평일 18시 이후 / 주말 전시간대\n"
                f"• 확인 시각: {datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')}"
            )
            tg_result = send_telegram(test_msg)
            
            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            res_data = {
                "test_mode": True,
                "telegram_result": tg_result,
                "token_exists": bool(TELEGRAM_BOT_TOKEN),
                "chat_id_exists": bool(TELEGRAM_CHAT_ID),
            }
            self.wfile.write(json.dumps(res_data, ensure_ascii=False).encode("utf-8"))
            return

        prev_keys = load_cache()
        current_items = check_seoul_api()
        current_keys = {item["key"] for item in current_items}

        new_items = [item for item in current_items if item["key"] not in prev_keys]

        telegram_sent = False
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
            telegram_sent = True

        save_cache(current_keys)

        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.end_headers()
        response_body = {
            "status": "ok",
            "available_count": len(current_items),
            "new_alerts_sent": len(new_items),
            "telegram_sent": telegram_sent,
            "excluded_rule": "서남센터 1~4번 코트만 제외",
            "available_courts": [f"[{i['area']}] {i['title']}" for i in current_items],
            "timestamp": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        }
        self.wfile.write(json.dumps(response_body, ensure_ascii=False).encode("utf-8"))
