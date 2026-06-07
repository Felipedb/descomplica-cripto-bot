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
  python news_bot.py                 # solta ate NEWS_PER_RUN noticia(s) da fila
  python news_bot.py --dry-run       # so mostra o que postaria (nao envia, nao salva estado)
  python news_bot.py --limit 1       # maximo de posts nesta rodada (padrao: NEWS_PER_RUN ou 1)
  python news_bot.py --loop          # roda continuamente (1 post a cada LOOP_INTERVAL s)
  ENABLE_X=0 python news_bot.py      # desliga a captura do X (so portais)

Estrategia de entrega (gotejamento):
  O bot mantem uma FILA de noticias pendentes em seen_news.json e solta poucas
  por execucao (padrao 1), priorizando as mais recentes. Assim, mesmo quando o
  agendador (cron do GitHub Actions) atrasa ou acumula rodadas, o canal recebe
  um fluxo espacado em vez de rajadas. Para fluxo perfeitamente regular, rode
  com --loop em um servidor sempre-ligado.

Variaveis de ambiente:
  TELEGRAM_BOT_TOKEN       -> token do bot do @BotFather
  TELEGRAM_NEWS_CHAT_ID    -> destino das noticias (ex.: @meucanal). Se vazio,
                              usa TELEGRAM_CHAT_ID como fallback.
  TELEGRAM_CHAT_ID         -> fallback do destino e alvo dos alertas de falha
  NEWS_PER_RUN             -> quantas noticias soltar por execucao (padrao 1)
  LOOP_INTERVAL            -> segundos entre ciclos no modo --loop (padrao 900)
  ENABLE_X                 -> 0 desliga a captura do X
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
MAX_QUEUE = 20          # tamanho maximo da fila de pendentes (poda os menos relevantes)
CAPTION_LIMIT = 1024    # limite de caracteres da legenda no sendPhoto

# quantas noticias soltar por execucao (gotejamento). 1 evita rajadas.
PER_RUN_DEFAULT = max(1, int(os.environ.get("NEWS_PER_RUN", "1") or "1"))

# intervalo minimo entre posts (minutos). Com cron */15, garante ~1 a cada 30 min
# mesmo quando o agendador do GitHub adianta, atrasa ou acumula execucoes.
MIN_GAP_MIN = max(1, int(os.environ.get("NEWS_MIN_GAP_MIN", "28") or "28"))

# Relevancia: noticia com esses termos no titulo/resumo sobe na fila e e postada
# antes das demais. Pesos maiores = assuntos mais "quentes".
RELEVANCE = {
    "bitcoin": 3, "btc": 3, "ethereum": 2, "eth": 2, "etf": 3, "sec": 2,
    "halving": 3, "blackrock": 3, "fed": 2, "regula": 2, "hack": 2,
    "recorde": 3, "maxima": 2, "máxima": 2, "all-time": 3, "ath": 2,
    "bilhao": 2, "bilhão": 2, "bilhoes": 2, "bilhões": 2,
    "trilhao": 3, "trilhão": 3, "trilhoes": 3, "trilhões": 3,
    "aprova": 2, "dispara": 2, "despenca": 2, "rompe": 1, "trump": 2,
    "binance": 2, "coinbase": 2, "microstrategy": 2, "strategy": 2,
    "saylor": 2, "ataque": 2, "roubo": 2, "golpe": 2, "processo": 1,
    "alta": 1, "queda": 1, "salto": 1, "tombo": 1, "lei": 1, "ouro": 1,
}


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

def load_state():
    """Le o estado. Retrocompativel com formatos antigos.

    Retorna (ids, queue, last_post_ts). A fila guarda itens completos ainda nao
    postados; last_post_ts e o epoch do ultimo envio (para a trava de tempo).
    """
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        ids = list(data.get("ids", []))
        queue = [q for q in data.get("queue", [])
                 if isinstance(q, dict) and q.get("id")
                 and q.get("title") and q.get("link")]
        last_ts = float(data.get("last_post_ts", 0) or 0)
        return ids, queue, last_ts
    except Exception:  # noqa: BLE001
        return [], [], 0.0


def save_state(ids, queue, last_ts):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"ids": ids[-MAX_SEEN:], "queue": queue[:MAX_QUEUE],
                   "last_post_ts": last_ts}, f, ensure_ascii=False)


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


def relevance_score(item):
    """Nota de relevancia pela presenca de termos de peso no titulo/resumo."""
    text = ((item.get("title") or "") + " " + (item.get("summary") or "")).lower()
    return sum(w for kw, w in RELEVANCE.items() if kw in text)


def _alert_failure(err):
    """Best-effort: avisa o admin no Telegram que a rodada falhou."""
    admin = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or TG_CHAT
    if not TG_TOKEN or not admin:
        return
    try:
        msg = f"⚠️ news_bot falhou: {type(err).__name__}: {str(err)[:300]}"
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": admin, "text": msg},
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


