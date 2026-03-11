from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch
from pydantic import BaseModel
from typing import Optional
import os, re, uuid, logging, httpx
from datetime import datetime, timezone

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── variáveis de ambiente ─────────────────────────────────────────────────────
API_ID    = os.environ["API_ID"]
API_HASH  = os.environ["API_HASH"]
PHONE     = os.environ["PHONE"]
SECRET_KEY= os.environ.get("API_KEY", "chave-secreta")

# ── Telethon com StringSession ────────────────────────────────────────────────
session_string = os.environ.get("SESSION_STRING", "")
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# ── configurações de sinais ───────────────────────────────────────────────────
EVOLUTION_URL      = os.environ.get("EVOLUTION_URL", "")
EVOLUTION_TOKEN    = os.environ.get("EVOLUTION_TOKEN", "")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "")
WHATSAPP_NUMBER    = os.environ.get("WHATSAPP_NUMBER", "")

# IDs dos grupos monitorados (negativos, separados por vírgula)
SIGNAL_GROUPS = [
    int(x.strip())
    for x in os.environ.get("TELEGRAM_SIGNAL_GROUPS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]

# ── fila de sinais (em memória) ───────────────────────────────────────────────
signal_queue:   list[dict] = []
signal_history: list[dict] = []

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="TS Signal Bridge", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── modelo para confirmação do MT5 ───────────────────────────────────────────
class ConfirmRequest(BaseModel):
    id: str
    status: str        # executed | failed | ignored
    message: str
    account: Optional[str] = ""

# ─────────────────────────────────────────────────────────────────────────────
# PARSER DE SINAIS
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL_MAP = {
    # Metais
    "gold": "XAUUSD",   "xauusd": "XAUUSD",
    "silver": "XAGUSD", "xagusd": "XAGUSD",
    # Forex majors
    "eurusd": "EURUSD", "gbpusd": "GBPUSD",
    "usdjpy": "USDJPY", "usdchf": "USDCHF",
    "audusd": "AUDUSD", "nzdusd": "NZDUSD",
    "usdcad": "USDCAD",
    # Forex cruzados
    "eurjpy": "EURJPY", "gbpjpy": "GBPJPY",
    "eurgbp": "EURGBP", "euraud": "EURAUD",
    "eurcad": "EURCAD",
    "gbpaud": "GBPAUD", "gbpcad": "GBPCAD",
    "gbpchf": "GBPCHF", "audcad": "AUDCAD",
    "audjpy": "AUDJPY", "cadjpy": "CADJPY",
    "chfjpy": "CHFJPY", "audnzd": "AUDNZD",
    "eurnzd": "EURNZD", "gbpnzd": "GBPNZD",
    # Índices
    "nas100": "NAS100", "nasdaq": "NAS100",
    "us30": "US30",     "dow": "US30",
    "us500": "US500",   "sp500": "US500",
    "uk100": "UK100",   "ftse": "UK100",
    "ger40": "GER40",   "dax": "GER40",
    "jp225": "JP225",   "nikkei": "JP225",
    # Cripto
    "btcusd": "BTCUSD", "bitcoin": "BTCUSD",
    "ethusd": "ETHUSD", "ethereum": "ETHUSD",
    "ltcusd": "LTCUSD", "litecoin": "LTCUSD",
    "xrpusd": "XRPUSD", "ripple": "XRPUSD",
    # Energia
    "usoil": "USOIL",   "wti": "USOIL",
    "ukoil": "UKOIL",   "brent": "UKOIL",
}

def extrair_numeros(line: str, min_val: float = 0.0001) -> list:
    return [float(n) for n in re.findall(r'\d+(?:\.\d+)?', line) if float(n) > min_val]

def pip_size(symbol: str) -> float:
    """1 pip em preço real por símbolo.
    Para XAUUSD, o grupo Gold Signals.io usa '50 pips' = $50 de movimento,
    portanto pip_size=1.0 (50 pips × 1.0 = $50 de distância).
    """
    mapping = {
        "XAUUSD": 1.0,   "XAGUSD": 0.1,
        "EURUSD": 0.0001, "GBPUSD": 0.0001, "AUDUSD": 0.0001,
        "NZDUSD": 0.0001, "USDCAD": 0.0001, "USDCHF": 0.0001,
        "USDJPY": 0.01,   "EURJPY": 0.01,   "GBPJPY": 0.01,
        "AUDJPY": 0.01,   "CADJPY": 0.01,   "CHFJPY": 0.01,
        "EURGBP": 0.0001, "EURAUD": 0.0001, "EURCAD": 0.0001,
        "GBPAUD": 0.0001, "GBPCAD": 0.0001, "GBPCHF": 0.0001,
        "AUDCAD": 0.0001, "AUDNZD": 0.0001, "EURNZD": 0.0001, "GBPNZD": 0.0001,
        "BTCUSD": 1.0,    "ETHUSD": 0.1,    "LTCUSD": 0.1,    "XRPUSD": 0.0001,
        "US30": 1.0, "US500": 0.1, "NAS100": 1.0, "GER40": 1.0, "UK100": 1.0,
        "USOIL": 0.01, "UKOIL": 0.01,
    }
    return mapping.get(symbol, 0.0001)

def convert_pips_to_prices(entry: float, pip_targets: list, trade_type: str, symbol: str) -> list:
    """Converte lista de pips relativos em preços absolutos."""
    size = pip_size(symbol)
    result = []
    for p in pip_targets:
        price = (entry + p * size) if trade_type == "BUY" else (entry - p * size)
        result.append(round(price, 2))
    return result

def parse_signal(text: str) -> Optional[dict]:
    # Normalizar \n literal para quebra de linha real
    text_clean = text.strip().replace('\\n', '\n')
    text_clean = re.sub(r'\s*[|;]\s*', '\n', text_clean)

    lines = [l.strip() for l in text_clean.split("\n") if l.strip()]
    if not lines:
        return None

    # ── Detectar símbolo ──────────────────────────────────────────────────────
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

    # ── Detectar tipo BUY/SELL ────────────────────────────────────────────────
    full_text_up = text_clean.upper()
    trade_type = None
    if re.search(r'\bBUY\b|\bCOMPRA\b|\bLONG\b', full_text_up):
        trade_type = "BUY"
    elif re.search(r'\bSELL\b|\bVENDA\b|\bSHORT\b', full_text_up):
        trade_type = "SELL"
    if not trade_type:
        return None

    # ── Detectar entry ────────────────────────────────────────────────────────
    entry = None

    # "between X till/to Y" — Forex GDP
    m = re.search(r'between\s+(\d+(?:\.\d+)?)\s+(?:till|to|and|-)\s+(\d+(?:\.\d+)?)', full_text_up)
    if m:
        entry = float(m.group(2))

    # "@ X - Y" ou "@ X / Y" — Gold Signals.io ("Buy Now @ 5097 - 5093")
    if not entry:
        m = re.search(r'@\s*(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', full_text_up)
        if m:
            v1, v2 = float(m.group(1)), float(m.group(2))
            entry = min(v1, v2) if trade_type == "BUY" else max(v1, v2)

    # "X/Y" — Gold Pro Trader ("#XAUUSD Buy 5180/5175")
    if not entry:
        for line in lines:
            h = re.sub(r'[^\w\s/\.\-]', ' ', line.upper())
            m = re.search(r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)', h)
            if m:
                v1, v2 = float(m.group(1)), float(m.group(2))
                if v1 > 100 and v2 > 100:
                    entry = v2
                    break

    # "@ X" simples
    if not entry:
        m = re.search(r'@\s*(\d+(?:\.\d+)?)', full_text_up)
        if m:
            entry = float(m.group(1))

    # "X-Y" ou "X/Y" na mesma linha que BUY/SELL/símbolo
    # Ex: "Buy Gold 5261.8-5251.8" | "XAUUSD Sell 5191/5194"
    if not entry:
        for line in lines:
            h = re.sub(r'[^\w\s/\.\-]', ' ', line.upper())
            if re.search(r'\bBUY\b|\bSELL\b', h) or any(k.upper() in h for k in SYMBOL_MAP):
                m = re.search(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', line)
                if m:
                    v1, v2 = float(m.group(1)), float(m.group(2))
                    if v1 > 100 and v2 > 100:
                        entry = min(v1,v2) if trade_type == "BUY" else max(v1,v2)
                        break

    # Fallback: último número da linha com BUY/SELL/símbolo
    if not entry:
        for line in lines:
            h = re.sub(r'[^\w\s/\.\-]', ' ', line.upper())
            if re.search(r'\bBUY\b|\bSELL\b', h) or any(k.upper() in h for k in SYMBOL_MAP):
                nums = extrair_numeros(line, min_val=0.0001)
                if nums:
                    entry = nums[-1]
                    break

    if not entry:
        return None

    # ── TPs e SL ──────────────────────────────────────────────────────────────
    sl = None
    tps_absolute = []
    tps_pips = []

    i = 0
    while i < len(lines):
        line = lines[i]
        up = line.upper()

        if re.search(r'\bSTOP\s*LOSS\b|\bSL\b|\bSI\b', up):
            # SL pode estar na mesma linha ou na próxima (Gold Signals.io: "Sl\n5178")
            nums = [float(n) for n in re.findall(r'\d+\.\d+', line)]
            if not nums:
                nums = [float(n) for n in re.findall(r'\d+', line) if float(n) > 10]
            if nums:
                sl = nums[-1]
            elif i + 1 < len(lines):
                nx = extrair_numeros(lines[i + 1], min_val=1.0)
                if nx:
                    sl = nx[-1]

        elif re.search(r'\bTP\d*\b|\d+TP\b|\bTARGET\b|\bALVO\b', up):
            # Detectar pips: "50/100Pips", "50pips", "50 pips"
            # Nota: \b não funciona entre dígito e letra, então usamos \d pips?
            is_pips = bool(re.search(r'\dpips?', up, re.IGNORECASE))
            if is_pips:
                # Pegar TODOS os números da linha (50 e 100 de "50/100Pips")
                all_nums = [float(n) for n in re.findall(r'\d+(?:\.\d+)?', line) if float(n) > 1]
                tps_pips.extend(all_nums)
            else:
                # Preços absolutos: preferir decimais, ou inteiros com 3+ dígitos
                nums_decimal = [float(n) for n in re.findall(r'\d+\.\d+', line) if float(n) > 0.001]
                nums_int     = [float(n) for n in re.findall(r'\b(\d{3,})\b', line)]
                if nums_decimal:
                    tps_absolute.extend(nums_decimal)
                elif nums_int:
                    tps_absolute.extend(nums_int)

        i += 1

    # Fallback global para TPs
    if not tps_absolute and not tps_pips:
        tp_matches = re.findall(r'(?:TP\s*\d*|TARGET\s*\d*)[\s.:]*?(\d+(?:\.\d+)?)', full_text_up)
        for v in tp_matches:
            fv = float(v)
            if '.' in v and fv > 0.001:
                tps_absolute.append(fv)
            elif fv >= 100:
                tps_absolute.append(fv)

    # Converter pips → preços absolutos
    if not tps_absolute and tps_pips:
        tps_absolute = convert_pips_to_prices(entry, tps_pips, trade_type, symbol)
        log.info(f"TPs convertidos de pips: {tps_pips} → preços: {tps_absolute}")

    if not tps_absolute:
        log.info("Sinal rejeitado — nenhum TP válido encontrado")
        return None

    parsed = {
        "id":     str(uuid.uuid4()),
        "symbol": symbol,
        "type":   trade_type,
        "entry":  entry,
        "sl":     sl or 0.0,
        "tps":    tps_absolute[:4],
        "source": "Telegram",
        "raw":    text_clean[:300],
        "time":   datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    log.info(f"PARSE OK | {parsed['symbol']} {parsed['type']} entry={parsed['entry']} sl={parsed['sl']} tps={parsed['tps']}")
    return parsed


# WHATSAPP — Evolution API
# ─────────────────────────────────────────────────────────────────────────────
async def enviar_whatsapp(mensagem: str):
    if not all([EVOLUTION_URL, EVOLUTION_TOKEN, WHATSAPP_NUMBER, EVOLUTION_INSTANCE]):
        log.warning("WhatsApp não configurado — pulando")
        return
    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url,
                headers={"apikey": EVOLUTION_TOKEN, "Content-Type": "application/json"},
                json={"number": WHATSAPP_NUMBER, "text": mensagem, "delay": 0}
            )
            log.info(f"WhatsApp {'OK' if r.status_code==201 else 'ERRO '+str(r.status_code)}: {mensagem[:60]}")
    except Exception as e:
        log.error(f"WhatsApp exceção: {e}")

def fmt_sinal(s: dict) -> str:
    tps = "\n".join([f"  TP{i+1}: {t}" for i, t in enumerate(s['tps'])])
    return (f"🔔 *SINAL RECEBIDO*\n"
            f"{'🟢 COMPRA' if s['type']=='BUY' else '🔴 VENDA'} {s['symbol']}\n"
            f"Entry: {s['entry']}\n{tps}\nSL: {s['sl']}\n"
            f"Fonte: {s['source']}\n⏰ {datetime.now().strftime('%H:%M:%S')}")

def fmt_exec(s: dict, status: str, msg: str) -> str:
    icon = "✅" if status == "executed" else "❌"
    return (f"{icon} *ORDEM {status.upper()}*\n"
            f"{s['type']} {s['symbol']} @ {s['entry']}\n{msg}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}")

# ─────────────────────────────────────────────────────────────────────────────
# LISTENER DO TELETHON — captura mensagens dos grupos
# ─────────────────────────────────────────────────────────────────────────────
def registrar_listener():
    @client.on(events.NewMessage(chats=SIGNAL_GROUPS if SIGNAL_GROUPS else None))
    async def handler(event):
        if not event.is_group and not event.is_channel:
            return

        chat  = await event.get_chat()
        texto = event.raw_text or ""
        nome  = getattr(chat, "title", str(event.chat_id))

        log.info(f"Mensagem recebida | Grupo: {nome} ({event.chat_id}) | Texto: {texto[:80]}")

        sinal = parse_signal(texto)
        if not sinal:
            log.info(f"Mensagem não reconhecida como sinal — ignorada")
            return

        sinal["source"] = nome
        signal_queue.append(sinal)
        log.info(f"✅ Sinal enfileirado: {sinal['id']} | {sinal['type']} {sinal['symbol']} @ {sinal['entry']} | {len(sinal['tps'])} TPs | SL: {sinal['sl']}")

        await enviar_whatsapp(fmt_sinal(sinal))

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP / SHUTDOWN
# ─────────────────────────────────────────────────────────────────────────────
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
    except Exception as e:
        log.error(f"Erro no startup: {e}")

@app.on_event("shutdown")
async def shutdown():
    if client.is_connected():
        await client.disconnect()
        log.info("Telegram desconectado")

# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS — MT5
# ─────────────────────────────────────────────────────────────────────────────
def check_token(authorization: str):
    if authorization.replace("Bearer ", "").strip() != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Token inválido")

@app.get("/health")
async def health():
    return {
        "status":       "online",
        "telegram":     client.is_connected(),
        "sinais_fila":  len(signal_queue),
        "sinais_total": len(signal_history),
        "grupos":       SIGNAL_GROUPS,
        "time":         datetime.now(timezone.utc).isoformat(),
    }

@app.get("/signal/pending")
async def get_pending(authorization: str = Header("")):
    """MT5 consulta sinal pendente a cada 5 segundos"""
    check_token(authorization)
    if not signal_queue:
        from fastapi.responses import Response
        return Response(status_code=204)
    return JSONResponse(status_code=200, content=signal_queue[0])

@app.post("/signal/confirm")
async def confirm_signal(body: ConfirmRequest, authorization: str = Header("")):
    """MT5 confirma execução da ordem"""
    check_token(authorization)

    sinal = next((s for s in signal_queue if s["id"] == body.id), None)
    if not sinal:
        sinal_hist = next((s for s in signal_history if s["id"] == body.id), None)
        if sinal_hist:
            return {"ok": True, "id": body.id, "status": "already_confirmed"}
        sinal = {"id": body.id, "symbol": "?", "type": "?", "entry": 0,
                 "tps": [], "sl": 0, "source": "MT5"}

    if sinal in signal_queue:
        signal_queue.remove(sinal)
    sinal.update({"status": body.status, "mt5_msg": body.message,
                  "account": body.account,
                  "executed": datetime.now(timezone.utc).isoformat()})
    signal_history.append(sinal)

    log.info(f"Confirmação MT5: {body.id} | {body.status} | {body.message}")
    await enviar_whatsapp(fmt_exec(sinal, body.status, body.message))
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
    """Injeta sinal manualmente para testar o MT5 e WhatsApp"""
    check_token(authorization)
    text = request_body.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="Campo 'text' obrigatório")
    sinal = parse_signal(text)
    if not sinal:
        raise HTTPException(status_code=422, detail="Texto não reconhecido como sinal")
    sinal["source"] = "Teste Manual"
    signal_queue.append(sinal)
    await enviar_whatsapp(fmt_sinal(sinal))
    return {"ok": True, "signal": sinal}

# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS — utilitários
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/groups")
async def list_groups(authorization: str = Header("")):
    check_token(authorization)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram não conectado")
    dialogs = await client.get_dialogs()
    groups  = [{"id": d.id, "name": d.name, "type": str(type(d.entity).__name__)}
               for d in dialogs if d.is_group or d.is_channel]
    return {"groups": groups, "total": len(groups)}

@app.get("/messages/{group_id}")
async def get_messages(group_id: int, limit: int = 20, authorization: str = Header("")):
    check_token(authorization)
    if not client.is_connected():
        raise HTTPException(status_code=503, detail="Telegram não conectado")
    msgs = await client.get_messages(group_id, limit=limit)
    return {"messages": [{"id": m.id, "text": m.text, "date": str(m.date)} for m in msgs]}
