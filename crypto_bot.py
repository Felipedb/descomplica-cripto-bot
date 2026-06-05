#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de status cripto para Telegram (envia uma IMAGEM/card por dia).

Coleta:
  - Fear & Greed Index (alternative.me)
  - Dominancia de BTC, ETH e demais altcoins + market cap total (CoinGecko /global)
  - Altcoin Season Index oficial (CoinMarketCap, top 100 vs BTC em 90 dias)
  - Preco e variacao 24h de BTC e ETH (CoinGecko /coins/markets)
  - Maiores altas e quedas em 24h (top movers)

Renderiza um card 1080x1350 (HTML + Playwright/Chromium) e envia como foto.
Se a imagem falhar por algum motivo, envia um resumo em texto (fallback).

Uso:
  python crypto_bot.py            # gera a imagem e envia ao Telegram
  python crypto_bot.py --dry-run  # gera a imagem e salva localmente (nao envia)

Variaveis de ambiente (para envio):
  TELEGRAM_BOT_TOKEN  -> token do bot do @BotFather
  TELEGRAM_CHAT_ID    -> ex.: @descomplicabtc  (ou id numerico)
  COINGECKO_DEMO_KEY  -> opcional, aumenta o rate limit do CoinGecko
"""

import os
import sys
import time
import html as htmllib
import argparse
from datetime import datetime, timezone, timedelta

import requests

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

FNG_URL = "https://api.alternative.me/fng/"
CG_BASE = "https://api.coingecko.com/api/v3"
CMC_ALTSEASON_URL = "https://api.coinmarketcap.com/data-api/v3/altcoin-season/chart"

BRT = timezone(timedelta(hours=-3))  # Brasilia (UTC-3)

CG_DEMO_KEY = os.environ.get("COINGECKO_DEMO_KEY", "").strip()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "card_template.html")

# Stablecoins e tokens lastreados/wrapped a excluir dos top movers
EXCLUDED_SYMBOLS = {
    "usdt", "usdc", "dai", "busd", "tusd", "usdd", "frax", "usdp", "gusd",
    "fdusd", "pyusd", "usde", "usdj", "eurt", "eurc", "usdx", "susd", "lusd",
    "usd1", "usdy", "usds", "rlusd", "ust", "mim",
    "wbtc", "weth", "steth", "wsteth", "wbeth", "reth", "cbeth", "meth",
    "weeth", "ezeth", "rseth", "lbtc", "cbbtc", "tbtc", "solvbtc", "msol",
    "jitosol", "bnsol", "wbnb", "beth", "reneth", "bsc-usd",
}

FNG_PT = {
    "Extreme Fear": "Medo Extremo", "Fear": "Medo", "Neutral": "Neutro",
    "Greed": "Ganancia", "Extreme Greed": "Ganancia Extrema",
}


# ---------------------------------------------------------------------------
# HTTP helper com retry/backoff
# ---------------------------------------------------------------------------

def http_get(url, params=None, headers=None, retries=5, timeout=20):
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                wait = 2 ** attempt * 3
                print(f"  rate limit (429), aguardando {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            print(f"  tentativa {attempt + 1} falhou ({e}); retry em {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"falha ao buscar {url}: {last_err}")


def cg_headers():
    return {"x-cg-demo-api-key": CG_DEMO_KEY} if CG_DEMO_KEY else None


# ---------------------------------------------------------------------------
# Coleta de dados
# ---------------------------------------------------------------------------

def get_fear_greed():
    item = http_get(FNG_URL, params={"limit": 1, "format": "json"})["data"][0]
    cls = item["value_classification"]
    return {"value": int(item["value"]), "pt": FNG_PT.get(cls, cls)}


def get_global():
    data = http_get(f"{CG_BASE}/global", headers=cg_headers())["data"]
    mcp = data["market_cap_percentage"]
    btc, eth = mcp.get("btc", 0.0), mcp.get("eth", 0.0)
    return {
        "btc_dominance": btc, "eth_dominance": eth,
        "alt_dominance": max(0.0, 100.0 - btc - eth),
        "total_mcap_usd": data["total_market_cap"]["usd"],
        "mcap_change_24h": data.get("market_cap_change_percentage_24h_usd", 0.0),
    }


def get_markets():
    params = {
        "vs_currency": "usd", "order": "market_cap_desc",
        "per_page": 250, "page": 1, "sparkline": "false",
        "price_change_percentage": "24h",
    }
    return http_get(f"{CG_BASE}/coins/markets", params=params, headers=cg_headers())


def get_coin(markets, coin_id):
    for c in markets:
        if c.get("id") == coin_id:
            return c
    return None


def get_altcoin_season():
    end = int(time.time())
    start = end - 10 * 86400
    data = http_get(CMC_ALTSEASON_URL, params={"start": start, "end": end},
                    headers={"User-Agent": "Mozilla/5.0"})
    points = data.get("data", {}).get("points", [])
    if not points:
        return None
    idx = int(round(float(points[-1]["altcoinIndex"])))
    if idx >= 75:
        label = "Altseason"
    elif idx <= 25:
        label = "Bitcoin Season"
    else:
        label = "Neutro / Transição"
    return {"index": idx, "label": label}


def get_top_movers(markets, top_n=200, k=5):
    pool = [c for c in markets[:top_n]
            if c.get("symbol", "").lower() not in EXCLUDED_SYMBOLS
            and c.get("price_change_percentage_24h") is not None]
    gainers = sorted(pool, key=lambda c: c["price_change_percentage_24h"], reverse=True)[:k]
    losers = sorted(pool, key=lambda c: c["price_change_percentage_24h"])[:k]
    return gainers, losers


# ---------------------------------------------------------------------------
# Formatacao (padrao brasileiro)
# ---------------------------------------------------------------------------

def br_num(n, dec=0):
    return f"{n:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_price(v):
    return br_num(v, 0 if v >= 10000 else 2)


def fmt_mcap(v):
    if v >= 1e12:
        return br_num(v / 1e12, 2) + " tri"
    if v >= 1e9:
        return br_num(v / 1e9, 2) + " bi"
    return br_num(v, 0)


def fg_color(v):
    if v < 25:
        return "#ea3943"
    if v < 45:
        return "#f7931a"
    if v < 55:
        return "#f3b30b"
    if v < 75:
        return "#7bd13b"
    return "#16c784"


def as_color(v):
    if v >= 75:
        return "#16c784"
    if v <= 25:
        return "#f7931a"
    return "#f3b30b"


def clamp(v, lo=2, hi=98):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Montagem do card (HTML -> PNG)
# ---------------------------------------------------------------------------

_ICON_CACHE = {}


def download_icon_b64(url):
    """Baixa o logo da moeda e devolve um data URI base64 (embutivel no HTML)."""
    if not url:
        return ""
    if url in _ICON_CACHE:
        return _ICON_CACHE[url]
    data = ""
    for _ in range(3):
        try:
            import base64
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            ct = r.headers.get("content-type", "image/png")
            data = "data:" + ct + ";base64," + base64.b64encode(r.content).decode()
            break
        except Exception:  # noqa: BLE001
            time.sleep(1)
    _ICON_CACHE[url] = data
    return data


def fmt_coin_price(v):
    if v >= 1000:
        return br_num(v, 0)
    if v >= 1:
        return br_num(v, 2)
    if v >= 0.01:
        return br_num(v, 4)
    if v >= 0.0001:
        return br_num(v, 6)
    return br_num(v, 8)


def _movers_html(items, positive):
    cls = "up" if positive else "dn"
    arrow = "\u25b2" if positive else "\u25bc"
    rows = ""
    for i, c in enumerate(items, 1):
        sym = htmllib.escape(c["symbol"].upper()[:8])
        icon = download_icon_b64(c.get("image", ""))
        price = fmt_coin_price(c.get("current_price") or 0)
        pct = br_num(abs(c["price_change_percentage_24h"]), 2)
        rows += (
            f'<div class="mrow"><span class="rk num">{i}</span>'
            f'<img src="{icon}">'
            f'<div class="mid"><div class="sym">{sym}</div>'
            f'<div class="p num">US$ {price}</div></div>'
            f'<div class="pct {cls} num">{arrow} {pct}%</div></div>'
        )
    return rows or '<div class="mrow">\u2014</div>'


def _ticker_item(label, chg):
    cls = "up" if chg >= 0 else "dn"
    ar = "\u25b2" if chg >= 0 else "\u25bc"
    return (f'<span class="tk"><span class="lbl">{label}</span> '
            f'<span class="{cls}">{ar} {br_num(abs(chg), 2)}%</span></span>')


def build_card_html(fng, glob, altseason, btc, eth, gainers, losers):
    now = datetime.now(BRT)
    edicao = (now - datetime(2025, 1, 1, tzinfo=BRT)).days

    b = glob["btc_dominance"] if glob else 0.0
    e = glob["eth_dominance"] if glob else 0.0
    a = glob["alt_dominance"] if glob else 0.0

    def pc(coin):
        return (coin.get("price_change_percentage_24h") or 0) if coin else 0

    parts = []
    if btc:
        parts.append(_ticker_item("BTC", pc(btc)))
    if eth:
        parts.append(_ticker_item("ETH", pc(eth)))
    if glob:
        parts.append(_ticker_item("MCAP", glob["mcap_change_24h"]))
    if fng:
        parts.append(f'<span class="tk"><span class="lbl">MEDO&amp;GAN.</span> {fng["value"]}</span>')
    if altseason:
        parts.append(f'<span class="tk"><span class="lbl">ALT.SEASON</span> {altseason["index"]}</span>')
    ticker = '<span class="sep">/</span>'.join(parts)

    repl = {
        "EDICAO": str(edicao),
        "DATA": now.strftime("%d.%m.%Y"),
        "HORA": now.strftime("%H:%M"),
        "TICKER": ticker,
        "FNG_VALUE": str(fng["value"]) if fng else "\u2014",
        "FNG_LABEL": htmllib.escape(fng["pt"]) if fng else "n/d",
        "FNG_POS": str(clamp(fng["value"], 0, 100)) if fng else "50",
        "FNG_COLOR": fg_color(fng["value"]) if fng else "#7a7264",
        "AS_VALUE": str(altseason["index"]) if altseason else "\u2014",
        "AS_LABEL": htmllib.escape(altseason["label"]) if altseason else "n/d",
        "AS_POS": str(clamp(altseason["index"], 0, 100)) if altseason else "50",
        "AS_COLOR": as_color(altseason["index"]) if altseason else "#7a7264",
        "DOM_BTC": br_num(b, 1), "DOM_ETH": br_num(e, 1), "DOM_ALT": br_num(a, 1),
        "DOM_BTC_W": f"{b:.1f}", "DOM_ETH_W": f"{e:.1f}", "DOM_ALT_W": f"{a:.1f}",
        "BTC_ICON": download_icon_b64(btc.get("image", "")) if btc else "",
        "BTC_PRICE": fmt_coin_price(btc["current_price"]) if btc else "\u2014",
        "BTC_CHG": br_num(abs(pc(btc)), 2),
        "BTC_ARROW": "\u25b2" if pc(btc) >= 0 else "\u25bc",
        "BTC_COLOR": "up" if pc(btc) >= 0 else "dn",
        "ETH_ICON": download_icon_b64(eth.get("image", "")) if eth else "",
        "ETH_PRICE": fmt_coin_price(eth["current_price"]) if eth else "\u2014",
        "ETH_CHG": br_num(abs(pc(eth)), 2),
        "ETH_ARROW": "\u25b2" if pc(eth) >= 0 else "\u25bc",
        "ETH_COLOR": "up" if pc(eth) >= 0 else "dn",
        "GAINERS": _movers_html(gainers, True),
        "LOSERS": _movers_html(losers, False),
    }
    tpl = open(TEMPLATE_PATH, encoding="utf-8").read()
    for k, vv in repl.items():
        tpl = tpl.replace("{{" + k + "}}", vv)
    return tpl


def render_card_png(html, out_path):
    """Renderiza o HTML em PNG 1080x1350 usando Chromium (Playwright)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1080, "height": 1350},
                                device_scale_factor=2)
        page.set_content(html, wait_until="networkidle")
        page.wait_for_timeout(1200)  # garante o carregamento das fontes
        page.screenshot(path=out_path, clip={"x": 0, "y": 0, "width": 1080, "height": 1350})
        browser.close()
    return out_path


