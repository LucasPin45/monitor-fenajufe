#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notificar_fenajufe_interesses.py v2
========================================
Monitor de Interesses (FENAJUFE) - Notificações automatizadas

Melhorias v2:
- Score mínimo para evitar falsos positivos
- Formato de mensagem melhorado (estilo Monitor Parlamentar)
- Mais exclusões para evitar lixo (radiodifusão, homenagens, etc.)
- Horário nas mensagens
- Filtro de situações fora de tramitação
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
API_SENADO_BASE = "https://legis.senado.leg.br/dadosabertos"
HEADERS = {"User-Agent": "MonitorInteresses-FENAJUFE/3.0"}
SENADO_HEADERS = {"Accept": "application/json", "User-Agent": "MonitorInteresses-FENAJUFE/3.0"}

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
DIAS_BUSCA = int(os.getenv("DIAS_BUSCA", "7"))
LIMITE_POR_TIPO = int(os.getenv("LIMITE_POR_TIPO", "50"))
TIPOS_MONITORADOS = os.getenv("TIPOS_MONITORADOS", "PL,PLP,PEC,MPV,PDL").split(",")

# =========================
# SCORE MÍNIMO PARA NOTIFICAR
# =========================
SCORE_MINIMO = int(os.getenv("SCORE_MINIMO", "25"))  # Evita falsos positivos

# =========================
# ARQUIVOS DE ESTADO
# =========================
ESTADO_FILE = Path(os.getenv("ESTADO_FILE", "estado_fenajufe.json"))
HISTORICO_FILE = Path(os.getenv("HISTORICO_FILE", "historico_fenajufe.json"))
RESUMO_DIA_FILE = Path(os.getenv("RESUMO_DIA_FILE", "resumo_dia_fenajufe.json"))

# =========================
# EXCLUSÕES GLOBAIS (evitar lixo)
# =========================
EXCLUSOES_GLOBAIS = [
    # Radiodifusão e telecomunicações
    "radiodifusao", "radio difusora", "emissora de radio", "frequencia modulada",
    "onda media", "radio comunitaria", "televisao", "tv ",
    # Homenagens e datas comemorativas
    "denomina", "denominacao", "homenagem", "dia nacional", "dia municipal",
    "patrono", "patrona", "titulo de cidadao", "medalha", "comenda",
    # Utilidade pública
    "utilidade publica", "declara de utilidade",
    # Nomes de ruas/logradouros
    "rodovia", "aeroporto", "ponte", "viaduto", "praca",
]

# =========================
# SITUAÇÕES FORA DE TRAMITAÇÃO
# =========================
SITUACOES_ENCERRADAS = [
    "arquivada", "arquivado",
    "transformada em norma", "transformado em norma",
    "transformada em lei", "transformado em lei",
    "perdeu a eficacia", "perda de eficacia",
    "retirada pelo autor", "retirado pelo autor",
    "prejudicada", "prejudicado",
    "rejeitada", "rejeitado",
    "vetado totalmente",
    "devolvida ao autor", "devolvido ao autor",
]

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

    # Exclusões do TOML + globais
    exclusoes_toml = [normalize_text(x) for x in (cliente.get("exclusoes", []) or [])]
    exclusoes = list(set(exclusoes_toml + EXCLUSOES_GLOBAIS))

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
                props = data.get("dados", [])[:LIMITE_POR_TIPO]
                for p in props:
                    p["casa"] = "CAMARA"
                todas.extend(props)
        except:
            pass
        time.sleep(0.15)
    return todas

# =========================
# SENADO: BUSCA MATÉRIAS
# =========================
TIPOS_SENADO = ["PLS", "PEC", "MPV", "PLC"]

