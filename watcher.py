# -*- coding: utf-8 -*-
"""
동작구시설관리공단(삼일수영장) 수강신청 페이지에서
'마감'이었던 강좌가 빈자리로 바뀌면 디스코드로 알림을 보내는 스크립트.

동작 원리:
1. 강좌 목록 페이지(여러 페이지)를 가져온다.
2. 각 강좌의 '잔여' 칸을 읽어서, 이전 실행 때 저장해둔 상태(state.json)와 비교한다.
3. '마감' -> '숫자(빈자리 있음)' 로 바뀐 강좌가 있으면 디스코드 웹훅으로 메시지를 보낸다.
4. 최신 상태를 state.json에 다시 저장한다. (다음 실행에서 비교용으로 사용)
"""

import json
import os
import re
import subprocess
import sys
import time

import requests
from bs4 import BeautifulSoup

# ── 감시 대상 설정 ────────────────────────────────────────────────
BASE_URL = "https://sports.idongjak.or.kr/home/171"
PARAMS_COMMON = {
    "category2": "ALL",
    "center": "DONGJAK07",   # 삼일수영장
    "category1": "01",       # 강습수영(단체)
}
MAX_PAGES = 10  # 페이지가 이보다 많아지지 않는 한 자동으로 끝까지 읽는다.

# ── 반복 실행 설정 (거의 실시간 감시용) ──────────────────────────
# CHECK_INTERVAL_SECONDS 마다 한 번씩 확인하면서,
# RUN_DURATION_SECONDS 시간이 지나면 스크립트가 스스로 종료된다.
# (GitHub Actions는 작업 하나가 6시간을 넘길 수 없기 때문에,
#  6시간이 되기 전에 안전하게 끝내고, 워크플로 스케줄이 새 작업을 다시 시작해준다.)
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "25"))
RUN_DURATION_SECONDS = int(os.environ.get("RUN_DURATION_SECONDS", str(5 * 3600 + 30 * 60)))  # 5시간 30분

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_page(page: int) -> str:
    params = dict(PARAMS_COMMON)
    params["page"] = page
    resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def parse_courses(html: str) -> dict:
    """표에서 강좌 정보를 뽑아서 {고유키: {name, time, remain, url}} 형태로 반환."""
    soup = BeautifulSoup(html, "html.parser")
    courses = {}

    for row in soup.select("table tr"):
        cells = row.find_all("td")
        # 헤더 기준 컬럼 순서: 번호,센터,종목/분류,강좌명,요일/시간,수강료,정원,잔여,신청 (9칸)
        if len(cells) != 9:
            continue

        name_cell, time_cell, remain_cell, apply_cell = (
            cells[3],
            cells[4],
            cells[7],
            cells[8],
        )

        link = apply_cell.find("a") or name_cell.find("a")
        if not link or not link.get("href"):
            continue

        href = link["href"]
        m = re.search(r"class_cd=([^&]+).*?item_cd=([^&]+)", href)
        if not m:
            continue

        key = f"{m.group(1)}_{m.group(2)}"
        name = name_cell.get_text(" ", strip=True)
        time_txt = time_cell.get_text(" ", strip=True)
        remain = remain_cell.get_text(strip=True)
        url = href if href.startswith("http") else "https://sports.idongjak.or.kr" + href

        courses[key] = {
            "name": name,
            "time": time_txt,
            "remain": remain,
            "url": url,
        }

    return courses


def fetch_all_courses() -> dict:
    all_courses = {}
    for page in range(1, MAX_PAGES + 1):
        html = fetch_page(page)
        courses = parse_courses(html)
        if not courses and page > 1:
            break
        all_courses.update(courses)
    return all_courses


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_open(remain: str) -> bool:
    remain = (remain or "").strip()
    return remain != "" and "마감" not in remain


def commit_and_push_state() -> None:
    """state.json 변경분을 깃허브에 커밋/푸시한다. (재시작 시에도 이전 상태를 기억하기 위함)"""
    try:
        subprocess.run(["git", "config", "user.name", "swim-watcher-bot"], check=True)
        subprocess.run(
            ["git", "config", "user.email", "actions@users.noreply.github.com"], check=True
        )
        subprocess.run(["git", "add", "state.json"], check=True, cwd=os.path.dirname(__file__) or ".")
        diff = subprocess.run(
            ["git", "diff", "--staged", "--quiet"], cwd=os.path.dirname(__file__) or "."
        )
        if diff.returncode == 0:
            return  # 변경 없음
        subprocess.run(["git", "commit", "-m", "update state [skip ci]"], check=True)
        subprocess.run(["git", "pull", "--rebase"], check=True)
        subprocess.run(["git", "push"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"state.json 커밋/푸시 중 오류(계속 진행함): {e}", file=sys.stderr)


def send_discord(message: str) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL이 설정되지 않았습니다. 알림을 보내지 않습니다.")
        return
    resp = requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
    if resp.status_code >= 300:
        print(f"디스코드 전송 실패: {resp.status_code} {resp.text}", file=sys.stderr)


def check_once(old_state: dict) -> dict:
    """한 번 사이트를 확인한다. 새로 빈자리가 생겼으면 디스코드로 알리고,
    상태가 바뀌었으면 파일에 저장 + 깃허브에 커밋한다. 최신 상태를 반환한다."""
    new_state = fetch_all_courses()

    if not new_state:
        print("강좌를 하나도 읽어오지 못했습니다. (일시적 오류일 수 있음, 다음 주기에 재시도)")
        return old_state

    newly_available = []
    for key, info in new_state.items():
        now_open = is_open(info["remain"])
        was_open = is_open(old_state.get(key, {}).get("remain", "마감"))
        if now_open and not was_open:
            newly_available.append(info)

    if newly_available:
        lines = ["🏊 **수영 강습 빈자리 발생!**"]
        for c in newly_available:
            lines.append(f"- **{c['name']}** ({c['time']}) — 잔여: {c['remain']}\n{c['url']}")
        send_discord("\n".join(lines))
        print(f"새로 빈자리 생긴 강좌 {len(newly_available)}건 알림 전송")

    if new_state != old_state:
        save_state(new_state)
        commit_and_push_state()

    return new_state


def main():
    state = load_state()
    start = time.time()
    check_count = 0

    while time.time() - start < RUN_DURATION_SECONDS:
        try:
            state = check_once(state)
        except Exception as e:  # 네트워크 오류 등으로 루프 전체가 죽지 않도록 방어
            print(f"확인 중 오류 발생(계속 진행함): {e}", file=sys.stderr)

        check_count += 1
        if check_count % 20 == 0:
            elapsed_min = int((time.time() - start) / 60)
            print(f"[{elapsed_min}분 경과] 지금까지 {check_count}번 확인함, 정상 동작 중")

        time.sleep(CHECK_INTERVAL_SECONDS)

    print("설정된 실행 시간이 끝나 스크립트를 종료합니다. (스케줄이 곧 새로 시작해줍니다)")


if __name__ == "__main__":
    main()
