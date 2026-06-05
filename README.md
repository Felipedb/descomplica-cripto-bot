# Bot de Status Cripto para Telegram

Todo dia ao meio-dia (horário de Brasília) o bot gera um **card visual** com o panorama do mercado cripto e envia como imagem para o Telegram.

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
(boletim editorial em papel claro, 1080x1500) e renderiza em PNG usando o Chromium headless
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
3. O agendamento já está em `.github/workflows/daily.yml` (15:00 UTC = 12:00 BRT).
4. Para testar na hora: aba **Actions -> Status Cripto Diario -> Run workflow**.

O workflow instala o Chromium automaticamente (`playwright install --with-deps chromium`).

## Arquivos

| Arquivo | Função |
|---|---|
| `crypto_bot.py` | Coleta os dados, monta o card e envia a imagem |
| `card_template.html` | Template visual (boletim editorial) do card |
| `requirements.txt` | Dependências (requests, playwright) |
| `.github/workflows/daily.yml` | Agendamento diário na nuvem |
| `.env.example` | Modelo das variáveis para rodar local |

## Segurança

O token do bot é secreto: use os Secrets do GitHub ou um `.env` local (já ignorado pelo Git).
Se vazar, gere outro no @BotFather (**/mybots -> bot -> API Token -> Revoke**).
