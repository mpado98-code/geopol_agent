"""
GeoMacro Overview Bot
=====================
Bot Telegram settimanale che genera un report macro-geopolitico sui 10 paesi
piu' rilevanti del mondo, combinando dati da World Bank, FRED, Yahoo Finance,
NewsAPI/GDELT e sintesi AI tramite Google Gemini.

Esecuzione: lo script e' end-to-end. Va lanciato da GitHub Actions (workflow
schedulato la domenica alle 17 UTC) oppure manualmente in locale dopo aver
esportato le 5 variabili d'ambiente:
    TELEGRAM_BOT_TOKEN_GEOMACRO
    TELEGRAM_CHAT_ID_GEOMACRO
    GEMINI_API_KEY
    FRED_API_KEY
    NEWSAPI_KEY

Assunzioni esplicite
--------------------
1) I dati World Bank hanno tipicamente 1-2 anni di ritardo: lo script riporta
   sempre l'anno del dato preso (campo "year") per trasparenza.
2) I dati militari (portaerei, sottomarini, testate, GFP rank) non hanno API
   gratuite affidabili: sono hardcoded in DATI_MILITARI_STATICI con fonte e
   anno di riferimento (SIPRI 2024 per spesa, Global Firepower 2025 per
   ranking, Federation of American Scientists 2025 per testate nucleari,
   IISS Military Balance 2024 per personale ed equipaggiamenti).
3) Il bond decennale russo via yfinance non e' affidabile per sanzioni: in
   quel caso lo script segnala "non disponibile".
4) NewsAPI free tier: 100 richieste/giorno. Con 10 paesi consumiamo 10
   richieste/run, quindi siamo sicuri.
5) Gemini free tier: 1500 richieste/giorno. Una sola chiamata di sintesi.
6) Lo script degrada graziosamente: se una fonte fallisce, continua con i
   dati che ha e lo segnala nel report finale.

Autore: generato per pipeline personale di monitoraggio macro-geopolitico.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

# yfinance e pandas sono usati solo per mercati/valute: import lazy in caso
# di problemi di rete che potrebbero rallentare l'avvio dello script
try:
    import yfinance as yf
    import pandas as pd
except Exception as _imp_err:  # pragma: no cover - safety net
    print(f"FATAL: impossibile importare yfinance/pandas: {_imp_err}", flush=True)
    sys.exit(1)


# ============================================================================
# CONFIGURAZIONE
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("geomacro")

# Token e chiavi API: presi dall'ambiente (GitHub Secrets)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_GEOMACRO", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID_GEOMACRO", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "").strip()

# Endpoint di terze parti
WORLDBANK_BASE = "https://api.worldbank.org/v2/country/{iso3}/indicator/{ind}"
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
NEWSAPI_BASE = "https://newsapi.org/v2/everything"
GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
GEMINI_API = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Limite per messaggio Telegram (lasciamo margine sotto 4096)
TELEGRAM_MAX_CHARS = 3900

# Timeouts (secondi)
HTTP_TIMEOUT = 25
GEMINI_TIMEOUT = 90

# Modelli Gemini in ordine di preferenza (free tier)
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]

# 10 paesi da coprire: codice ISO3 -> nome italiano + bandiera + ISO2 per news
COUNTRIES: Dict[str, Dict[str, str]] = {
    "USA": {"nome": "Stati Uniti", "bandiera": "🇺🇸", "query": "United States"},
    "CHN": {"nome": "Cina", "bandiera": "🇨🇳", "query": "China"},
    "JPN": {"nome": "Giappone", "bandiera": "🇯🇵", "query": "Japan"},
    "DEU": {"nome": "Germania", "bandiera": "🇩🇪", "query": "Germany"},
    "IND": {"nome": "India", "bandiera": "🇮🇳", "query": "India"},
    "GBR": {"nome": "Regno Unito", "bandiera": "🇬🇧", "query": "United Kingdom"},
    "FRA": {"nome": "Francia", "bandiera": "🇫🇷", "query": "France"},
    "ITA": {"nome": "Italia", "bandiera": "🇮🇹", "query": "Italy"},
    "BRA": {"nome": "Brasile", "bandiera": "🇧🇷", "query": "Brazil"},
    "RUS": {"nome": "Russia", "bandiera": "🇷🇺", "query": "Russia"},
}

# Indicatori World Bank: codice -> etichetta italiana
# Espansi su suggerimento panel di esperti (macro/geopolitica/tech).
WB_INDICATORS: Dict[str, str] = {
    # Crescita e benessere
    "NY.GDP.MKTP.CD": "gdp_nominale_usd",
    "NY.GDP.PCAP.CD": "gdp_per_capita_usd",
    "NY.GDP.MKTP.KD.ZG": "gdp_growth_yoy_pct",
    "SL.UEM.TOTL.ZS": "disoccupazione_pct",
    "FP.CPI.TOTL.ZG": "inflazione_cpi_pct",
    # Finanza pubblica e contabilita' nazionale
    "GC.DOD.TOTL.GD.ZS": "debito_pubblico_pct_pil",
    "NE.CON.PRVT.ZS": "consumi_privati_pct_pil",
    "NE.GDI.TOTL.ZS": "investimenti_pct_pil",
    "NE.CON.GOVT.ZS": "spesa_pubblica_pct_pil",
    "NE.EXP.GNFS.ZS": "export_pct_pil",
    "NE.IMP.GNFS.ZS": "import_pct_pil",
    # Estero (richiesta del macroeconomista)
    "TX.VAL.MRCH.CD.WT": "export_merci_usd",
    "TM.VAL.MRCH.CD.WT": "import_merci_usd",
    "FI.RES.TOTL.CD": "riserve_valutarie_usd",
    "BN.CAB.XOKA.GD.ZS": "conto_corrente_pct_pil",
    # Tecnologia e innovazione (richiesta dell'esperto tech)
    "GB.XPD.RSDV.GD.ZS": "rd_spending_pct_pil",
    "IT.NET.USER.ZS": "internet_users_pct",
    "TX.VAL.TECH.MF.ZS": "high_tech_export_pct_manuf",
    # Mercati capitali (richiesta dell'esperto finanza)
    "CM.MKT.LCAP.CD": "market_cap_usd",
    "CM.MKT.LCAP.GD.ZS": "market_cap_pct_pil",
    "CM.MKT.LDOM.NO": "listed_companies",
    # Militare
    "MS.MIL.XPND.GD.ZS": "spesa_militare_pct_pil",
    "MS.MIL.XPND.CD": "spesa_militare_usd",
    "MS.MIL.TOTL.P1": "personale_militare_attivo",
}

# Series ID FRED per tassi banche centrali / inflazione recente
FRED_SERIES: Dict[str, Dict[str, str]] = {
    "USA": {"tasso": "FEDFUNDS", "cpi_yoy": "CPIAUCSL"},
    # ECBDFR si applica a Germania, Francia, Italia (Eurozona)
    "DEU": {"tasso": "ECBDFR"},
    "FRA": {"tasso": "ECBDFR"},
    "ITA": {"tasso": "ECBDFR"},
    "GBR": {"tasso": "IUDSOIA"},
    "JPN": {"tasso": "INTDSRJPM193N"},
    "CHN": {"tasso": "INTDSRCNM193N"},
    "IND": {"tasso": "INTDSRINM193N"},
    "BRA": {"tasso": "INTDSRBRM193N"},
    "RUS": {"tasso": "INTDSRRUM193N"},
}

# Ticker yfinance: indice azionario principale per paese
INDICI_TICKERS: Dict[str, str] = {
    "USA": "^GSPC",
    "CHN": "000001.SS",
    "JPN": "^N225",
    "DEU": "^GDAXI",
    "IND": "^BSESN",
    "GBR": "^FTSE",
    "FRA": "^FCHI",
    "ITA": "FTSEMIB.MI",
    "BRA": "^BVSP",
    "RUS": "IMOEX.ME",  # spesso non disponibile per sanzioni
}

# Cambi valuta vs USD (yfinance)
VALUTE_TICKERS: Dict[str, str] = {
    "USA": "",  # dollaro base, nessuna conversione
    "CHN": "CNY=X",
    "JPN": "JPY=X",
    "DEU": "EUR=X",
    "IND": "INR=X",
    "GBR": "GBP=X",
    "FRA": "EUR=X",
    "ITA": "EUR=X",
    "BRA": "BRL=X",
    "RUS": "RUB=X",
}

# Bond decennali governativi (dove yfinance fornisce un simbolo affidabile)
BOND_TICKERS: Dict[str, str] = {
    "USA": "^TNX",   # 10Y Treasury yield * 10
    "DEU": "",       # nessun simbolo yfinance affidabile per Bund 10Y
    "GBR": "",
    "JPN": "",
    "FRA": "",
    "ITA": "",
    "CHN": "",
    "IND": "",
    "BRA": "",
    "RUS": "",
}

# ----------------------------------------------------------------------------
# DATI MILITARI STATICI
# ----------------------------------------------------------------------------
# Fonti:
#   - Spesa militare: SIPRI Military Expenditure Database, 2024 (release 2025)
#   - Personale attivo + equipaggiamento: IISS Military Balance 2024
#   - Testate nucleari: Federation of American Scientists, stock stimato 2025
#   - Global Firepower Index: GFP ranking 2025
# I valori sono indicativi e cambiano lentamente.
DATI_MILITARI_STATICI: Dict[str, Dict[str, Any]] = {
    "USA": {
        "spesa_militare_usd_b": 916.0, "spesa_yoy_growth_pct": 2.4,
        "spesa_militare_pct_pil": 3.4, "personale_attivo": 1_328_000,
        "portaerei_operative": 11, "sottomarini": 71,
        "unita_superficie_maggiori": 92,  # cacciatorpediniere + fregate + incrociatori
        "aerei_combattimento": 5209, "caccia_5g": 630,  # F-35 + F-22
        "testate_nucleari": 5044, "global_firepower_rank": 1,
        "alleanze": ["NATO", "G7", "G20", "AUKUS", "Five Eyes"],
        "conflitti_attivi": ["Supporto Ucraina", "Supporto Israele", "Tensioni Mar Cinese Mer."],
    },
    "CHN": {
        "spesa_militare_usd_b": 296.0, "spesa_yoy_growth_pct": 6.0,
        "spesa_militare_pct_pil": 1.7, "personale_attivo": 2_035_000,
        "portaerei_operative": 3, "sottomarini": 61,
        "unita_superficie_maggiori": 86, "aerei_combattimento": 3309,
        "caccia_5g": 200,  # J-20 stima
        "testate_nucleari": 500, "global_firepower_rank": 3,
        "alleanze": ["BRICS", "G20", "SCO"],
        "conflitti_attivi": ["Tensioni Taiwan", "Dispute Mar Cinese", "Confine India"],
    },
    "JPN": {
        "spesa_militare_usd_b": 50.2, "spesa_yoy_growth_pct": 11.4,
        "spesa_militare_pct_pil": 1.2, "personale_attivo": 247_000,
        "portaerei_operative": 0, "sottomarini": 22,
        "unita_superficie_maggiori": 47, "aerei_combattimento": 1443,
        "caccia_5g": 30,  # F-35 in consegna
        "testate_nucleari": 0, "global_firepower_rank": 7,
        "alleanze": ["G7", "G20", "Quad"],
        "conflitti_attivi": ["Tensioni Cina/Corea Nord", "Dispute Senkaku"],
    },
    "DEU": {
        "spesa_militare_usd_b": 88.5, "spesa_yoy_growth_pct": 23.2,
        "spesa_militare_pct_pil": 2.1, "personale_attivo": 181_000,
        "portaerei_operative": 0, "sottomarini": 6,
        "unita_superficie_maggiori": 17, "aerei_combattimento": 600,
        "caccia_5g": 8,  # F-35 ordinati, prime consegne
        "testate_nucleari": 0, "global_firepower_rank": 9,
        "alleanze": ["NATO", "G7", "G20", "UE"],
        "conflitti_attivi": ["Supporto Ucraina (eABM, leopard)"],
    },
    "IND": {
        "spesa_militare_usd_b": 83.6, "spesa_yoy_growth_pct": 4.6,
        "spesa_militare_pct_pil": 2.4, "personale_attivo": 1_455_000,
        "portaerei_operative": 2, "sottomarini": 17,
        "unita_superficie_maggiori": 28, "aerei_combattimento": 2296,
        "caccia_5g": 0,  # AMCA in sviluppo
        "testate_nucleari": 172, "global_firepower_rank": 4,
        "alleanze": ["BRICS", "G20", "Quad", "SCO"],
        "conflitti_attivi": ["Confine Cina (LAC)", "Tensioni Pakistan"],
    },
    "GBR": {
        "spesa_militare_usd_b": 81.8, "spesa_yoy_growth_pct": 4.8,
        "spesa_militare_pct_pil": 2.3, "personale_attivo": 184_000,
        "portaerei_operative": 2, "sottomarini": 10,
        "unita_superficie_maggiori": 18, "aerei_combattimento": 631,
        "caccia_5g": 36,  # F-35B
        "testate_nucleari": 225, "global_firepower_rank": 6,
        "alleanze": ["NATO", "G7", "G20", "AUKUS", "Five Eyes"],
        "conflitti_attivi": ["Supporto Ucraina", "Operazioni Mar Rosso"],
    },
    "FRA": {
        "spesa_militare_usd_b": 61.3, "spesa_yoy_growth_pct": 6.5,
        "spesa_militare_pct_pil": 2.1, "personale_attivo": 203_000,
        "portaerei_operative": 1, "sottomarini": 10,
        "unita_superficie_maggiori": 22, "aerei_combattimento": 976,
        "caccia_5g": 0,  # FCAS in sviluppo
        "testate_nucleari": 290, "global_firepower_rank": 11,
        "alleanze": ["NATO", "G7", "G20", "UE"],
        "conflitti_attivi": ["Supporto Ucraina", "Ritiro Sahel"],
    },
    "ITA": {
        "spesa_militare_usd_b": 35.5, "spesa_yoy_growth_pct": 5.9,
        "spesa_militare_pct_pil": 1.5, "personale_attivo": 161_500,
        "portaerei_operative": 2, "sottomarini": 8,
        "unita_superficie_maggiori": 20, "aerei_combattimento": 404,
        "caccia_5g": 25,  # F-35A/B
        "testate_nucleari": 0, "global_firepower_rank": 10,
        "alleanze": ["NATO", "G7", "G20", "UE"],
        "conflitti_attivi": ["Operazioni NATO", "Missione UNIFIL Libano"],
    },
    "BRA": {
        "spesa_militare_usd_b": 22.9, "spesa_yoy_growth_pct": 2.0,
        "spesa_militare_pct_pil": 1.1, "personale_attivo": 360_000,
        "portaerei_operative": 0, "sottomarini": 6,
        "unita_superficie_maggiori": 9, "aerei_combattimento": 715,
        "caccia_5g": 0,
        "testate_nucleari": 0, "global_firepower_rank": 12,
        "alleanze": ["BRICS", "G20", "Mercosur"],
        "conflitti_attivi": ["Sicurezza interna Amazzonia"],
    },
    "RUS": {
        "spesa_militare_usd_b": 109.0, "spesa_yoy_growth_pct": 24.0,
        "spesa_militare_pct_pil": 5.9, "personale_attivo": 1_320_000,
        "portaerei_operative": 1, "sottomarini": 64,
        "unita_superficie_maggiori": 32, "aerei_combattimento": 3652,
        "caccia_5g": 22,  # Su-57 stima operativi
        "testate_nucleari": 5580, "global_firepower_rank": 2,
        "alleanze": ["BRICS", "G20", "CSTO", "SCO"],
        "conflitti_attivi": ["Guerra Ucraina", "Presenza Siria/Africa (Wagner)"],
    },
}

# ----------------------------------------------------------------------------
# DATI AZIONARI AGGREGATI (richiesta esperto finanza)
# ----------------------------------------------------------------------------
# Fonti: World Federation of Exchanges, S&P/MSCI fact sheets, Bloomberg,
# Yardeni Research, dati FY 2024 consolidati. I valori riguardano il listino
# nazionale principale (USA = S&P 500; CHN = A-shares Shanghai+Shenzhen;
# JPN = TOPIX; DEU = Prime Standard; IND = BSE; GBR = FTSE All-Share;
# FRA = SBF 120; ITA = FTSE Italia All-Share; BRA = B3; RUS = MOEX).
STOCK_MARKET_AGGREGATES: Dict[str, Dict[str, Any]] = {
    "USA": {"market_cap_usd_b": 50000, "ricavi_aggregati_usd_b": 16400,
            "utili_aggregati_usd_b": 2050, "pe_ratio": 25.0,
            "dividend_yield_pct": 1.3, "listed_companies": 4300},
    "CHN": {"market_cap_usd_b": 10500, "ricavi_aggregati_usd_b": 13200,
            "utili_aggregati_usd_b": 850, "pe_ratio": 13.0,
            "dividend_yield_pct": 2.5, "listed_companies": 5300},
    "JPN": {"market_cap_usd_b": 6500, "ricavi_aggregati_usd_b": 7500,
            "utili_aggregati_usd_b": 550, "pe_ratio": 16.0,
            "dividend_yield_pct": 2.0, "listed_companies": 3900},
    "DEU": {"market_cap_usd_b": 2200, "ricavi_aggregati_usd_b": 2500,
            "utili_aggregati_usd_b": 150, "pe_ratio": 16.0,
            "dividend_yield_pct": 3.0, "listed_companies": 430},
    "IND": {"market_cap_usd_b": 5000, "ricavi_aggregati_usd_b": 1500,
            "utili_aggregati_usd_b": 130, "pe_ratio": 24.0,
            "dividend_yield_pct": 1.2, "listed_companies": 5300},
    "GBR": {"market_cap_usd_b": 2800, "ricavi_aggregati_usd_b": 3500,
            "utili_aggregati_usd_b": 280, "pe_ratio": 13.0,
            "dividend_yield_pct": 3.7, "listed_companies": 1900},
    "FRA": {"market_cap_usd_b": 2700, "ricavi_aggregati_usd_b": 2900,
            "utili_aggregati_usd_b": 220, "pe_ratio": 14.0,
            "dividend_yield_pct": 3.2, "listed_companies": 470},
    "ITA": {"market_cap_usd_b": 750, "ricavi_aggregati_usd_b": 900,
            "utili_aggregati_usd_b": 80, "pe_ratio": 10.0,
            "dividend_yield_pct": 4.5, "listed_companies": 220},
    "BRA": {"market_cap_usd_b": 800, "ricavi_aggregati_usd_b": 700,
            "utili_aggregati_usd_b": 80, "pe_ratio": 9.0,
            "dividend_yield_pct": 7.0, "listed_companies": 360},
    "RUS": {"market_cap_usd_b": 550, "ricavi_aggregati_usd_b": 500,
            "utili_aggregati_usd_b": 70, "pe_ratio": 5.0,
            "dividend_yield_pct": 9.0, "listed_companies": 190},
}

# ----------------------------------------------------------------------------
# RATING SOVRANO S&P (2025) — richiesta esperto macro/finanza
# ----------------------------------------------------------------------------
RATING_SOVRANO: Dict[str, Dict[str, str]] = {
    "USA": {"sp": "AA+", "outlook": "Stabile"},
    "CHN": {"sp": "A+", "outlook": "Stabile"},
    "JPN": {"sp": "A+", "outlook": "Stabile"},
    "DEU": {"sp": "AAA", "outlook": "Stabile"},
    "IND": {"sp": "BBB-", "outlook": "Positivo"},
    "GBR": {"sp": "AA", "outlook": "Stabile"},
    "FRA": {"sp": "AA-", "outlook": "Negativo"},
    "ITA": {"sp": "BBB", "outlook": "Stabile"},
    "BRA": {"sp": "BB", "outlook": "Stabile"},
    "RUS": {"sp": "NR (ritirato)", "outlook": "Default sel. (Fitch CCC-)"},
}

# ----------------------------------------------------------------------------
# POSIZIONAMENTO GEOPOLITICO — richiesta esperto geopolitico
# ----------------------------------------------------------------------------
# Partner commerciali (export+import 2023, fonti: UN Comtrade/WTO).
# Energia: dipendenza netta da import in % consumo primario (IEA 2023).
#   negativo = esportatore netto, positivo = importatore netto.
# Sanzioni: status sintetico (impose/subisce/none).
GEO_POSITIONING: Dict[str, Dict[str, Any]] = {
    "USA": {"partner": ["Canada", "Messico", "Cina"], "energia_dip_pct": -19,
            "sanzioni": "Impone (Russia, Iran, Corea N., Cina tech)"},
    "CHN": {"partner": ["USA", "Hong Kong", "Giappone"], "energia_dip_pct": 22,
            "sanzioni": "Subisce (USA tech, UE export control)"},
    "JPN": {"partner": ["Cina", "USA", "Corea Sud"], "energia_dip_pct": 88,
            "sanzioni": "Allineato sanzioni Russia"},
    "DEU": {"partner": ["USA", "Cina", "Francia"], "energia_dip_pct": 64,
            "sanzioni": "Impone (Russia via UE)"},
    "IND": {"partner": ["USA", "Cina", "UAE"], "energia_dip_pct": 47,
            "sanzioni": "Nessuna allineata occidentale"},
    "GBR": {"partner": ["USA", "Germania", "Paesi Bassi"], "energia_dip_pct": 35,
            "sanzioni": "Impone (Russia, Iran, Cina HR)"},
    "FRA": {"partner": ["Germania", "Italia", "USA"], "energia_dip_pct": 47,
            "sanzioni": "Impone (via UE)"},
    "ITA": {"partner": ["Germania", "Francia", "USA"], "energia_dip_pct": 73,
            "sanzioni": "Impone (via UE)"},
    "BRA": {"partner": ["Cina", "USA", "Argentina"], "energia_dip_pct": -23,
            "sanzioni": "Neutrale"},
    "RUS": {"partner": ["Cina", "Turchia", "India"], "energia_dip_pct": -85,
            "sanzioni": "Subisce (G7+UE pacchetto pieno)"},
}

# ----------------------------------------------------------------------------
# CAMPIONI TECNOLOGICI NAZIONALI — richiesta esperto tech
# ----------------------------------------------------------------------------
TECH_LEADERS: Dict[str, List[str]] = {
    "USA": ["NVIDIA", "Microsoft", "Apple", "Alphabet", "Meta"],
    "CHN": ["Tencent", "Alibaba", "BYD", "Huawei", "SMIC"],
    "JPN": ["Toyota", "Sony", "SoftBank", "Tokyo Electron", "Keyence"],
    "DEU": ["SAP", "Siemens", "Infineon", "Deutsche Telekom"],
    "IND": ["TCS", "Infosys", "Reliance Jio", "HCL Tech", "Wipro"],
    "GBR": ["ARM Holdings", "BAE Systems", "Sage Group", "AstraZeneca"],
    "FRA": ["LVMH", "Dassault Systèmes", "Schneider Electric", "Capgemini"],
    "ITA": ["STMicroelectronics", "Leonardo", "Reply", "Prysmian"],
    "BRA": ["Nubank", "Embraer", "MercadoLibre BR", "Totvs"],
    "RUS": ["Yandex", "Kaspersky", "Sber Tech", "VK"],
}


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class CountryReport:
    iso3: str
    nome: str
    bandiera: str
    macro: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # ind -> {value, year}
    tasso_centrale: Optional[Dict[str, Any]] = None
    inflazione_recente: Optional[Dict[str, Any]] = None
    indice: Optional[Dict[str, Any]] = None
    valuta: Optional[Dict[str, Any]] = None
    bond_10y: Optional[Dict[str, Any]] = None
    militare: Dict[str, Any] = field(default_factory=dict)
    azionario_aggregato: Dict[str, Any] = field(default_factory=dict)
    rating: Dict[str, str] = field(default_factory=dict)
    geopolitica: Dict[str, Any] = field(default_factory=dict)
    tech_leaders: List[str] = field(default_factory=list)
    news: List[Dict[str, str]] = field(default_factory=list)


# ============================================================================
# UTILITY
# ============================================================================

def env_check() -> None:
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN_GEOMACRO")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID_GEOMACRO")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not FRED_API_KEY:
        log.warning("FRED_API_KEY mancante: tassi banche centrali saranno N/D")
    if not NEWSAPI_KEY:
        log.warning("NEWSAPI_KEY mancante: tenterò solo GDELT per le news")
    if missing:
        log.error("Variabili d'ambiente OBBLIGATORIE mancanti: %s", ", ".join(missing))
        sys.exit(2)


def safe_get(url: str, params: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None,
             timeout: int = HTTP_TIMEOUT) -> Optional[requests.Response]:
    """GET protetto con try/except: ritorna None su errore."""
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            log.warning("GET %s -> HTTP %s", url, r.status_code)
            return None
        return r
    except requests.RequestException as e:
        log.warning("GET %s -> Exception %s", url, e)
        return None


def fmt_number(x: Any, decimals: int = 2, suffix: str = "") -> str:
    """Formatta un numero in stile italiano con separatori, gestendo None/N/D."""
    if x is None or (isinstance(x, float) and (x != x)):
        return "N/D"
    try:
        if abs(float(x)) >= 1e12:
            return f"{float(x)/1e12:.{decimals}f}T{suffix}"
        if abs(float(x)) >= 1e9:
            return f"{float(x)/1e9:.{decimals}f}B{suffix}"
        if abs(float(x)) >= 1e6:
            return f"{float(x)/1e6:.{decimals}f}M{suffix}"
        return f"{float(x):,.{decimals}f}{suffix}".replace(",", "_").replace(".", ",").replace("_", ".")
    except (ValueError, TypeError):
        return str(x)


def fmt_pct(x: Any, decimals: int = 2) -> str:
    if x is None:
        return "N/D"
    try:
        return f"{float(x):+.{decimals}f}%"
    except (ValueError, TypeError):
        return "N/D"


def esc(s: Any) -> str:
    """Escape HTML per Telegram parse_mode=HTML."""
    return html.escape(str(s if s is not None else ""))


# ============================================================================
# STADIO 1 — WORLD BANK
# ============================================================================

def fetch_worldbank_indicator(iso3: str, indicator: str) -> Optional[Dict[str, Any]]:
    """Ritorna il valore non-nullo piu' recente disponibile per (paese, indicatore)."""
    url = WORLDBANK_BASE.format(iso3=iso3, ind=indicator)
    r = safe_get(url, params={"format": "json", "per_page": 10})
    if r is None:
        return None
    try:
        payload = r.json()
        if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
            return None
        for row in payload[1]:  # gia' ordinati dal piu' recente al piu' vecchio
            if row.get("value") is not None:
                return {"value": row["value"], "year": row.get("date")}
        return None
    except (ValueError, KeyError) as e:
        log.warning("Parsing WorldBank %s/%s: %s", iso3, indicator, e)
        return None


