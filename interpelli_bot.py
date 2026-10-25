#!/usr/bin/env python3
"""
Interpelli Bot — scova nuovi interpelli per supplenze e invia notifiche Telegram.

Come funziona
- Legge l'elenco delle fonti da sources.txt (siti USP, scuole, aggregatori).
- Per ogni fonte prova prima il feed RSS/Atom (anche con autodiscovery),
  altrimenti scansiona la pagina HTML cercando link che parlano di interpelli.
- Tiene traccia di cio' che ha gia' visto in seen.json, cosi' notifica solo le novita'.
- Filtri opzionali via variabili d'ambiente (parole chiave, province, classi di concorso).

Variabili d'ambiente
  TELEGRAM_BOT_TOKEN   token del bot (da @BotFather)          [obbligatoria]
  TELEGRAM_CHAT_ID     id della chat/canale destinatario       [obbligatoria]
  INCLUDE_KEYWORDS     es. "sostegno,ADSS,A022" (vuoto = tutto)
  EXCLUDE_KEYWORDS     es. "ATA,collaboratore"
  SOURCES_FILE         default: sources.txt
  SEEN_FILE            default: seen.json
  DRY_RUN=1            stampa a video invece di inviare
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (InterpelliBot; +https://github.com/)"
TIMEOUT = 20
KEYWORD = re.compile(r"interpell", re.I)
MAX_SEEN = 20000  # limite per non far crescere seen.json all'infinito


# ---------------------------------------------------------------- utilita'
def env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def item_id(link: str, title: str) -> str:
    return hashlib.sha1(f"{link}|{title}".encode()).hexdigest()


def load_seen(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_seen(path: Path, seen: dict) -> None:
    if len(seen) > MAX_SEEN:  # tieni i piu' recenti
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-MAX_SEEN:])
    path.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")


def load_sources(path: Path) -> list[tuple[str, str]]:
    """Righe 'Nome | URL'. Righe vuote o con # ignorate."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, url = [p.strip() for p in line.split("|", 1)]
        else:
            name, url = line, line
        out.append((name, url))
    return out


def fetch(url: str) -> requests.Response | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        print(f"  ! errore su {url}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- estrazione
def from_feed(content: bytes, base: str) -> list[dict]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        title = (e.get("title") or "").strip()
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")[:400]
        items.append({
            "title": title,
            "link": urljoin(base, e.get("link", "")),
            "text": f"{title} {summary}",
        })
    return items


def find_feed_url(html: str, base: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("link", rel="alternate",
                    type=re.compile(r"application/(rss|atom)\+xml"))
    return urljoin(base, tag["href"]) if tag and tag.get("href") else None


def from_html(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = a["href"]
        if KEYWORD.search(text) or KEYWORD.search(href):
            items.append({"title": text or href, "link": urljoin(base, href), "text": text})
    return items


def scan_source(url: str) -> list[dict]:
    r = fetch(url)
    if r is None:
        return []
    ctype = r.headers.get("content-type", "")
    if "xml" in ctype or r.text.lstrip().startswith("<?xml"):
        items = from_feed(r.content, url)
    else:
        feed_url = find_feed_url(r.text, url)
        items = []
        if feed_url:
            fr = fetch(feed_url)
            if fr is not None:
                items = from_feed(fr.content, feed_url)
        items += from_html(r.text, url)
    # tieni solo cio' che parla di interpelli
    return [i for i in items if KEYWORD.search(i["text"] + " " + i["link"])]


def passes_filters(item: dict, include: list[str], exclude: list[str]) -> bool:
    blob = f"{item['title']} {item['text']} {item['link']}".lower()
    if include and not any(k in blob for k in include):
        return False
    if exclude and any(k in blob for k in exclude):
        return False
    return True


# ---------------------------------------------------------------- notifiche
def notify(text: str) -> None:
    if os.getenv("DRY_RUN") == "1":
        print("NOTIFICA:\n" + text + "\n")
        return
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    ).raise_for_status()
    time.sleep(1)  # rispetta i limiti di Telegram


# ---------------------------------------------------------------- main
def main() -> None:
    sources = load_sources(Path(os.getenv("SOURCES_FILE", "sources.txt")))
    seen_path = Path(os.getenv("SEEN_FILE", "seen.json"))
    seen = load_seen(seen_path)
    first_run = not seen
    include, exclude = env_list("INCLUDE_KEYWORDS"), env_list("EXCLUDE_KEYWORDS")

    new_items = []
    for name, url in sources:
        print(f"- {name}")
        for it in scan_source(url):
            iid = item_id(it["link"], it["title"])
            if iid in seen:
                continue
            seen[iid] = int(time.time())
            if passes_filters(it, include, exclude):
                new_items.append((name, it))

    if first_run:
        # Primo avvio: memorizza tutto senza notificare, altrimenti arriverebbero
        # centinaia di messaggi su avvisi vecchi.
        print(f"Primo avvio: memorizzati {len(seen)} elementi, nessuna notifica.")
        notify(f"✅ Interpelli Bot attivo. Monitoro {len(sources)} fonti.")
    else:
        for name, it in new_items:
            notify(f"🔔 Nuovo interpello — {name}\n{it['title']}\n{it['link']}")
        print(f"Nuovi interpelli notificati: {len(new_items)}")

    save_seen(seen_path, seen)


if __name__ == "__main__":
    main()