def fmt_chg(v):
    return ("\U0001F7E2 +" if v >= 0 else "\U0001F534 -") + br_num(abs(v), 2) + "%"


def _cap_movers(items, positive):
    sign = "+" if positive else "-"
    emoji = "\U0001F7E2" if positive else "\U0001F534"
    out = []
    for c in items[:5]:
        sym = htmllib.escape(c["symbol"].upper()[:10])
        out.append(f"\u2022 {sym}: {emoji} {sign}{br_num(abs(c['price_change_percentage_24h']), 2)}%")
    return out


def build_caption(fng, glob, altseason, btc, eth, gainers, losers):
    """Legenda completa em texto, enviada junto com a imagem."""
    now = datetime.now(BRT).strftime("%d/%m/%Y")

    def pc(c):
        return (c.get("price_change_percentage_24h") or 0) if c else 0

    L = [f"\U0001F4CA <b>Status Cripto</b> \u2014 {now}", ""]
    if fng:
        L += [f"\U0001F631 <b>Fear &amp; Greed:</b> {fng['value']}/100 ({htmllib.escape(fng['pt'])})", ""]
    if glob:
        L += ["\U0001F451 <b>Domin\u00e2ncia</b>",
              f"\u2022 Bitcoin: {br_num(glob['btc_dominance'], 1)}%",
              f"\u2022 Ethereum: {br_num(glob['eth_dominance'], 1)}%",
              f"\u2022 Demais altcoins: {br_num(glob['alt_dominance'], 1)}%", ""]
    if altseason:
        L += [f"\u2696\uFE0F <b>Altcoin Season Index:</b> {altseason['index']}/100 ({htmllib.escape(altseason['label'])})",
              "<i>top 100 altcoins vs BTC nos \u00faltimos 90 dias</i>", ""]
    if btc or eth:
        L.append("\U0001F4B0 <b>Pre\u00e7os</b>")
        if btc:
            L.append(f"\u2022 BTC: US$ {fmt_coin_price(btc['current_price'])} ({fmt_chg(pc(btc))})")
        if eth:
            L.append(f"\u2022 ETH: US$ {fmt_coin_price(eth['current_price'])} ({fmt_chg(pc(eth))})")
        L.append("")
    if glob:
        L += ["\U0001F310 <b>Market Cap Total</b>",
              f"\u2022 US$ {fmt_mcap(glob['total_mcap_usd'])} ({fmt_chg(glob['mcap_change_24h'])} em 24h)", ""]
    if gainers:
        L.append("\U0001F680 <b>Maiores Altas (24h)</b>")
        L += _cap_movers(gainers, True)
        L.append("")
    if losers:
        L.append("\U0001F4C9 <b>Maiores Quedas (24h)</b>")
        L += _cap_movers(losers, False)
        L.append("")
    L.append("<i>Fontes: alternative.me, CoinGecko e CoinMarketCap</i>")
    cap = "\n".join(L).strip()
    if len(cap) > 1024:
        cap = cap[:1015].rstrip() + "\u2026"
    return cap