def collect_worldbank(reports: Dict[str, CountryReport]) -> None:
    log.info("🌍 Stadio 1: World Bank — %d paesi x %d indicatori",
             len(reports), len(WB_INDICATORS))
    for iso3, rep in reports.items():
        for ind_code, label in WB_INDICATORS.items():
            data = fetch_worldbank_indicator(iso3, ind_code)
            if data is not None:
                rep.macro[label] = data
        log.info("  ✓ %s: %d/%d indicatori macro", iso3,
                 len(rep.macro), len(WB_INDICATORS))


# ============================================================================
# STADIO 2 — FRED
# ============================================================================

def fetch_fred_series(series_id: str, limit: int = 1) -> Optional[Dict[str, Any]]:
    if not FRED_API_KEY:
        return None
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "limit": limit,
        "sort_order": "desc",
    }
    r = safe_get(FRED_BASE, params=params)
    if r is None:
        return None
    try:
        obs = r.json().get("observations", [])
        for o in obs:
            if o.get("value") not in (None, ".", ""):
                return {"value": float(o["value"]), "date": o.get("date")}
        return None
    except (ValueError, KeyError, TypeError) as e:
        log.warning("Parsing FRED %s: %s", series_id, e)
        return None


def collect_fred(reports: Dict[str, CountryReport]) -> None:
    log.info("🏦 Stadio 2: FRED — tassi banche centrali")
    for iso3, rep in reports.items():
        meta = FRED_SERIES.get(iso3, {})
        sid_tasso = meta.get("tasso")
        if sid_tasso:
            rep.tasso_centrale = fetch_fred_series(sid_tasso)
        sid_cpi = meta.get("cpi_yoy")
        if sid_cpi:
            rep.inflazione_recente = fetch_fred_series(sid_cpi)
        log.info("  ✓ %s: tasso=%s",
                 iso3, "OK" if rep.tasso_centrale else "N/D")


