#!/usr/bin/env python3
"""Spotify (καλλιτέχνες που ακολουθείς) -> άρθρα από AI -> στατικό site στο docs/.

Τοπική προεπισκόπηση χωρίς κλειδιά:  python build.py --demo
"""
import html
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "docs"
SEEN_FILE = DATA / "seen.json"
ARTICLES_FILE = DATA / "articles.json"

SITE_NAME = os.getenv("SITE_NAME", "Νέα Μουσική")
RECENT_DAYS = int(os.getenv("RECENT_DAYS", "14"))       # άρθρο μόνο για κυκλοφορίες των τελευταίων Χ ημερών
MAX_NEW = int(os.getenv("MAX_NEW_PER_RUN", "10"))       # ανώτατο όριο άρθρων ανά εκτέλεση (έλεγχος κόστους)
MODEL = os.getenv("AI_MODEL", "claude-haiku-5-5")
MARKET = os.getenv("SPOTIFY_MARKET", "GR")
API = "https://api.spotify.com/v1"

SYSTEM_PROMPT = (
    "Είσαι συντάκτης ενός μουσικού blog. Γράψε σύντομο ειδησεογραφικό άρθρο "
    "90-130 λέξεων στα ελληνικά για τη νέα κυκλοφορία που περιγράφεται. "
    "Χρησιμοποίησε ΜΟΝΟ τα στοιχεία που σου δίνονται. Μην επινοείς στίχους, θεματολογία, "
    "συνεργασίες, δηλώσεις, κριτική ή περιγραφή του ήχου, γιατί δεν έχεις ακούσει το κομμάτι. "
    "Πρώτη γραμμή: ο τίτλος του άρθρου (χωρίς εισαγωγικά, χωρίς markdown). "
    "Μετά μια κενή γραμμή και το κείμενο σε 1-2 παραγράφους."
)


# ---------- βοηθητικά ----------

def need(name):
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Λείπει η μεταβλητή περιβάλλοντος {name} (GitHub Secret).")
    return value


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def parse_date(s):
    p = s.split("-")
    try:
        return date(int(p[0]), int(p[1]) if len(p) > 1 else 1, int(p[2]) if len(p) > 2 else 1)
    except (ValueError, IndexError):
        return date.min


def fmt_date(s):
    return parse_date(s).strftime("%d/%m/%Y") if len(s) == 10 else s


def esc(s):
    return html.escape(str(s), quote=True)


# ---------- Spotify ----------

def spotify_token():
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "refresh_token", "refresh_token": need("SPOTIFY_REFRESH_TOKEN")},
        auth=(need("SPOTIFY_CLIENT_ID"), need("SPOTIFY_CLIENT_SECRET")),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def sp_get(token, url, params=None):
    for _ in range(5):
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Το Spotify επιστρέφει συνεχώς 429 (rate limit).")


def followed_artists(token):
    artists, after = [], None
    while True:
        params = {"type": "artist", "limit": 50}
        if after:
            params["after"] = after
        page = sp_get(token, f"{API}/me/following", params)["artists"]
        artists += page["items"]
        after = (page.get("cursors") or {}).get("after")
        if not after or not page["items"]:
            return artists


def latest_releases(token, artist_id):
    params = {"include_groups": "album,single", "limit": 10, "market": MARKET}
    return sp_get(token, f"{API}/artists/{artist_id}/albums", params)["items"]


# ---------- AI ----------

def write_article(album):
    names = ", ".join(a["name"] for a in album["artists"])
    facts = (
        f"Καλλιτέχνης/ες: {names}\n"
        f"Τίτλος: {album['name']}\n"
        f"Τύπος: {album['album_type']}\n"
        f"Ημερομηνία κυκλοφορίας: {album['release_date']}\n"
        f"Αριθμός κομματιών: {album.get('total_tracks', '?')}"
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": need("ANTHROPIC_API_KEY"),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": MODEL, "max_tokens": 600, "system": SYSTEM_PROMPT,
              "messages": [{"role": "user", "content": facts}]},
        timeout=90,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"]).strip()
    lines = text.split("\n")
    title = lines[0].strip().strip('"«»#* ').strip()
    body = "\n".join(lines[1:]).strip()
    if not title or not body:
        title, body = f"{names}: {album['name']}", text
    return title, body


