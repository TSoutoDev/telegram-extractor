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
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from pathlib import Path
from fastapi.responses import FileResponse

# ── logging ───────────────────────────────────────────────────────────────────
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "signal_bridge.log")

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5 * 1024 * 1024,
    backupCount=2,
    encoding="utf-8"
)

file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])

log = logging.getLogger(__name__)

load_dotenv(override=True)

# ── variáveis de ambiente ─────────────────────────────────────────────────────
API_ID   = os.environ["API_ID"]
API_HASH = os.environ["API_HASH"]
PHONE    = os.environ["PHONE"]
SECRET_KEY = os.environ.get("API_KEY", "chave-secreta")
DATABASE_URL = os.environ["DATABASE_URL"]
SIGNAL_MAX_AGE_SECONDS = int(os.getenv("SIGNAL_MAX_AGE_SECONDS", "60"))

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def expirar_sinais_antigos():
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE signals
                SET status = 'expired'
                WHERE status = 'pending'
                  AND expires_at <= NOW()
            """)
        )

def salvar_sinal_db(sinal):
    with engine.begin() as conn:
        resultado = conn.execute(
            text("""
                INSERT INTO signals (
                    id,
                    symbol,
                    type,
                    entry,
                    entry_min,
                    entry_max,
                    sl,
                    tps,
                    source,
                    telegram_group_id,
                    telegram_message_id,
                    received_at,
                    expires_at,
                    status
                )
                VALUES (
                    :id,
                    :symbol,
                    :type,
                    :entry,
                    :entry_min,
                    :entry_max,
                    :sl,
                    CAST(:tps AS jsonb),
                    :source,
                    :telegram_group_id,
                    :telegram_message_id,
                    NOW(),
                    NOW() + (:max_age * INTERVAL '1 second'),
                    'pending'
                )
                ON CONFLICT (telegram_group_id, telegram_message_id)
                DO NOTHING
            """),
            {
                "id": sinal["id"],
                "symbol": sinal["symbol"],
                "type": sinal["type"],
                "entry": sinal["entry"],
                "entry_min": sinal.get("entry_min"),
                "entry_max": sinal.get("entry_max"),
                "sl": sinal.get("sl"),
                "tps": json.dumps(sinal.get("tps", [])),
                "source": sinal.get("source"),
                "telegram_group_id": sinal.get("telegram_group_id"),
                "telegram_message_id": sinal.get("telegram_message_id"),
                "max_age": SIGNAL_MAX_AGE_SECONDS,
            }
        )

        return resultado.rowcount > 0
    
# ── Bot Telegram para notificações ───────────────────────────────────────────
TG_NOTIFY_TOKEN  = os.environ.get("NOTIF_KEY", "")
TG_NOTIFY_CHATID = os.environ.get("NOTIF_CHAT", "")

# ── Telethon com StringSession ────────────────────────────────────────────────
session_string = os.environ.get("SESSION_STRING", "")
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

SIGNAL_GROUPS = [
    int(x.strip())
    for x in os.environ.get("TELEGRAM_SIGNAL_GROUPS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="TS Signal Bridge", version="2.7.1")

BASE_DIR = Path(__file__).resolve().parent


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(
        str(BASE_DIR / "dashboard.html"),
        media_type="text/html"
    )

dashboard_engine = create_engine(
    os.environ["DATABASE_URL"],
    pool_pre_ping=True
)

@app.get("/dashboard/data", include_in_schema=False)
def dashboard_data(
    periodo: str = "hoje",
    page: int = 1,
    page_size: int = 20
):

    if periodo not in {"hoje", "7", "30", "tudo"}:
        periodo = "hoje"

    if page < 1:
        page = 1

    if page_size < 1:
        page_size = 20

    if page_size > 100:
        page_size = 100

    offset = (page - 1) * page_size

    with dashboard_engine.connect() as conn:

        resumo = conn.execute(text("""
            SELECT

                (
                    SELECT COUNT(*)
                    FROM signals s
                    WHERE
                        :periodo = 'tudo'

                        OR (
                            :periodo = 'hoje'
                            AND (s.received_at AT TIME ZONE 'America/Sao_Paulo')::date
                                = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                        )

                        OR (
                            :periodo = '7'
                            AND s.received_at >= NOW() - INTERVAL '7 days'
                        )

                        OR (
                            :periodo = '30'
                            AND s.received_at >= NOW() - INTERVAL '30 days'
                        )
                ) AS sinais,

                (
                    SELECT COUNT(*)
                    FROM signal_mt5_events e
                    INNER JOIN signals s
                        ON s.id = e.signal_id
                    WHERE
                        e.event_type IN ('MARKET_EXECUTED', 'PENDING_EXECUTED')

                        AND (
                            :periodo = 'tudo'

                            OR (
                                :periodo = 'hoje'
                                AND (s.received_at AT TIME ZONE 'America/Sao_Paulo')::date
                                    = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                            )

                            OR (
                                :periodo = '7'
                                AND s.received_at >= NOW() - INTERVAL '7 days'
                            )

                            OR (
                                :periodo = '30'
                                AND s.received_at >= NOW() - INTERVAL '30 days'
                            )
                        )
                ) AS ordens_executadas,

                (
                    SELECT COUNT(*)
                    FROM signal_mt5_events e
                    INNER JOIN signals s
                        ON s.id = e.signal_id
                    WHERE
                        e.event_type = 'PENDING_CANCELLED_TP1_ALREADY_HIT'

                        AND (
                            :periodo = 'tudo'

                            OR (
                                :periodo = 'hoje'
                                AND (s.received_at AT TIME ZONE 'America/Sao_Paulo')::date
                                    = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                            )

                            OR (
                                :periodo = '7'
                                AND s.received_at >= NOW() - INTERVAL '7 days'
                            )

                            OR (
                                :periodo = '30'
                                AND s.received_at >= NOW() - INTERVAL '30 days'
                            )
                        )
                ) AS ordens_canceladas,

                (
                    SELECT COALESCE(
                        SUM(
                            substring(
                                e.comment
                                FROM 'resultado (-?[0-9]+[.]?[0-9]*) USD'
                            )::numeric
                        ),
                        0
                    )
                    FROM signal_mt5_events e

                    INNER JOIN signals s
                        ON s.id = e.signal_id

                    WHERE
                        e.event_type = 'POSITION_CLOSED'

                        AND (
                            :periodo = 'tudo'

                            OR (
                                :periodo = 'hoje'
                                AND (s.received_at AT TIME ZONE 'America/Sao_Paulo')::date
                                    = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                            )

                            OR (
                                :periodo = '7'
                                AND s.received_at >= NOW() - INTERVAL '7 days'
                            )

                            OR (
                                :periodo = '30'
                                AND s.received_at >= NOW() - INTERVAL '30 days'
                            )
                        )
                ) AS resultado_liquido

        """), {
            "periodo": periodo
        }).mappings().one()


        grupos = conn.execute(text("""
            SELECT
                COALESCE(s.source, 'Sem grupo') AS grupo,

                COUNT(DISTINCT s.id) AS sinais,

                COUNT(*) FILTER (
                    WHERE e.event_type IN (
                        'MARKET_EXECUTED',
                        'PENDING_EXECUTED'
                    )
                ) AS ordens,

                COUNT(*) FILTER (
                    WHERE e.event_type =
                        'PENDING_CANCELLED_TP1_ALREADY_HIT'
                ) AS canceladas,

                COALESCE(
                    SUM(
                        CASE
                            WHEN e.event_type = 'POSITION_CLOSED'
                            AND substring(
                                e.comment
                                FROM 'resultado (-?[0-9]+[.]?[0-9]*) USD'
                            )::numeric > 0

                            THEN substring(
                                e.comment
                                FROM 'resultado (-?[0-9]+[.]?[0-9]*) USD'
                            )::numeric

                            ELSE 0
                        END
                    ),
                    0
                ) AS lucro,

                COALESCE(
                    ABS(
                        SUM(
                            CASE
                                WHEN e.event_type = 'POSITION_CLOSED'
                                AND substring(
                                    e.comment
                                    FROM 'resultado (-?[0-9]+[.]?[0-9]*) USD'
                                )::numeric < 0

                                THEN substring(
                                    e.comment
                                    FROM 'resultado (-?[0-9]+[.]?[0-9]*) USD'
                                )::numeric

                                ELSE 0
                            END
                        )
                    ),
                    0
                ) AS prejuizo,

                COALESCE(
                    SUM(
                        CASE
                            WHEN e.event_type = 'POSITION_CLOSED'
                            THEN substring(
                                e.comment
                                FROM 'resultado (-?[0-9]+[.]?[0-9]*) USD'
                            )::numeric
                            ELSE 0
                        END
                    ),
                    0
                ) AS liquido

            FROM signals s

            LEFT JOIN signal_mt5_events e
                ON e.signal_id = s.id

            WHERE
                :periodo = 'tudo'

                OR (
                    :periodo = 'hoje'
                    AND (s.received_at AT TIME ZONE 'America/Sao_Paulo')::date
                        = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                )

                OR (
                    :periodo = '7'
                    AND s.received_at >= NOW() - INTERVAL '7 days'
                )

                OR (
                    :periodo = '30'
                    AND s.received_at >= NOW() - INTERVAL '30 days'
                )

            GROUP BY s.source

            ORDER BY liquido DESC

        """), {
            "periodo": periodo
        }).mappings().all()

        total_ultimos_sinais = conn.execute(text("""
            SELECT COUNT(*)

            FROM signals s

            WHERE
                :periodo = 'tudo'

                OR (
                    :periodo = 'hoje'
                    AND (s.received_at AT TIME ZONE 'America/Sao_Paulo')::date
                        = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                )

                OR (
                    :periodo = '7'
                    AND s.received_at >= NOW() - INTERVAL '7 days'
                )

                OR (
                    :periodo = '30'
                    AND s.received_at >= NOW() - INTERVAL '30 days'
                )
        """), {
            "periodo": periodo
        }).scalar_one()
                
        ultimos_sinais = conn.execute(text("""
            SELECT
                s.id,
                s.received_at,
                s.source AS grupo,
                s.type,
                s.entry,
                s.entry_min,
                s.entry_max,
                s.sl,
                s.status,

                COALESCE(
                    (
                        SELECT SUM(
                            substring(
                                e.comment
                                FROM 'resultado (-?[0-9]+[.]?[0-9]*) USD'
                            )::numeric
                        )
                        FROM signal_mt5_events e

                        WHERE e.signal_id = s.id
                          AND e.event_type = 'POSITION_CLOSED'
                    ),
                    0
                ) AS resultado

            FROM signals s

            WHERE
                :periodo = 'tudo'

                OR (
                    :periodo = 'hoje'
                    AND (s.received_at AT TIME ZONE 'America/Sao_Paulo')::date
                        = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                )

                OR (
                    :periodo = '7'
                    AND s.received_at >= NOW() - INTERVAL '7 days'
                )

                OR (
                    :periodo = '30'
                    AND s.received_at >= NOW() - INTERVAL '30 days'
                )

            ORDER BY s.received_at DESC

            LIMIT :page_size
            OFFSET :offset

        """), {
                "periodo": periodo,
                "page_size": page_size,
                "offset": offset
            }).mappings().all()

        total_paginas = (total_ultimos_sinais + page_size - 1) // page_size

        return {
            "sinais": int(resumo["sinais"] or 0),
            "ordens_executadas":
                int(resumo["ordens_executadas"] or 0),
            "ordens_canceladas":
                int(resumo["ordens_canceladas"] or 0),
            "resultado_liquido":
                float(resumo["resultado_liquido"] or 0),

            "grupos": [
                {
                    "grupo": item["grupo"],
                    "sinais": int(item["sinais"] or 0),
                    "ordens": int(item["ordens"] or 0),
                    "canceladas": int(item["canceladas"] or 0),
                    "lucro": float(item["lucro"] or 0),
                    "prejuizo": float(item["prejuizo"] or 0),
                    "liquido": float(item["liquido"] or 0)
                }
                for item in grupos
            ],

            "ultimos_sinais": [
                {
                    "id": str(item["id"]),
                    "horario": item["received_at"].isoformat(),
                    "grupo": item["grupo"] or "Sem grupo",
                    "tipo": item["type"],
                    "entry": float(item["entry"] or 0),
                    "entry_min": float(item["entry_min"] or 0),
                    "entry_max": float(item["entry_max"] or 0),
                    "sl": float(item["sl"] or 0),
                    "status": item["status"],
                    "resultado": float(item["resultado"] or 0)
                }
                for item in ultimos_sinais
            ],

            "paginacao": {
                "pagina_atual": page,
                "por_pagina": page_size,
                "total_registros": int(total_ultimos_sinais),
                "total_paginas": int(total_paginas),
                "tem_anterior": page > 1,
                "tem_proxima": page < total_paginas
            }
        }


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

# Eventos enviados pelo MT5 para histórico
class Mt5EventRequest(BaseModel):
    signal_id: str
    event_type: str
    status: str | None = None
    comment: str | None = None
    account: str | None = None
    ticket: int | None = None
    symbol: str | None = None
    price: float | None = None

# =============================================================================
# NOTIFICAÇÃO TELEGRAM
# =============================================================================
async def enviar_telegram(mensagem: str) -> None:
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

    text_clean = text.strip().replace("\\\n", "\n")

    text_clean = re.sub(r"\s*[|;]\s*", "\n", text_clean)

    # Normaliza diferentes formatos de zona de entrada.
    #
    # Exemplos aceitos:
    # 4305/4302
    # 4305 / 4302
    # 4293//4296
    # 4293// 4296
    # 4293 // 4296
    # 4340_4337
    # 4340 _ 4337
    #
    # Todos se tornam internamente: 4305/4302
    text_clean = re.sub(
        r"(?<=\d)\s*(?:/+|_)\s*(?=\d)",
        "/",
        text_clean
    )

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

    # Alguns provedores usam GOLD em vez de XAUUSD.
    # Exemplo: GOLD BUY NOW 4323/4318
    if not symbol and re.search(r"\bGOLD\b", text_clean.upper()):
        symbol = "XAUUSD"

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
                    entry = entry_min if trade_type == "BUY" else entry_max
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
    @client.on(
        events.NewMessage(
            chats=SIGNAL_GROUPS if SIGNAL_GROUPS else None,
            incoming=True,
            outgoing=True
        )
    )
    async def handler(event):

        if e_final_de_semana():
            log.debug("Final de semana — mensagem ignorada")
            return

        # ---------------------------------------------------------------------
        # PROTEÇÃO CONTRA MENSAGEM ANTIGA DO TELEGRAM
        # ---------------------------------------------------------------------
        data_mensagem = event.message.date

        if data_mensagem.tzinfo is None:
            data_mensagem = data_mensagem.replace(tzinfo=timezone.utc)

        agora = datetime.now(timezone.utc)
        idade_segundos = (agora - data_mensagem).total_seconds()

        if idade_segundos > SIGNAL_MAX_AGE_SECONDS:
            log.info(
                f"Mensagem antiga ignorada | "
                f"Idade: {idade_segundos:.1f}s | "
                f"Telegram: {data_mensagem.isoformat()}"
            )
            return

        # ---------------------------------------------------------------------
        # DADOS DA MENSAGEM
        # ---------------------------------------------------------------------
        chat = await event.get_chat()
        texto = event.raw_text or ""
        nome = getattr(chat, "title", str(event.chat_id))

        log.info(
            f"Mensagem recebida | Grupo: {nome} ({event.chat_id}) | "
            f"Idade: {idade_segundos:.1f}s | "
            f"Texto: {texto[:80]}"
        )

        # Ignora mensagens que não sejam de grupo/canal
        if not event.is_group and not event.is_channel:
            return

        # ---------------------------------------------------------------------
        # PARSER DO SINAL
        # ---------------------------------------------------------------------
        sinal = parse_signal(texto)

        if not sinal:
            log.info("Mensagem não reconhecida como sinal — ignorada")
            return

        # ---------------------------------------------------------------------
        # IDENTIFICAÇÃO DO TELEGRAM
        # ---------------------------------------------------------------------
        sinal["source"] = nome
        sinal["telegram_group_id"] = event.chat_id
        sinal["telegram_message_id"] = event.message.id

        # ---------------------------------------------------------------------
        # SALVAR NO POSTGRESQL
        # ---------------------------------------------------------------------
        salvo = salvar_sinal_db(sinal)

        if not salvo:
            log.info(
                f"Mensagem duplicada ignorada | "
                f"Grupo: {event.chat_id} | "
                f"Mensagem: {event.message.id}"
            )
            return

        log.info(
            f"Sinal salvo no banco: {sinal['id']} | "
            f"{sinal['type']} {sinal['symbol']} @ {sinal['entry']} | "
            f"{len(sinal['tps'])} TPs | "
            f"SL: {sinal['sl']}"
        )

        # ---------------------------------------------------------------------
        # NOTIFICAÇÃO TELEGRAM
        # ---------------------------------------------------------------------
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
            log.info("Conectado ao Telegram com sucesso")

        registrar_listener()
        log.info(f"Listener ativo | Grupos monitorados: {SIGNAL_GROUPS or 'TODOS'}")

        await enviar_telegram(
            "🤖 <b>TS Signal Bridge v2.7.1</b>\n"
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
    expirar_sinais_antigos()

    with engine.begin() as conn:
        pendentes = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM signals
                WHERE status = 'pending'
                  AND expires_at > NOW()
            """)
        ).scalar()

        total = conn.execute(
            text("SELECT COUNT(*) FROM signals")
        ).scalar()

    return {
        "status": "online",
        "telegram": client.is_connected(),
        "sinais_fila": pendentes,
        "sinais_total": total,
        "grupos": SIGNAL_GROUPS,
        "time": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/signal/pending")
async def get_pending(authorization: str = Header(""), symbol: str = ""):
    check_token(authorization)

    familias = {
        "XAUUSD": {"XAUUSD"},
        "FOREX": {
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
            "EURJPY", "GBPJPY", "EURGBP", "EURAUD", "EURCAD", "GBPAUD", "GBPCAD",
            "GBPCHF", "AUDCAD", "AUDJPY", "CADJPY", "CHFJPY", "AUDNZD", "EURNZD",
            "GBPNZD"
        },
        "INDEX": {"US30", "US500", "NAS100", "GER40", "UK100", "JP225"},
        "CRYPTO": {"BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD"},
        "OIL": {"USOIL", "UKOIL"},
    }

    # Primeiro, qualquer sinal vencido deixa de ser elegível
    expirar_sinais_antigos()

    with engine.begin() as conn:

        if not symbol:
            resultado = conn.execute(
                text("""
                    SELECT
                        id,
                        symbol,
                        type,
                        entry,
                        entry_min,
                        entry_max,
                        sl,
                        tps,
                        source,
                        received_at,
                        expires_at,
                        status
                    FROM signals
                    WHERE status = 'pending'
                      AND expires_at > NOW()
                    ORDER BY received_at ASC
                    LIMIT 1
                """)
            ).mappings().first()

        else:
            sym_upper = symbol.upper()
            aceitos = familias.get(sym_upper, {sym_upper})

            resultado = conn.execute(
                text("""
                    SELECT
                        id,
                        symbol,
                        type,
                        entry,
                        entry_min,
                        entry_max,
                        sl,
                        tps,
                        source,
                        received_at,
                        expires_at,
                        status
                    FROM signals
                    WHERE status = 'pending'
                      AND expires_at > NOW()
                      AND symbol = ANY(:simbolos)
                    ORDER BY received_at ASC
                    LIMIT 1
                """),
                {
                    "simbolos": list(aceitos)
                }
            ).mappings().first()

    if not resultado:
        from fastapi.responses import Response
        return Response(status_code=204)

    sinal = {
        "id": str(resultado["id"]),
        "symbol": resultado["symbol"],
        "type": resultado["type"],
        "entry": float(resultado["entry"]) if resultado["entry"] is not None else None,
        "entry_min": float(resultado["entry_min"]) if resultado["entry_min"] is not None else None,
        "entry_max": float(resultado["entry_max"]) if resultado["entry_max"] is not None else None,
        "sl": float(resultado["sl"]) if resultado["sl"] is not None else None,
        "tps": resultado["tps"] or [],
        "source": resultado["source"],
        "time": resultado["received_at"].isoformat(),
        "expires_at": resultado["expires_at"].isoformat(),
        "status": resultado["status"]
    }

    return JSONResponse(status_code=200, content=sinal)

# =============================================================================
# /signal/confirm
# Sempre retorna 200 para o EA.
# =============================================================================
@app.post("/signal/confirm")
async def confirm_signal(body: ConfirmRequest, authorization: str = Header("")):
    check_token(authorization)

    try:
        with engine.begin() as conn:

            sinal = conn.execute(
                text("""
                    SELECT
                        id,
                        symbol,
                        type,
                        entry,
                        sl,
                        tps,
                        source,
                        status
                    FROM signals
                    WHERE id = :id
                    LIMIT 1
                """),
                {
                    "id": body.id
                }
            ).mappings().first()

            # -------------------------------------------------------------
            # ID desconhecido
            # -------------------------------------------------------------
            if not sinal:
                log.info(
                    f"Confirmação de ID desconhecido: {body.id} | "
                    f"{body.status} — aceito sem erro"
                )

                return {
                    "ok": True,
                    "id": body.id,
                    "status": "not_found_ignored"
                }

            # -------------------------------------------------------------
            # Já foi confirmado anteriormente
            # -------------------------------------------------------------
            if sinal["status"] not in ("pending", "waiting_entry"):
                log.info(
                    f"Confirmação duplicada ignorada: {body.id} | "
                    f"status atual={sinal['status']}"
                )

                return {
                    "ok": True,
                    "id": body.id,
                    "status": "already_confirmed"
                }

            # -------------------------------------------------------------
            # Atualiza confirmação recebida do MT5
            # -------------------------------------------------------------
            resultado = conn.execute(
                text("""
                    UPDATE signals
                    SET
                        status = :status,

                        executed_at = CASE
                            WHEN :is_executed THEN NOW()
                            ELSE executed_at
                        END,

                        execution_message = :message,
                        account = :account

                    WHERE id = :id
                        AND status IN ('pending', 'waiting_entry')
                """),
                {
                    "id": body.id,
                    "status": body.status,
                    "is_executed": body.status == "executed",
                    "message": body.message,
                    "account": (
                        str(body.account)
                        if body.account is not None
                        else None
                    )
                }
            )

            if resultado.rowcount == 0:
                log.info(
                    f"Confirmação não alterou registro: {body.id} | "
                    f"status={body.status}"
                )

        # -----------------------------------------------------------------
        # Confirmação gravada
        # -----------------------------------------------------------------
        log.info(
            f"Confirmação MT5: {body.id} | "
            f"{body.status} | {body.message}"
        )

        # -----------------------------------------------------------------
        # NOTIFICAÇÕES
        # -----------------------------------------------------------------
        if body.status == "executed":

            emoji = "🟢" if sinal.get("type") == "BUY" else "🔴"

            msg = (
                f"{emoji} <b>{sinal.get('type')}  •  "
                f"{sinal.get('symbol')}</b>\n"
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
                f"⚠️ <b>Falha ao abrir ordem — "
                f"{sinal.get('symbol')}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"❌ Status: <code>failed</code>\n"
                f"📋 Motivo: {body.message}\n"
                f"🏦 Conta: <code>{body.account}</code>\n"
                f"⏱ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
            )

            await enviar_telegram(msg)

        return {
            "ok": True,
            "id": body.id,
            "status": body.status
        }

    except Exception:
        # Nunca retorna 500 para o EA
        log.exception(
            f"Erro inesperado em /signal/confirm | id={body.id}"
        )

        return {
            "ok": True,
            "id": body.id,
            "status": "error_ignored"
        }
# =============================================================================
# EVENTOS MT5 - HISTÓRICO DE EXECUÇÃO
# Registra no banco os eventos ocorridos no MT5 relacionados a cada sinal,
# como criação de ordem, execução, rejeição, cancelamento, timeout, TP e SL.
# Este endpoint é apenas para auditoria e não altera o status principal do sinal.
# ============================================================================= 
@app.post("/signal/mt5-event")
async def signal_mt5_event(
    body: Mt5EventRequest,
    authorization: str = Header("")
):
    check_token(authorization)

    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO signal_mt5_events (
                        signal_id,
                        event_type,
                        status,
                        comment,
                        account,
                        ticket,
                        symbol,
                        price,
                        created_at
                    )
                    VALUES (
                        :signal_id,
                        :event_type,
                        :status,
                        :comment,
                        :account,
                        :ticket,
                        :symbol,
                        :price,
                        NOW()
                    )
                """),
                {
                    "signal_id": body.signal_id,
                    "event_type": body.event_type,
                    "status": body.status,
                    "comment": body.comment,
                    "account": body.account,
                    "ticket": body.ticket,
                    "symbol": body.symbol,
                    "price": body.price
                }
            )

        log.info(
            f"Evento MT5 salvo | "
            f"Sinal: {body.signal_id} | "
            f"Evento: {body.event_type} | "
            f"Status: {body.status} | "
            f"Ticket: {body.ticket}"
        )

        return {
            "ok": True,
            "signal_id": body.signal_id,
            "event_type": body.event_type
        }

    except Exception:
        log.exception(
            f"Erro ao salvar evento MT5 | "
            f"Sinal: {body.signal_id} | "
            f"Evento: {body.event_type}"
        )

        return {
            "ok": False,
            "signal_id": body.signal_id,
            "event_type": body.event_type
        }
       
@app.get("/signals/queue")
async def get_queue(authorization: str = Header("")):
    check_token(authorization)

    expirar_sinais_antigos()

    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    id,
                    symbol,
                    type,
                    entry,
                    entry_min,
                    entry_max,
                    sl,
                    tps,
                    source,
                    received_at,
                    expires_at,
                    status
                FROM signals
                WHERE status = 'pending'
                  AND expires_at > NOW()
                ORDER BY received_at ASC
            """)
        ).mappings().all()

    sinais = []

    for r in rows:
        sinais.append({
            "id": str(r["id"]),
            "symbol": r["symbol"],
            "type": r["type"],
            "entry": float(r["entry"]) if r["entry"] is not None else None,
            "entry_min": float(r["entry_min"]) if r["entry_min"] is not None else None,
            "entry_max": float(r["entry_max"]) if r["entry_max"] is not None else None,
            "sl": float(r["sl"]) if r["sl"] is not None else None,
            "tps": r["tps"] or [],
            "source": r["source"],
            "time": r["received_at"].isoformat(),
            "expires_at": r["expires_at"].isoformat(),
            "status": r["status"],
        })

    return {
        "queue": sinais,
        "count": len(sinais)
    }

