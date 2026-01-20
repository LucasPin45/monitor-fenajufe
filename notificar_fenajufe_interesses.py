#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notificar_fenajufe_interesses.py
========================================
Monitor de Interesses (FENAJUFE) - Notificações automatizadas
Inspirado na lógica do Monitor Zanatta:
- Email só recebe: matches encontrados + resumo do dia
- Telegram recebe tudo (bom dia, sem novidades, matches, resumo)

Uso (via env var):
  MODO_EXECUCAO=bom_dia | varredura | resumo
"""

import os
import json
import re
import html
import time
import ssl
import smtplib
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Any, Tuple, Optional

import requests

# =========================
# TIMEZONE / API
# =========================
TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")
API_CAMARA_BASE = "https://dadosabertos.camara.leg.br/api/v2"
HEADERS = {"User-Agent": "MonitorInteresses-FENAJUFE/1.0"}

# =========================
# LINKS / CANAIS
# =========================
LINK_PAINEL = os.getenv("LINK_PAINEL", "https://monitor-fenajufe.streamlit.app/")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

EMAIL_SMTP_SERVER = os.getenv("EMAIL_SMTP_SERVER", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECIPIENTS = os.getenv("EMAIL_RECIPIENTS", "")

NOTIFICAR_TELEGRAM = os.getenv("NOTIFICAR_TELEGRAM", "true").lower() == "true"
NOTIFICAR_EMAIL = os.getenv("NOTIFICAR_EMAIL", "true").lower() == "true"

MODO_EXECUCAO = os.getenv("MODO_EXECUCAO", "varredura").lower()

# =========================
# CONFIG TOML
# =========================
CONFIG_TOML_PATH = os.getenv("CONFIG_TOML_PATH", "config_fenajufe.toml")

# =========================
# JANELA DE VARREDURA
# =========================
DIAS_BUSCA = int(os.getenv("DIAS_BUSCA", "7"))          # proposições apresentadas nos últimos N dias
LIMITE_POR_TIPO = int(os.getenv("LIMITE_POR_TIPO", "50"))
TIPOS_MONITORADOS = os.getenv("TIPOS_MONITORADOS", "PL,PLP,PEC,MPV,PDL").split(",")

# =========================
# ARQUIVOS DE ESTADO
# =========================
ESTADO_FILE = Path(os.getenv("ESTADO_FILE", "estado_fenajufe.json"))
HISTORICO_FILE = Path(os.getenv("HISTORICO_FILE", "historico_fenajufe.json"))
RESUMO_DIA_FILE = Path(os.getenv("RESUMO_DIA_FILE", "resumo_dia_fenajufe.json"))

# =========================
# UTIL
# =========================
def now_bsb() -> datetime:
    return datetime.now(TZ_BRASILIA)

def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    nfkd = unicodedata.normalize("NFD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return no_accents.lower().strip()

def safe_get(d: Dict, path: List[str], default=""):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

def load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return default

def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# =========================
# TOML LOADER (py>=3.11)
# =========================
def load_toml(path: str) -> Dict[str, Any]:
    import tomllib
    with open(path, "rb") as f:
        return tomllib.load(f)

def parse_config_fenajufe(toml_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Espera:
      - cliente.temas.<id>.nome
      - cliente.temas.<id>.palavras
      - cliente.temas.<id>.peso
      - cliente.exclusoes = [...]
    E também aceita "palavras" e "peso" no topo (se você usar isso).
    """
    cliente = toml_data.get("cliente", {})
    temas_raw = cliente.get("temas", {}) or {}

    temas = {}
    tema_pesos = {}
    for tema_id, obj in temas_raw.items():
        nome = (obj.get("nome") or tema_id).strip()
        palavras = obj.get("palavras", []) or []
        peso = int(obj.get("peso", 10))
        temas[nome] = [normalize_text(p) for p in palavras]
        tema_pesos[nome] = peso

    exclusoes = [normalize_text(x) for x in (cliente.get("exclusoes", []) or [])]

    # fallback opcional (se você tiver "palavras = [...]" no topo)
    palavras_topo = toml_data.get("palavras", []) or []
    peso_topo = int(toml_data.get("peso", 10))
    palavras_principais = [normalize_text(p) for p in palavras_topo]

    return {
        "temas": temas,
        "tema_pesos": tema_pesos,
        "exclusoes": exclusoes,
        "palavras_principais": palavras_principais,
        "peso_topo": peso_topo,
    }

