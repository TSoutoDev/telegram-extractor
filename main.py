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
# Ex: TELEGRAM_SIGNAL_GROUPS=-1001234567890,-1009876543210
SIGNAL_GROUPS = [
    int(x.strip())
    for x in os.environ.get("TELEGRAM_SIGNAL_GROUPS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]

# ── fila de sinais (em memória) ───────────────────────────────────────────────
signal_queue:   list[dict] = []
signal_history: list[dict] = []

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="TS Signal Bridge", version="2.0.0")

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
    "gold": "XAUUSD", "xauusd": "XAUUSD",
    "eurusd": "EURUSD", "gbpusd": "GBPUSD",
    "usdjpy": "USDJPY", "btcusd": "BTCUSD",
    "xagusd": "XAGUSD", "silver": "XAGUSD",
    "nas100": "NAS100", "nasdaq": "NAS100",
    "us30": "US30", "dow": "US30",
}

def parse_signal(text: str) -> Optional[dict]:
    # Normalizar \n literal para quebra de linha real
    text_clean = text.strip().replace('\\n', '\n')
    text_clean = re.sub(r'\s*[|;]\s*', '\n', text_clean)

    lines = [l.strip() for l in text_clean.split("\n") if l.strip()]
    if not lines:
        return None

    # Limpar emojis e caracteres especiais do header
    header = re.sub(r'[^\w\s/\.\-]', ' ', lines[0].upper())
    header = re.sub(r'\s+', ' ', header).strip()

    # Detectar símbolo em todas as linhas
    symbol = None
    for search in [header] + [l.upper() for l in lines[1:]]:
        for key, val in SYMBOL_MAP.items():
            if key.upper() in search:
                symbol = val
                break
        if symbol:
            break
    if not symbol:
        return None

    # Detectar tipo BUY/SELL no texto completo
    full_text_up = text_clean.upper()
    trade_type = None
    if re.search(r'\bBUY\b|\bCOMPRA\b|\bLONG\b', full_text_up):
        trade_type = "BUY"
    elif re.search(r'\bSELL\b|\bVENDA\b|\bSHORT\b', full_text_up):
        trade_type = "SELL"
    if not trade_type:
        return None

    # Detectar entry
    entry = None
    m = re.search(r'(\d{3,6}(?:\.\d+)?)\s*/\s*(\d{3,6}(?:\.\d+)?)', header)
    if m:
        entry = float(m.group(2))
    else:
        m = re.search(r'@\s*(\d{3,6}(?:\.\d+)?)', header)
        if m:
            entry = float(m.group(1))
        else:
            nums = re.findall(r'\d{3,6}(?:\.\d+)?', header)
            if nums:
                entry = float(nums[-1])
    if not entry:
        return None

    # TPs e SL em todas as linhas
    tps, sl = [], None
    for line in lines:
        up = line.upper().replace('.', ' ').replace(':', ' ')
        if re.search(r'\bSL\b|\bSTOP\b', up):
            nums = re.findall(r'\d{3,6}(?:\.\d+)?', line)
            if nums:
                sl = float(nums[-1])
        elif re.search(r'\bTP\b|\bTARGET\b|\bALVO\b', up):
            nums = re.findall(r'\d{3,6}(?:\.\d+)?', line)
            if nums:
                tps.append(float(nums[-1]))

    # Fallback — extrair TPs do texto completo
    if not tps:
        tp_matches = re.findall(r'TP\s*\d*[\s.:]*?(\d{3,6}(?:\.\d+)?)', full_text_up)
        tps = [float(v) for v in tp_matches]

    if not tps:
        return None

    return {
        "id":     str(uuid.uuid4()),
        "symbol": symbol,
        "type":   trade_type,
        "entry":  entry,
        "sl":     sl or 0.0,
        "tps":    tps,
        "source": "Telegram",
        "raw":    text_clean[:300],
        "time":   datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }

# ─────────────────────────────────────────────────────────────────────────────
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
    """
    Registra o handler de novas mensagens.
    Se SIGNAL_GROUPS estiver vazio, monitora TODOS os grupos.
    """
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
            return

        sinal["source"] = nome
        signal_queue.append(sinal)
        log.info(f"✅ Sinal enfileirado: {sinal['id']} | {sinal['type']} {sinal['symbol']} @ {sinal['entry']} | {len(sinal['tps'])} TPs")

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
        # Sinal já foi processado — buscar no histórico
        sinal_hist = next((s for s in signal_history if s["id"] == body.id), None)
        if sinal_hist:
            return {"ok": True, "id": body.id, "status": "already_confirmed"}
        # Criar entrada mínima para não quebrar o WhatsApp
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
    # Disparar WhatsApp igual ao fluxo real
    await enviar_whatsapp(fmt_sinal(sinal))
    return {"ok": True, "signal": sinal}

# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS — seu código original preservado
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