@app.get("/signals/history")
async def get_history(authorization: str = Header("")):
    check_token(authorization)

    expirar_sinais_antigos()

    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    id,
                    symbol,
                    type,
                    entry,
                    entry_min,
                    entry_max,
                    sl,
                    tps,
                    source,
                    received_at,
                    expires_at,
                    status,
                    executed_at,
                    execution_message,
                    account
                FROM signals
                ORDER BY received_at DESC
                LIMIT 50
            """)
        ).mappings().all()

        total = conn.execute(
            text("SELECT COUNT(*) FROM signals")
        ).scalar()

    sinais = []

    for r in rows:
        sinais.append({
            "id": str(r["id"]),
            "symbol": r["symbol"],
            "type": r["type"],
            "entry": float(r["entry"]) if r["entry"] is not None else None,
            "entry_min": float(r["entry_min"]) if r["entry_min"] is not None else None,
            "entry_max": float(r["entry_max"]) if r["entry_max"] is not None else None,
            "sl": float(r["sl"]) if r["sl"] is not None else None,
            "tps": r["tps"] or [],
            "source": r["source"],
            "received_at": r["received_at"].isoformat(),
            "expires_at": r["expires_at"].isoformat(),
            "status": r["status"],
            "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None,
            "execution_message": r["execution_message"],
            "account": r["account"],
        })

    return {
        "signals": sinais,
        "total": total
    }

@app.delete("/signals/queue")
async def clear_queue(authorization: str = Header("")):
    check_token(authorization)

    with engine.begin() as conn:
        resultado = conn.execute(
            text("""
                UPDATE signals
                SET status = 'cancelled'
                WHERE status = 'pending'
            """)
        )

    return {
        "ok": True,
        "cancelled": resultado.rowcount
    }
@app.post("/signal/test")
async def test_signal(request_body: dict, authorization: str = Header("")):
    check_token(authorization)

    texto = request_body.get("text", "")

    if not texto:
        raise HTTPException(
            status_code=400,
            detail="Campo 'text' obrigatório"
        )

    sinal = parse_signal(texto)

    if not sinal:
        raise HTTPException(
            status_code=422,
            detail="Texto não reconhecido como sinal"
        )

    sinal["source"] = "Teste Manual"

    salvar_sinal_db(sinal)

    return {
        "ok": True,
        "signal": sinal
    }

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
