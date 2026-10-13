"""클라우드 발행 회차(GitHub Actions) — 차례면 카드뉴스 1편 발행, 다음에 깰 대기 시간을 GITHUB_OUTPUT 'next' 로.

규칙(사용자 2026-10-10): 하루 10편 · 09:00 부터 · 간격 60분 이상(줄이지 않는다) · 21:00 넘으면 그날은 끝.
겹침 방지: 이 저장소 기록(state/ledger.json)과 인스타 실제 마지막 게시 시각 둘 다 60분 확인.
덤: 07~22시엔 aliyah79.com wp-cron 을 깨운다(PC 가 없어도 워드프레스 예약 글이 제시간에 나가게).
python3 cloud/tick.py [--dry]   (표준 라이브러리만)
"""
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))  # 한국 = 서머타임 없음(윈도우엔 tzdata 가 없어 고정 +9)
ROOT = Path(__file__).resolve().parent.parent
ST = ROOT / "state"
REPO = os.environ.get("GITHUB_REPOSITORY", "KangJH79/insta-cardnews")
GRAPH = "https://graph.instagram.com/v23.0"
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
UID = os.environ.get("IG_USER_ID", "")
DRY = "--dry" in sys.argv


def jl(name, d):
    p = ST / name
    return json.loads(p.read_text("utf-8")) if p.exists() else d


def js(name, obj):
    (ST / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), "utf-8")


CFG = jl("config.json", {})
N = int(CFG.get("posts_per_day", 10))
GAP = dt.timedelta(minutes=int(CFG.get("min_gap_minutes", 60)))
MAX_FAIL = int(CFG.get("max_fail_per_day", 3))


class IGErr(RuntimeError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def api(method, path, **params):
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    url = f"{GRAPH}/{path}"
    for i in range(3):
        try:
            if method == "GET":
                req = urllib.request.Request(url + "?" + data.decode())
            else:
                req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode()).get("error", {})
            except Exception:  # noqa: BLE001
                err = {}
            if e.code >= 500 and i < 2:
                time.sleep(5 * (i + 1))
                continue
            raise IGErr(f"HTTP {e.code} code={err.get('code')} {err.get('message', '')[:160]}", err.get("code"))
        except (urllib.error.URLError, TimeoutError) as e:
            if i < 2:
                time.sleep(5 * (i + 1))
                continue
            raise IGErr(f"네트워크 {e}")


def wp_ping(now):
    if 7 <= now.hour < 22:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://aliyah79.com/wp-cron.php?doing_wp_cron={time.time():.4f}",
                headers={"User-Agent": "Mozilla/5.0 aliyah-cron (cloud)"}), timeout=30).read()
        except Exception:  # noqa: BLE001
            pass


def kst(s):
    return dt.datetime.fromisoformat(s).astimezone(KST)


def wait_ready(cid, tries=40):
    for _ in range(tries):  # 최대 2분
        st = api("GET", cid, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise IGErr(f"컨테이너 {st}", "container")
        time.sleep(3)
    raise IGErr("컨테이너 준비 2분 넘음", "container")


def head_ok(u):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=30) as r:
            return r.status == 200 and "image" in (r.headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        return False


def head_size(u):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=30) as r:
            return int(r.headers.get("Content-Length") or 0) if r.status == 200 else 0
    except Exception:  # noqa: BLE001
        return 0


def publish_reel(item):
    """★2026-10-14 릴스(비팔로워 통로) — video_url = media 브랜치 raw 주소.
    (10-14 실측: raw 는 mp4 를 application/octet-stream 으로 주지만 인스타가 받아 FINISHED · graph.instagram.com 은
     upload_type=resumable 을 안 받음 'video_url is required') · 처리 대기 최대 5분."""
    url = f"https://raw.githubusercontent.com/{REPO}/media/{item['id']}/reel.mp4"
    size = head_size(url)
    if size < 100_000:
        raise IGErr(f"릴스 파일 주소 안 열림·너무 작음({size}B)", "media")
    cid = api("POST", f"{UID}/media", media_type="REELS", video_url=url, caption=item["caption"],
              share_to_feed="true", thumb_offset=str(item.get("thumb_offset_ms") or 1000))["id"]
    wait_ready(cid, tries=100)
    if DRY:
        print(f"DRY: 릴스 컨테이너 FINISHED({size // 1024}KB) — 발행 안 함")
        return None
    mid = api("POST", f"{UID}/media_publish", creation_id=cid)["id"]
    info = api("GET", mid, fields="permalink,timestamp,caption,media_product_type")
    return {"id": item["id"], "media_id": mid, "permalink": info.get("permalink"), "format": "reel",
            "published_at": dt.datetime.now(KST).isoformat(timespec="seconds"), "cards": item["cards"],
            "caption_ok": (info.get("caption") or "").strip() == item["caption"].strip()}