def buscar_materias_senado_periodo(data_inicio: str, data_fim: str, tipos: List[str] = None) -> List[Dict[str, Any]]:
    """Busca matérias do Senado e normaliza para formato compatível com Câmara"""
    if tipos is None:
        tipos = TIPOS_SENADO
    
    materias = []
    
    try:
        dt_inicio = datetime.strptime(data_inicio, "%Y-%m-%d")
        dt_fim = datetime.strptime(data_fim, "%Y-%m-%d")
    except:
        return materias
    
    for ano in range(dt_inicio.year, dt_fim.year + 1):
        for tipo in tipos:
            try:
                url = f"{API_SENADO_BASE}/materia/pesquisa/lista"
                params = {"sigla": tipo, "ano": ano, "tramitando": "S"}
                
                r = requests.get(url, params=params, headers=SENADO_HEADERS, timeout=30)
                
                if r.status_code == 200:
                    data = r.json()
                    pesquisa = data.get("PesquisaBasica", {})
                    mats = pesquisa.get("Materias", {})
                    
                    if mats:
                        lista = mats.get("Materia", [])
                        if isinstance(lista, dict):
                            lista = [lista]
                        
                        for mat in lista[:LIMITE_POR_TIPO]:
                            # Verificar data
                            data_apres = mat.get("DataApresentacao", "")
                            if data_apres:
                                try:
                                    dt = datetime.strptime(data_apres, "%Y-%m-%d")
                                    if dt.date() < dt_inicio.date() or dt.date() > dt_fim.date():
                                        continue
                                except:
                                    pass
                            
                            # Normalizar
                            norm = normalizar_materia_senado(mat)
                            if norm:
                                materias.append(norm)
                
            except:
                pass
            time.sleep(0.2)
    
    return materias[:LIMITE_POR_TIPO]

def normalizar_materia_senado(mat: Dict) -> Optional[Dict]:
    """Normaliza matéria do Senado para formato compatível com Câmara"""
    try:
        codigo = mat.get("Codigo", "") or mat.get("CodigoMateria", "")
        sigla = mat.get("Sigla", "") or mat.get("SiglaSubtipoMateria", "")
        numero = mat.get("Numero", "") or mat.get("NumeroMateria", "")
        ano = mat.get("Ano", "") or mat.get("AnoMateria", "")
        ementa = mat.get("Ementa", "") or mat.get("EmentaMateria", "")
        
        situacao_info = mat.get("SituacaoAtual", {})
        if isinstance(situacao_info, dict):
            situacao = situacao_info.get("Descricao", "Em tramitação")
            local = situacao_info.get("Local", "")
        else:
            situacao = "Em tramitação"
            local = ""
        
        return {
            "id": f"SF-{codigo}",
            "siglaTipo": sigla,
            "numero": numero,
            "ano": ano,
            "ementa": ementa,
            "dataApresentacao": mat.get("DataApresentacao", ""),
            "keywords": mat.get("IndexacaoMateria", ""),
            "casa": "SENADO",
            "statusProposicao": {
                "descricaoSituacao": situacao,
                "siglaOrgao": local,
                "dataHora": mat.get("DataUltimaAtualizacao", "")
            }
        }
    except:
        return None