# ============================================================================
# STADIO 3 — YFINANCE: INDICI, VALUTE, BOND
# ============================================================================

def fetch_yf_perf(ticker: str) -> Optional[Dict[str, Any]]:
    """Scarica 13 mesi di prezzi e calcola perf 1M/3M/6M/1A + livello attuale."""
    if not ticker:
        return None
    try:
        df = yf.download(ticker, period="13mo", interval="1d",
                         progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        closes = df["Close"].dropna()
        if closes.empty:
            return None
        last = float(closes.iloc[-1])
        def perf_n_days(n: int) -> Optional[float]:
            if len(closes) <= n:
                return None
            past = float(closes.iloc[-(n + 1)])
            if past == 0:
                return None
            return (last / past - 1.0) * 100.0
        return {
            "ticker": ticker,
            "livello": last,
            "perf_1m": perf_n_days(21),
            "perf_3m": perf_n_days(63),
            "perf_6m": perf_n_days(126),
            "perf_1y": perf_n_days(252),
        }
    except Exception as e:
        log.warning("yfinance %s: %s", ticker, e)
        return None


def collect_markets(reports: Dict[str, CountryReport]) -> None:
    log.info("💹 Stadio 3: Yahoo Finance — indici, valute, bond")
    for iso3, rep in reports.items():
        tkr_idx = INDICI_TICKERS.get(iso3, "")
        if tkr_idx:
            rep.indice = fetch_yf_perf(tkr_idx)
            if rep.indice is None and iso3 == "RUS":
                rep.indice = {"ticker": tkr_idx, "nota": "non disponibile (sanzioni)"}
        tkr_fx = VALUTE_TICKERS.get(iso3, "")
        if tkr_fx:
            rep.valuta = fetch_yf_perf(tkr_fx)
        tkr_b = BOND_TICKERS.get(iso3, "")
        if tkr_b:
            rep.bond_10y = fetch_yf_perf(tkr_b)
        log.info("  ✓ %s: idx=%s fx=%s bond=%s",
                 iso3,
                 "OK" if rep.indice and rep.indice.get("livello") else "N/D",
                 "OK" if rep.valuta and rep.valuta.get("livello") else "N/D",
                 "OK" if rep.bond_10y and rep.bond_10y.get("livello") else "N/D")


# ============================================================================
# STADIO 4 — MILITARE (STATICO)
# ============================================================================

def collect_military(reports: Dict[str, CountryReport]) -> None:
    log.info("🛡️ Stadio 4: dati statici (militare/finanza/rating/geo/tech)")
    for iso3, rep in reports.items():
        rep.militare = DATI_MILITARI_STATICI.get(iso3, {})
        rep.azionario_aggregato = STOCK_MARKET_AGGREGATES.get(iso3, {})
        rep.rating = RATING_SOVRANO.get(iso3, {})
        rep.geopolitica = GEO_POSITIONING.get(iso3, {})
        rep.tech_leaders = TECH_LEADERS.get(iso3, [])


# ============================================================================
# STADIO 5 — NEWS (NEWSAPI + GDELT FALLBACK)
# ============================================================================

def fetch_newsapi(query: str, days: int = 7, page_size: int = 5) -> List[Dict[str, str]]:
    if not NEWSAPI_KEY:
        return []
    frm = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "q": query,
        "from": frm,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": NEWSAPI_KEY,
    }
    r = safe_get(NEWSAPI_BASE, params=params)
    if r is None:
        return []
    try:
        articles = r.json().get("articles", []) or []
        out = []
        for a in articles[:page_size]:
            out.append({
                "title": (a.get("title") or "").strip(),
                "source": ((a.get("source") or {}).get("name") or "").strip(),
                "date": (a.get("publishedAt") or "")[:10],
                "desc": (a.get("description") or "").strip()[:240],
                "url": a.get("url") or "",
            })
        return out
    except (ValueError, KeyError, TypeError) as e:
        log.warning("Parsing NewsAPI: %s", e)
        return []


