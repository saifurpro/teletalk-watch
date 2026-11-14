import json, os, re, hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

ROOT = "teletalk.com.bd"
SEEDS = ["dof.teletalk.com.bd", "dls.teletalk.com.bd"]
DATA = "data/notices.json"
HDR = {"User-Agent": "Mozilla/5.0 (compatible; TeletalkWatch/1.0)"}
BD = timezone(timedelta(hours=6))
now = lambda: datetime.now(BD).isoformat(timespec="seconds")

# order matters: first match wins
TYPES = [
    ("admit", ["admit card", "admit", "প্রবেশপত্র", "এডমিট", "অ্যাডমিট"]),
    ("result", ["result", "ফলাফল", "রেজাল্ট", "selected", "নির্বাচিত"]),
    ("circular", ["circular", "recruitment", "vacancy", "নিয়োগ", "আবেদন", "apply", "বিজ্ঞপ্তি"]),
    ("notice", ["notice", "নোটিশ", "news", "সংবাদ"]),
]
SUBPAGE = ["notice", "নোটিশ", "circular", "বিজ্ঞপ্তি", "news", "result", "admit"]


def classify(text, href):
    t = (text + " " + href).lower()
    for name, words in TYPES:
        if any(w in t for w in words):
            return name
    return "notice" if href.lower().endswith(".pdf") else None


# bare menu labels such as "Admit Card" exist on every site all year: not a real publication
GENERIC = re.compile(
    r"^(home|admit card|admit|result|results|notice|notices|circular|circulars|news|apply|apply online|"
    r"নোটিশ|ফলাফল|প্রবেশপত্র|এডমিট কার্ড|অ্যাডমিট কার্ড|নিয়োগ বিজ্ঞপ্তি|বিজ্ঞপ্তি)$", re.I)


def in_menu(a):
    for p in a.parents:
        if p.name in ("nav", "header", "footer"):
            return True
        c = " ".join(p.get("class", [])) + " " + (p.get("id") or "")
        if re.search(r"nav|menu|header|footer|breadcrumb", c, re.I):
            return True
    return False


def get(url):
    for u in (url, url.replace("https://", "http://")):
        try:
            r = requests.get(u, headers=HDR, timeout=25)
            if r.status_code < 400:
                r.encoding = r.apparent_encoding or r.encoding
                return r
        except requests.RequestException:
            pass
    return None


def discover(known):
    hosts = set(SEEDS) | set(known)
    try:
        r = requests.get(f"https://crt.sh/?q=%25.{ROOT}&output=json", headers=HDR, timeout=60)
        for row in r.json():
            for n in row.get("name_value", "").split("\n"):
                n = n.strip().lower().lstrip("*.")
                if n.endswith(ROOT) and n != ROOT and " " not in n:
                    hosts.add(n)
    except Exception as e:
        print("crt.sh failed:", e)
    return hosts


def scrape(host):
    home = get(f"https://{host}/")
    if not home:
        return host, None
    pages, items, title = [home], {}, host
    soup0 = BeautifulSoup(home.text, "lxml")
    if soup0.title and soup0.title.get_text(strip=True):
        title = soup0.title.get_text(strip=True)[:120]
    subs = []
    for a in soup0.find_all("a", href=True):
        u = urljoin(home.url, a["href"])
        if urlparse(u).netloc == urlparse(home.url).netloc and any(
            w in a.get_text(" ", strip=True).lower() for w in SUBPAGE
        ) and u not in subs and not u.lower().endswith(".pdf"):
            subs.append(u)
    for u in subs[:3]:
        r = get(u)
        if r:
            pages.append(r)
    for p in pages:
        for a in BeautifulSoup(p.text, "lxml").find_all("a", href=True):
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            href = urljoin(p.url, a["href"])
            if len(text) < 4 or len(text) > 300 or href.startswith(("javascript", "mailto", "tel")):
                continue
            is_pdf = href.lower().split("?")[0].endswith(".pdf")
            if not is_pdf and (in_menu(a) or GENERIC.match(text.strip(" :-|"))):
                continue
            typ = classify(text, href)
            if not typ:
                continue
            iid = hashlib.sha1(f"{host}|{href}|{text.lower()}".encode()).hexdigest()[:16]
            items[iid] = {"id": iid, "host": host, "type": typ, "title": text, "url": href}
    return host, {"title": title, "items": items}


def tg(msg):
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": msg[:4000], "disable_web_page_preview": True}, timeout=20)
        except requests.RequestException:
            pass


def main():
    os.makedirs("data", exist_ok=True)
    db = {"sites": {}, "items": {}, "history": []}
    if os.path.exists(DATA):
        db = json.load(open(DATA, encoding="utf-8"))
    sites, items, hist, t = db["sites"], db["items"], db["history"], now()
    alerts = []

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(scrape, sorted(discover(sites.keys()))))

    for host, res in results:
        s = sites.get(host)
        if res is None:  # unreachable: keep old data, log state change once
            if s and s.get("status") == "up":
                s["status"] = "down"
                hist.append({"time": t, "host": host, "kind": "down", "text": "সাইট এক্সেস করা যাচ্ছে না"})
            elif not s:
                sites[host] = {"title": host, "status": "down", "first_seen": t, "last_checked": t, "count": 0}
            else:
                s["last_checked"] = t
            continue
        first = s is None
        if first:
            s = sites[host] = {"first_seen": t}
            hist.append({"time": t, "host": host, "kind": "site", "text": f"নতুন সাবডোমেইন পাওয়া গেছে: {res['title']}"})
            if len(sites) > 3:
                alerts.append(f"🌐 নতুন সাইট: {host}")
        elif s.get("status") == "down":
            hist.append({"time": t, "host": host, "kind": "up", "text": "সাইট আবার চালু"})
        s.update(title=res["title"], status="up", last_checked=t, count=len(res["items"]))

        for iid, it in res["items"].items():
            if iid in items:
                items[iid].update(last_seen=t, active=True)
                continue
            it.update(first_seen=t, last_seen=t, active=True, base=first)  # base = existed at first scan
            items[iid] = it
            if not first:
                hist.append({"time": t, "host": host, "kind": it["type"], "text": it["title"], "url": it["url"]})
                alerts.append(f"🔔 [{it['type']}] {host}\n{it['title']}\n{it['url']}")
        for iid, it in items.items():
            if it["host"] == host and iid not in res["items"] and it.get("active"):
                it["active"] = False
                hist.append({"time": t, "host": host, "kind": "removed", "text": "সরানো হয়েছে: " + it["title"]})

    db["updated"], db["history"] = t, hist[-1500:]
    json.dump(db, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for i in range(0, len(alerts), 8):
        tg("\n\n".join(alerts[i:i + 8]))
    print(f"sites={len(sites)} items={len(items)} new_alerts={len(alerts)}")


if __name__ == "__main__":
    main()
