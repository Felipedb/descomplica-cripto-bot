#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
news_bot.py - Bot hibrido de noticias cripto para Telegram.

Mescla duas fontes e posta cada noticia nova como imagem + legenda no Telegram:

  1) Portais BR (RSS, ja em portugues):
     Cointelegraph Brasil, BeInCrypto Brasil, Livecoins, Portal do Bitcoin, CriptoFacil.

  2) Perfis de cripto no X (versao gratuita, via instancias Nitter):
     o texto vem em ingles e e traduzido para portugues.
     A captura do X e TOLERANTE A FALHA: se nenhuma instancia responder,
     o bot simplesmente ignora o X naquela rodada e segue com os portais.

Evita repetir noticia usando um estado local (seen_news.json).
Feito para rodar a cada 30 min no GitHub Actions.

Uso:
  python news_bot.py                 # coleta e posta as novidades no Telegram
  python news_bot.py --dry-run       # so mostra o que postaria (nao envia, nao salva estado)
  python news_bot.py --limit 5       # maximo de posts nesta rodada (padrao 6)
  ENABLE_X=0 python news_bot.py      # desliga a captura do X (so portais)

Variaveis de ambiente (para envio):
  TELEGRAM_BOT_TOKEN       -> token do bot do @BotFather
  TELEGRAM_NEWS_CHAT_ID    -> destino das noticias (ex.: @meucanal). Se vazio,
                              usa TELEGRAM_CHAT_ID como fallback.