def publish(item):
    if item.get("format") == "reel":
        try:
            return publish_reel(item)
        except IGErr as e:
            if e.code in (190, 10, 200, 4, 17, 32, 613) or "limit" in str(e).lower():
                raise
            print(f"릴스 실패 → 같은 글을 카드(캐러셀)로: {e}")
    urls = [f"https://raw.githubusercontent.com/{REPO}/media/{item['id']}/{i:02d}.jpg" for i in range(1, item["cards"] + 1)]
    bad = [u for u in urls if not head_ok(u)]
    if bad:
        raise IGErr(f"카드 그림 주소 안 열림 {len(bad)}장", "media")
    kids = [api("POST", f"{UID}/media", image_url=u, is_carousel_item="true")["id"] for u in urls]
    for k in kids:
        wait_ready(k)
    parent = api("POST", f"{UID}/media", media_type="CAROUSEL", children=",".join(kids), caption=item["caption"])["id"]
    wait_ready(parent)
    if DRY:
        print("DRY: 컨테이너까지 OK(인스타가 그림을 가져감) — 발행 안 함")
        return None
    mid = api("POST", f"{UID}/media_publish", creation_id=parent)["id"]
    info = api("GET", mid, fields="permalink,timestamp,caption,children{id}")
    n = len((info.get("children") or {}).get("data", []))
    return {"id": item["id"], "media_id": mid, "permalink": info.get("permalink"), "format": "carousel",
            "published_at": dt.datetime.now(KST).isoformat(timespec="seconds"), "cards": n,
            "caption_ok": (info.get("caption") or "").strip() == item["caption"].strip()}


def commit(msg):
    if DRY or not os.environ.get("GITHUB_ACTIONS"):
        return
    for i in range(3):
        subprocess.run(["git", "add", "state"], cwd=ROOT, check=True)
        if not subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
            return
        subprocess.run(["git", "-c", "user.name=insta-tick", "-c", "user.email=tick@insta-cardnews", "commit", "-q", "-m", msg],
                       cwd=ROOT, check=True)
        if subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], cwd=ROOT)
    print("기록 올리기 실패(3번)")


def wait_name(minutes):
    if minutes >= 150:
        return "wait120"
    if minutes >= 35:
        return "wait30"
    if minutes >= 6:
        return "wait5"
    return "wait1"


def main():
    now = dt.datetime.now(KST)
    wp_ping(now)
    q, led, blk = jl("queue.json", []), jl("ledger.json", []), jl("blocked.json", {})
    today = now.date().isoformat()
    sh, sm = map(int, CFG.get("start", "09:00").split(":"))
    eh, em = map(int, CFG.get("end", "21:00").split(":"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    tomorrow = start + dt.timedelta(days=1)
    pub_today = [r for r in led if kst(r["published_at"]).date().isoformat() == today]
    fails = [it for it in q if it.get("status") == "failed" and (it.get("failed_at") or "")[:10] == today]
    last = max([kst(r["published_at"]) for r in led] or [now - dt.timedelta(days=1)])
    due = max(start + GAP * len(pub_today), last + GAP)
    if blk.get("until"):
        due = max(due, kst(blk["until"]))
    msg = ""
    if len(pub_today) >= N or now >= end or len(fails) >= MAX_FAIL:
        due = max(tomorrow, kst(blk["until"])) if blk.get("until") else tomorrow
        msg = f"오늘 끝(발행 {len(pub_today)}/{N}, 실패 {len(fails)})"
    elif now >= due:
        ready = [it for it in q if it.get("status") == "ready" and it["id"] not in {r["id"] for r in led}]
        if not ready:
            msg = "발행할 '준비' 카드뉴스 없음"
            due = now + dt.timedelta(minutes=30)
        else:
            # 인스타 실제 마지막 게시 시각으로 한 번 더(60분)
            try:
                m = api("GET", f"{UID}/media", fields="timestamp", limit=1).get("data") or []
                if m:
                    api_last = dt.datetime.strptime(m[0]["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(KST)
                    if now < api_last + GAP:
                        due = api_last + GAP
                        msg = f"인스타 마지막 게시 {api_last:%H:%M} → {due:%H:%M} 까지 기다림"
            except IGErr as e:
                msg = f"마지막 게시 확인 실패({e}) → 30분 뒤"
                due = now + dt.timedelta(minutes=30)
            if not msg:
                item = ready[0]
                try:
                    rec = publish(item)
                    if rec:
                        led.append(rec)
                        item["status"] = "published"
                        js("ledger.json", led)
                        js("queue.json", q)
                        if blk:
                            js("blocked.json", {})
                        commit(f"published {item['id']}")
                        msg = f"발행 {item['id']} {rec.get('format')} {rec['permalink']} ({rec['cards']}장, 본문 일치 {rec['caption_ok']})"
                        due = dt.datetime.now(KST) + GAP
                    else:
                        msg = "DRY 끝"
                except IGErr as e:
                    if e.code in (190, 10, 200, 4, 17, 32, 613) or "limit" in str(e).lower():
                        js("blocked.json", {"until": (now + dt.timedelta(minutes=60)).isoformat(timespec="seconds"), "why": str(e)})
                        msg = f"계정·한도 문제 → 60분 쉼: {e}"
                        due = now + dt.timedelta(minutes=60)
                    else:
                        item["status"] = "failed"
                        item["error"] = str(e)
                        item["failed_at"] = now.isoformat(timespec="seconds")
                        js("queue.json", q)
                        msg = f"실패 {item['id']}: {e} → 다음 글은 5분 뒤"
                        due = now + dt.timedelta(minutes=5)
                    commit(f"tick: {msg[:60]}")
    else:
        msg = f"다음 차례 {due:%m-%d %H:%M} (오늘 {len(pub_today)}/{N})"
    mins = max(0, math.ceil((due - dt.datetime.now(KST)).total_seconds() / 60))
    nxt = wait_name(mins)
    print(f"[{now:%m-%d %H:%M}] {msg} · 다음 깨움 {nxt}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"next={nxt}\n")


if __name__ == "__main__":
    if "--test" in sys.argv:  # 시각 무시하고 첫 '준비' 글로 컨테이너까지만(발행 안 함)
        DRY = True
        it = next(it for it in jl("queue.json", []) if it.get("status") == "ready")
        print("시험", it["id"], publish(it))
    else:
        main()
