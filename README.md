# TS Signal Bridge — API Railway

> Servidor que escuta canais do Telegram, parseia sinais de trading e os serve via HTTP para os EAs no MetaTrader 5.

---

## Como funciona

```
Canais Telegram
      ↓
  Railway API  ←  main.py (FastAPI + Telethon)
      ↓
GET /signal/pending   ←  EA MT5 consulta a cada 5s
      ↓
POST /signal/confirm  ←  EA confirma após abrir ordens
      ↓
  WhatsApp            ←  notificação via Evolution API
```

---

## Tecnologias

- **FastAPI** — servidor HTTP
- **Telethon** — cliente Telegram (StringSession)
- **Evolution API** — notificações WhatsApp

---

## Variáveis de Ambiente

Configure estas variáveis no Railway antes de fazer deploy:

| Variável | Descrição |
|---|---|
| `API_ID` | ID da aplicação Telegram |
| `API_HASH` | Hash da aplicação Telegram |
| `PHONE` | Número de telefone da conta Telegram |
| `SESSION_STRING` | Sessão Telethon serializada |
| `TELEGRAM_PASSWORD` | Senha 2FA do Telegram (se tiver) |
| `TELEGRAM_SIGNAL_GROUPS` | IDs dos grupos monitorados (separados por vírgula) |
| `API_KEY` | Token de autenticação dos EAs |
| `EVOLUTION_URL` | URL da Evolution API |
| `EVOLUTION_TOKEN` | Token da Evolution API |
| `EVOLUTION_INSTANCE` | Nome da instância WhatsApp |
| `WHATSAPP_NUMBER` | Número de destino das notificações |

Copie o arquivo `.env.example` para `.env` e preencha com seus valores.  
**Nunca suba o `.env` para o Git.**

---

## Deploy no Railway

```bash
# 1. Fork ou clone este repositório
# 2. Crie um projeto no Railway e conecte o repositório
# 3. Configure as variáveis de ambiente acima
# 4. O deploy é automático a cada push na branch main
```

---

## Endpoints

| Método | Endpoint | Descrição |
|---|---|---|
| `GET` | `/health` | Status do servidor e conexão Telegram |
| `GET` | `/signal/pending?symbol=XAUUSD` | Próximo sinal da fila filtrado por grupo |
| `POST` | `/signal/confirm` | EA confirma execução da ordem |
| `GET` | `/signals/queue` | Ver fila completa |
| `GET` | `/signals/history` | Histórico dos últimos 50 sinais |
| `DELETE` | `/signals/queue` | Limpar fila manualmente |
| `POST` | `/signal/test` | Injetar sinal manual para teste |
| `GET` | `/groups` | Listar grupos do Telegram conectados |
| `GET` | `/messages/{group_id}` | Ver mensagens recentes de um grupo |

---

## Filtro por família de símbolos

O parâmetro `?symbol=` garante que cada EA só recebe sinais do seu grupo:

| Parâmetro | Símbolos aceitos |
|---|---|
| `?symbol=XAUUSD` | XAUUSD, XAGUSD |
| `?symbol=FOREX` | EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD... |
| `?symbol=INDEX` | US30, NAS100, GER40, UK100, JP225 |
| `?symbol=CRYPTO` | BTCUSD, ETHUSD, LTCUSD, XRPUSD |
| `?symbol=OIL` | USOIL, UKOIL |

Sem o parâmetro, retorna o primeiro da fila (retrocompatível).

---

## Filtros do Parser (v2.2+)

| Filtro | O que rejeita |
|---|---|
| Anti-recap | "closed trades", "TOTAL: +700 PIPS", "daily result" |
| Faixa de preço | Entry impossível para o ativo (ex: GBPUSD @ 100.0) |
| TPs inválidos | TPs iguais ao entry ou fora da faixa do símbolo |
| SL obrigatório | XAUUSD, BTCUSD, NAS100, US30 sem Stop Loss |

---

## Estrutura do JSON de Sinal

```json
{
  "id": "uuid-v4",
  "symbol": "XAUUSD",
  "type": "SELL",
  "entry": 5186.0,
  "entry_min": 5182.0,
  "entry_max": 5186.0,
  "sl": 5190.0,
  "tps": [5086.0, 4986.0, 4886.0, 4786.0],
  "source": "Gold Signals.io",
  "time": "2026-03-12T21:16:38Z",
  "status": "pending"
}
```

> `entry_min` e `entry_max` só aparecem quando o canal enviou um range (ex: `@ 5182 - 5186`). Sinais pontuais não incluem esses campos.

---

## Versões

| Versão | Mudanças |
|---|---|
| v2.1 | Versão inicial |
| v2.2 | Filtros anti-recap, validação de faixa de preço, SL obrigatório |
| v2.3 | Suporte a range de entrada (`entry_min` / `entry_max`) |

---

## Licença

Uso privado — Copyright 2026, TSouto.