"""

import os
import re
import sys
import json
import time
import html as htmllib
import argparse
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

import requests

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

BRT = timezone(timedelta(hours=-3))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(SCRIPT_DIR, "seen_news.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = (os.environ.get("TELEGRAM_NEWS_CHAT_ID", "").strip()
           or os.environ.get("TELEGRAM_CHAT_ID", "").strip())

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Redes do Felipe (entram no rodape de toda mensagem)
SOCIAL = {
    "youtube": "https://www.youtube.com/@Felipebatista_btc",
    "instagram": "https://www.instagram.com/felipebatista.btc/",
    "x": "https://x.com/TheFelipex0",
}

# Portais (RSS). lang "pt" = ja em portugues; "en" = sera traduzido.
# Cointelegraph Brasil saiu do ar (410), entao usamos o global (EN) traduzido.
RSS_FEEDS = [
    ("Cointelegraph", "https://cointelegraph.com/rss", "en"),
    ("BeInCrypto Brasil", "https://br.beincrypto.com/feed/", "pt"),
    ("Livecoins", "https://livecoins.com.br/feed/", "pt"),
    ("Portal do Bitcoin", "https://portaldobitcoin.uol.com.br/feed/", "pt"),
    ("CriptoFacil", "https://www.criptofacil.com/feed/", "pt"),
    ("Cointimes", "https://www.cointimes.com.br/feed/", "pt"),
]

# Perfis de cripto no X (texto em ingles -> traduzido para PT)
X_ACCOUNTS = [
    "BitcoinArchive",
    "TedPillows",
    "WatcherGuru",
    "WuBlockchain",
    "DocumentingBTC",
]

# Instancias Nitter para tentar, em ordem. Mudam com o tempo: e a parte
# fragil do projeto. Se nenhuma responder, o X e ignorado (sem erro fatal).
NITTER_INSTANCES = [
    "nitter.net",
    "nitter.poast.org",
    "nitter.privacydev.net",
    "lightbrd.com",
    "nitter.tiekoetter.com",
]

MAX_SEEN = 800          # quantos IDs guardar no estado (poda os mais antigos)
CAPTION_LIMIT = 1024    # limite de caracteres da legenda no sendPhoto


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def http_get(url, headers=None, timeout=20, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers or {"User-Agent": UA}, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET falhou {url}: {last}")


def clean_text(s):
    """Remove tags HTML e normaliza espacos."""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Coleta: portais BR (RSS)
# ---------------------------------------------------------------------------

def _extract_image(item, desc_html):
    """Tenta achar a URL de uma imagem no item do RSS."""
    for tag in item.iter():
        t = tag.tag.lower()
        url = tag.get("url") if hasattr(tag, "get") else None
        if not url:
            continue
        if t.endswith("content") and "image" in (tag.get("type") or "image"):
            return url
        if t.endswith("thumbnail"):
            return url
    enc = item.find("enclosure")
    if enc is not None and "image" in (enc.get("type") or ""):
        return enc.get("url", "")
    m = re.search(r'<img[^>]+src="([^"]+)"', desc_html or "")
    return m.group(1) if m else ""


def fetch_rss(name, url, lang="pt"):
    items = []
    try:
        xml = http_get(url).text
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except Exception as e:  # noqa: BLE001
        print(f"  [RSS] {name}: falhou ({e})", file=sys.stderr)
        return items
    for it in root.findall(".//item")[:12]:
        title = clean_text(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        desc_html = (it.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                     or it.findtext("description") or "")
        summary = clean_text(desc_html)
        image = _extract_image(it, desc_html)
        if not title or not link:
            continue
        items.append({
            "id": "rss:" + guid,
            "title": title,
            "summary": summary,
            "link": link,
            "image": image,
            "source": name,
            "lang": lang,
        })
    return items


# ---------------------------------------------------------------------------
# Coleta: perfis do X (Nitter RSS) - tolerante a falha
# ---------------------------------------------------------------------------

def _nitter_image(desc_html):
    m = re.search(r'<img[^>]+src="([^"]+)"', desc_html or "")
    if not m:
        return ""
    src = htmllib.unescape(m.group(1))
    # Nitter serve a imagem por /pic/<encoded>. Reconstroi a URL real do Twitter.
    mm = re.search(r"/pic/(.+)$", src)
    if mm:
        from urllib.parse import unquote
        path = unquote(mm.group(1))
        return "https://pbs.twimg.com/" + path.lstrip("/")
    return src if src.startswith("http") else ""


def fetch_x_account(user):
    """Pega os tweets recentes de um perfil via Nitter. Retorna [] se falhar."""
    for inst in NITTER_INSTANCES:
        url = f"https://{inst}/{user}/rss"
        try:
            xml = http_get(url, timeout=12, retries=1).text
            root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
        except Exception:  # noqa: BLE001
            continue
        items = []
        for it in root.findall(".//item")[:6]:
            text = clean_text(it.findtext("title"))
            link = (it.findtext("link") or "").strip()
            # ignora respostas e retweets (comecam com "R to @" / "RT by")
            if text.lower().startswith(("r to @", "rt by")):
                continue
            m = re.search(r"/status/(\d+)", link)
            tid = m.group(1) if m else link
            desc_html = it.findtext("description") or ""
            image = _nitter_image(desc_html)
            if not text:
                continue
            items.append({
                "id": "x:" + str(tid),
                "title": text,
                "summary": "",
                "link": f"https://x.com/{user}/status/{tid}",
                "image": image,
                "source": "@" + user,
                "lang": "en",
            })
        if items:
            return items
    print(f"  [X] @{user}: nenhuma instancia respondeu (ignorando)", file=sys.stderr)
    return []


# ---------------------------------------------------------------------------
# Traducao EN -> PT (so para os tweets)
# ---------------------------------------------------------------------------

def translate_en_pt(text):
    text = (text or "").strip()
    if not text:
        return text
    try:
        from deep_translator import GoogleTranslator
        out = GoogleTranslator(source="auto", target="pt").translate(text[:4900])
        return out or text
    except Exception as e:  # noqa: BLE001
        print(f"  [traducao] falhou ({e}); mantendo original", file=sys.stderr)
        return text


# ---------------------------------------------------------------------------
# Estado (evita repetir noticia)
# ---------------------------------------------------------------------------

def load_seen():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return list(data.get("ids", []))
    except Exception:  # noqa: BLE001
        return []


def save_seen(ids):
    ids = ids[-MAX_SEEN:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"ids": ids}, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Montagem da legenda (estilo BitNada, com as redes do Felipe)
# ---------------------------------------------------------------------------

def _footer():
    return ('➡️ Siga: '
            f'▶️ <a href="{SOCIAL["youtube"]}">YouTube</a> · '
            f'📸 <a href="{SOCIAL["instagram"]}">Instagram</a> · '
            f'𝕏 <a href="{SOCIAL["x"]}">X</a>')


def build_caption(item):
    title = item["title"]
    if item["lang"] == "en":
        title = translate_en_pt(title)
    title = re.sub(r"\s+", " ", title).strip()

    src = htmllib.escape(item["source"])
    link = htmllib.escape(item["link"], quote=True)
    fonte = f'📰 Fonte: <a href="{link}">{src}</a>'
    foot = _footer()

    summary = ""
    if item["lang"] == "pt":
        summary = clean_text(item.get("summary", ""))
        # nao repetir o titulo dentro do resumo
        if summary[:40].lower() == title[:40].lower():
            summary = ""
        if len(summary) > 280:
            summary = summary[:280].rsplit(" ", 1)[0] + "…"

    def assemble(t, s):
        head = "<b>" + htmllib.escape(t) + "</b>"
        blocks = [head] + ([htmllib.escape(s)] if s else []) + [fonte, foot]
        return "\n\n".join(blocks)

    # tira o resumo se estourar; depois encurta o titulo ate caber
    if len(assemble(title, summary)) > CAPTION_LIMIT:
        summary = ""
    while len(assemble(title, summary)) > CAPTION_LIMIT and len(title) > 40:
        title = title[:len(title) - 40].rsplit(" ", 1)[0] + "…"
    return assemble(title, summary)


# ---------------------------------------------------------------------------
# Envio ao Telegram
# ---------------------------------------------------------------------------

def tg_api(method, payload):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    for i in range(3):
        try:
            r = requests.post(url, data=payload, timeout=30)
            j = r.json()
            if j.get("ok"):
                return j
            print(f"  [tg] {method}: {j.get('description')}", file=sys.stderr)
            return j
        except Exception as e:  # noqa: BLE001
            print(f"  [tg] {method} tentativa {i + 1}: {e}", file=sys.stderr)
            time.sleep(2 * (i + 1))
    return {"ok": False}


def send_item(item):
    caption = build_caption(item)
    image = item.get("image", "")
    if image:
        j = tg_api("sendPhoto", {
            "chat_id": TG_CHAT, "photo": image,
            "caption": caption, "parse_mode": "HTML",
        })
        if j.get("ok"):
            return True
        # imagem falhou (URL quebrada etc.) -> manda como texto
    j = tg_api("sendMessage", {
        "chat_id": TG_CHAT, "text": caption, "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })
    return bool(j.get("ok"))


# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------

def collect():
    items = []
    for name, url, lang in RSS_FEEDS:
        items += fetch_rss(name, url, lang)
    if os.environ.get("ENABLE_X", "1") != "0":
        for user in X_ACCOUNTS:
            items += fetch_x_account(user)
            time.sleep(1)
    return items


def _title_key(title):
    return re.sub(r"\W+", "", (title or "").lower())[:60]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra o que postaria, sem enviar nem salvar estado")
    ap.add_argument("--limit", type=int, default=6,
                    help="maximo de posts nesta rodada")
    args = ap.parse_args()

    if not args.dry_run and (not TG_TOKEN or not TG_CHAT):
        print("ERRO: defina TELEGRAM_BOT_TOKEN e TELEGRAM_NEWS_CHAT_ID "
              "(ou rode com --dry-run).", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    seen_set = set(seen)
    first_run = len(seen_set) == 0

    items = collect()
    new = [it for it in items if it["id"] not in seen_set]
    print(f"coletados {len(items)} | novos {len(new)} | primeira_execucao={first_run}",
          file=sys.stderr)

    # na primeira execucao evita inundar o canal: posta no maximo 1
    limit = 1 if first_run else args.limit

    posted = 0
    used_titles = set()
    for it in new:
        if posted >= limit:
            break
        tk = _title_key(it["title"])
        if tk in used_titles:
            continue
        if args.dry_run:
            tag = "[traduz EN]" if it["lang"] == "en" else ""
            print(f"\nDRY {it['source']} {tag}")
            print(f"  {it['title'][:100]}")
            print(f"  img={bool(it['image'])} {it['link']}")
            used_titles.add(tk)
            posted += 1
            continue
        if send_item(it):
            used_titles.add(tk)
            posted += 1
            time.sleep(2)

    if not args.dry_run:
        for it in items:
            if it["id"] not in seen_set:
                seen.append(it["id"])
                seen_set.add(it["id"])
        save_seen(seen)

    print(f"postados {posted}", file=sys.stderr)


if __name__ == "__main__":
    main()
