from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from pydantic import BaseModel
from typing import Optional
import os, re, uuid, logging, json
import urllib.request, urllib.parse
from datetime import datetime, timezone

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── variáveis de ambiente ─────────────────────────────────────────────────────
API_ID   = os.environ["API_ID"]
API_HASH = os.environ["API_HASH"]
PHONE    = os.environ["PHONE"]
SECRET_KEY = os.environ.get("API_KEY", "chave-secreta")

# ── Bot Telegram para notificações ───────────────────────────────────────────
# Configure essas duas variáveis no Railway (Settings > Variables):
#   TG_NOTIFY_TOKEN  → token do bot  (ex: 7412345678:AAFxxxxx)
#   TG_NOTIFY_CHATID → chat_id do destino (ex: -1001234567890 para grupo/canal
#                       ou 123456789 para DM)
TG_NOTIFY_TOKEN  = os.environ.get("TG_NOTIFY_TOKEN", "")
TG_NOTIFY_CHATID = os.environ.get("TG_NOTIFY_CHATID", "")

# ── Telethon com StringSession ────────────────────────────────────────────────
session_string = os.environ.get("SESSION_STRING", "")
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

SIGNAL_GROUPS = [
    int(x.strip())
    for x in os.environ.get("TELEGRAM_SIGNAL_GROUPS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]

# ── fila de sinais (em memória) ───────────────────────────────────────────────
signal_queue:   list[dict] = []
signal_history: list[dict] = []

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="TS Signal Bridge", version="2.7.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConfirmRequest(BaseModel):
    id:      str
    status:  str
    message: str
    account: Optional[str] = ""

# =============================================================================
# NOTIFICAÇÃO TELEGRAM (substitui WhatsApp)
# =============================================================================
async def enviar_telegram(mensagem: str) -> None:
    """Envia mensagem via Bot API do Telegram (parse_mode=HTML)."""
    if not TG_NOTIFY_TOKEN or not TG_NOTIFY_CHATID:
        log.warning("TG_NOTIFY_TOKEN ou TG_NOTIFY_CHATID não configurados — notificação ignorada")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_NOTIFY_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id":    TG_NOTIFY_CHATID,
            "text":       mensagem,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info(f"Telegram notificado com sucesso [HTTP {resp.status}]")
    except Exception as e:
        log.error(f"Erro ao enviar notificação Telegram: {e}")


def _fmt_tps(tps: list) -> str:
    return " / ".join(str(tp) for tp in tps)


# =============================================================================
# FILTRO FINAL DE SEMANA
# =============================================================================
def e_final_de_semana() -> bool:
    return datetime.now(timezone.utc).weekday() >= 5

# =============================================================================
# FILTROS ANTI-LIXO
# =============================================================================
_RECAP_PATTERNS = [
    r"closed\s+trade",
    r"total[:\s]+[+\-]?\d+\s*pips",
    r"\bweekly\s+result",
    r"\bdaily\s+result",
    r"\bresult[o]?\s+do\s+dia",
    r"\btrades?\s+fechad",
    r"\bprofit\s+today",
    r"\bperformance\s+update",
    r"\bscore\s+today",
    r"tp\s*\d+\s+(?:hit|atingido|alcançado|batido)",
    r"(?:hit|atingido)\s+tp\s*\d+",
]
_RECAP_RE = re.compile("|".join(_RECAP_PATTERNS), re.IGNORECASE)

def _e_recap(text: str) -> bool:
    return bool(_RECAP_RE.search(text))

_PRICE_RANGES = {
    "XAUUSD": (1000.0, 9999.0),
    "XAGUSD": (10.0, 200.0),
    "EURUSD": (0.80, 1.60),
    "GBPUSD": (1.00, 2.00),
    "AUDUSD": (0.50, 1.20),
    "NZDUSD": (0.40, 1.10),
    "USDCAD": (1.00, 1.80),
    "USDCHF": (0.70, 1.30),
    "USDJPY": (80.0, 200.0),
    "EURJPY": (100.0, 200.0),
    "GBPJPY": (120.0, 230.0),
    "AUDJPY": (55.0, 130.0),
    "CADJPY": (70.0, 130.0),
    "CHFJPY": (100.0, 180.0),
    "EURGBP": (0.60, 1.00),
    "EURAUD": (1.30, 2.00),
    "EURCAD": (1.20, 1.80),
    "GBPAUD": (1.50, 2.30),
    "GBPCAD": (1.50, 2.20),
    "GBPCHF": (1.00, 1.60),
    "AUDCAD": (0.80, 1.20),
    "AUDNZD": (0.90, 1.30),
    "EURNZD": (1.40, 1.90),
    "GBPNZD": (1.80, 2.40),
    "BTCUSD": (10000.0, 500000.0),
    "ETHUSD": (500.0, 30000.0),
    "LTCUSD": (30.0, 2000.0),
    "XRPUSD": (0.10, 20.0),
    "US30":   (20000.0, 60000.0),
    "US500":  (2000.0, 8000.0),
    "NAS100": (10000.0, 30000.0),
    "GER40":  (10000.0, 30000.0),
    "UK100":  (6000.0, 12000.0),
    "JP225":  (20000.0, 60000.0),
    "USOIL":  (20.0, 200.0),
    "UKOIL":  (20.0, 200.0),
}

def _preco_valido(symbol: str, price: float) -> bool:
    if price <= 0:
        return False
    if symbol not in _PRICE_RANGES:
        return True
    mn, mx = _PRICE_RANGES[symbol]
    return mn <= price <= mx

SL_OBRIGATORIO_TODOS = True
_SL_OBRIGATORIO = {"XAUUSD", "BTCUSD", "ETHUSD", "NAS100", "US30"}

# =============================================================================
# PARSER DE SINAIS
# =============================================================================
SYMBOL_MAP = {
    "gold": "XAUUSD", "xauusd": "XAUUSD",
    "goldm": "XAUUSD", "goldm#": "XAUUSD",
    "xauusd.": "XAUUSD", "gold.": "XAUUSD",
    "silver": "XAGUSD", "xagusd": "XAGUSD",
    "eurusd": "EURUSD", "gbpusd": "GBPUSD",
    "usdjpy": "USDJPY", "usdchf": "USDCHF",
    "audusd": "AUDUSD", "nzdusd": "NZDUSD",
    "usdcad": "USDCAD",
    "eurjpy": "EURJPY", "gbpjpy": "GBPJPY",
    "eurgbp": "EURGBP", "euraud": "EURAUD",
    "eurcad": "EURCAD",
    "gbpaud": "GBPAUD", "gbpcad": "GBPCAD",
    "gbpchf": "GBPCHF", "audcad": "AUDCAD",
    "audjpy": "AUDJPY", "cadjpy": "CADJPY",
    "chfjpy": "CHFJPY", "audnzd": "AUDNZD",
    "eurnzd": "EURNZD", "gbpnzd": "GBPNZD",
    "nas100": "NAS100", "nasdaq": "NAS100",
    "us30": "US30", "dow": "US30",
    "us500": "US500", "sp500": "US500",
    "uk100": "UK100", "ftse": "UK100",
    "ger40": "GER40", "dax": "GER40",
    "jp225": "JP225", "nikkei": "JP225",
    "btcusd": "BTCUSD", "bitcoin": "BTCUSD",
    "ethusd": "ETHUSD", "ethereum": "ETHUSD",
    "ltcusd": "LTCUSD", "litecoin": "LTCUSD",
    "xrpusd": "XRPUSD", "ripple": "XRPUSD",
    "usoil": "USOIL", "wti": "USOIL",
    "ukoil": "UKOIL", "brent": "UKOIL",
}

def extrair_numeros(line: str, min_val: float = 0.0001) -> list:
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", line) if float(n) > min_val]

def pip_size(symbol: str) -> float:
    mapping = {
        "XAUUSD": 1.0,   "XAGUSD": 0.1,
        "EURUSD": 0.0001,"GBPUSD": 0.0001,"AUDUSD": 0.0001,
        "NZDUSD": 0.0001,"USDCAD": 0.0001,"USDCHF": 0.0001,
        "USDJPY": 0.01,  "EURJPY": 0.01,  "GBPJPY": 0.01,
        "AUDJPY": 0.01,  "CADJPY": 0.01,  "CHFJPY": 0.01,
        "EURGBP": 0.0001,"EURAUD": 0.0001,"EURCAD": 0.0001,
        "GBPAUD": 0.0001,"GBPCAD": 0.0001,"GBPCHF": 0.0001,
        "AUDCAD": 0.0001,"AUDNZD": 0.0001,"EURNZD": 0.0001,"GBPNZD": 0.0001,
        "BTCUSD": 1.0,   "ETHUSD": 0.1,   "LTCUSD": 0.1,"XRPUSD": 0.0001,
        "US30":   1.0,   "US500":  0.1,   "NAS100": 1.0,"GER40":  1.0,"UK100": 1.0,
        "USOIL":  0.01,  "UKOIL":  0.01,
    }
    return mapping.get(symbol, 0.0001)

def convert_pips_to_prices(entry: float, pip_targets: list, trade_type: str, symbol: str) -> list:
    size = pip_size(symbol)
    result = []
    for p in pip_targets:
        price = (entry + p * size) if trade_type == "BUY" else (entry - p * size)
        result.append(round(price, 2))
    return result

def parse_signal(text: str) -> Optional[dict]:
    text_clean = text.strip().replace("\\n", "\n")
    text_clean = re.sub(r"\s*[|;]\s*", "\n", text_clean)
    lines = [l.strip() for l in text_clean.split("\n") if l.strip()]
    if not lines:
        return None

    if _e_recap(text_clean):
        log.info("Rejeitado: mensagem identificada como recap/resultado")
        return None

    symbol = None
    for search in [l.upper() for l in lines]:
        for key, val in SYMBOL_MAP.items():
            if key.upper() in search:
                symbol = val
                break
        if symbol:
            break
    if not symbol:
        return None

    full_text_up = text_clean.upper()
    trade_type = None
    if re.search(r"\bBUY\b|\bCOMPRA\b|\bLONG\b", full_text_up):
        trade_type = "BUY"
    elif re.search(r"\bSELL\b|\bVENDA\b|\bSHORT\b", full_text_up):
        trade_type = "SELL"
    if not trade_type:
        return None

    entry = None
    entry_min = None
    entry_max = None

    m = re.search(r"between\s+(\d+(?:\.\d+)?)\s+(?:till|to|and|-)\s+(\d+(?:\.\d+)?)", full_text_up)
    if m:
        v1, v2 = float(m.group(1)), float(m.group(2))
        entry_min, entry_max = min(v1, v2), max(v1, v2)
        entry = entry_min if trade_type == "BUY" else entry_max

    if not entry:
        m = re.search(r"@\s*(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)", full_text_up)
        if m:
            v1, v2 = float(m.group(1)), float(m.group(2))
            entry_min, entry_max = min(v1, v2), max(v1, v2)
            entry = entry_min if trade_type == "BUY" else entry_max

    if not entry:
        for line in lines:
            h = re.sub(r"[^\w\s/\.\-]", " ", line.upper())
            m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", h)
            if m:
                v1, v2 = float(m.group(1)), float(m.group(2))
                if v1 > 100 and v2 > 100:
                    entry_min, entry_max = min(v1, v2), max(v1, v2)
                    entry = entry_max
                    break

    if not entry:
        m = re.search(r"@\s*(\d+(?:\.\d+)?)", full_text_up)
        if m:
            entry = float(m.group(1))

    if not entry:
        m = re.search(
            r"(?:TRADING\s+ON|PRICE\s+IS(?:\s+AT)?|PIVOT\s+LEVEL\s+"
            r"|TESTS?\s+(?:AN?\s+)?(?:IMPORTANT\s+)?(?:PSYCHOLOGICAL\s+)?LEVEL"
            r"|INSTRUMENT\s+TESTS?)\s*(\d+(?:\.\d+)?)",
            full_text_up
        )
        if m:
            entry = float(m.group(1))

    if not entry:
        for idx, line in enumerate(lines):
            up_line = line.upper()
            if re.search(
                r"TESTS?\s+(?:AN?\s+)?(?:IMPORTANT\s+)?(?:PSYCHOLOGICAL\s+)?LEVEL"
                r"|INSTRUMENT\s+TESTS?|TRADING\s+ON|PIVOT\s+LEVEL",
                up_line
            ):
                for next_line in lines[idx:idx+3]:
                    nums = re.findall(r"\d+(?:\.\d+)?", next_line)
                    for n in nums:
                        candidate = float(n)
                        if _preco_valido(symbol, candidate):
                            entry = candidate
                            break
                    if entry:
                        break
            if entry:
                break

    if not entry:
        m = re.search(r"\bENTRY\s*[:\-]\s*(\d+(?:\.\d+)?)", full_text_up)
        if m:
            entry = float(m.group(1))

    if not entry:
        for line in lines:
            h = re.sub(r"[^\w\s/\.\-]", " ", line.upper())
            if re.search(r"\bBUY\b|\bSELL\b", h) or any(k.upper() in h for k in SYMBOL_MAP):
                m = re.search(r"(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)", line)
                if m:
                    v1, v2 = float(m.group(1)), float(m.group(2))
                    if v1 > 100 and v2 > 100:
                        entry_min, entry_max = min(v1, v2), max(v1, v2)
                        entry = entry_min if trade_type == "BUY" else entry_max
                        break

    if not entry:
        for line in lines:
            h = re.sub(r"[^\w\s/\.\-]", " ", line.upper())
            if re.search(r"\bBUY\b|\bSELL\b", h) or any(k.upper() in h for k in SYMBOL_MAP):
                nums = extrair_numeros(line, min_val=0.0001)
                if nums:
                    entry = nums[-1]
                    break

    if not entry:
        return None

    if not _preco_valido(symbol, entry):
        log.info(f"Rejeitado: entry={entry} fora da faixa esperada para {symbol}")
        return None

    sl = None
    tps_absolute = []
    tps_pips = []

    i = 0
    while i < len(lines):
        line = lines[i]
        up   = line.upper()

        if re.search(
            r"\bSTOP\s*LOSS\b|\bSL\b|\bSI\b"
            r"|\bRECOMMENDED\s+STOP\s+LOSS\b|\bMY\s+STOP\s+LOSS\b|\bSTOP\s*[:\-]",
            up
        ):
            nums = [float(n) for n in re.findall(r"\d+\.\d+", line)]
            if not nums:
                nums = [float(n) for n in re.findall(r"\d+", line) if float(n) > 10]
            if nums:
                sl = nums[-1]
            elif i + 1 < len(lines):
                nx = extrair_numeros(lines[i + 1], min_val=1.0)
                if nx:
                    sl = nx[-1]

        elif re.search(
            r"\bTP\d*\b|\d+TP\b|\bTARGET\b|\bALVO\b|\bTAKE\s*PROFIT\b|\bTARGET\s*[:\-]",
            up
        ):
            is_pips = bool(re.search(r"\dpips?", up, re.IGNORECASE))
            if is_pips:
                all_nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", line) if float(n) > 1]
                tps_pips.extend(all_nums)
            else:
                nums_decimal = [float(n) for n in re.findall(r"\d+\.\d+", line) if float(n) > 0.001]
                nums_int = [float(n) for n in re.findall(r"\b(\d{3,})\b", line)]
                if nums_decimal:
                    tps_absolute.extend(nums_decimal)
                elif nums_int:
                    tps_absolute.extend(nums_int)

        i += 1

    tp_standalone = re.findall(r"\bTP\s*[:\-]?\s*(\d+(?:\.\d+)?)", full_text_up)
    for v in tp_standalone:
        fv = float(v)
        if fv > 100 and fv not in tps_absolute:
            tps_absolute.append(fv)

    if not tps_absolute and not tps_pips:
        tp_matches = re.findall(r"(?:TP\s*\d*|TARGET\s*\d*)[\s.:]*?(\d+(?:\.\d+)?)", full_text_up)
        for v in tp_matches:
            fv = float(v)
            if "." in v and fv > 0.001:
                tps_absolute.append(fv)
            elif fv >= 100:
                tps_absolute.append(fv)

    if not tps_absolute and tps_pips:
        tps_absolute = convert_pips_to_prices(entry, tps_pips, trade_type, symbol)
        log.info(f"TPs convertidos de pips: {tps_pips} → preços: {tps_absolute}")

    if not tps_absolute:
        log.info("Sinal rejeitado — nenhum TP válido encontrado")
        return None

    tps_validos = [tp for tp in tps_absolute if _preco_valido(symbol, tp) and tp != entry]
    if not tps_validos:
        log.info(f"Rejeitado: todos os TPs inválidos para {symbol}")
        return None

    if trade_type == "BUY":
        tps_validos = [tp for tp in tps_validos if tp > entry]
        if sl and sl >= entry:
            log.warning(f"SL {sl} >= entry {entry} em BUY — SL removido")
            sl = None
    else:
        tps_validos = [tp for tp in tps_validos if tp < entry]
        if sl and sl <= entry:
            log.warning(f"SL {sl} <= entry {entry} em SELL — SL removido")
            sl = None

    if not tps_validos:
        log.info(f"Rejeitado: nenhum TP no lado correto da entry para {trade_type} {symbol} @ {entry}")
        return None

    if SL_OBRIGATORIO_TODOS or symbol in _SL_OBRIGATORIO:
        if not sl:
            log.warning(f"Rejeitado: SL ausente para {symbol}")
            return None

    parsed = {
        "id":     str(uuid.uuid4()),
        "symbol": symbol,
        "type":   trade_type,
        "entry":  entry,
        "sl":     sl or 0.0,
        "tps":    tps_validos[:4],
        "source": "Telegram",
        "raw":    text_clean[:300],
        "time":   datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

    if entry_min is not None:
        parsed["entry_min"] = entry_min
        parsed["entry_max"] = entry_max

    log.info(
        f"PARSE OK | {parsed['symbol']} {parsed['type']} "
        f"entry={parsed['entry']} sl={parsed['sl']} tps={parsed['tps']}"
    )
    return parsed

# =============================================================================
# LISTENER DO TELETHON
# =============================================================================
def registrar_listener():
    @client.on(events.NewMessage(chats=SIGNAL_GROUPS if SIGNAL_GROUPS else None, incoming=True, outgoing=True))
    async def handler(event):
        if e_final_de_semana():
            log.debug("Final de semana — mensagem ignorada")
            return

        chat  = await event.get_chat()
        texto = event.raw_text or ""
        nome  = getattr(chat, "title", str(event.chat_id))
        log.info(f"Mensagem recebida | Grupo: {nome} ({event.chat_id}) | Texto: {texto[:80]}")

        if not event.is_group and not event.is_channel:
            return

        sinal = parse_signal(texto)
        if not sinal:
            log.info("Mensagem não reconhecida como sinal — ignorada")
            return

        sinal["source"] = nome
        signal_queue.append(sinal)

        log.info(
            f"Sinal enfileirado: {sinal['id']} | "
            f"{sinal['type']} {sinal['symbol']} @ {sinal['entry']} | "
            f"{len(sinal['tps'])} TPs | SL: {sinal['sl']}"
        )

        # ── Notificação Telegram: sinal recebido na fila ──────────────────────
        emoji = "🟢" if sinal["type"] == "BUY" else "🔴"
        msg = (
            f"{emoji} <b>{sinal['type']}  •  {sinal['symbol']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📥 <b>Sinal recebido na fila</b>\n"
            f"💲 Entry: <code>{sinal['entry']}</code>\n"
            f"🛡 Stop Loss: <code>{sinal['sl']}</code>\n"
            f"🎯 TPs: <code>{_fmt_tps(sinal['tps'])}</code>\n"
            f"📡 Grupo: {nome}\n"
            f"⏱ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await enviar_telegram(msg)

# =============================================================================
# STARTUP / SHUTDOWN
# =============================================================================
@app.on_event("startup")
async def startup():
    try:
        if not client.is_connected():
            await client.start(
                phone=PHONE,
                password=os.environ.get("TELEGRAM_PASSWORD")
            )
            session_str = client.session.save()
            log.info(f"Conectado ao Telegram | SESSION_STRING={session_str}")

        registrar_listener()
        log.info(f"Listener ativo | Grupos monitorados: {SIGNAL_GROUPS or 'TODOS'}")

        await enviar_telegram(
            "🤖 <b>TS Signal Bridge v2.7.0</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ API iniciada com sucesso\n"
            f"📡 Grupos monitorados: {SIGNAL_GROUPS or 'TODOS'}\n"
            f"⏱ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
    except Exception as e:
        log.error(f"Erro no startup: {e}")

@app.on_event("shutdown")
async def shutdown():
    if client.is_connected():
        await client.disconnect()
        log.info("Telegram desconectado")
    await enviar_telegram(
        "🔴 <b>TS Signal Bridge</b>\n"
        "⏹ API encerrada\n"
        f"⏱ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )

# =============================================================================
# ENDPOINTS — MT5
# =============================================================================
def check_token(authorization: str):
    if authorization.replace("Bearer ", "").strip() != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Token inválido")

@app.get("/health")
async def health():
    return {
        "status":        "online",
        "telegram":      client.is_connected(),
        "sinais_fila":   len(signal_queue),
        "sinais_total":  len(signal_history),
        "grupos":        SIGNAL_GROUPS,
        "time":          datetime.now(timezone.utc).isoformat(),
    }

@app.get("/signal/pending")
async def get_pending(authorization: str = Header(""), symbol: str = ""):
    check_token(authorization)

    familias = {
        "XAUUSD": {"XAUUSD", "XAGUSD"},
        "FOREX":  {"EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","NZDUSD","USDCAD",
                   "EURJPY","GBPJPY","EURGBP","EURAUD","EURCAD","GBPAUD","GBPCAD",
                   "GBPCHF","AUDCAD","AUDJPY","CADJPY","CHFJPY","AUDNZD","EURNZD","GBPNZD"},
        "INDEX":  {"US30","US500","NAS100","GER40","UK100","JP225"},
        "CRYPTO": {"BTCUSD","ETHUSD","LTCUSD","XRPUSD"},
        "OIL":    {"USOIL","UKOIL"},
    }

    if not symbol:
        if not signal_queue:
            from fastapi.responses import Response
            return Response(status_code=204)
        return JSONResponse(status_code=200, content=signal_queue[0])

    sym_upper = symbol.upper()
    aceitos   = familias.get(sym_upper, {sym_upper})

    sinal = next((s for s in signal_queue if s.get("symbol", "") in aceitos), None)
    if not sinal:
        from fastapi.responses import Response
        return Response(status_code=204)
    return JSONResponse(status_code=200, content=sinal)

@app.post("/signal/confirm")
async def confirm_signal(body: ConfirmRequest, authorization: str = Header("")):
    check_token(authorization)

    sinal = next((s for s in signal_queue if s["id"] == body.id), None)
    if not sinal:
        sinal_hist = next((s for s in signal_history if s["id"] == body.id), None)
        if sinal_hist:
            return {"ok": True, "id": body.id, "status": "already_confirmed"}
        sinal = {"id": body.id, "symbol": "?", "type": "?", "entry": 0, "tps": [], "sl": 0, "source": "MT5"}

    if sinal in signal_queue:
        signal_queue.remove(sinal)

    sinal.update({
        "status":   body.status,
        "mt5_msg":  body.message,
        "account":  body.account,
        "executed": datetime.now(timezone.utc).isoformat(),
    })
    signal_history.append(sinal)

    log.info(f"Confirmação MT5: {body.id} | {body.status} | {body.message}")

    # ── Notificação Telegram: confirmação do MT5 ──────────────────────────────
    if body.status == "executed":
        emoji = "🟢" if sinal.get("type") == "BUY" else "🔴"
        msg = (
            f"{emoji} <b>{sinal.get('type')}  •  {sinal.get('symbol')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>Ordem aberta no MT5</b>\n"
            f"💲 Entry: <code>{sinal.get('entry')}</code>\n"
            f"🛡 Stop Loss: <code>{sinal.get('sl')}</code>\n"
            f"🎯 TPs: <code>{_fmt_tps(sinal.get('tps', []))}</code>\n"
            f"🏦 Conta: <code>{body.account}</code>\n"
            f"📋 {body.message}\n"
            f"⏱ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await enviar_telegram(msg)
    elif body.status == "failed":
        msg = (
            f"⚠️ <b>Falha ao abrir ordem — {sinal.get('symbol')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ Status: <code>failed</code>\n"
            f"📋 Motivo: {body.message}\n"
            f"🏦 Conta: <code>{body.account}</code>\n"
            f"⏱ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
        await enviar_telegram(msg)

    return {"ok": True, "id": body.id, "status": body.status}

@app.get("/signals/queue")
async def get_queue(authorization: str = Header("")):
    check_token(authorization)
    return {"queue": signal_queue, "count": len(signal_queue)}

@app.get("/signals/history")
async def get_history(authorization: str = Header("")):
    check_token(authorization)
    return {"signals": signal_history[-50:], "total": len(signal_history)}

@app.delete("/signals/queue")
async def clear_queue(authorization: str = Header("")):
    check_token(authorization)
    signal_queue.clear()
    return {"ok": True}

@app.post("/signal/test")
async def test_signal(request_body: dict, authorization: str = Header("")):
    check_token(authorization)
    text = request_body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="Campo 'text' obrigatório")
    sinal = parse_signal(text)
    if not sinal:
        raise HTTPException(status_code=422, detail="Texto não reconhecido como sinal")
    sinal["source"] = "Teste Manual"
    signal_queue.append(sinal)
    return {"ok": True, "signal": sinal}

@app.get("/groups")
async def list_groups(authorization: str = Header("")):
    check_token(authorization)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram não conectado")
    dialogs = await client.get_dialogs()
    groups = [
        {"id": d.id, "name": d.name, "type": str(type(d.entity).__name__)}
        for d in dialogs if d.is_group or d.is_channel
    ]
    return {"groups": groups, "total": len(groups)}

@app.get("/messages/{group_id}")
async def get_messages(group_id: int, limit: int = 20, authorization: str = Header("")):
    check_token(authorization)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram não conectado")
    msgs = await client.get_messages(group_id, limit=limit)
    return {"messages": [{"id": m.id, "text": m.text, "date": str(m.date)} for m in msgs]}