def fetch_gdelt(query: str, page_size: int = 5) -> List[Dict[str, str]]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": page_size,
        "sort": "DateDesc",
        "timespan": "7d",
    }
    r = safe_get(GDELT_BASE, params=params)
    if r is None:
        return []
    try:
        articles = r.json().get("articles", []) or []
        out = []
        for a in articles[:page_size]:
            out.append({
                "title": (a.get("title") or "").strip(),
                "source": (a.get("domain") or "").strip(),
                "date": (a.get("seendate") or "")[:8],
                "desc": "",
                "url": a.get("url") or "",
            })
        return out
    except (ValueError, KeyError, TypeError) as e:
        log.warning("Parsing GDELT: %s", e)
        return []


def collect_news(reports: Dict[str, CountryReport]) -> None:
    log.info("📰 Stadio 5: news (NewsAPI + GDELT fallback)")
    for iso3, rep in reports.items():
        nome = COUNTRIES[iso3]["query"]
        query = f'"{nome}" AND (economy OR military OR politics OR election OR central bank)'
        news = fetch_newsapi(query, days=7, page_size=5)
        if not news:
            log.info("  ↪ %s: NewsAPI vuoto, provo GDELT", iso3)
            news = fetch_gdelt(query, page_size=5)
        rep.news = news
        log.info("  ✓ %s: %d notizie", iso3, len(news))