def buscar_proposicoes_ambas_casas(data_inicio: str, data_fim: str, tipos_camara: List[str]) -> List[Dict[str, Any]]:
    """Busca proposições da Câmara e Senado"""
    todas = []
    
    # Câmara
    props_camara = buscar_proposicoes_periodo(data_inicio, data_fim, tipos_camara)
    todas.extend(props_camara)
    
    # Senado
    props_senado = buscar_materias_senado_periodo(data_inicio, data_fim)
    todas.extend(props_senado)
    
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
# MATCHING (com validações extras)
# =========================
def calcular_match(proposicao: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ementa = normalize_text(proposicao.get("ementa", ""))
    keywords = normalize_text(proposicao.get("keywords", ""))
    descricao = normalize_text(proposicao.get("descricaoTipo", ""))
    texto = f"{ementa} {keywords} {descricao}"

    # Verificar situação - ignorar encerradas
    situacao = normalize_text(safe_get(proposicao, ["statusProposicao", "descricaoSituacao"], ""))
    for sit in SITUACOES_ENCERRADAS:
        if sit in situacao:
            return None

    # Exclusões (config + globais)
    for exc in cfg["exclusoes"]:
        if exc and exc in texto:
            return None

    # Match de palavras principais
    palavras_match = []
    for p in cfg["palavras_principais"]:
        if p and p in texto:
            palavras_match.append(p)

    # Match de temas
    temas_match = []
    palavras_tema_match = []  # Para mostrar quais palavras bateram
    for tema, palavras in cfg["temas"].items():
        for p in palavras:
            if p and p in texto:
                temas_match.append(tema)
                palavras_tema_match.append(p)
                break

    if not palavras_match and not temas_match:
        return None

    # Score ponderado
    score = 0.0
    score += min(len(palavras_match) * float(cfg["peso_topo"]), 40.0)

    for tema in set(temas_match):
        peso = float(cfg["tema_pesos"].get(tema, 10))
        score += min(peso, 20.0)

    # Bonus por múltiplos temas
    if len(set(temas_match)) >= 2:
        score += 15.0

    # Bonus por regime de urgência
    regime = normalize_text(safe_get(proposicao, ["statusProposicao", "regime"], ""))
    if "urgencia" in regime:
        score += 20.0

    score = min(score, 100.0)

    # Nível de alerta
    situacao_norm = normalize_text(safe_get(proposicao, ["statusProposicao", "descricaoSituacao"], ""))
    if any(x in situacao_norm for x in ["ordem do dia", "em pauta", "incluida na ordem", "em votacao"]):
        nivel = "CRITICO"
    elif "urgencia" in regime or score >= 60:
        nivel = "ALTO"
    elif score >= 40:
        nivel = "MEDIO"
    else:
        nivel = "BAIXO"

    return {
        "proposicao_id": str(proposicao.get("id", "")),
        "temas_match": sorted(list(set(temas_match))),
        "palavras_match": sorted(list(set(palavras_match + palavras_tema_match)))[:10],
        "score": score,
        "nivel": nivel,
    }

# =========================
# FORMATAÇÃO (Telegram - Estilo Monitor Parlamentar)
# =========================
def trunc(texto: str, n: int = 300) -> str:
    if not texto:
        return ""
    t = str(texto).strip()
    return t if len(t) <= n else (t[:n] + "...")

def emoji_nivel(nivel: str) -> str:
    return {"CRITICO": "🚨", "ALTO": "⚠️", "MEDIO": "🔔", "BAIXO": "📋"}.get(nivel, "ℹ️")

def formatar_alerta_match(match: Dict[str, Any], prop: Dict[str, Any], status: Dict[str, str]) -> str:
    """Formato estilo Monitor Parlamentar Informa - suporta Câmara e Senado"""
    ident = format_sigla_num_ano(prop.get("siglaTipo", ""), prop.get("numero", ""), prop.get("ano", ""))
    ementa = trunc(prop.get("ementa", ""), 350)
    orgao = status.get("siglaOrgao") or safe_get(prop, ["statusProposicao", "siglaOrgao"], "")
    situacao = status.get("situacao") or safe_get(prop, ["statusProposicao", "descricaoSituacao"], "Em tramitação")
    relator = status.get("relator") or safe_get(prop, ["statusProposicao", "nomeRelator"], "")
    despacho = status.get("despacho") or safe_get(prop, ["statusProposicao", "despacho"], "")
    
    hora = now_bsb().strftime("%H:%M")
    data = now_bsb().strftime("%d/%m/%Y")

    temas_txt = ", ".join(match["temas_match"]) if match["temas_match"] else "Geral"
    palavras_txt = ", ".join(match["palavras_match"][:5]) if match["palavras_match"] else "-"

    # Identificar casa e gerar link correto
    casa = prop.get("casa", "CAMARA")
    pid = str(match['proposicao_id'])
    if pid.startswith("SF-"):
        casa = "SENADO"
    
    if casa == "SENADO":
        casa_label = "SF"
        codigo_senado = pid.replace("SF-", "")
        link = f"https://www25.senado.leg.br/web/atividade/materias/-/materia/{codigo_senado}"
    else:
        casa_label = "CD"
        link = f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={pid}"

    # Formato inspirado no Monitor Parlamentar
    msg = f"""<b>🎯 Monitor FENAJUFE Informa</b> | {casa_label} | <b>{html.escape(ident)}</b>
<i>{html.escape(ementa)}</i>

<b>📌 Status:</b> {html.escape(situacao)}
<b>🏛️ Órgão:</b> {html.escape(orgao)}"""

    if relator:
        msg += f"\n<b>👤 Relator(a):</b> {html.escape(relator)}"

    if despacho:
        despacho_curto = trunc(despacho, 150)
        msg += f"\n<b>📝 Despacho:</b> {html.escape(despacho_curto)}"

    msg += f"""

<b>🏷️ Temas:</b> {html.escape(temas_txt)}
<b>🔑 Palavras-chave:</b> {html.escape(palavras_txt)}
<b>📊 Relevância:</b> {match["score"]:.0f}/100

<b>📲 Tramitação:</b> <a href="{link}">Clique aqui</a>
🖥️ <a href="{LINK_PAINEL}">Abrir Painel</a>

<i>⏰ {hora} - {data}</i>"""

    return msg

def formatar_mensagem_bom_dia() -> str:
    data = now_bsb().strftime("%d/%m/%Y")
    hora = now_bsb().strftime("%H:%M")
    return f"""☀️ <b>Bom dia! Monitor FENAJUFE</b>
<i>{data} às {hora}</i>

Varredura automática ativada para hoje.
• Telegram: alertas em tempo real
• Email: matches + resumo do dia

🖥️ <a href="{LINK_PAINEL}">Abrir painel</a>
"""

def formatar_sem_novidades_completa() -> str:
    hora = now_bsb().strftime("%H:%M")
    data = now_bsb().strftime("%d/%m")
    return f"""✅ <b>Monitor FENAJUFE</b> | {data} às {hora}
Nenhuma nova matéria relevante identificada.

Isso significa que não houve proposições novas nos temas monitorados.

🖥️ <a href="{LINK_PAINEL}">Abrir painel</a>
"""

def formatar_sem_novidades_curta() -> str:
    hora = now_bsb().strftime("%H:%M")
    return f"✅ Monitor FENAJUFE: sem novidades ({hora})."

def formatar_resumo_dia(matches_enviados: List[str]) -> str:
    data = now_bsb().strftime("%d/%m/%Y")
    hora = now_bsb().strftime("%H:%M")
    total = len(matches_enviados)
    
    if total == 0:
        corpo = "Nenhuma matéria relevante foi identificada hoje nos temas monitorados."
    else:
        itens = "\n".join([f"• {html.escape(x)}" for x in matches_enviados[:15]])
        extra = f"\n… e mais {total-15} matéria(s)" if total > 15 else ""
        corpo = f"<b>Matérias identificadas ({total}):</b>\n{itens}{extra}"

    return f"""🌙 <b>Resumo do Dia — FENAJUFE</b>
<i>{data} às {hora}</i>

{corpo}

🖥️ <a href="{LINK_PAINEL}">Abrir painel completo</a>
"""

# =========================
# EMAIL (HTML)
# =========================
def extrair_texto_plano(mensagem_telegram_html: str) -> str:
    texto = re.sub(r"<[^>]+>", "", mensagem_telegram_html)
    texto = texto.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return texto

def telegram_para_email_html(mensagem_telegram_html: str, assunto: str) -> str:
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
            <td style="background:#8B0000;color:#fff;padding:18px 22px;">
              <div style="font-size:16px;font-weight:700;">⚖️ Monitor Legislativo — FENAJUFE</div>
              <div style="font-size:12px;opacity:.9;margin-top:4px;">Notificação automática</div>
            </td>
          </tr>
          <tr>
            <td style="padding:18px 22px;color:#111;font-size:14px;line-height:1.5;">
              {mensagem_telegram_html}
              <div style="margin-top:18px;text-align:center;">
                <a href="{LINK_PAINEL}" style="display:inline-block;background:#8B0000;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:700;">
                  🖥️ Abrir Painel
                </a>
              </div>
            </td>
          </tr>
          <tr>
            <td style="background:#f8f9fb;padding:14px 22px;color:#6b7280;font-size:12px;text-align:center;border-top:1px solid #eef0f4;">
              <a href="{LINK_PAINEL}" style="color:#8B0000;text-decoration:none;">{html.escape(LINK_PAINEL)}</a>
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
    hora = now_bsb().strftime("%H:%M")
    print(f"⏰ Horário: {hora}")
    
    estado = carregar_estado()
    historico = carregar_historico()

    resumo = carregar_resumo_dia()
    hoje = now_bsb().strftime("%Y-%m-%d")
    if resumo.get("data") != hoje:
        inicializar_resumo_dia()

    toml_data = load_toml(CONFIG_TOML_PATH)
    cfg = parse_config_fenajufe(toml_data)

    fim = now_bsb().date()
    ini = (now_bsb() - timedelta(days=DIAS_BUSCA)).date()
    data_inicio = ini.strftime("%Y-%m-%d")
    data_fim = fim.strftime("%Y-%m-%d")

    print(f"📅 Período: {data_inicio} → {data_fim}")
    print(f"📊 Score mínimo: {SCORE_MINIMO}")
    
    # Buscar de AMBAS AS CASAS
    props = buscar_proposicoes_ambas_casas(data_inicio, data_fim, TIPOS_MONITORADOS)
    
    # Contar por casa
    props_camara = len([p for p in props if p.get("casa") == "CAMARA"])
    props_senado = len([p for p in props if p.get("casa") == "SENADO"])
    print(f"📦 Proposições coletadas: {len(props)} (CD: {props_camara} | SF: {props_senado})")

    novos_alertas = 0
    descartados_score = 0

    for p in props:
        pid = str(p.get("id", ""))
        if not pid:
            continue

        # Só buscar detalhes para proposições da Câmara (Senado já vem normalizado)
        if p.get("casa") == "CAMARA" and not str(pid).startswith("SF-"):
            det = fetch_proposicao_detalhes(pid)
            if det:
                det["casa"] = "CAMARA"
                p = det

        match = calcular_match(p, cfg)
        if not match:
            continue

        # FILTRO: Score mínimo para evitar falsos positivos
        if match["score"] < SCORE_MINIMO:
            descartados_score += 1
            sigla = format_sigla_num_ano(p.get("siglaTipo", ""), p.get("numero", ""), p.get("ano", ""))
            print(f"   ⏭️ Descartado (score {match['score']:.0f} < {SCORE_MINIMO}): {sigla}")
            continue

        status = fetch_status_proposicao(pid)
        datahora = status.get("dataHora", "") or safe_get(p, ["statusProposicao", "dataHora"], "")
        key = chave_notificacao(pid, datahora)

        if key in historico["notificados"]:
            continue

        historico["notificados"][key] = {
            "ts": now_bsb().isoformat(),
            "proposicao": format_sigla_num_ano(p.get("siglaTipo", ""), p.get("numero", ""), p.get("ano", "")),
            "nivel": match["nivel"],
            "score": match["score"],
        }
        salvar_historico(historico)

        sigla = historico["notificados"][key]["proposicao"] or f"ID {pid}"
        msg = formatar_alerta_match(match, p, status)
        assunto = f"{emoji_nivel(match['nivel'])} FENAJUFE | {sigla}"

        notificar_ambos(msg, assunto)
        adicionar_ao_resumo(sigla)

        novos_alertas += 1
        time.sleep(1)

    print(f"📉 Descartados por score baixo: {descartados_score}")

    if novos_alertas == 0:
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
    print("🤖 MONITOR FENAJUFE v2 — Notificações")
    print("=" * 72)

    hora = now_bsb().strftime("%H:%M")
    data = now_bsb().strftime("%d/%m/%Y")
    print(f"⏰ {data} às {hora} (Brasília)")
    print()

    print("📡 CANAIS:")
    print(f"   Telegram: {'ON' if NOTIFICAR_TELEGRAM else 'OFF'}")
    print(f"   Email:    {'ON' if NOTIFICAR_EMAIL else 'OFF'}")
    print(f"🧭 MODO: {MODO_EXECUCAO}")
    print(f"📄 Config: {CONFIG_TOML_PATH}")
    print(f"📊 Score mínimo: {SCORE_MINIMO}")
    print()

    if MODO_EXECUCAO == "bom_dia":
        executar_bom_dia()
    elif MODO_EXECUCAO == "resumo":
        executar_resumo()
    else:
        executar_varredura()

if __name__ == "__main__":
    main()