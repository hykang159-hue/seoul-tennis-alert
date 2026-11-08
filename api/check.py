from http.server import BaseHTTPRequestHandler
import os
import json
import urllib.parse
from datetime import datetime, timezone, timedelta
import requests

SEOUL_API_KEY = os.environ.get("SEOUL_API_KEY", "5364436a77796d733931694d494558")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# 감시할 타겟 자치구 (필요시 인근 서대문구, 영등포구, 양천구 등 추가 가능)
TARGET_AREAS = {"강서구", "구로구", "마포구"}

# 상품명(SVCNM)에 아래 키워드 중 하나라도 반드시 포함되어야만 감시 대상에 편입
REQUIRED_KEYWORDS = ["주말", "토/일", "토·일", "토요일", "일요일", "공휴일", "휴일", "야간"]

# 제외할 키워드 (이용 제한 코트 등)
EXCLUDE_KEYWORDS = ["65세", "전용코트"]

CACHE_FILE = "/tmp/seoul_tennis_state.json"
MEMORY_CACHE = None  # None이면 서버 재시작 직후(Cold Start)를 의미


def load_cache():
    """이전 접수중 코트 목록 로드 (최초 실행 시 None 반환)"""
    global MEMORY_CACHE
    if MEMORY_CACHE is not None:
        return MEMORY_CACHE
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                MEMORY_CACHE = set(json.load(f))
                return MEMORY_CACHE
        except Exception:
            pass
    return None


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
        return {"ok": False, "error": "Token or Chat ID missing"}
    
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


def is_valid_court(place: str, svcnm: str) -> bool:
    """
    1) 제외 키워드(65세, 전용코트 등)가 없고
    2) 필수 키워드(주말, 토/일, 공휴일, 휴일, 야간)가 상품명에 명시된 코트만 통과
    """
    combined = f"{place} {svcnm}"
    if any(ex in combined for ex in EXCLUDE_KEYWORDS):
        return False
    return any(req in svcnm for req in REQUIRED_KEYWORDS)


def fetch_seoul_courts(all_areas=False, include_closed=False) -> list:
    service_type = urllib.parse.quote("테니스장")
    url = f"http://openAPI.seoul.go.kr:8088/{SEOUL_API_KEY}/json/ListPublicReservationSport/1/1000/{service_type}"
    
    matched_items = []
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

            # 서비스종료/접수종료는 제외
            if stat in {"서비스종료", "접수종료"}:
                continue

            # 지역 필터 (all_areas=True면 서울시 전체 조회)
            if not all_areas and area not in TARGET_AREAS:
                continue

            # 상태 필터 (include_closed=False면 '접수중'만 조회)
            if not include_closed and stat != "접수중":
                continue

            # 주말/공휴일/야간 명시 코트 여부 검증
            if is_valid_court(place, svcnm):
                matched_items.append({
                    "key": svcid,
                    "area": area,
                    "place": place,
                    "title": svcnm,
                    "status": stat,
                    "hours": f"{v_min}~{v_max}",
                    "url": svcurl,
                })
    except Exception as e:
        print("Seoul API Error:", e)

    return matched_items


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if CRON_SECRET and f"key={CRON_SECRET}" not in self.path:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        # [기능 1] ?list=true 접속 시: 주말/야간으로 올라오는 코트가 어디어디 있는지 전체 조회
        if "list=true" in self.path:
            target_area_courts = fetch_seoul_courts(all_areas=False, include_closed=True)
            all_seoul_courts = fetch_seoul_courts(all_areas=True, include_closed=True)

            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            res_data = {
                "설명": "상품명에 주말/토·일/공휴일/휴일/야간이 명시된 테니스장 목록 (마감 포함)",
                "1_내_관심지역_목록(강서/구로/마포)": [
                    f"[{c['area']} | {c['place']}] {c['title']} ({c['status']})"
                    for c in target_area_courts
                ],
                "2_서울시_전체_참고목록": [
                    f"[{c['area']} | {c['place']}] {c['title']} ({c['status']})"
                    for c in all_seoul_courts
                ],
            }
            self.wfile.write(json.dumps(res_data, ensure_ascii=False, indent=2).encode("utf-8"))
            return

        # [기능 2] ?test=true 접속 시: 텔레그램 발송 테스트
        if "test=true" in self.path:
            test_msg = (
                "🎾 <b>[서울시 테니스 알리미] 필터 업데이트 완료!</b>\n\n"
                f"• 감시 지역: {', '.join(TARGET_AREAS)}\n"
                f"• 감시 조건: 상품명에 <b>{', '.join(REQUIRED_KEYWORDS)}</b> 포함된 코트만 알림\n"
                "• 상시 열려있는 일반 코트(서남센터 등) 무분별 알림 차단됨"
            )
            tg_result = send_telegram(test_msg)
            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"test_mode": True, "telegram_result": tg_result}, ensure_ascii=False).encode("utf-8"))
            return

        # [기능 3] 기본 크론잡 모니터링 루틴
        prev_keys = load_cache()
        current_items = fetch_seoul_courts(all_areas=False, include_closed=False)
        current_keys = {item["key"] for item in current_items}

        telegram_sent = False
        new_items = []

        # 서버가 처음 켜졌을 때(prev_keys is None)는 기존 접수중 코트 알림을 보내지 않고 캐시만 초기화
        if prev_keys is not None:
            new_items = [item for item in current_items if item["key"] not in prev_keys]
            if new_items:
                lines = ["🎾 <b>[서울시 공공예약] 주말/야간 테니스장 빈자리 오픈!</b>\n"]
                for item in new_items[:10]:
                    lines.append(
                        f"• <b>[{item['area']} | {item['place']}]</b>\n"
                        f"  └ 코트명: {item['title']}\n"
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
            "cold_start_initialized": prev_keys is None,
            "currently_open_count": len(current_items),
            "new_alerts_sent": len(new_items),
            "telegram_sent": telegram_sent,
            "currently_open_courts": [f"[{i['area']} | {i['place']}] {i['title']}" for i in current_items],
            "timestamp": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        }
        self.wfile.write(json.dumps(response_body, ensure_ascii=False, indent=2).encode("utf-8"))