def to_record(album, title, body):
    images = album.get("images") or []
    return {
        "id": album["id"],
        "title": title,
        "body": body,
        "artists": ", ".join(a["name"] for a in album["artists"]),
        "name": album["name"],
        "type": album["album_type"],
        "release_date": album["release_date"],
        "cover": images[0]["url"] if images else "",
        "url": (album.get("external_urls") or {}).get("spotify", ""),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------- site ----------

CSS = """
:root{--bg:#f3f5f9;--ink:#12141c;--muted:#5b6172;--rule:#d9deea;--accent:#2640ff;--panel:#e8ecf5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1220;--ink:#eceef6;--muted:#9aa1b8;--rule:#262b40;--accent:#8c99ff;--panel:#181c30}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.65 Literata,Georgia,serif}
a{color:inherit}
a:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
header,main,footer{max-width:880px;margin:0 auto;padding:0 20px}
header{padding-top:28px;padding-bottom:18px;border-bottom:2px solid var(--ink)}
header a{font:800 clamp(28px,6vw,40px)/1 Commissioner,system-ui,sans-serif;letter-spacing:-.02em;text-decoration:none}
main{padding-top:28px;padding-bottom:40px}
h1,h2,.meta{font-family:Commissioner,system-ui,sans-serif}
.meta{color:var(--muted);font-size:15px;margin:0}
.lead{display:grid;gap:18px;grid-template-columns:1fr;text-decoration:none;padding-bottom:28px;border-bottom:1px solid var(--rule)}
.lead img{width:100%;aspect-ratio:1;object-fit:cover;background:var(--panel)}
.lead h2{font-size:clamp(26px,5vw,38px);line-height:1.15;margin:8px 0;letter-spacing:-.01em}
.lead p{margin:0 0 10px}
.lead:hover h2,.row:hover h2{text-decoration:underline;text-decoration-color:var(--accent);text-underline-offset:4px}
.row{display:grid;grid-template-columns:76px 1fr;gap:16px;align-items:center;text-decoration:none;padding:16px 0;border-bottom:1px solid var(--rule)}
.row img,.row .ph{width:76px;height:76px;object-fit:cover;background:var(--panel)}
.row h2{font-size:19px;line-height:1.3;margin:0 0 2px}
@media (min-width:720px){.lead{grid-template-columns:340px 1fr;align-items:center}}
article h1{font-size:clamp(28px,6vw,42px);line-height:1.15;letter-spacing:-.01em;margin:10px 0}
article .cover{width:min(100%,340px);aspect-ratio:1;object-fit:cover;background:var(--panel);display:block}
article p{max-width:64ch}
article iframe{border:0;width:100%;max-width:640px;margin-top:14px}
.open{display:inline-block;margin-top:12px;font:700 16px Commissioner,system-ui,sans-serif;color:var(--accent)}
footer{padding-bottom:40px;color:var(--muted);font:14px/1.5 Commissioner,system-ui,sans-serif}
"""

FONTS = ("https://fonts.googleapis.com/css2?family=Commissioner:wght@500;700;800"
         "&family=Literata:opsz,wght@7..72,400;7..72,600&display=swap&subset=greek")


def page(title, body, depth=0):
    pre = "../" * depth
    return (
        '<!doctype html><html lang="el"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f'<link rel="stylesheet" href="{FONTS}"><link rel="stylesheet" href="{pre}style.css"></head><body>'
        f'<header><a href="{pre}index.html">{esc(SITE_NAME)}</a></header><main>{body}</main>'
        "<footer>Τα άρθρα γράφονται αυτόματα από AI με βάση τα στοιχεία κυκλοφορίας του Spotify.</footer>"
        "</body></html>"
    )


def type_label(t):
    return "Άλμπουμ" if t == "album" else "Single"


def meta_line(a):
    return f"{esc(a['artists'])} | {type_label(a['type'])} | {esc(fmt_date(a['release_date']))}"


def first_paragraph(body):
    return body.split("\n\n")[0].strip()


def index_html(articles):
    if not articles:
        return page(SITE_NAME, "<p>Δεν υπάρχουν άρθρα ακόμα. Θα εμφανιστούν με την επόμενη εκτέλεση.</p>")
    lead, rest = articles[0], articles[1:]
    cover = f'<img src="{esc(lead["cover"])}" alt="Εξώφυλλο: {esc(lead["name"])}">' if lead["cover"] else ""
    out = (
        f'<a class="lead" href="a/{esc(lead["id"])}.html">{cover}<div>'
        f'<p class="meta">{meta_line(lead)}</p><h2>{esc(lead["title"])}</h2>'
        f'<p>{esc(first_paragraph(lead["body"]))}</p></div></a>'
    )
    for a in rest:
        img = f'<img src="{esc(a["cover"])}" alt="" loading="lazy">' if a["cover"] else '<div class="ph"></div>'
        out += (f'<a class="row" href="a/{esc(a["id"])}.html">{img}<div>'
                f'<h2>{esc(a["title"])}</h2><p class="meta">{meta_line(a)}</p></div></a>')
    return page(SITE_NAME, out)


def article_html(a):
    cover = f'<img class="cover" src="{esc(a["cover"])}" alt="Εξώφυλλο: {esc(a["name"])}">' if a["cover"] else ""
    paras = "".join(f"<p>{esc(p.strip())}</p>" for p in a["body"].split("\n\n") if p.strip())
    embed = (f'<iframe src="https://open.spotify.com/embed/album/{esc(a["id"])}" height="352" '
             'allow="encrypted-media" loading="lazy" title="Spotify player"></iframe>')
    link = f'<a class="open" href="{esc(a["url"])}">Άνοιγμα στο Spotify</a>' if a["url"] else ""
    body = f'<article>{cover}<h1>{esc(a["title"])}</h1><p class="meta">{meta_line(a)}</p>{paras}{embed}<br>{link}</article>'
    return page(a["title"], body, depth=1)


def render(articles):
    articles = sorted(articles, key=lambda a: (parse_date(a["release_date"]), a["created"]), reverse=True)
    (SITE / "a").mkdir(parents=True, exist_ok=True)
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    (SITE / "style.css").write_text(CSS.strip(), encoding="utf-8")
    (SITE / "index.html").write_text(index_html(articles), encoding="utf-8")
    for a in articles:
        (SITE / "a" / f"{a['id']}.html").write_text(article_html(a), encoding="utf-8")


DEMO = [
    {"id": "demo1", "title": "Δοκιμαστικό άρθρο: νέο single από τον Demo Artist",
     "body": "Αυτό είναι δείγμα κειμένου για να δεις πώς φαίνεται το site.\n\nΤα πραγματικά άρθρα θα γράφονται από το AI.",
     "artists": "Demo Artist", "name": "Demo Single", "type": "single", "release_date": "2026-10-16",
     "cover": "", "url": "https://open.spotify.com", "created": "2026-10-20T06:00:00+00:00"},
    {"id": "demo2", "title": "Δοκιμαστικό άρθρο: νέο άλμπουμ από τους Example Band",
     "body": "Δεύτερο δείγμα, για τη λίστα της αρχικής σελίδας.",
     "artists": "Example Band", "name": "Demo Album", "type": "album", "release_date": "2026-10-09",
     "cover": "", "url": "https://open.spotify.com", "created": "2026-10-20T06:00:00+00:00"},
]


# ---------- κύριο πρόγραμμα ----------

def main():
    if "--demo" in sys.argv:
        render(DEMO)
        print("Έτοιμο: άνοιξε το docs/index.html")
        return

    articles = load(ARTICLES_FILE, [])
    seen = set(load(SEEN_FILE, []))
    cutoff = date.today() - timedelta(days=RECENT_DAYS)

    token = spotify_token()
    artists = followed_artists(token)
    print(f"Καλλιτέχνες που ακολουθείς: {len(artists)}")

    fresh = {}
    for artist in artists:
        for album in latest_releases(token, artist["id"]):
            if album["id"] in seen or album["id"] in fresh:
                continue
            if parse_date(album["release_date"]) < cutoff:
                seen.add(album["id"])  # παλιά κυκλοφορία: την σημειώνουμε χωρίς άρθρο
            else:
                fresh[album["id"]] = album

    queue = sorted(fresh.values(), key=lambda al: parse_date(al["release_date"]), reverse=True)[:MAX_NEW]
    print(f"Νέες κυκλοφορίες για άρθρο: {len(queue)}")

    for album in queue:
        try:
            title, body = write_article(album)
        except Exception as e:  # δεν σημειώνεται ως seen, θα ξαναπροσπαθήσει αύριο
            print(f"Αποτυχία για {album['name']}: {e}")
            continue
        articles.append(to_record(album, title, body))
        seen.add(album["id"])
        print("Άρθρο:", title)

    save(ARTICLES_FILE, articles)
    save(SEEN_FILE, sorted(seen))
    render(articles)


if __name__ == "__main__":
    main()
