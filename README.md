# Bot de Status Cripto para Telegram

Todo dia às 18h (horário de Brasília) o bot gera um **card visual** com o panorama do mercado cripto e envia como imagem para o Telegram.

## O que o card mostra

- **Medo & Ganância** (Fear & Greed) e **Altcoin Season** com medidores
- **Altcoin Season Index** (índice oficial do CoinMarketCap, top 100 vs BTC em 90 dias)
- **Dominância** de Bitcoin, Ethereum e Altcoins (barra)
- **Preço** de BTC e ETH com variação 24h
- **Market Cap total** do mercado com variação 24h
- **5 maiores altas** e **5 maiores quedas** em 24h (top 200), cada uma com o **logo** da moeda

Fontes gratuitas, sem chave obrigatória: alternative.me, CoinGecko e CoinMarketCap.

## Como funciona

`crypto_bot.py` coleta os dados, preenche o template `card_template.html`
(boletim editorial em papel claro, 1080x1400) e renderiza em PNG usando o Chromium headless
(Playwright). A imagem é enviada via `sendPhoto`. Se a renderização falhar, o bot
envia um resumo em texto como fallback.

## Status atual

- Bot: **@Descomplica_Cripto_Bot** ("Descomplica Cripto Bot")
- Teste já enviado com sucesso (imagem) no chat privado com o bot
- Destino final: grupo **Descomplica Cripto** (@descomplicabtc)

## Falta 1 passo para postar no grupo

O bot ainda não é membro do grupo. Faça uma vez:

1. Abra o grupo **Descomplica Cripto**.
2. Nome do grupo no topo -> **Administradores** -> **Adicionar administrador**.
3. Busque **@Descomplica_Cripto_Bot** e adicione.
4. Mantenha a permissão **Enviar mensagens**. Salve.

Depois, o destino (`TELEGRAM_CHAT_ID`) pode ser `@descomplicabtc` ou o id `-1001703101989`.

## Testar localmente

Requisitos: Python 3.10+.

```bash
pip install -r requirements.txt
python -m playwright install chromium

# pre-visualizar (gera o PNG, nao envia):
python crypto_bot.py --dry-run --out preview.png

# enviar de verdade:
export TELEGRAM_BOT_TOKEN="seu_token"
export TELEGRAM_CHAT_ID="@descomplicabtc"
python crypto_bot.py
```

No Windows (PowerShell), troque `export X=Y` por `$env:X="Y"`.
O token fica no @BotFather: **/mybots -> escolha o bot -> API Token**.

## Rodar automático todo dia (GitHub Actions)

1. Suba esta pasta para um repositório no GitHub (inclua a pasta `.github/` e o `card_template.html`).
2. **Settings -> Secrets and variables -> Actions -> New repository secret**:
   - `TELEGRAM_BOT_TOKEN` (token do @BotFather)
   - `TELEGRAM_CHAT_ID` (`@descomplicabtc`)
   - `COINGECKO_DEMO_KEY` (opcional)
3. O agendamento já está em `.github/workflows/daily.yml` (21:00 UTC = 18:00 BRT).
4. Para testar na hora: aba **Actions -> Status Cripto Diario -> Run workflow**.

O workflow instala o Chromium automaticamente (`playwright install --with-deps chromium`).

## Arquivos

| Arquivo | Função |
|---|---|
| `crypto_bot.py` | Boletim diário: coleta dados, monta o card e envia a imagem |
| `card_template.html` | Template visual (boletim editorial) do card |
| `news_bot.py` | Bot de notícias: solta manchetes novas em fluxo espaçado (gotejamento) |
| `seen_news.json` | Estado das notícias já postadas (evita repetir) |
| `requirements.txt` | Dependências (requests, playwright, deep-translator) |
| `.github/workflows/daily.yml` | Agendamento do boletim diário (18h BRT) |
| `.github/workflows/news.yml` | Worker contínuo de notícias (1 post a cada ~30 min) |
| `.env.example` | Modelo das variáveis para rodar local |

## Segundo bot: Notícias automáticas (news_bot.py)

Além do boletim diário, há um segundo bot que publica **notícias** ao longo do dia, no estilo de canais como o BitNada: cada notícia vira uma mensagem com imagem + manchete + fonte + as suas redes no rodapé (YouTube, Instagram, X).

Ele mescla duas fontes:

- **Portais BR (RSS, já em português):** BeInCrypto Brasil, Livecoins, Portal do Bitcoin, CriptoFácil, Cointimes e Cointelegraph (global, traduzido para PT).
- **Perfis de cripto no X (gratuito, via Nitter):** o texto vem em inglês e é traduzido. Esta é a parte **frágil**: as instâncias Nitter caem com frequência. Se nenhuma responder, o bot simplesmente ignora o X naquela rodada e segue com os portais, sem erro.

Para **não repetir** notícia, o bot guarda em `seen_news.json` o que já foi postado. Na primeira execução ele posta só 1 item (para não inundar o canal) e marca o resto como visto.

Para **não postar notícia velha**, o bot lê o `pubDate` de cada item e ignora tudo com mais de `NEWS_MAX_AGE_H` horas (padrão 8). A mesma validade se aplica à fila: item que esperar demais expira em silêncio em vez de ser postado atrasado. Também há um histórico de manchetes (`titles`) que evita repostar a mesma notícia quando outro portal cobre o mesmo fato.

### Entrega em gotejamento (evita rajadas)

O cron do GitHub Actions é "best-effort" (atrasa e pula execuções), então, em vez de depender dele, o `news.yml` sobe um **worker contínuo**: um único job vive cerca de 5h e, dentro dele, roda o bot a cada **~30 min** (via `sleep`), com uma trava de tempo (`NEWS_MIN_GAP_MIN`) que garante o espaçamento mesmo se o agendador adiantar ou acumular execuções. O bot mantém uma **fila** de notícias pendentes em `seen_news.json` e solta **1 por execução** (`NEWS_PER_RUN`), priorizando as mais relevantes. Uma notícia só é marcada como vista quando é de fato postada, então nada se perde. O cron horário (`0 * * * *`) é só uma rede de segurança para reerguer o worker caso ele caia, e o `concurrency` evita workers duplicados.

Para um fluxo **perfeitamente regular**, rode num servidor sempre-ligado:

```bash
LOOP_INTERVAL=1800 python news_bot.py --loop   # 1 post a cada 30 min
```

Se algum run falhar, o workflow envia um aviso no Telegram (passo `if: failure()`).

Destino: crie o secret **`TELEGRAM_NEWS_CHAT_ID`** com o canal/grupo das notícias (pode ser o mesmo `@descomplicabtc` ou um novo canal só de notícias). Se ficar vazio, usa o `TELEGRAM_CHAT_ID`.

Testar local (não envia):

```bash
python news_bot.py --dry-run
```

Agendamento: `.github/workflows/news.yml` roda como worker contínuo (1 post a cada ~30 min) e salva o estado (fila + vistos) de volta no repositório (precisa de `permissions: contents: write`, já configurado no workflow).

Para ajustar: edite no topo do `news_bot.py` as listas `RSS_FEEDS` (portais), `X_ACCOUNTS` (perfis do X), `NITTER_INSTANCES` (espelhos do X) e `SOCIAL` (suas redes).

## Segurança

O token do bot é secreto: use os Secrets do GitHub ou um `.env` local (já ignorado pelo Git).
Se vazar, gere outro no @BotFather (**/mybots -> bot -> API Token -> Revoke**).