# ============================================================================
# STADIO 6 — GEMINI AI
# ============================================================================

def build_dossier(reports: Dict[str, CountryReport]) -> str:
    """Costruisce un dossier testuale compatto per il prompt Gemini."""
    parts: List[str] = []
    parts.append(f"DATA REPORT: {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC\n")
    for iso3, rep in reports.items():
        parts.append(f"\n=== {iso3} - {rep.nome} ===")
        # macro
        if rep.macro:
            macro_lines = []
            for k, v in rep.macro.items():
                if v and v.get("value") is not None:
                    macro_lines.append(f"{k}={v['value']:.2f} ({v.get('year','')})")
            if macro_lines:
                parts.append("MACRO: " + "; ".join(macro_lines))
        # tassi
        if rep.tasso_centrale and rep.tasso_centrale.get("value") is not None:
            parts.append(f"TASSO_BC: {rep.tasso_centrale['value']:.2f}% "
                         f"({rep.tasso_centrale.get('date','')})")
        # indice
        if rep.indice and rep.indice.get("livello") is not None:
            i = rep.indice
            parts.append(f"INDEX {i.get('ticker')}: livello={i['livello']:.2f}, "
                         f"1M={i.get('perf_1m')}, 3M={i.get('perf_3m')}, "
                         f"6M={i.get('perf_6m')}, 1Y={i.get('perf_1y')}")
        # valuta
        if rep.valuta and rep.valuta.get("livello") is not None:
            v = rep.valuta
            parts.append(f"FX {v.get('ticker')}: livello={v['livello']:.4f}, "
                         f"1Y={v.get('perf_1y')}")
        # bond
        if rep.bond_10y and rep.bond_10y.get("livello") is not None:
            b = rep.bond_10y
            parts.append(f"BOND10Y {b.get('ticker')}: livello={b['livello']:.2f}")
        # militare esteso
        if rep.militare:
            m = rep.militare
            parts.append(f"MIL: spesa={m.get('spesa_militare_usd_b')}B USD "
                         f"({m.get('spesa_militare_pct_pil')}% PIL, "
                         f"YoY {m.get('spesa_yoy_growth_pct')}%), "
                         f"personale={m.get('personale_attivo')}, "
                         f"portaerei={m.get('portaerei_operative')}, "
                         f"unita_superficie={m.get('unita_superficie_maggiori')}, "
                         f"caccia_5g={m.get('caccia_5g')}, "
                         f"testate_nuc={m.get('testate_nucleari')}, "
                         f"GFP_rank={m.get('global_firepower_rank')}, "
                         f"alleanze={','.join(m.get('alleanze', []))}, "
                         f"conflitti={','.join(m.get('conflitti_attivi', []))}")
        # azionario aggregato (richiesta esperto finanza)
        if rep.azionario_aggregato:
            a = rep.azionario_aggregato
            parts.append(f"EQUITY_AGG: mkt_cap={a.get('market_cap_usd_b')}B USD, "
                         f"ricavi={a.get('ricavi_aggregati_usd_b')}B, "
                         f"utili={a.get('utili_aggregati_usd_b')}B, "
                         f"PE={a.get('pe_ratio')}, "
                         f"div_yield={a.get('dividend_yield_pct')}%, "
                         f"listed={a.get('listed_companies')}")
        # rating sovrano
        if rep.rating:
            parts.append(f"RATING: S&P {rep.rating.get('sp','N/D')} "
                         f"({rep.rating.get('outlook','N/D')})")
        # geopolitica strutturale
        if rep.geopolitica:
            g = rep.geopolitica
            parts.append(f"GEO: partner={','.join(g.get('partner', []))}, "
                         f"energia_dip={g.get('energia_dip_pct')}%, "
                         f"sanzioni={g.get('sanzioni','N/D')}")
        # tech leaders
        if rep.tech_leaders:
            parts.append(f"TECH_LEADERS: {', '.join(rep.tech_leaders[:5])}")
        # news (titoli abbreviati)
        if rep.news:
            head = " | ".join(f"[{n.get('source','?')}] {n.get('title','')[:120]}"
                              for n in rep.news[:5])
            parts.append(f"NEWS: {head}")
    return "\n".join(parts)


GEMINI_PROMPT_TEMPLATE = """Sei un team di analisti senior composto da: macroeconomista, esperto militare, geopolitico, esperto tech, PM equity globale. Analizza il dossier sotto sfruttando TUTTI i blocchi dati (MACRO, EQUITY_AGG, RATING, MIL, GEO, TECH_LEADERS, NEWS) e produci un output STRETTAMENTE in JSON valido, in italiano, senza testo prima o dopo.

Schema JSON richiesto:
{{
  "executive_summary": "6-8 righe sullo stato globale: macro, polarizzazione geopolitica, divergenza mercati capitali, tendenze settimanali.",
  "schede_paesi": [
    {{"iso3": "USA", "scheda": "3-4 righe: situazione macro, salute listino (ricavi/utili/multipli), evento geopolitico chiave, outlook breve."}},
    ... una per ciascuno dei 10 paesi nell'ordine: USA, CHN, JPN, DEU, IND, GBR, FRA, ITA, BRA, RUS
  ],
  "tensioni_geopolitiche": [
    {{"titolo": "...", "severita": "basso|medio|alto", "impatto_mercati": "1-2 righe"}},
    ... esattamente 3 tensioni
  ],
  "opportunita_allocazione": [
    {{"idea": "es. Long Treasury 10Y", "tesi": "2 righe argomentate (cita multipli/rating/flussi/dipendenze quando rilevante)"}},
    ... esattamente 3 opportunità
  ],
  "rischi_sistemici": [
    "rischio 1 in 1-2 righe (geopolitico, finanziario, energetico, tech o militare)",
    "rischio 2 in 1-2 righe",
    "rischio 3 in 1-2 righe"
  ],
  "lente_finanziaria": "3-4 righe sul confronto cross-listino: quali mercati hanno ricavi/utili e multipli sostenibili, quali sono cari, quali value trap.",
  "lente_geopolitica": "3-4 righe sulle dipendenze strutturali (energia, partner commerciali, sanzioni) che spiegano il pricing dei rischi questa settimana.",
  "lente_tech": "3-4 righe su corsa tecnologica, R&D, campioni nazionali e implicazioni per i mercati."
}}

REGOLE:
- Output: SOLO JSON, nessun markdown, nessun blocco ```.
- Mantieni le righe brevi, niente bullet, niente formattazione interna.
- Se mancano dati su un paese, dillo nella scheda.
- Le 10 schede DEVONO essere presenti, una per paese, nell'ordine indicato.

DOSSIER:
{dossier}
"""