# =========================
# CÂMARA: BUSCA PROPOSIÇÕES
# =========================
def buscar_proposicoes_periodo(data_inicio: str, data_fim: str, tipos: List[str]) -> List[Dict[str, Any]]:
    todas = []
    for tipo in tipos:
        params = {
            "siglaTipo": tipo.strip(),
            "dataInicio": data_inicio,
            "dataFim": data_fim,
            "ordem": "DESC",
            "ordenarPor": "id",
            "itens": min(LIMITE_POR_TIPO, 100),
        }
        try:
            r = requests.get(f"{API_CAMARA_BASE}/proposicoes", params=params, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                data = r.json()
                todas.extend(data.get("dados", [])[:LIMITE_POR_TIPO])
        except:
            pass
        time.sleep(0.15)
    return todas

def fetch_proposicao_detalhes(proposicao_id: str) -> Dict[str, Any]:
    try:
        r = requests.get(f"{API_CAMARA_BASE}/proposicoes/{proposicao_id}", headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json().get("dados", {}) or {}
    except:
        pass
    return {}

def fetch_status_proposicao(proposicao_id: str) -> Dict[str, str]:
    det = fetch_proposicao_detalhes(proposicao_id)
    status = det.get("statusProposicao", {}) or {}
    return {
        "situacao": status.get("descricaoSituacao", "") or "",
        "siglaOrgao": status.get("siglaOrgao", "") or "",
        "dataHora": status.get("dataHora", "") or "",
        "despacho": status.get("despacho", "") or "",
        "regime": status.get("regime", "") or "",
        "relator": status.get("nomeRelator", "") or "",
    }

def format_sigla_num_ano(sigla: str, numero: Any, ano: Any) -> str:
    sigla = (sigla or "").strip()
    numero = (str(numero) or "").strip()
    ano = (str(ano) or "").strip()
    if sigla and numero and ano:
        return f"{sigla} {numero}/{ano}"
    return ""

# =========================
# MATCHING (temas + palavras topo + exclusões)
# =========================
def calcular_match(proposicao: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ementa = normalize_text(proposicao.get("ementa", ""))
    keywords = normalize_text(proposicao.get("keywords", ""))
    descricao = normalize_text(proposicao.get("descricaoTipo", ""))
    texto = f"{ementa} {keywords} {descricao}"

    # exclusões
    for exc in cfg["exclusoes"]:
        if exc and exc in texto:
            return None

    palavras_match = []
    for p in cfg["palavras_principais"]:
        if p and p in texto:
            palavras_match.append(p)

    temas_match = []
    for tema, palavras in cfg["temas"].items():
        for p in palavras:
            if p and p in texto:
                temas_match.append(tema)
                break

    if not palavras_match and not temas_match:
        return None

    # score (simples e explicável)
    score = 0.0
    score += min(len(palavras_match) * float(cfg["peso_topo"]), 40.0)

    # temas ponderados por peso
    for tema in set(temas_match):
        peso = float(cfg["tema_pesos"].get(tema, 10))
        score += min(peso, 15.0)  # trava para não explodir

    score = min(score, 100.0)

    # nível (heurística)
    # - se a situação sugerir pauta/ordem do dia, sobe para CRÍTICO
    situacao = normalize_text(safe_get(proposicao, ["statusProposicao", "descricaoSituacao"], ""))
    regime = normalize_text(safe_get(proposicao, ["statusProposicao", "regime"], ""))
    if any(x in situacao for x in ["ordem do dia", "em pauta", "incluida na ordem do dia", "matéria em votação"]):
        nivel = "CRITICO"
    elif "urgencia" in regime or score >= 70:
        nivel = "ALTO"
    elif score >= 50:
        nivel = "MEDIO"
    else:
        nivel = "BAIXO"

    return {
        "proposicao_id": str(proposicao.get("id", "")),
        "temas_match": sorted(list(set(temas_match))),
        "palavras_match": sorted(list(set(palavras_match)))[:10],
        "score": score,
        "nivel": nivel,
    }

# =========================
# FORMATAÇÃO (Telegram + Email)
# =========================
def trunc(texto: str, n: int = 220) -> str:
    if not texto:
        return ""
    t = str(texto).strip()
    return t if len(t) <= n else (t[:n] + "...")

def emoji_nivel(nivel: str) -> str:
    return {"CRITICO": "🚨", "ALTO": "⚠️", "MEDIO": "🔔", "BAIXO": "📋"}.get(nivel, "ℹ️")

def formatar_alerta_match(match: Dict[str, Any], prop: Dict[str, Any], status: Dict[str, str]) -> str:
    ident = format_sigla_num_ano(prop.get("siglaTipo", ""), prop.get("numero", ""), prop.get("ano", ""))
    ementa = trunc(prop.get("ementa", ""), 240)
    orgao = status.get("siglaOrgao") or safe_get(prop, ["statusProposicao", "siglaOrgao"], "")
    situacao = status.get("situacao") or safe_get(prop, ["statusProposicao", "descricaoSituacao"], "Em tramitação")
    relator = status.get("relator") or safe_get(prop, ["statusProposicao", "nomeRelator"], "")

    temas_txt = ", ".join(match["temas_match"]) if match["temas_match"] else "Palavras-chave (geral)"
    palavras_txt = ", ".join(match["palavras_match"]) if match["palavras_match"] else "-"

    link = f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={match['proposicao_id']}"

    return f"""{emoji_nivel(match["nivel"])} <b>FENAJUFE | {match["nivel"]}</b>

<b>{html.escape(ident)}</b>
<i>{html.escape(ementa)}</i>

<b>Status:</b> {html.escape(situacao)}
<b>Órgão:</b> {html.escape(orgao)}
<b>Relator(a):</b> {html.escape(relator or "—")}
<b>Temas:</b> {html.escape(temas_txt)}
<b>Gatilhos:</b> {html.escape(palavras_txt)}
<b>Relevância:</b> {match["score"]:.0f}/100

🔗 <a href="{link}">Ver tramitação</a>
🖥️ <a href="{LINK_PAINEL}">Abrir painel</a>
"""

def formatar_mensagem_bom_dia() -> str:
    data = now_bsb().strftime("%d/%m/%Y")
    return f"""☀️ <b>Bom dia! Monitor FENAJUFE</b>
<i>{data} — Varredura automática ativada.</i>

• Telegram recebe status e alertas em tempo real
• Email recebe somente quando houver match + resumo do dia

🖥️ <a href="{LINK_PAINEL}">Abrir painel</a>
"""

def formatar_sem_novidades_completa() -> str:
    hora = now_bsb().strftime("%H:%M")
    return f"""✅ <b>Monitor FENAJUFE</b>
Sem novos matches nesta varredura ({hora}).

Isso é bom: nada novo bateu nos temas/palavras configurados.
🖥️ <a href="{LINK_PAINEL}">Abrir painel</a>
"""

def formatar_sem_novidades_curta() -> str:
    hora = now_bsb().strftime("%H:%M")
    return f"✅ Monitor FENAJUFE: sem novidades ({hora})."

def formatar_resumo_dia(matches_enviados: List[str]) -> str:
    data = now_bsb().strftime("%d/%m/%Y")
    total = len(matches_enviados)
    if total == 0:
        corpo = "Nenhum match foi detectado hoje."
    else:
        # lista curta
        itens = "\n".join([f"• {html.escape(x)}" for x in matches_enviados[:12]])
        extra = f"\n… +{total-12} item(ns)" if total > 12 else ""
        corpo = f"<b>Matches do dia ({total}):</b>\n{itens}{extra}"

    return f"""🌙 <b>Resumo do Dia — FENAJUFE ({data})</b>

{corpo}

🖥️ <a href="{LINK_PAINEL}">Abrir painel</a>
"""

# =========================
# EMAIL (HTML)
# =========================
def extrair_texto_plano(mensagem_telegram_html: str) -> str:
    texto = re.sub(r"<[^>]+>", "", mensagem_telegram_html)
    texto = texto.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return texto

def telegram_para_email_html(mensagem_telegram_html: str, assunto: str) -> str:
    # Layout simples, inspirado no Zanatta (botão + rodapé)
    return f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(assunto)}</title>
</head>
<body style="margin:0;padding:0;background:#f2f4f7;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f7;padding:24px 0;">
    <tr>
      <td align="center">
        <table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e6e9ef;">
          <tr>
            <td style="background:#1a365d;color:#fff;padding:18px 22px;">
              <div style="font-size:16px;font-weight:700;">Monitor de Interesses — FENAJUFE</div>
              <div style="font-size:12px;opacity:.9;margin-top:4px;">Notificação automática</div>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 22px;color:#111;font-size:14px;line-height:1.45;">
              {mensagem_telegram_html}
              <div style="margin-top:18px;text-align:center;">
                <a href="{LINK_PAINEL}" style="display:inline-block;background:#2f7d32;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:700;">
                  🖥️ Abrir Painel
                </a>
              </div>
            </td>
          </tr>
          <tr>
            <td style="background:#f8f9fb;padding:14px 22px;color:#6b7280;font-size:12px;text-align:center;border-top:1px solid #eef0f4;">
              Painel: <a href="{LINK_PAINEL}" style="color:#1a365d;text-decoration:none;">{html.escape(LINK_PAINEL)}</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

def enviar_email(mensagem_telegram_html: str, assunto: str) -> bool:
    if not (EMAIL_SENDER and EMAIL_PASSWORD and EMAIL_RECIPIENTS):
        print("⏭️ Email: configuração incompleta")
        return False

    recipients = [e.strip() for e in EMAIL_RECIPIENTS.split(",") if e.strip()]
    if not recipients:
        print("⏭️ Email: sem destinatários")
        return False

    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = f"Monitor FENAJUFE <{EMAIL_SENDER}>"
    msg["To"] = ", ".join(recipients)

    texto_plano = extrair_texto_plano(mensagem_telegram_html) + f"\n\n---\nPainel: {LINK_PAINEL}"
    msg.attach(MIMEText(texto_plano, "plain", "utf-8"))

    html_email = telegram_para_email_html(mensagem_telegram_html, assunto)
    msg.attach(MIMEText(html_email, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        print(f"✅ Email: enviado ({len(recipients)} destinatário(s))")
        return True
    except smtplib.SMTPAuthenticationError:
        print("❌ Email: falha na autenticação")
        return False
    except Exception as e:
        print(f"❌ Email: erro: {e}")
        return False

def enviar_telegram(mensagem_html: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("⏭️ Telegram: credenciais faltando")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("✅ Telegram: enviado")
        return True
    except Exception as e:
        print(f"❌ Telegram: erro: {e}")
        return False

def notificar_telegram_apenas(mensagem_html: str) -> bool:
    if NOTIFICAR_TELEGRAM:
        return enviar_telegram(mensagem_html)
    print("⏭️ Telegram: desabilitado")
    return False

def notificar_ambos(mensagem_html: str, assunto: str) -> bool:
    resultados = []
    if NOTIFICAR_TELEGRAM:
        resultados.append(enviar_telegram(mensagem_html))
    else:
        print("⏭️ Telegram: desabilitado")

    if NOTIFICAR_EMAIL:
        resultados.append(enviar_email(mensagem_html, assunto))
    else:
        print("⏭️ Email: desabilitado")

    return any(resultados)

# =========================
# LÓGICA DE DEDUPE / RESUMO
# =========================
def carregar_estado():
    return load_json(ESTADO_FILE, {"ultima_novidade": True})

def salvar_estado(tever_novidade: bool):
    save_json(ESTADO_FILE, {"ultima_novidade": bool(tever_novidade)})

def carregar_historico():
    return load_json(HISTORICO_FILE, {"notificados": {}})

def salvar_historico(h):
    save_json(HISTORICO_FILE, h)

def chave_notificacao(proposicao_id: str, datahora_status: str) -> str:
    # dedupe por proposição + timestamp do status
    return f"{proposicao_id}::{datahora_status or 'sem_data'}"

def carregar_resumo_dia():
    return load_json(RESUMO_DIA_FILE, {"data": "", "itens": []})

def salvar_resumo_dia(r):
    save_json(RESUMO_DIA_FILE, r)

def inicializar_resumo_dia():
    hoje = now_bsb().strftime("%Y-%m-%d")
    salvar_resumo_dia({"data": hoje, "itens": []})

def adicionar_ao_resumo(sigla: str):
    resumo = carregar_resumo_dia()
    hoje = now_bsb().strftime("%Y-%m-%d")
    if resumo.get("data") != hoje:
        resumo = {"data": hoje, "itens": []}
    if sigla not in resumo["itens"]:
        resumo["itens"].append(sigla)
    salvar_resumo_dia(resumo)

# =========================
# MODOS
# =========================
def executar_bom_dia():
    print("☀️ MODO: BOM DIA")
    inicializar_resumo_dia()
    notificar_telegram_apenas(formatar_mensagem_bom_dia())
    print("✅ Bom dia enviado (Telegram).")

def executar_resumo():
    print("🌙 MODO: RESUMO DO DIA")
    resumo = carregar_resumo_dia()
    itens = resumo.get("itens", [])
    msg = formatar_resumo_dia(itens)
    notificar_ambos(msg, "🌙 Monitor FENAJUFE — Resumo do Dia")
    print("✅ Resumo enviado (Telegram + Email).")

def executar_varredura():
    print("🔍 MODO: VARREDURA")
    estado = carregar_estado()
    historico = carregar_historico()

    # garantir resumo do dia atualizado
    resumo = carregar_resumo_dia()
    hoje = now_bsb().strftime("%Y-%m-%d")
    if resumo.get("data") != hoje:
        inicializar_resumo_dia()

    # carregar config
    toml_data = load_toml(CONFIG_TOML_PATH)
    cfg = parse_config_fenajufe(toml_data)

    fim = now_bsb().date()
    ini = (now_bsb() - timedelta(days=DIAS_BUSCA)).date()
    data_inicio = ini.strftime("%Y-%m-%d")
    data_fim = fim.strftime("%Y-%m-%d")

    print(f"📅 Período: {data_inicio} → {data_fim}")
    props = buscar_proposicoes_periodo(data_inicio, data_fim, TIPOS_MONITORADOS)
    print(f"📦 Proposições coletadas: {len(props)}")

    novos_alertas = 0

    for p in props:
        pid = str(p.get("id", ""))
        if not pid:
            continue

        # enriquecer com detalhes (para ter status/ementa completos)
        det = fetch_proposicao_detalhes(pid)
        if det:
            p = det

        match = calcular_match(p, cfg)
        if not match:
            continue

        status = fetch_status_proposicao(pid)
        datahora = status.get("dataHora", "") or safe_get(p, ["statusProposicao", "dataHora"], "")
        key = chave_notificacao(pid, datahora)

        if key in historico["notificados"]:
            continue

        # registrar histórico antes de enviar (reduz risco de duplicar em falha parcial)
        historico["notificados"][key] = {
            "ts": now_bsb().isoformat(),
            "proposicao": format_sigla_num_ano(p.get("siglaTipo", ""), p.get("numero", ""), p.get("ano", "")),
            "nivel": match["nivel"],
            "score": match["score"],
        }
        salvar_historico(historico)

        sigla = historico["notificados"][key]["proposicao"] or f"ID {pid}"
        msg = formatar_alerta_match(match, p, status)
        assunto = f"{emoji_nivel(match['nivel'])} FENAJUFE | Match: {sigla}"

        # match = Telegram + Email
        notificar_ambos(msg, assunto)
        adicionar_ao_resumo(sigla)

        novos_alertas += 1
        time.sleep(1)

    if novos_alertas == 0:
        # sem novidades = APENAS Telegram, alternando completa/curta igual Zanatta
        if estado.get("ultima_novidade", True):
            notificar_telegram_apenas(formatar_sem_novidades_completa())
        else:
            notificar_telegram_apenas(formatar_sem_novidades_curta())

    salvar_estado(novos_alertas > 0)
    print(f"✅ Varredura concluída. Novos alertas: {novos_alertas}")

# =========================
# MAIN
# =========================
def main():
    print("=" * 72)
    print("🤖 MONITOR FENAJUFE — Notificações (Interesses)")
    print("=" * 72)

    print("📡 CANAIS:")
    print(f"   Telegram: {'ON' if NOTIFICAR_TELEGRAM else 'OFF'}")
    print(f"   Email:    {'ON' if NOTIFICAR_EMAIL else 'OFF'}")
    print(f"🧭 MODO: {MODO_EXECUCAO}")
    print(f"📄 Config: {CONFIG_TOML_PATH}")
    print()

    if MODO_EXECUCAO == "bom_dia":
        executar_bom_dia()
    elif MODO_EXECUCAO == "resumo":
        executar_resumo()
    else:
        executar_varredura()

if __name__ == "__main__":
    main()