# ---------------------------------------------------------------------------
# Envio ao Telegram
# ---------------------------------------------------------------------------


def _creds():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise SystemExit("ERRO: defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.")
    return token, chat


def send_photo(path, caption):
    token, chat = _creds()
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    last_err = None
    for attempt in range(4):
        try:
            with open(path, "rb") as ph:
                resp = requests.post(url, data={"chat_id": chat, "caption": caption,
                                                "parse_mode": "HTML"},
                                     files={"photo": ph}, timeout=60)
            if not resp.ok:
                raise RuntimeError(f"Telegram {resp.status_code}: {resp.text}")
            print("Imagem enviada com sucesso ao Telegram.")
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  envio de foto falhou ({e}); retry...", file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"falha ao enviar foto: {last_err}")


def send_text(text):
    token, chat = _creds()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat, "text": text,
                                    "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Telegram {resp.status_code}: {resp.text}")
    print("Texto (fallback) enviado ao Telegram.")
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def collect():
    def safe(fn, default=None, label=""):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            print(f"  aviso: {label} indisponivel ({e})", file=sys.stderr)
            return default

    fng = safe(get_fear_greed, None, "Fear & Greed")
    glob = safe(get_global, None, "dados globais")
    altseason = safe(get_altcoin_season, None, "Altcoin Season")
    markets = safe(get_markets, [], "mercados") or []
    btc = get_coin(markets, "bitcoin") if markets else None
    eth = get_coin(markets, "ethereum") if markets else None
    gainers, losers = get_top_movers(markets) if markets else ([], [])
    return fng, glob, altseason, btc, eth, gainers, losers


def main():
    ap = argparse.ArgumentParser(description="Bot de status cripto (imagem) para Telegram")
    ap.add_argument("--dry-run", action="store_true", help="gera a imagem e salva, sem enviar")
    ap.add_argument("--out", default="card_preview.png", help="caminho do PNG no modo dry-run")
    args = ap.parse_args()

    fng, glob, altseason, btc, eth, gainers, losers = collect()
    caption = build_caption(fng, glob, altseason, btc, eth, gainers, losers)

    try:
        html = build_card_html(fng, glob, altseason, btc, eth, gainers, losers)
        out = args.out if args.dry_run else "/tmp/status_cripto.png"
        render_card_png(html, out)
    except Exception as e:  # noqa: BLE001
        print(f"AVISO: falha ao gerar a imagem ({e}); usando texto.", file=sys.stderr)
        if args.dry_run:
            raise
        send_text(caption)
        return

    if args.dry_run:
        print(f"Imagem gerada: {out}")
    else:
        try:
            send_photo(out, caption)
        except Exception as e:  # noqa: BLE001
            print(f"AVISO: falha ao enviar imagem ({e}); enviando texto.", file=sys.stderr)
            send_text(caption)


if __name__ == "__main__":
    main()