def run_once(per_run, dry_run=False):
    """Uma rodada: coleta, enfileira novidades, ordena por relevancia e (se a
    trava de tempo permitir) solta ate `per_run` noticia(s) do topo da fila.

    - A fila e ordenada por relevancia (RELEVANCE): a mais "quente" sai antes.
    - Uma noticia so e marcada como vista quando postada; o resto fica na fila.
    - A trava (MIN_GAP_MIN) garante ~1 post a cada 30 min mesmo com o cron */15
      e com os atrasos/adiantamentos do agendador do GitHub.
    Retorna o numero de itens postados.
    """
    seen, queue, last_ts = load_state()
    seen_set = set(seen)
    queued_ids = {q["id"] for q in queue}

    items = collect()
    fresh = 0
    for it in items:
        if it["id"] in seen_set or it["id"] in queued_ids:
            continue
        it["score"] = relevance_score(it)
        queue.append(it)
        queued_ids.add(it["id"])
        fresh += 1

    # dedup defensivo + garante score nos itens que ja estavam na fila
    seen_q, deduped = set(), []
    for q in queue:
        if q["id"] in seen_q or q["id"] in seen_set:
            continue
        if "score" not in q:
            q["score"] = relevance_score(q)
        seen_q.add(q["id"])
        deduped.append(q)
    # ordena por relevancia (desc); empate mantem ordem de chegada (estavel)
    deduped.sort(key=lambda q: q.get("score", 0), reverse=True)
    queue = deduped[:MAX_QUEUE]

    now = time.time()
    gap_left = MIN_GAP_MIN * 60 - (now - last_ts)
    print(f"coletados {len(items)} | novos {fresh} | fila {len(queue)} | "
          f"gap_restante {max(0, int(gap_left / 60))}min", file=sys.stderr)

    # trava de tempo: so posta se ja passou o intervalo minimo desde o ultimo post
    if gap_left > 0 and not dry_run:
        save_state(seen, queue, last_ts)
        print("postados 0 (trava de tempo ativa)", file=sys.stderr)
        return 0

    posted = 0
    used_titles = set()
    while queue and posted < per_run:
        it = queue[0]
        tk = _title_key(it["title"])
        if tk in used_titles:
            queue.pop(0)
            continue
        if dry_run:
            tag = "[traduz EN]" if it.get("lang") == "en" else ""
            print(f"\nDRY score={it.get('score')} {it['source']} {tag}  "
                  f"{it['title'][:85]}", file=sys.stderr)
            used_titles.add(tk)
            queue.pop(0)
            posted += 1
            continue
        if send_item(it):
            used_titles.add(tk)
            seen.append(it["id"])
            seen_set.add(it["id"])
            queue.pop(0)
            last_ts = time.time()
            posted += 1
            time.sleep(2)
        else:
            # Falha de envio: tenta de novo na proxima rodada. Apos 3 tentativas
            # descarta o item para nao travar a fila.
            it["tries"] = int(it.get("tries", 0)) + 1
            if it["tries"] >= 3:
                print(f"  [drop] desistindo de {it['id']} apos {it['tries']} "
                      f"tentativas", file=sys.stderr)
                seen.append(it["id"])
                seen_set.add(it["id"])
                queue.pop(0)
            break

    if not dry_run:
        save_state(seen, queue, last_ts)
    print(f"postados {posted} | fila_restante {len(queue)}", file=sys.stderr)
    return posted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra o que postaria, sem enviar nem salvar estado")
    ap.add_argument("--limit", type=int, default=None,
                    help="maximo de posts nesta rodada (padrao: NEWS_PER_RUN ou 1)")
    ap.add_argument("--loop", action="store_true",
                    help="roda continuamente, 1 post a cada LOOP_INTERVAL s")
    args = ap.parse_args()

    if not args.dry_run and (not TG_TOKEN or not TG_CHAT):
        print("ERRO: defina TELEGRAM_BOT_TOKEN e TELEGRAM_NEWS_CHAT_ID "
              "(ou rode com --dry-run).", file=sys.stderr)
        sys.exit(1)

    per_run = max(1, args.limit if args.limit is not None else PER_RUN_DEFAULT)

    if not args.loop:
        try:
            run_once(per_run, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001
            _alert_failure(e)
            raise
        return

    interval = max(60, int(os.environ.get("LOOP_INTERVAL", "900") or "900"))
    print(f"modo loop: 1 ciclo a cada {interval}s (per_run={per_run})",
          file=sys.stderr)
    while True:
        try:
            run_once(per_run, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001
            print(f"  [loop] rodada falhou: {e}", file=sys.stderr)
            _alert_failure(e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