def _tolerant_json_parse(text: str) -> Optional[Dict[str, Any]]:
    """Parser JSON tollerante: rimuove ```...```, chiude virgolette/parentesi orfane."""
    if not text:
        return None
    s = text.strip()
    # rimuovi eventuali fence markdown
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # prova parse diretto
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # trova primo "{" e ultimo "}" plausibili
    start = s.find("{")
    end = s.rfind("}")
    if start == -1:
        return None
    candidate = s[start:end + 1] if end > start else s[start:]
    # bilancia parentesi e virgolette
    open_b = candidate.count("{") - candidate.count("}")
    open_sb = candidate.count("[") - candidate.count("]")
    open_q = candidate.count('"') % 2
    if open_q:
        candidate += '"'
    candidate += "]" * max(0, open_sb)
    candidate += "}" * max(0, open_b)
    # rimuovi virgole pendenti
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        log.warning("Tolerant JSON parse fallito: %s", e)
        return None


def call_gemini(prompt: str) -> Optional[Dict[str, Any]]:
    """Chiama Gemini con fallback su 3 modelli e retry esponenziale su 429."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 0.5,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    backoffs = [10, 30, 60]
    for model in GEMINI_MODELS:
        url = GEMINI_API.format(model=model)
        for attempt, wait_s in enumerate([0] + backoffs):
            if wait_s:
                log.info("⏳ Gemini %s: attesa %ss prima del retry %d",
                         model, wait_s, attempt)
                time.sleep(wait_s)
            try:
                r = requests.post(
                    url,
                    params={"key": GEMINI_API_KEY},
                    json=body,
                    timeout=GEMINI_TIMEOUT,
                )
            except requests.RequestException as e:
                log.warning("Gemini %s exception: %s", model, e)
                continue
            if r.status_code == 429:
                log.warning("Gemini %s: 429 quota", model)
                continue
            if r.status_code >= 400:
                log.warning("Gemini %s: HTTP %s body=%s",
                            model, r.status_code, r.text[:300])
                break  # passa al prossimo modello
            try:
                payload = r.json()
                cand = (payload.get("candidates") or [{}])[0]
                content = cand.get("content") or {}
                parts = content.get("parts") or []
                text = "".join(p.get("text", "") for p in parts)
                if not text:
                    log.warning("Gemini %s: risposta senza testo", model)
                    break
                parsed = _tolerant_json_parse(text)
                if parsed:
                    log.info("✅ Gemini %s: risposta JSON valida", model)
                    return parsed
                log.warning("Gemini %s: testo non parsabile", model)
                break
            except (ValueError, KeyError) as e:
                log.warning("Gemini %s parsing risposta: %s", model, e)
                break
    return None


def fallback_synthesis(reports: Dict[str, CountryReport]) -> Dict[str, Any]:
    """Se Gemini fallisce, costruiamo una sintesi minimale dai dati raw."""
    schede = []
    for iso3, rep in reports.items():
        bits = []
        g = rep.macro.get("gdp_growth_yoy_pct", {})
        i = rep.macro.get("inflazione_cpi_pct", {})
        u = rep.macro.get("disoccupazione_pct", {})
        if g.get("value") is not None:
            bits.append(f"GDP YoY {g['value']:.1f}% ({g.get('year')})")
        if i.get("value") is not None:
            bits.append(f"CPI {i['value']:.1f}% ({i.get('year')})")
        if u.get("value") is not None:
            bits.append(f"Disocc. {u['value']:.1f}%")
        if rep.indice and rep.indice.get("perf_1m") is not None:
            bits.append(f"Indice 1M {rep.indice['perf_1m']:+.1f}%")
        schede.append({"iso3": iso3, "scheda": "; ".join(bits) or "Dati limitati."})
    return {
        "executive_summary": "Sintesi AI non disponibile in questa esecuzione. "
                             "Vengono mostrati i dati grezzi per i 10 paesi.",
        "schede_paesi": schede,
        "tensioni_geopolitiche": [
            {"titolo": "Sintesi non disponibile", "severita": "medio",
             "impatto_mercati": "Consultare le news raw per dettagli."}
        ] * 3,
        "opportunita_allocazione": [
            {"idea": "Sintesi non disponibile",
             "tesi": "Modello AI non raggiungibile in questa esecuzione."}
        ] * 3,
        "rischi_sistemici": [
            "Sintesi AI non generata: consultare le sezioni dati grezzi."
        ] * 3,
        "lente_finanziaria": "Confronto cross-listino non generato (AI non disponibile).",
        "lente_geopolitica": "Analisi dipendenze strutturali non generata.",
        "lente_tech": "Analisi competitivita' tech non generata.",
    }


def run_gemini(reports: Dict[str, CountryReport]) -> Dict[str, Any]:
    log.info("🤖 Stadio 6: Gemini AI")
    dossier = build_dossier(reports)
    # tronca se troppo lungo (limite prudenziale a 30k caratteri di prompt)
    if len(dossier) > 30000:
        dossier = dossier[:30000] + "\n...[TRONCATO]"
    prompt = GEMINI_PROMPT_TEMPLATE.format(dossier=dossier)
    parsed = call_gemini(prompt)
    if parsed:
        return parsed
    log.warning("Gemini fallito su tutti i modelli: uso sintesi di fallback")
    return fallback_synthesis(reports)


# ============================================================================
# STADIO 7 — TELEGRAM
# ============================================================================

def telegram_send(text: str) -> bool:
    """Invia un singolo messaggio HTML su Telegram, con split automatico
    se supera la soglia massima."""
    chunks = []
    while text:
        if len(text) <= TELEGRAM_MAX_CHARS:
            chunks.append(text)
            break
        # split su newline piu' vicino al limite
        cut = text.rfind("\n", 0, TELEGRAM_MAX_CHARS)
        if cut <= 0:
            cut = TELEGRAM_MAX_CHARS
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    url = TELEGRAM_API.format(token=TELEGRAM_TOKEN)
    all_ok = True
    for ch in chunks:
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": ch,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=HTTP_TIMEOUT)
            if r.status_code >= 400:
                log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
                all_ok = False
            else:
                log.info("📤 Telegram: %d caratteri inviati", len(ch))
        except requests.RequestException as e:
            log.warning("Telegram exception: %s", e)
            all_ok = False
        time.sleep(0.5)  # rate-limit cortesia
    return all_ok


def build_msg1_header(synthesis: Dict[str, Any]) -> str:
    today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    lines = [
        f"🌐 <b>GeoMacro Overview</b> — {esc(today)}",
        "Report settimanale macro-geopolitico sui 10 paesi piu' rilevanti.",
        "",
        "📋 <b>Executive Summary</b>",
        esc(synthesis.get("executive_summary", "N/D")),
    ]
    return "\n".join(lines)


def build_msg2_macro_table(reports: Dict[str, CountryReport]) -> str:
    rows = ["🌍 <b>Tabella Macro (Top 10 Paesi)</b>",
            "<i>Fonte: World Bank + S&P (rating). Anno tra parentesi = ultimo dato.</i>",
            ""]
    for iso3, rep in reports.items():
        gdp = rep.macro.get("gdp_nominale_usd", {})
        gpc = rep.macro.get("gdp_per_capita_usd", {})
        growth = rep.macro.get("gdp_growth_yoy_pct", {})
        infl = rep.macro.get("inflazione_cpi_pct", {})
        unemp = rep.macro.get("disoccupazione_pct", {})
        debt = rep.macro.get("debito_pubblico_pct_pil", {})
        exp_m = rep.macro.get("export_merci_usd", {})
        imp_m = rep.macro.get("import_merci_usd", {})
        ca = rep.macro.get("conto_corrente_pct_pil", {})
        res = rep.macro.get("riserve_valutarie_usd", {})
        # saldo commerciale calcolato
        sb = None
        if exp_m.get("value") is not None and imp_m.get("value") is not None:
            sb = float(exp_m["value"]) - float(imp_m["value"])
        rows.append(
            f"{rep.bandiera} <b>{esc(rep.nome)}</b> ({iso3})\n"
            f"  GDP: {esc(fmt_number(gdp.get('value'), 2, ' USD'))} "
            f"({esc(gdp.get('year','-'))}) | "
            f"pro capite: {esc(fmt_number(gpc.get('value'), 0, ' USD'))}\n"
            f"  Crescita YoY: {esc(fmt_pct(growth.get('value'), 2))} | "
            f"CPI: {esc(fmt_pct(infl.get('value'), 2))} | "
            f"Disocc.: {esc(fmt_pct(unemp.get('value'), 1))}\n"
            f"  Debito/PIL: {esc(fmt_pct(debt.get('value'), 1))} | "
            f"Conto corr./PIL: {esc(fmt_pct(ca.get('value'), 1))}\n"
            f"  Saldo merci: {esc(fmt_number(sb, 1, ' USD') if sb is not None else 'N/D')} | "
            f"Riserve FX: {esc(fmt_number(res.get('value'), 1, ' USD'))}\n"
            f"  Rating S&P: <b>{esc(rep.rating.get('sp','N/D'))}</b> "
            f"({esc(rep.rating.get('outlook','-'))})"
        )
        rows.append("")
    return "\n".join(rows)


def build_msg3_finance_table(reports: Dict[str, CountryReport]) -> str:
    rows = ["💹 <b>Tabella Finanziaria (Top 10 Paesi)</b>",
            "<i>Indice, valuta, tasso BC, bond 10Y</i>",
            ""]
    for iso3, rep in reports.items():
        idx = rep.indice or {}
        fx = rep.valuta or {}
        bond = rep.bond_10y or {}
        tasso = rep.tasso_centrale or {}
        idx_line = "N/D"
        if idx.get("livello") is not None:
            idx_line = (f"{esc(idx.get('ticker'))} {esc(fmt_number(idx['livello'], 2))} "
                        f"| 1M {esc(fmt_pct(idx.get('perf_1m'), 1))} "
                        f"| 3M {esc(fmt_pct(idx.get('perf_3m'), 1))} "
                        f"| 1Y {esc(fmt_pct(idx.get('perf_1y'), 1))}")
        elif idx.get("nota"):
            idx_line = esc(idx["nota"])
        fx_line = "N/D"
        if fx.get("livello") is not None:
            fx_line = (f"{esc(fx.get('ticker'))} {esc(fmt_number(fx['livello'], 4))} "
                       f"| 1Y {esc(fmt_pct(fx.get('perf_1y'), 1))}")
        elif iso3 == "USA":
            fx_line = "USD (valuta base)"
        bond_line = "N/D"
        if bond.get("livello") is not None:
            bond_line = f"{esc(bond.get('ticker'))} {esc(fmt_number(bond['livello'], 2))}"
        tasso_line = "N/D"
        if tasso.get("value") is not None:
            tasso_line = f"{esc(fmt_pct(tasso['value'], 2))} ({esc(tasso.get('date','-'))})"
        rows.append(
            f"{rep.bandiera} <b>{esc(rep.nome)}</b>\n"
            f"  Indice: {idx_line}\n"
            f"  Valuta: {fx_line} | Tasso BC: {tasso_line}\n"
            f"  Bond 10Y: {bond_line}"
        )
        rows.append("")
    return "\n".join(rows)


def build_msg3b_equity_aggregates(reports: Dict[str, CountryReport]) -> str:
    """Nuovo: ricavi/utili/multipli aggregati del listino (richiesta esperto finanza)."""
    rows = ["📊 <b>Valore Economico dei Listini</b>",
            "<i>Mkt cap, ricavi e utili aggregati, P/E, dividend yield (FY2024)</i>",
            "<i>Fonti: WFE, S&P/MSCI, Bloomberg, Yardeni Research</i>",
            ""]
    for iso3, rep in reports.items():
        a = rep.azionario_aggregato or {}
        if not a:
            rows.append(f"{rep.bandiera} <b>{esc(rep.nome)}</b>: dati non disponibili")
            rows.append("")
            continue
        mc = a.get("market_cap_usd_b", 0)
        rv = a.get("ricavi_aggregati_usd_b", 0)
        ut = a.get("utili_aggregati_usd_b", 0)
        margine = (ut / rv * 100) if rv else None
        rows.append(
            f"{rep.bandiera} <b>{esc(rep.nome)}</b>\n"
            f"  Market cap: <b>{esc(fmt_number(mc * 1e9, 2, ' USD'))}</b> | "
            f"P/E: {esc(a.get('pe_ratio','N/D'))} | "
            f"Div yield: {esc(a.get('dividend_yield_pct','N/D'))}%\n"
            f"  Ricavi aggreg.: <b>{esc(fmt_number(rv * 1e9, 2, ' USD'))}</b> | "
            f"Utili aggreg.: <b>{esc(fmt_number(ut * 1e9, 2, ' USD'))}</b>\n"
            f"  Margine netto: {esc(f'{margine:.1f}%' if margine is not None else 'N/D')} | "
            f"Società quotate: {esc(a.get('listed_companies','N/D'))}"
        )
        rows.append("")
    return "\n".join(rows)


def build_msg4_military_table(reports: Dict[str, CountryReport]) -> str:
    rows = ["🛡️ <b>Tabella Militare (Top 10 Paesi)</b>",
            "<i>Fonti: SIPRI 2024, IISS Military Balance 2024, FAS 2025, GFP 2025</i>",
            ""]
    for iso3, rep in reports.items():
        m = rep.militare or {}
        conflitti = ", ".join(m.get("conflitti_attivi", [])) or "Nessuno significativo"
        rows.append(
            f"{rep.bandiera} <b>{esc(rep.nome)}</b> — GFP #{esc(m.get('global_firepower_rank','N/D'))}\n"
            f"  Spesa: <b>{esc(m.get('spesa_militare_usd_b','N/D'))}B USD</b> "
            f"({esc(m.get('spesa_militare_pct_pil','N/D'))}% PIL, "
            f"YoY {esc(fmt_pct(m.get('spesa_yoy_growth_pct'), 1))})\n"
            f"  Personale: {esc(fmt_number(m.get('personale_attivo'), 0))}\n"
            f"  Portaerei: {esc(m.get('portaerei_operative','N/D'))} | "
            f"Sottomarini: {esc(m.get('sottomarini','N/D'))} | "
            f"Unità superficie: {esc(m.get('unita_superficie_maggiori','N/D'))}\n"
            f"  Aerei combat.: {esc(m.get('aerei_combattimento','N/D'))} "
            f"(di cui 5ª gen: <b>{esc(m.get('caccia_5g','N/D'))}</b>)\n"
            f"  Testate nucleari: <b>{esc(m.get('testate_nucleari','N/D'))}</b>\n"
            f"  Alleanze: {esc(', '.join(m.get('alleanze', [])) or 'N/D')}\n"
            f"  ⚔️ Conflitti/operazioni: {esc(conflitti)}"
        )
        rows.append("")
    return "\n".join(rows)


def build_msg_tech(reports: Dict[str, CountryReport]) -> str:
    """Nuovo: blocco competitività tecnologica (richiesta esperto tech)."""
    rows = ["💻 <b>Competitività Tecnologica</b>",
            "<i>R&D/PIL, internet, export hi-tech (World Bank) + campioni nazionali</i>",
            ""]
    for iso3, rep in reports.items():
        rd = rep.macro.get("rd_spending_pct_pil", {})
        net = rep.macro.get("internet_users_pct", {})
        ht = rep.macro.get("high_tech_export_pct_manuf", {})
        leaders = ", ".join(rep.tech_leaders[:5]) if rep.tech_leaders else "N/D"
        rows.append(
            f"{rep.bandiera} <b>{esc(rep.nome)}</b>\n"
            f"  R&D/PIL: {esc(fmt_pct(rd.get('value'), 2))} "
            f"({esc(rd.get('year','-'))})\n"
            f"  Internet users: {esc(fmt_pct(net.get('value'), 1))} "
            f"| Hi-tech export: {esc(fmt_pct(ht.get('value'), 1))} "
            f"({esc(ht.get('year','-'))})\n"
            f"  🏆 Campioni: {esc(leaders)}"
        )
        rows.append("")
    return "\n".join(rows)


def build_msg_geopolitics(reports: Dict[str, CountryReport]) -> str:
    """Nuovo: posizionamento geopolitico strutturale (richiesta esperto geopolitica)."""
    rows = ["🌐 <b>Posizionamento Geopolitico Strutturale</b>",
            "<i>Partner commerciali, dipendenza energetica, sanzioni</i>",
            ""]
    for iso3, rep in reports.items():
        g = rep.geopolitica or {}
        partner = ", ".join(g.get("partner", [])) or "N/D"
        dip = g.get("energia_dip_pct")
        if dip is None:
            dip_str = "N/D"
        elif dip < 0:
            dip_str = f"<b>esportatore netto</b> ({abs(dip)}%)"
        else:
            dip_str = f"<b>importatore netto</b> ({dip}%)"
        rows.append(
            f"{rep.bandiera} <b>{esc(rep.nome)}</b>\n"
            f"  Top 3 partner: {esc(partner)}\n"
            f"  Energia: {dip_str}\n"
            f"  Sanzioni: {esc(g.get('sanzioni','N/D'))}"
        )
        rows.append("")
    return "\n".join(rows)


def build_msg5_country_cards(synthesis: Dict[str, Any],
                             reports: Dict[str, CountryReport]) -> str:
    rows = ["📑 <b>Schede Paese (sintesi AI)</b>", ""]
    schede = synthesis.get("schede_paesi", []) or []
    by_iso = {s.get("iso3"): s for s in schede if isinstance(s, dict)}
    for iso3, rep in reports.items():
        scheda = by_iso.get(iso3, {}).get("scheda", "Sintesi non disponibile.")
        rows.append(f"{rep.bandiera} <b>{esc(rep.nome)}</b>")
        rows.append(esc(scheda))
        rows.append("")
    return "\n".join(rows)


def build_msg6_tensions(synthesis: Dict[str, Any]) -> str:
    rows = ["⚔️ <b>Tensioni Geopolitiche Attive</b>", ""]
    tensioni = synthesis.get("tensioni_geopolitiche", []) or []
    sev_emoji = {"basso": "🟢", "medio": "🟡", "alto": "🔴"}
    for i, t in enumerate(tensioni[:3], start=1):
        if not isinstance(t, dict):
            continue
        sev = (t.get("severita") or "medio").lower()
        rows.append(f"{sev_emoji.get(sev, '⚪')} <b>{i}. {esc(t.get('titolo','N/D'))}</b>")
        rows.append(f"   Severità: {esc(sev.upper())}")
        rows.append(f"   Impatto mercati: {esc(t.get('impatto_mercati','N/D'))}")
        rows.append("")
    return "\n".join(rows)


def build_msg7_opps_risks(synthesis: Dict[str, Any]) -> str:
    rows = ["🎯 <b>Opportunità di Allocazione Macro</b>", ""]
    opps = synthesis.get("opportunita_allocazione", []) or []
    for i, o in enumerate(opps[:3], start=1):
        if not isinstance(o, dict):
            continue
        rows.append(f"<b>{i}. {esc(o.get('idea','N/D'))}</b>")
        rows.append(f"   Tesi: {esc(o.get('tesi','N/D'))}")
        rows.append("")
    rows.append("⚠️ <b>Rischi Sistemici da Monitorare</b>")
    rows.append("")
    rischi = synthesis.get("rischi_sistemici", []) or []
    for i, r in enumerate(rischi[:3], start=1):
        rows.append(f"<b>{i}.</b> {esc(r)}")
        rows.append("")
    return "\n".join(rows)


def build_msg_lenti(synthesis: Dict[str, Any]) -> str:
    """Nuovo: 3 lenti tematiche AI (finanza/geopolitica/tech)."""
    rows = ["🔎 <b>Lenti Tematiche (Sintesi AI)</b>", ""]
    rows.append("💰 <b>Lente Finanziaria</b>")
    rows.append(esc(synthesis.get("lente_finanziaria", "N/D")))
    rows.append("")
    rows.append("🌐 <b>Lente Geopolitica</b>")
    rows.append(esc(synthesis.get("lente_geopolitica", "N/D")))
    rows.append("")
    rows.append("💻 <b>Lente Tech</b>")
    rows.append(esc(synthesis.get("lente_tech", "N/D")))
    return "\n".join(rows)


def build_msg8_disclaimer() -> str:
    return (
        "ℹ️ <b>Disclaimer</b>\n"
        "Questo report e' generato automaticamente combinando dati di fonti "
        "pubbliche (World Bank, FRED, Yahoo Finance, SIPRI, IISS, FAS, GFP, "
        "NewsAPI/GDELT) e una sintesi AI tramite Google Gemini.\n"
        "Non costituisce consulenza finanziaria o di investimento. I dati "
        "macroeconomici possono avere 1-2 anni di ritardo. La sintesi AI puo' "
        "contenere errori: verifica sempre le fonti primarie prima di "
        "prendere decisioni operative.\n"
        "🤖 Generato il "
        f"{esc(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}"
    )


def send_report(reports: Dict[str, CountryReport],
                synthesis: Dict[str, Any]) -> None:
    log.info("📨 Stadio 7: invio Telegram (12 messaggi)")
    messaggi = [
        ("Header + Executive Summary", build_msg1_header(synthesis)),
        ("Tabella Macro", build_msg2_macro_table(reports)),
        ("Tabella Finanziaria", build_msg3_finance_table(reports)),
        ("Valore Economico Listini", build_msg3b_equity_aggregates(reports)),
        ("Tabella Militare", build_msg4_military_table(reports)),
        ("Posizionamento Geopolitico", build_msg_geopolitics(reports)),
        ("Competitività Tech", build_msg_tech(reports)),
        ("Schede Paese AI", build_msg5_country_cards(synthesis, reports)),
        ("Tensioni Geopolitiche", build_msg6_tensions(synthesis)),
        ("Lenti Tematiche AI", build_msg_lenti(synthesis)),
        ("Opportunità + Rischi", build_msg7_opps_risks(synthesis)),
        ("Disclaimer", build_msg8_disclaimer()),
    ]
    for label, body in messaggi:
        log.info("  → invio: %s (%d char)", label, len(body))
        telegram_send(body)


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    log.info("🚀 GeoMacro Overview Bot — start")
    env_check()

    # Inizializza report contenitori
    reports: Dict[str, CountryReport] = {
        iso3: CountryReport(iso3=iso3, nome=meta["nome"], bandiera=meta["bandiera"])
        for iso3, meta in COUNTRIES.items()
    }

    # Pipeline
    try:
        collect_worldbank(reports)
    except Exception as e:
        log.error("Stadio 1 World Bank: errore non gestito %s", e)
    try:
        collect_fred(reports)
    except Exception as e:
        log.error("Stadio 2 FRED: errore non gestito %s", e)
    try:
        collect_markets(reports)
    except Exception as e:
        log.error("Stadio 3 Markets: errore non gestito %s", e)
    try:
        collect_military(reports)
    except Exception as e:
        log.error("Stadio 4 Military: errore non gestito %s", e)
    try:
        collect_news(reports)
    except Exception as e:
        log.error("Stadio 5 News: errore non gestito %s", e)

    try:
        synthesis = run_gemini(reports)
    except Exception as e:
        log.error("Stadio 6 Gemini: errore non gestito %s", e)
        synthesis = fallback_synthesis(reports)

    try:
        send_report(reports, synthesis)
    except Exception as e:
        log.error("Stadio 7 Telegram: errore non gestito %s", e)
        return 1

    log.info("✅ GeoMacro Overview Bot — completato")
    return 0


if __name__ == "__main__":
    sys.exit(main())
