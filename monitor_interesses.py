# monitor_interesses.py - v1.0
# ============================================================
# Monitor de Interesses - Plataforma de Monitoramento Legislativo
# Para clientes corporativos e consultorias de Relações Governamentais
# 
# Baseado na arquitetura do Monitor Zanatta, adaptado para:
# - Múltiplos clientes com configurações independentes
# - Monitoramento por palavras-chave e temas
# - Lógica de negócio corporativa (risco, oportunidade, impacto)
# - Escalabilidade horizontal
# ============================================================

import datetime
import concurrent.futures
import time
import unicodedata
import json
import hashlib
from functools import lru_cache
from io import BytesIO
from urllib.parse import urlparse
import re
from zoneinfo import ZoneInfo
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd
import requests
import streamlit as st

# ============================================================
# ENUMS E CLASSES DE CONFIGURAÇÃO
# ============================================================

class NivelAlerta(Enum):
    """Níveis de alerta para priorização de notificações"""
    CRITICO = 1      # Votação hoje/amanhã, urgência aprovada
    ALTO = 2         # Pauta confirmada esta semana, novo relator
    MEDIO = 3        # Movimentação relevante, audiência pública
    BAIXO = 4        # Nova proposição, tramitação rotineira
    INFO = 5         # Informativo, resumo diário

class TipoRelatorio(Enum):
    """Tipos de relatórios gerados pelo sistema"""
    AGENDA_SEMANAL = "agenda_semanal"
    RETROSPECTIVA_SEMANAL = "retrospectiva_semanal"
    ALERTA_DIARIO = "alerta_diario"
    EXECUTIVO_MENSAL = "executivo_mensal"
    LINHA_DO_TEMPO = "linha_do_tempo"

class CanalEntrega(Enum):
    """Canais de entrega de alertas e relatórios"""
    TELEGRAM = "telegram"
    EMAIL = "email"
    PAINEL = "painel"
    XLSX = "xlsx"
    PDF = "pdf"

@dataclass
class ConfiguracaoCliente:
    """Configuração de monitoramento para um cliente específico"""
    id_cliente: str
    nome_cliente: str
    nome_exibicao: str
    ativo: bool = True
    
    # Temas e palavras-chave
    temas: Dict[str, List[str]] = field(default_factory=dict)
    palavras_chave_principais: List[str] = field(default_factory=list)
    palavras_chave_exclusao: List[str] = field(default_factory=list)
    
    # Comissões de interesse
    comissoes_estrategicas: List[str] = field(default_factory=list)
    
    # Parlamentares de interesse (autores, relatores)
    parlamentares_interesse: List[str] = field(default_factory=list)
    
    # Tipos de proposição para monitorar
    tipos_proposicao: List[str] = field(default_factory=lambda: ["PL", "PLP", "PEC", "MPV", "PDL"])
    
    # Configurações de alerta
    telegram_chat_id: Optional[str] = None
    emails_notificacao: List[str] = field(default_factory=list)
    horario_silencioso_inicio: int = 22  # 22h
    horario_silencioso_fim: int = 7      # 7h
    
    # Configurações de relatório
    dia_relatorio_semanal: int = 1  # Segunda-feira
    hora_relatorio_semanal: int = 7  # 7h
    
    # Plano e limites
    plano: str = "professional"
    max_temas: int = 10
    max_usuarios: int = 10

@dataclass
class Match:
    """Representa uma correspondência entre proposição e interesse do cliente"""
    proposicao_id: str
    cliente_id: str
    score_relevancia: float
    temas_match: List[str]
    palavras_match: List[str]
    nivel_alerta: NivelAlerta
    data_deteccao: datetime.datetime
    notificado: bool = False

# ============================================================
# CONFIGURAÇÕES GLOBAIS E CONSTANTES
# ============================================================

# Timezone Brasil
TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

# URLs das APIs
API_CAMARA_BASE = "https://dadosabertos.camara.leg.br/api/v2"
API_SENADO_BASE = "https://legis.senado.leg.br/dadosabertos"

# Emojis para níveis de alerta
EMOJI_ALERTA = {
    NivelAlerta.CRITICO: "🚨",
    NivelAlerta.ALTO: "⚠️",
    NivelAlerta.MEDIO: "🔔",
    NivelAlerta.BAIXO: "📋",
    NivelAlerta.INFO: "ℹ️"
}

# Situações que indicam matéria em pauta (CRÍTICO)
SITUACOES_PAUTA = [
    "incluída na ordem do dia",
    "em pauta",
    "pronta para pauta",
    "aguardando deliberação",
    "matéria em votação"
]

# Situações que indicam tramitação ativa (ALTO)
SITUACOES_TRAMITACAO_ATIVA = [
    "aguardando parecer",
    "aguardando designação de relator",
    "em tramitação",
    "pronta para deliberação"
]

# Situações FORA de tramitação (excluir do monitoramento)
SITUACOES_FORA_TRAMITACAO = [
    "arquivada",
    "arquivado",
    "transformada em norma jurídica",
    "transformado em norma jurídica",
    "transformada em lei",
    "perdeu a eficácia",
    "perda de eficácia",
    "retirada pelo autor",
    "retirado pelo autor",
    "prejudicada",
    "prejudicado",
    "devolvida ao autor",
    "devolvido ao autor",
    "vetado totalmente",
    "declarada prejudicada",
    "declarado prejudicado",
    "rejeitada",
    "rejeitado",
    "não apreciada",
    "não apreciado",
]

# Classificação de impacto por tipo de proposição
PESO_TIPO_PROPOSICAO = {
    "PEC": 5,   # Emenda Constitucional - maior impacto
    "PLP": 4,   # Lei Complementar
    "MPV": 4,   # Medida Provisória - urgente
    "PL": 3,    # Projeto de Lei
    "PDL": 2,   # Decreto Legislativo
    "PRC": 1,   # Projeto de Resolução
    "REQ": 1    # Requerimento
}

# Temas pré-configurados (templates para novos clientes)
TEMAS_TEMPLATE = {
    "Saúde": [
        "anvisa", "medicamento", "plano de saúde", "sus", "vacina", 
        "hospital", "farmácia", "genérico", "biossimilar", "registro sanitário"
    ],
    "Tributário": [
        "imposto", "tributo", "icms", "pis", "cofins", "irpj", "csll",
        "reforma tributária", "ibs", "cbs", "zona franca", "incentivo fiscal"
    ],
    "Trabalhista": [
        "clt", "trabalho", "trabalhador", "emprego", "sindicato",
        "terceirização", "home office", "férias", "13º", "fgts"
    ],
    "Ambiental": [
        "meio ambiente", "ibama", "licenciamento", "carbono", "sustentável",
        "desmatamento", "reserva legal", "app", "código florestal"
    ],
    "Energia": [
        "energia", "aneel", "tarifa", "distribuidora", "geração",
        "renovável", "solar", "eólica", "gás natural", "petróleo"
    ],
    "Financeiro": [
        "banco central", "bacen", "pix", "drex", "fintech", "open banking",
        "crédito", "juros", "regulação bancária", "cvm", "mercado de capitais"
    ],
    "Tecnologia": [
        "lgpd", "dados pessoais", "inteligência artificial", "ia",
        "marco civil", "plataformas digitais", "criptomoeda", "blockchain"
    ],
    "Agronegócio": [
        "agricultura", "pecuária", "agrotóxico", "defensivo", "funrural",
        "crédito rural", "seguro agrícola", "código florestal"
    ]
}

# ============================================================
# FUNÇÕES UTILITÁRIAS
# ============================================================

@lru_cache(maxsize=1)
def carregar_logo_base64() -> Optional[str]:
    """Carrega a logo da FENAJUFE e converte para base64 (com cache)"""
    import base64
    try:
        logo_url = "https://www.fenajufe.org.br/wp-content/uploads/2025/01/Logo-300x84-1.png"
        response = requests.get(logo_url, timeout=5, headers={"User-Agent": "MonitorFENAJUFE/1.0"})
        if response.status_code == 200:
            return base64.b64encode(response.content).decode()
    except:
        pass
    return None

def get_brasilia_now() -> datetime.datetime:
    """Retorna a data/hora atual no fuso horário de Brasília"""
    return datetime.datetime.now(TZ_BRASILIA)

def normalize_text(text: str) -> str:
    """Normaliza texto removendo acentos e convertendo para minúsculas"""
    if not isinstance(text, str):
        return ""
    nfkd = unicodedata.normalize("NFD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return no_accents.lower().strip()

def generate_client_hash(nome_cliente: str) -> str:
    """Gera um hash único para identificar o cliente"""
    return hashlib.md5(nome_cliente.encode()).hexdigest()[:12]

def is_horario_silencioso(config: ConfiguracaoCliente) -> bool:
    """Verifica se está no horário silencioso do cliente"""
    hora_atual = get_brasilia_now().hour
    inicio = config.horario_silencioso_inicio
    fim = config.horario_silencioso_fim
    
    if inicio > fim:  # Ex: 22h às 7h (cruza meia-noite)
        return hora_atual >= inicio or hora_atual < fim
    else:
        return inicio <= hora_atual < fim

def calcular_dias_uteis(data_inicio: datetime.date, data_fim: datetime.date) -> int:
    """Conta dias úteis entre duas datas (excluindo fins de semana)"""
    if data_inicio is None or data_fim is None:
        return 0
    if data_fim < data_inicio:
        return 0
    dias = 0
    atual = data_inicio
    while atual <= data_fim:
        if atual.weekday() < 5:  # Segunda a sexta
            dias += 1
        atual += datetime.timedelta(days=1)
    return dias

def format_sigla_num_ano(sigla: str, numero: str, ano: str) -> str:
    """Formata identificação da proposição: 'PL 1234/2025'"""
    sigla = (sigla or "").strip()
    numero = (str(numero) or "").strip()
    ano = (str(ano) or "").strip()
    if sigla and numero and ano:
        return f"{sigla} {numero}/{ano}"
    return ""

def parse_datetime(iso_str: str) -> Optional[datetime.datetime]:
    """Converte string ISO para datetime"""
    if not iso_str:
        return None
    try:
        return pd.to_datetime(iso_str, errors="coerce")
    except:
        return None

def days_since(dt) -> Optional[int]:
    """Calcula dias desde uma data até hoje"""
    if dt is None or pd.isna(dt):
        return None
    d = pd.Timestamp(dt).tz_localize(None) if getattr(dt, "tzinfo", None) else pd.Timestamp(dt)
    today = pd.Timestamp(datetime.date.today())
    return int((today - d.normalize()).days)

# ============================================================
# SISTEMA DE MATCHING DE PALAVRAS-CHAVE
# ============================================================

class MatchingEngine:
    """Motor de matching entre proposições e interesses dos clientes"""
    
    def __init__(self, config: ConfiguracaoCliente):
        self.config = config
        self._prepare_patterns()
    
    def _prepare_patterns(self):
        """Prepara padrões de busca otimizados"""
        # Palavras-chave principais (normalizadas)
        self.palavras_principais = [
            normalize_text(p) for p in self.config.palavras_chave_principais
        ]
        
        # Palavras de exclusão
        self.palavras_exclusao = [
            normalize_text(p) for p in self.config.palavras_chave_exclusao
        ]
        
        # Temas com suas palavras-chave
        self.temas_palavras = {}
        for tema, palavras in self.config.temas.items():
            self.temas_palavras[tema] = [normalize_text(p) for p in palavras]
    
    def calcular_match(self, proposicao: Dict) -> Optional[Match]:
        """
        Calcula se uma proposição faz match com os interesses do cliente.
        Retorna Match se houver correspondência, None caso contrário.
        """
        # Extrair texto para análise
        ementa = normalize_text(proposicao.get("ementa", ""))
        keywords = normalize_text(proposicao.get("keywords", ""))
        descricao = normalize_text(proposicao.get("descricaoTipo", ""))
        texto_analise = f"{ementa} {keywords} {descricao}"
        
        # Verificar exclusões primeiro
        for palavra_exc in self.palavras_exclusao:
            if palavra_exc in texto_analise:
                return None  # Excluído
        
        # Verificar se está FORA de tramitação (arquivada, transformada em lei, etc.)
        situacao = normalize_text(proposicao.get("statusProposicao", {}).get("descricaoSituacao", ""))
        for sit_fora in SITUACOES_FORA_TRAMITACAO:
            if sit_fora in situacao:
                return None  # Fora de tramitação - excluir
        
        # Calcular matches de palavras-chave principais
        palavras_match = []
        for palavra in self.palavras_principais:
            if palavra in texto_analise:
                palavras_match.append(palavra)
        
        # Calcular matches de temas
        temas_match = []
        for tema, palavras in self.temas_palavras.items():
            for palavra in palavras:
                if palavra in texto_analise:
                    temas_match.append(tema)
                    break  # Uma palavra por tema é suficiente
        
        # Se não houve match, retornar None
        if not palavras_match and not temas_match:
            return None
        
        # Calcular score de relevância
        score = self._calcular_score(proposicao, palavras_match, temas_match)
        
        # Determinar nível de alerta
        nivel = self._determinar_nivel_alerta(proposicao, score)
        
        return Match(
            proposicao_id=str(proposicao.get("id", "")),
            cliente_id=self.config.id_cliente,
            score_relevancia=score,
            temas_match=list(set(temas_match)),
            palavras_match=list(set(palavras_match)),
            nivel_alerta=nivel,
            data_deteccao=get_brasilia_now()
        )
    
    def _calcular_score(self, proposicao: Dict, palavras_match: List[str], temas_match: List[str]) -> float:
        """Calcula score de relevância de 0 a 100"""
        score = 0.0
        
        # Pontos por palavras-chave (máx 40 pontos)
        score += min(len(palavras_match) * 10, 40)
        
        # Pontos por temas (máx 30 pontos)
        score += min(len(temas_match) * 10, 30)
        
        # Pontos por tipo de proposição (máx 15 pontos)
        tipo = proposicao.get("siglaTipo", "").upper()
        peso_tipo = PESO_TIPO_PROPOSICAO.get(tipo, 1)
        score += peso_tipo * 3
        
        # Pontos por situação (máx 15 pontos)
        situacao = normalize_text(proposicao.get("statusProposicao", {}).get("descricaoSituacao", ""))
        if any(s in situacao for s in SITUACOES_PAUTA):
            score += 15
        elif any(s in situacao for s in SITUACOES_TRAMITACAO_ATIVA):
            score += 10
        
        return min(score, 100)
    
    def _determinar_nivel_alerta(self, proposicao: Dict, score: float) -> NivelAlerta:
        """Determina o nível de alerta baseado na situação e score"""
        situacao = normalize_text(proposicao.get("statusProposicao", {}).get("descricaoSituacao", ""))
        
        # CRÍTICO: Em pauta ou votação iminente
        if any(s in situacao for s in SITUACOES_PAUTA):
            return NivelAlerta.CRITICO
        
        # Verificar urgência
        regime = normalize_text(proposicao.get("statusProposicao", {}).get("regime", ""))
        if "urgencia" in regime or "urgente" in regime:
            return NivelAlerta.ALTO
        
        # Baseado no score
        if score >= 70:
            return NivelAlerta.ALTO
        elif score >= 50:
            return NivelAlerta.MEDIO
        elif score >= 30:
            return NivelAlerta.BAIXO
        else:
            return NivelAlerta.INFO

# ============================================================
# FUNÇÕES DE COLETA DE DADOS
# ============================================================

@lru_cache(maxsize=1000)
def fetch_proposicao_detalhes(proposicao_id: str) -> Dict:
    """Busca detalhes completos de uma proposição (com cache)"""
    try:
        url = f"{API_CAMARA_BASE}/proposicoes/{proposicao_id}"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            return data.get("dados", {})
    except Exception as e:
        st.warning(f"Erro ao buscar proposição {proposicao_id}: {e}")
    return {}

@lru_cache(maxsize=100)
def fetch_tramitacoes(proposicao_id: str) -> List[Dict]:
    """Busca tramitações de uma proposição (com cache)"""
    try:
        url = f"{API_CAMARA_BASE}/proposicoes/{proposicao_id}/tramitacoes"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            return data.get("dados", [])
    except Exception:
        pass
    return []

def buscar_proposicoes_periodo(
    data_inicio: datetime.date,
    data_fim: datetime.date,
    tipos: List[str] = None,
    situacao_id: int = None,
    limite_por_tipo: int = 50
) -> List[Dict]:
    """
    Busca proposições apresentadas em um período.
    Retorna lista de proposições com dados básicos.
    Limitado para performance.
    """
    if tipos is None:
        tipos = ["PL", "PLP", "PEC", "MPV", "PDL"]
    
    todas_proposicoes = []
    
    for tipo in tipos:
        try:
            params = {
                "siglaTipo": tipo,
                "dataInicio": data_inicio.strftime("%Y-%m-%d"),
                "dataFim": data_fim.strftime("%Y-%m-%d"),
                "ordem": "DESC",
                "ordenarPor": "id",
                "itens": min(limite_por_tipo, 100)  # Limitar para performance
            }
            
            if situacao_id:
                params["codSituacao"] = situacao_id
            
            url = f"{API_CAMARA_BASE}/proposicoes"
            response = requests.get(url, params=params, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                proposicoes = data.get("dados", [])
                todas_proposicoes.extend(proposicoes[:limite_por_tipo])
                        
        except Exception as e:
            st.warning(f"Erro ao buscar {tipo}: {e}")
    
    return todas_proposicoes

def buscar_eventos_periodo(
    data_inicio: datetime.date,
    data_fim: datetime.date,
    orgaos: List[str] = None
) -> List[Dict]:
    """
    Busca eventos (reuniões de comissão, plenário) em um período.
    A API da Câmara geralmente só retorna eventos futuros ou muito recentes.
    """
    todos_eventos = []
    
    try:
        params = {
            "dataInicio": data_inicio.strftime("%Y-%m-%d"),
            "dataFim": data_fim.strftime("%Y-%m-%d"),
            "ordem": "ASC",
            "ordenarPor": "dataHoraInicio",
            "itens": 100
        }
        
        url = f"{API_CAMARA_BASE}/eventos"
        response = requests.get(url, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            eventos = data.get("dados", [])
            
            # Filtrar por órgãos se especificado
            if orgaos and eventos:
                orgaos_upper = [o.upper() for o in orgaos]
                # Filtrar eventos que tenham ao menos um órgão da lista
                eventos_filtrados = []
                for e in eventos:
                    orgaos_evento = e.get("orgaos", [])
                    if not orgaos_evento:  # Se não tem órgão, incluir
                        eventos_filtrados.append(e)
                    elif any(org.get("sigla", "").upper() in orgaos_upper for org in orgaos_evento):
                        eventos_filtrados.append(e)
                eventos = eventos_filtrados
            
            todos_eventos.extend(eventos)
            
            # Verificar se há paginação
            links = data.get("links", [])
            next_link = next((l for l in links if l.get("rel") == "next"), None)
            
            while next_link and len(todos_eventos) < 500:  # Limite de segurança
                response = requests.get(next_link["href"], timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    novos_eventos = data.get("dados", [])
                    
                    # Aplicar filtro de órgãos novamente
                    if orgaos and novos_eventos:
                        orgaos_upper = [o.upper() for o in orgaos]
                        novos_eventos = [
                            e for e in novos_eventos 
                            if not e.get("orgaos") or any(
                                org.get("sigla", "").upper() in orgaos_upper 
                                for org in e.get("orgaos", [])
                            )
                        ]
                    
                    todos_eventos.extend(novos_eventos)
                    links = data.get("links", [])
                    next_link = next((l for l in links if l.get("rel") == "next"), None)
                else:
                    break
                    
    except Exception as e:
        st.warning(f"Erro ao buscar eventos: {e}")
        st.warning(f"Erro ao buscar eventos: {e}")
    
    return todos_eventos

def buscar_pauta_evento(evento_id: str) -> List[Dict]:
    """Busca a pauta de um evento específico"""
    try:
        url = f"{API_CAMARA_BASE}/eventos/{evento_id}/pauta"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            return data.get("dados", [])
    except Exception:
        pass
    return []


@lru_cache(maxsize=500)
def fetch_status_proposicao(proposicao_id: str) -> Dict:
    """Busca status atualizado de uma proposição (com cache)"""
    try:
        url = f"{API_CAMARA_BASE}/proposicoes/{proposicao_id}"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json().get("dados", {})
            status = data.get("statusProposicao", {})
            return {
                "situacao": status.get("descricaoSituacao", ""),
                "siglaOrgao": status.get("siglaOrgao", ""),
                "dataHora": status.get("dataHora", ""),
                "despacho": status.get("despacho", ""),
                "regime": status.get("regime", ""),
                "relator": status.get("nomeRelator", "") if status.get("nomeRelator") else "",
                "uriRelator": status.get("uriRelator", ""),
            }
    except Exception:
        pass
    return {}


def build_status_map(ids: List[str]) -> Dict[str, Dict]:
    """Constrói mapa de status para lista de IDs usando processamento paralelo"""
    status_map = {}
    
    def fetch_one(prop_id):
        return prop_id, fetch_status_proposicao(prop_id)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        results = list(executor.map(fetch_one, ids))
    
    for prop_id, status in results:
        status_map[prop_id] = status
    
    return status_map


def calcular_dias_parado(data_str: str) -> int:
    """Calcula dias desde a última movimentação"""
    if not data_str:
        return None
    try:
        # Formato: "2025-01-15T14:30:00"
        data = datetime.datetime.fromisoformat(data_str.replace("Z", "+00:00"))
        if data.tzinfo:
            data = data.replace(tzinfo=None)
        hoje = datetime.datetime.now()
        return (hoje - data).days
    except Exception:
        return None


def get_alerta_emoji_dias(dias):
    """Retorna emoji de alerta baseado em dias parado"""
    if dias is None:
        return ""
    if dias <= 2:
        return "🚨"  # Urgentíssimo
    if dias <= 5:
        return "⚠️"  # Urgente
    if dias <= 15:
        return "🔔"  # Recente
    return ""


def sanitize_text_pdf(text: str) -> str:
    """Remove caracteres não suportados pela fonte do PDF (emojis)"""
    if not text:
        return ""
    # Remove emojis e caracteres especiais
    return ''.join(c for c in str(text) if ord(c) < 65536 and unicodedata.category(c) != 'So')

# ============================================================
# PROCESSAMENTO DE PROPOSIÇÕES
# ============================================================

def processar_proposicoes_para_cliente(
    proposicoes: List[Dict],
    config: ConfiguracaoCliente
) -> List[Match]:
    """
    Processa lista de proposições e retorna matches para o cliente.
    Usa processamento paralelo para performance.
    """
    engine = MatchingEngine(config)
    matches = []
    
    def processar_uma(prop):
        # Enriquecer com detalhes se necessário
        if "statusProposicao" not in prop:
            detalhes = fetch_proposicao_detalhes(str(prop.get("id", "")))
            prop.update(detalhes)
        return engine.calcular_match(prop)
    
    # Processamento paralelo
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        resultados = list(executor.map(processar_uma, proposicoes))
    
    # Filtrar None
    matches = [m for m in resultados if m is not None]
    
    # Ordenar por nível de alerta e score
    matches.sort(key=lambda m: (m.nivel_alerta.value, -m.score_relevancia))
    
    return matches

def criar_dataframe_matches(matches: List[Match], proposicoes: Dict[str, Dict], status_map: Dict[str, Dict] = None) -> pd.DataFrame:
    """
    Cria DataFrame consolidado com matches e informações das proposições.
    Inclui campos adicionais como Relator, Dias Parado, etc.
    """
    registros = []
    
    for match in matches:
        prop = proposicoes.get(match.proposicao_id, {})
        
        # Extrair status da proposição ou do mapa de status
        if status_map and match.proposicao_id in status_map:
            status_ext = status_map[match.proposicao_id]
        else:
            status_ext = {}
        
        status = prop.get("statusProposicao", {})
        
        # Data do status
        data_status = status_ext.get("dataHora") or status.get("dataHora", "")
        data_status_fmt = data_status[:10] if data_status else ""
        
        # Calcular dias parado
        dias_parado = calcular_dias_parado(data_status)
        
        # Emoji de alerta baseado nos dias
        alerta_emoji = get_alerta_emoji_dias(dias_parado)
        
        registro = {
            "ID": match.proposicao_id,
            "Alerta": alerta_emoji,
            "Proposição": format_sigla_num_ano(
                prop.get("siglaTipo", ""),
                prop.get("numero", ""),
                prop.get("ano", "")
            ),
            "Tipo": prop.get("siglaTipo", ""),
            "Ano": prop.get("ano", ""),
            "Situação atual": status_ext.get("situacao") or status.get("descricaoSituacao", ""),
            "Órgão (sigla)": status_ext.get("siglaOrgao") or status.get("siglaOrgao", ""),
            "Relator(a)": status_ext.get("relator") or "Aguardando",
            "Último andamento": status_ext.get("despacho") or status.get("despacho", "")[:100] if status.get("despacho") else "",
            "Data do status": data_status_fmt,
            "Parado (dias)": dias_parado,
            "Ementa": prop.get("ementa", ""),
            "Autor": "; ".join([a.get("nome", "") for a in prop.get("autores", [])[:3]]) if prop.get("autores") else "",
            "Temas Match": ", ".join(match.temas_match),
            "Palavras Match": ", ".join(match.palavras_match[:5]),
            "Score": match.score_relevancia,
            "Nível Alerta": match.nivel_alerta.name,
            "Link": f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={match.proposicao_id}"
        }
        
        registros.append(registro)
    
    df = pd.DataFrame(registros)
    
    # Ordenar por dias parado (mais recente primeiro)
    if not df.empty and "Parado (dias)" in df.columns:
        df = df.sort_values("Parado (dias)", ascending=True, na_position='last')
    
    return df

# ============================================================
# SISTEMA DE ALERTAS
# ============================================================

class SistemaAlertas:
    """Gerencia envio de alertas para clientes"""
    
    def __init__(self, config: ConfiguracaoCliente, bot_token: str = None):
        self.config = config
        self.bot_token = bot_token
    
    def enviar_telegram(self, mensagem: str, nivel: NivelAlerta = NivelAlerta.INFO) -> bool:
        """Envia mensagem para o Telegram do cliente"""
        if not self.config.telegram_chat_id or not self.bot_token:
            return False
        
        # Verificar horário silencioso (exceto CRÍTICO)
        if nivel != NivelAlerta.CRITICO and is_horario_silencioso(self.config):
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.telegram_chat_id,
                "text": mensagem,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            
            response = requests.post(url, json=payload, timeout=10)
            return response.status_code == 200
        except Exception:
            return False
    
    def formatar_alerta_match(self, match: Match, proposicao: Dict) -> str:
        """Formata mensagem de alerta para um match"""
        emoji = EMOJI_ALERTA.get(match.nivel_alerta, "ℹ️")
        nivel_texto = {
            NivelAlerta.CRITICO: "ALERTA CRÍTICO",
            NivelAlerta.ALTO: "ALERTA ALTO",
            NivelAlerta.MEDIO: "ATENÇÃO",
            NivelAlerta.BAIXO: "INFORMATIVO",
            NivelAlerta.INFO: "INFO"
        }.get(match.nivel_alerta, "INFO")
        
        # Identificação da proposição
        identificacao = format_sigla_num_ano(
            proposicao.get("siglaTipo", ""),
            proposicao.get("numero", ""),
            proposicao.get("ano", "")
        )
        
        # Status
        status = proposicao.get("statusProposicao", {})
        situacao = status.get("descricaoSituacao", "Em tramitação")
        orgao = status.get("siglaOrgao", "")
        
        # Ementa (truncada)
        ementa = proposicao.get("ementa", "")
        if len(ementa) > 200:
            ementa = ementa[:200] + "..."
        
        # Montar mensagem
        mensagem = f"""{emoji} <b>{nivel_texto}</b>

<b>{identificacao}</b>
<i>{ementa}</i>

<b>Status:</b> {situacao}
<b>Órgão:</b> {orgao}
<b>Temas:</b> {', '.join(match.temas_match) if match.temas_match else 'Palavra-chave'}
<b>Relevância:</b> {match.score_relevancia:.0f}/100

🔗 <a href="https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={match.proposicao_id}">Ver detalhes</a>
"""
        return mensagem
    
    def enviar_digest_diario(self, matches: List[Match], proposicoes: Dict[str, Dict]) -> bool:
        """Envia resumo diário consolidado"""
        if not matches:
            return True
        
        # Agrupar por nível
        criticos = [m for m in matches if m.nivel_alerta == NivelAlerta.CRITICO]
        altos = [m for m in matches if m.nivel_alerta == NivelAlerta.ALTO]
        outros = [m for m in matches if m.nivel_alerta not in [NivelAlerta.CRITICO, NivelAlerta.ALTO]]
        
        data_hoje = get_brasilia_now().strftime("%d/%m/%Y")
        
        mensagem = f"""📊 <b>Resumo do Dia - {data_hoje}</b>

<b>📌 Movimentações detectadas:</b>
• 🚨 Críticos: {len(criticos)}
• ⚠️ Alta prioridade: {len(altos)}
• 📋 Outros: {len(outros)}

"""
        
        # Listar críticos
        if criticos:
            mensagem += "<b>🚨 Atenção imediata:</b>\n"
            for m in criticos[:3]:
                prop = proposicoes.get(m.proposicao_id, {})
                ident = format_sigla_num_ano(prop.get("siglaTipo", ""), prop.get("numero", ""), prop.get("ano", ""))
                mensagem += f"• {ident}\n"
            mensagem += "\n"
        
        # Listar altos
        if altos:
            mensagem += "<b>⚠️ Acompanhar:</b>\n"
            for m in altos[:5]:
                prop = proposicoes.get(m.proposicao_id, {})
                ident = format_sigla_num_ano(prop.get("siglaTipo", ""), prop.get("numero", ""), prop.get("ano", ""))
                mensagem += f"• {ident}\n"
        
        mensagem += "\n📊 Acesse o painel para detalhes completos."
        
        return self.enviar_telegram(mensagem, NivelAlerta.INFO)

# ============================================================
# GERAÇÃO DE RELATÓRIOS
# ============================================================

def sanitize_text_pdf(text: str) -> str:
    """Sanitiza texto para uso em PDF"""
    if not text:
        return ""
    # Substituir caracteres problemáticos
    replacements = {
        '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
        '\u2022': '-', '\u2026': '...',
        '\xa0': ' '
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # Remover outros caracteres não-ASCII problemáticos
    text = text.encode('latin-1', errors='replace').decode('latin-1')
    return text

def to_xlsx_bytes(df: pd.DataFrame, sheet_name: str = "Dados") -> tuple:
    """Exporta DataFrame para Excel em memória"""
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
    output.seek(0)
    return (output.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx")

def gerar_relatorio_pdf(
    df: pd.DataFrame,
    titulo: str,
    subtitulo: str,
    cliente: ConfiguracaoCliente
) -> tuple:
    """
    Gera relatório PDF profissional para cliente corporativo.
    """
    try:
        from fpdf import FPDF
        
        class RelatorioPDF(FPDF):
            def __init__(self, cliente_nome):
                super().__init__(orientation='P', unit='mm', format='A4')
                self.cliente_nome = cliente_nome
            
            def header(self):
                # Barra superior azul
                self.set_fill_color(26, 54, 93)  # Azul corporativo
                self.rect(0, 0, 210, 28, 'F')
                
                # Logo/Título
                self.set_font('Helvetica', 'B', 18)
                self.set_text_color(255, 255, 255)
                self.set_y(8)
                self.cell(0, 10, 'MONITOR DE INTERESSES', align='C')
                
                # Nome do cliente
                self.set_font('Helvetica', '', 10)
                self.set_y(18)
                self.cell(0, 5, self.cliente_nome, align='C')
                
                self.ln(25)
            
            def footer(self):
                self.set_y(-15)
                self.set_font('Helvetica', 'I', 8)
                self.set_text_color(128, 128, 128)
                self.cell(60, 10, f'Gerado em: {get_brasilia_now().strftime("%d/%m/%Y %H:%M")}', align='L')
                self.cell(0, 10, f'Pagina {self.page_no()}', align='R')
        
        pdf = RelatorioPDF(cliente.nome_exibicao)
        pdf.set_auto_page_break(auto=True, margin=20)
        pdf.add_page()
        
        # Título do relatório
        pdf.set_y(35)
        pdf.set_font('Helvetica', 'B', 16)
        pdf.set_text_color(26, 54, 93)
        pdf.cell(0, 10, sanitize_text_pdf(titulo), ln=True, align='C')
        
        # Subtítulo
        pdf.set_font('Helvetica', '', 11)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, sanitize_text_pdf(subtitulo), ln=True, align='C')
        
        pdf.ln(5)
        pdf.set_draw_color(26, 54, 93)
        pdf.set_line_width(0.5)
        pdf.line(20, pdf.get_y(), 190, pdf.get_y())
        pdf.ln(8)
        
        # Resumo executivo
        pdf.set_font('Helvetica', 'B', 12)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(0, 8, f'Total de materias monitoradas: {len(df)}', ln=True)
        
        # Contagem por nível
        if 'Nível Alerta' in df.columns:
            pdf.set_font('Helvetica', '', 10)
            criticos = len(df[df['Nível Alerta'] == 'CRITICO'])
            altos = len(df[df['Nível Alerta'] == 'ALTO'])
            medios = len(df[df['Nível Alerta'] == 'MEDIO'])
            
            if criticos > 0:
                pdf.set_text_color(180, 50, 50)
                pdf.cell(0, 5, f'  Criticos: {criticos}', ln=True)
            if altos > 0:
                pdf.set_text_color(200, 120, 0)
                pdf.cell(0, 5, f'  Alta prioridade: {altos}', ln=True)
            if medios > 0:
                pdf.set_text_color(100, 100, 0)
                pdf.cell(0, 5, f'  Media prioridade: {medios}', ln=True)
        
        pdf.ln(10)
        
        # Listagem de matérias
        pdf.set_font('Helvetica', 'B', 12)
        pdf.set_text_color(26, 54, 93)
        pdf.cell(0, 8, 'Materias em Destaque', ln=True)
        pdf.ln(3)
        
        # Iterar pelas matérias (máx 30 para não ficar muito longo)
        for idx, row in df.head(30).iterrows():
            # Verificar espaço na página
            if pdf.get_y() > 250:
                pdf.add_page()
            
            # Emoji e identificação (remover emoji para PDF)
            emoji = sanitize_text_pdf(str(row.get('Alerta', '')))
            proposicao = row.get('Proposição', '')
            
            pdf.set_font('Helvetica', 'B', 11)
            pdf.set_text_color(0, 0, 0)
            
            # Usar indicador textual em vez de emoji
            nivel = row.get('Nível Alerta', '')
            indicador = ""
            if nivel == "CRITICO":
                indicador = "[CRITICO] "
                pdf.set_text_color(180, 50, 50)
            elif nivel == "ALTO":
                indicador = "[ALTO] "
                pdf.set_text_color(200, 120, 0)
            elif nivel == "MEDIO":
                indicador = "[MEDIO] "
                pdf.set_text_color(100, 100, 0)
            
            pdf.cell(0, 6, f'{indicador}{sanitize_text_pdf(proposicao)}', ln=True)
            pdf.set_text_color(0, 0, 0)
            
            pdf.set_x(15)
            
            # Situação
            situacao = row.get('Situação atual', '') or row.get('Situação', '')
            if situacao:
                pdf.set_font('Helvetica', 'B', 9)
                pdf.set_text_color(100, 100, 100)
                pdf.cell(20, 5, 'Status: ', ln=False)
                pdf.set_font('Helvetica', '', 9)
                pdf.set_text_color(60, 60, 60)
                pdf.cell(0, 5, sanitize_text_pdf(str(situacao))[:60], ln=True)
                pdf.set_x(15)
            
            # Ementa
            ementa = row.get('Ementa', '')
            if ementa:
                pdf.set_font('Helvetica', 'I', 8)
                pdf.set_text_color(80, 80, 80)
                ementa_trunc = sanitize_text_pdf(str(ementa))[:250]
                if len(str(ementa)) > 250:
                    ementa_trunc += '...'
                pdf.multi_cell(180, 4, ementa_trunc)
            
            # Temas
            temas = row.get('Temas Match', '')
            if temas:
                pdf.set_x(15)
                pdf.set_font('Helvetica', 'B', 8)
                pdf.set_text_color(26, 54, 93)
                pdf.cell(15, 4, 'Temas: ', ln=False)
                pdf.set_font('Helvetica', '', 8)
                pdf.cell(0, 4, sanitize_text_pdf(str(temas)), ln=True)
            
            # Linha divisória
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            pdf.set_line_width(0.2)
            pdf.line(15, pdf.get_y(), 195, pdf.get_y())
            pdf.ln(4)
        
        # Gerar output
        output = BytesIO()
        pdf.output(output)
        return (output.getvalue(), "application/pdf", "pdf")
        
    except ImportError:
        raise Exception("Biblioteca fpdf2 não disponível. Instale com: pip install fpdf2")

def gerar_agenda_semanal(
    config: ConfiguracaoCliente,
    data_inicio: datetime.date,
    data_fim: datetime.date
) -> Dict:
    """
    Gera a Agenda Legislativa da Semana para o cliente.
    Retorna dict com dados estruturados e DataFrames.
    """
    # Buscar eventos da semana
    eventos = buscar_eventos_periodo(data_inicio, data_fim, config.comissoes_estrategicas)
    
    # Processar pautas dos eventos
    materias_pauta = []
    for evento in eventos:
        pauta = buscar_pauta_evento(str(evento.get("id", "")))
        for item in pauta:
            prop_uri = item.get("uriProposicao", "")
            if prop_uri:
                prop_id = prop_uri.split("/")[-1]
                prop_detalhes = fetch_proposicao_detalhes(prop_id)
                materias_pauta.append({
                    "evento_id": evento.get("id"),
                    "evento_data": evento.get("dataHoraInicio", "")[:10],
                    "evento_hora": evento.get("dataHoraInicio", "")[11:16] if evento.get("dataHoraInicio") else "",
                    "orgao": evento.get("orgaos", [{}])[0].get("sigla", "") if evento.get("orgaos") else "",
                    "proposicao": prop_detalhes
                })
    
    # Aplicar filtros do cliente
    engine = MatchingEngine(config)
    matches_pauta = []
    
    for item in materias_pauta:
        prop = item.get("proposicao", {})
        match = engine.calcular_match(prop)
        if match:
            # Forçar nível CRÍTICO para matérias em pauta
            match.nivel_alerta = NivelAlerta.CRITICO
            matches_pauta.append({
                **item,
                "match": match
            })
    
    # Organizar por dia
    agenda_por_dia = {}
    for item in matches_pauta:
        data = item.get("evento_data", "")
        if data not in agenda_por_dia:
            agenda_por_dia[data] = []
        agenda_por_dia[data].append(item)
    
    return {
        "periodo_inicio": data_inicio,
        "periodo_fim": data_fim,
        "total_eventos": len(eventos),
        "total_materias_interesse": len(matches_pauta),
        "agenda_por_dia": agenda_por_dia,
        "eventos_raw": eventos
    }

# ============================================================
# CONFIGURAÇÃO DA APLICAÇÃO STREAMLIT
# ============================================================

def configurar_pagina():
    """Configura a página Streamlit"""
    st.set_page_config(
        page_title="Monitor FENAJUFE | Monitoramento Legislativo",
        page_icon="⚖️",
        layout="wide",
        initial_sidebar_state="collapsed"  # Sidebar começa fechada
    )
    
    # CSS customizado - Cores FENAJUFE (vermelho e azul escuro)
    st.markdown("""
    <style>
    /* Esconder sidebar */
    [data-testid="stSidebar"] {
        display: none;
    }
    [data-testid="stSidebarNav"] {
        display: none;
    }
    
    /* Header FENAJUFE */
    .main-header {
        background: linear-gradient(135deg, #8B0000 0%, #1a1a2e 100%);
        padding: 1.5rem 2rem;
        border-radius: 10px;
        margin-bottom: 1rem;
    }
    .main-header h1 {
        color: white;
        margin: 0;
        font-size: 1.8rem;
    }
    .main-header p {
        color: #e0e0e0;
        margin: 0.3rem 0 0 0;
        font-size: 0.9rem;
    }
    
    /* Logo container */
    .logo-container {
        background: white;
        padding: 8px 15px;
        border-radius: 8px;
        display: inline-block;
    }
    .logo-container img {
        max-height: 50px;
        width: auto;
    }
    
    /* Cards de métricas - cor FENAJUFE */
    .metric-card {
        background: white;
        padding: 1rem;
        border-radius: 8px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        border-left: 4px solid #8B0000;
    }
    
    /* Botões primários */
    .stButton > button[kind="primary"] {
        background-color: #8B0000 !important;
        border-color: #8B0000 !important;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #a00000 !important;
        border-color: #a00000 !important;
    }
    
    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: #f0f0f0;
        border-radius: 4px 4px 0 0;
    }
    .stTabs [aria-selected="true"] {
        background-color: #8B0000 !important;
        color: white !important;
    }
    
    /* Alertas */
    .alerta-critico {
        background: #fff5f5;
        border-left: 4px solid #8B0000;
        padding: 1rem;
        border-radius: 4px;
        margin: 0.5rem 0;
    }
    .alerta-alto {
        background: #fffaf0;
        border-left: 4px solid #dd6b20;
        padding: 1rem;
        border-radius: 4px;
        margin: 0.5rem 0;
    }
    
    /* Multiselect tags */
    .stMultiSelect [data-baseweb="tag"] {
        background-color: #8B0000 !important;
    }
    
    /* Quebra de texto nas tabelas */
    div[data-testid="stDataFrame"] * {
        white-space: normal !important;
        word-break: break-word !important;
    }
    
    /* Info boxes */
    .stAlert {
        border-radius: 8px;
    }
    
    /* Links */
    a {
        color: #8B0000 !important;
    }
    a:hover {
        color: #a00000 !important;
    }
    </style>
    """, unsafe_allow_html=True)

def carregar_configuracao_cliente() -> Optional[ConfiguracaoCliente]:
    """Carrega configuração do cliente a partir dos secrets do Streamlit"""
    try:
        # Ler seção cliente
        cliente_config = st.secrets.get("cliente", {})
        
        # Processar temas - converter de {nome, palavras, peso} para {tema: [palavras]}
        temas_processados = {}
        temas_raw = cliente_config.get("temas", {})
        
        # Se temas_raw é um objeto AttrDict do Streamlit, converter para dict
        if hasattr(temas_raw, 'to_dict'):
            temas_raw = temas_raw.to_dict()
        
        for tema_key, tema_data in temas_raw.items():
            if isinstance(tema_data, dict):
                # Formato: {nome: "...", palavras: [...], peso: N}
                palavras = tema_data.get("palavras", [])
                if hasattr(palavras, 'to_list'):
                    palavras = list(palavras)
                temas_processados[tema_key] = list(palavras)
            elif isinstance(tema_data, (list, tuple)):
                # Formato direto: [palavras]
                temas_processados[tema_key] = list(tema_data)
        
        # Se não encontrou temas, usar template
        if not temas_processados:
            temas_processados = TEMAS_TEMPLATE
        
        # Processar exclusões
        exclusoes = cliente_config.get("exclusoes", [])
        if hasattr(exclusoes, 'to_list'):
            exclusoes = list(exclusoes)
        
        # Processar comissões
        comissoes_config = cliente_config.get("comissoes", {})
        if hasattr(comissoes_config, 'to_dict'):
            comissoes_config = comissoes_config.to_dict()
        
        comissoes = []
        if isinstance(comissoes_config, dict):
            comissoes = list(comissoes_config.get("prioritarias", [])) + list(comissoes_config.get("secundarias", []))
        elif isinstance(comissoes_config, (list, tuple)):
            comissoes = list(comissoes_config)
        
        # Ler Telegram dos secrets separados
        telegram_config = st.secrets.get("telegram", {})
        telegram_chat_id = telegram_config.get("chat_id", cliente_config.get("telegram_chat_id"))
        
        # Consolidar todas as palavras-chave dos temas como principais
        todas_palavras = []
        for palavras in temas_processados.values():
            todas_palavras.extend(palavras)
        
        return ConfiguracaoCliente(
            id_cliente=cliente_config.get("id", generate_client_hash(cliente_config.get("nome", "default"))),
            nome_cliente=cliente_config.get("nome", "Cliente Demo"),
            nome_exibicao=cliente_config.get("nome_completo", cliente_config.get("nome", "Cliente Demo")),
            temas=temas_processados,
            palavras_chave_principais=todas_palavras,
            palavras_chave_exclusao=list(exclusoes),
            comissoes_estrategicas=comissoes,
            telegram_chat_id=telegram_chat_id,
            emails_notificacao=list(cliente_config.get("emails", [])),
            plano=cliente_config.get("plano", "professional")
        )
    except Exception as e:
        st.warning(f"Usando configuração demo: {e}")
        # Retornar configuração demo
        return ConfiguracaoCliente(
            id_cliente="demo",
            nome_cliente="Empresa Demo",
            nome_exibicao="Empresa Demo S.A.",
            temas=TEMAS_TEMPLATE,
            palavras_chave_principais=["reforma tributária", "medicamento", "energia"],
            comissoes_estrategicas=["CFT", "CSSF", "CME"]
        )


def verificar_autenticacao() -> bool:
    """Sistema de autenticação simples"""
    # Se já está autenticado, retornar True
    if st.session_state.get("autenticado", False):
        return True
    
    # Tentar ler credenciais dos secrets
    try:
        usuarios = st.secrets.get("auth", {}).get("usuarios", {})
        if hasattr(usuarios, 'to_dict'):
            usuarios = usuarios.to_dict()
    except Exception:
        usuarios = {}
    
    # Se não há usuários configurados, permitir acesso direto
    if not usuarios:
        st.session_state["autenticado"] = True
        st.session_state["usuario"] = "visitante"
        return True
    
    # Carregar logo (com cache)
    logo_base64 = carregar_logo_base64()
    
    if logo_base64:
        logo_html = f'<img src="data:image/png;base64,{logo_base64}" alt="FENAJUFE" style="max-width: 200px; margin-bottom: 1rem;">'
    else:
        logo_html = ''
    
    # Mostrar tela de login com identidade FENAJUFE
    st.markdown(f"""
    <div style="max-width: 420px; margin: 80px auto; padding: 2rem; 
                background: white; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);
                border-top: 4px solid #8B0000;">
        <div style="text-align: center; margin-bottom: 1.5rem;">
            {logo_html}
            <h2 style="color: #8B0000; margin: 0;">⚖️ Monitor Legislativo</h2>
            <p style="color: #666; margin-top: 0.5rem;">FENAJUFE - Faça login para acessar</p>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form"):
            usuario = st.text_input("👤 Usuário", placeholder="Digite seu usuário")
            senha = st.text_input("🔑 Senha", type="password", placeholder="Digite sua senha")
            submit = st.form_submit_button("Entrar", use_container_width=True)
            
            if submit:
                if usuario in usuarios and usuarios[usuario] == senha:
                    st.session_state["autenticado"] = True
                    st.session_state["usuario"] = usuario
                    st.rerun()
                else:
                    st.error("❌ Usuário ou senha inválidos")
    
    return False

# ============================================================
# INTERFACE PRINCIPAL
# ============================================================

def render_header(config: ConfiguracaoCliente):
    """Renderiza cabeçalho da aplicação com logo FENAJUFE"""
    
    # Carregar logo (com cache)
    logo_base64 = carregar_logo_base64()
    
    # Construir HTML do header
    if logo_base64:
        logo_html = f'<img src="data:image/png;base64,{logo_base64}" alt="FENAJUFE" style="height: 45px; width: auto;">'
    else:
        logo_html = '<span style="font-size: 2rem;">⚖️</span>'
    
    st.markdown(f"""
    <div style="background: linear-gradient(135deg, #8B0000 0%, #1a1a2e 100%); 
                padding: 1rem 1.5rem; border-radius: 10px; margin-bottom: 1rem;
                display: flex; align-items: center; gap: 20px; flex-wrap: wrap;">
        <div style="background: white; padding: 8px 15px; border-radius: 8px; min-height: 50px; display: flex; align-items: center;">
            {logo_html}
        </div>
        <div style="flex: 1;">
            <h1 style="color: white; margin: 0; font-size: 1.6rem;">Monitor Legislativo</h1>
            <p style="color: #e0e0e0; margin: 0.2rem 0 0 0; font-size: 0.85rem;">
                Monitoramento Legislativo Automatizado | {config.nome_exibicao}
            </p>
        </div>
    </div>
    """, unsafe_allow_html=True)

def render_metricas_resumo(df: pd.DataFrame):
    """Renderiza cards de métricas resumo"""
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        total = len(df)
        st.metric("📊 Total Monitorado", total)
    
    with col2:
        criticos = len(df[df['Nível Alerta'] == 'CRITICO']) if 'Nível Alerta' in df.columns else 0
        st.metric("🚨 Críticos", criticos, delta=None if criticos == 0 else "Ação imediata")
    
    with col3:
        altos = len(df[df['Nível Alerta'] == 'ALTO']) if 'Nível Alerta' in df.columns else 0
        st.metric("⚠️ Alta Prioridade", altos)
    
    with col4:
        # Calcular média de score
        if 'Score' in df.columns and len(df) > 0:
            media_score = df['Score'].astype(float).mean()
            st.metric("📈 Score Médio", f"{media_score:.0f}")
        else:
            st.metric("📈 Score Médio", "N/A")

def render_tabela_materias(df: pd.DataFrame, key_suffix: str = ""):
    """Renderiza tabela de matérias com formatação e seleção"""
    if df.empty:
        st.info("Nenhuma matéria encontrada com os critérios atuais.")
        return None
    
    # Colunas para exibição (estilo Zanatta)
    colunas_exibir = [
        "Alerta", "Proposição", "Tipo", "Ano", "Situação atual", "Órgão (sigla)", 
        "Relator(a)", "Último andamento", "Data do status", "Parado (dias)",
        "Link", "Ementa"
    ]
    colunas_exibir = [c for c in colunas_exibir if c in df.columns]
    
    # Tabela com seleção
    sel = st.dataframe(
        df[colunas_exibir],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Alerta": st.column_config.TextColumn("", width="small", help="🚨 ≤2d | ⚠️ ≤5d | 🔔 ≤15d"),
            "Link": st.column_config.LinkColumn("Link", display_text="abrir"),
            "Ementa": st.column_config.TextColumn("Ementa", width="large"),
            "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100),
            "Parado (dias)": st.column_config.NumberColumn("Parado (dias)", format="%d"),
        },
        key=f"df_materias{key_suffix}"
    )
    
    st.caption("🚨 ≤2 dias (URGENTÍSSIMO) | ⚠️ ≤5 dias (URGENTE) | 🔔 ≤15 dias (Recente)")
    
    return sel

def main():
    """Função principal da aplicação"""
    configurar_pagina()
    
    # Verificar autenticação ANTES de carregar qualquer coisa
    if not verificar_autenticacao():
        return
    
    # Carregar configuração do cliente
    config = carregar_configuracao_cliente()
    if not config:
        st.error("Erro ao carregar configuração do cliente.")
        return
    
    # Cabeçalho com logout
    col_header, col_logout = st.columns([9, 1])
    with col_header:
        render_header(config)
    with col_logout:
        st.markdown("<br>", unsafe_allow_html=True)
        st.caption(f"👤 {st.session_state.get('usuario', 'visitante')}")
        if st.button("🚪 Sair", key="logout"):
            st.session_state["autenticado"] = False
            st.session_state.pop("usuario", None)
            st.rerun()
    
    # ============================================================
    # FILTROS INLINE (sem sidebar)
    # ============================================================
    st.markdown("### ⚙️ Filtros de Busca")
    
    col_periodo1, col_periodo2, col_tipos, col_niveis = st.columns([1, 1, 2, 2])
    
    hoje = datetime.date.today()
    with col_periodo1:
        data_inicio = st.date_input("📅 De", hoje - datetime.timedelta(days=7), key="filtro_data_inicio")
    with col_periodo2:
        data_fim = st.date_input("📅 Até", hoje, key="filtro_data_fim")
    with col_tipos:
        tipos_selecionados = st.multiselect(
            "📋 Tipos",
            options=["PL", "PLP", "PEC", "MPV", "PDL", "PRC", "REQ"],
            default=["PL", "PLP", "PEC", "MPV"],
            key="filtro_tipos"
        )
    with col_niveis:
        niveis_selecionados = st.multiselect(
            "🚨 Prioridade",
            options=["CRITICO", "ALTO", "MEDIO", "BAIXO", "INFO"],
            default=["CRITICO", "ALTO", "MEDIO"],
            key="filtro_niveis"
        )
    
    # Segunda linha de filtros
    col_temas, col_busca, col_btn = st.columns([3, 3, 1])
    
    with col_temas:
        temas_disponiveis = list(config.temas.keys())
        temas_selecionados = st.multiselect(
            "🏷️ Temas",
            options=temas_disponiveis,
            default=[],
            key="filtro_temas"
        )
    with col_busca:
        busca_texto = st.text_input(
            "🔍 Buscar",
            value="",
            placeholder="PL 1234/2025 ou palavra-chave...",
            key="filtro_busca"
        )
    with col_btn:
        st.markdown("<br>", unsafe_allow_html=True)
        btn_carregar = st.button("▶️ Carregar", type="primary", key="btn_carregar_dados")
    
    # Info do cliente
    st.caption(f"📌 **{config.nome_exibicao}** | Temas: {len(config.temas)} | Palavras-chave: {len(config.palavras_chave_principais)}")
    
    st.markdown("---")
    
    # Consolidar filtros
    filtros = {
        "data_inicio": data_inicio,
        "data_fim": data_fim,
        "tipos": tipos_selecionados,
        "niveis": niveis_selecionados,
        "temas": temas_selecionados,
        "busca": busca_texto
    }
    
    # Criar abas
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Dashboard",
        "📋 Matérias",
        "📅 Agenda",
        "📈 Relatórios",
        "⚙️ Configurações"
    ])
    
    # ============================================================
    # ABA 1: DASHBOARD
    # ============================================================
    with tab1:
        st.markdown("### Visão Geral do Monitoramento")
        
        # Botão limpar cache
        col_cache, col_info = st.columns([1, 4])
        with col_cache:
            if st.button("🧹 Limpar cache", key="limpar_cache_tab1"):
                fetch_proposicao_detalhes.cache_clear()
                fetch_tramitacoes.cache_clear()
                fetch_status_proposicao.cache_clear()
                st.session_state.pop("df_matches", None)
                st.session_state.pop("props_dict", None)
                st.session_state.pop("matches", None)
                st.session_state.pop("status_map", None)
                st.success("✅ Cache limpo!")
                st.rerun()
        with col_info:
            st.info("💡 Matérias arquivadas, transformadas em lei, com perda de eficácia ou retiradas são automaticamente excluídas.")
        
        # Carregar dados automaticamente ou quando clicar no botão
        if btn_carregar or 'df_matches' not in st.session_state:
            with st.spinner("Carregando proposições..."):
                proposicoes = buscar_proposicoes_periodo(
                    filtros["data_inicio"],
                    filtros["data_fim"],
                    filtros["tipos"]
                )
                
                if proposicoes:
                    st.caption(f"📋 {len(proposicoes)} proposições encontradas no período")
                    
                    # Processar matches
                    with st.spinner("Aplicando filtros de interesse..."):
                        matches = processar_proposicoes_para_cliente(proposicoes, config)
                    
                    st.caption(f"✅ {len(matches)} proposições relevantes em tramitação")
                    
                    # Criar dicionário de proposições para referência
                    props_dict = {str(p.get("id", "")): p for p in proposicoes}
                    
                    # Buscar status atualizado
                    with st.spinner("Carregando status atualizado..."):
                        ids_matches = [m.proposicao_id for m in matches]
                        status_map = build_status_map(ids_matches)
                    
                    # Criar DataFrame
                    df_matches = criar_dataframe_matches(matches, props_dict, status_map)
                    
                    # Aplicar filtros adicionais
                    if filtros["niveis"]:
                        df_matches = df_matches[df_matches['Nível Alerta'].isin(filtros["niveis"])]
                    
                    if filtros["temas"]:
                        mask = df_matches['Temas Match'].apply(
                            lambda x: any(t in x for t in filtros["temas"]) if x else False
                        )
                        df_matches = df_matches[mask]
                    
                    # Filtro de busca
                    if filtros["busca"].strip():
                        busca_lower = filtros["busca"].lower()
                        mask = df_matches.apply(lambda row: busca_lower in str(row).lower(), axis=1)
                        df_matches = df_matches[mask]
                    
                    # Armazenar no session state
                    st.session_state['df_matches'] = df_matches
                    st.session_state['props_dict'] = props_dict
                    st.session_state['matches'] = matches
                    st.session_state['status_map'] = status_map
                    
                    # Métricas resumo
                    render_metricas_resumo(df_matches)
                    
                    st.markdown("---")
                    
                    # Alertas críticos (baseado em dias parado)
                    if 'Parado (dias)' in df_matches.columns:
                        df_urgentes = df_matches[df_matches['Parado (dias)'].fillna(999) <= 5].head(5)
                        if not df_urgentes.empty:
                            st.markdown("### 🚨 Movimentação Recente (≤5 dias)")
                            for _, row in df_urgentes.iterrows():
                                emoji = row.get('Alerta', '')
                                st.markdown(f"""
                                **{emoji} {row['Proposição']}** - {row.get('Situação atual', '')}
                                - Órgão: {row.get('Órgão (sigla)', '')} | Parado: {row.get('Parado (dias)', 'N/A')} dias
                                - Temas: {row.get('Temas Match', '')}
                                """)
                    
                    # Gráfico de distribuição
                    st.markdown("### 📊 Distribuição por Nível de Alerta")
                    if 'Nível Alerta' in df_matches.columns:
                        dist = df_matches['Nível Alerta'].value_counts()
                        st.bar_chart(dist)
                
                else:
                    st.info("Nenhuma proposição encontrada no período selecionado.")
    
    # ============================================================
    # ABA 2: MATÉRIAS
    # ============================================================
    with tab2:
        st.markdown("### 📋 Lista de Matérias Monitoradas")
        
        # Botão limpar cache
        col_cache2, col_space2 = st.columns([1, 4])
        with col_cache2:
            if st.button("🧹 Limpar cache", key="limpar_cache_tab2"):
                fetch_proposicao_detalhes.cache_clear()
                fetch_tramitacoes.cache_clear()
                fetch_status_proposicao.cache_clear()
                st.session_state.clear()
                st.success("✅ Cache limpo!")
                st.rerun()
        
        if 'df_matches' in st.session_state and not st.session_state['df_matches'].empty:
            df = st.session_state['df_matches'].copy()
            
            # Filtros no estilo Zanatta
            st.markdown("#### 🗂️ Filtros de Proposições")
            col_ano, col_tipo = st.columns(2)
            
            with col_ano:
                if 'Ano' in df.columns:
                    anos_disp = sorted([a for a in df["Ano"].dropna().unique() if str(a).isdigit()], reverse=True)
                    anos_sel = st.multiselect("Ano", options=anos_disp, default=anos_disp[:3] if len(anos_disp) >= 3 else anos_disp, key="anos_tab2")
                else:
                    anos_sel = []
            
            with col_tipo:
                if 'Tipo' in df.columns:
                    tipos_disp = sorted([t for t in df["Tipo"].dropna().unique() if str(t).strip()])
                    tipos_sel = st.multiselect("Tipo", options=tipos_disp, default=tipos_disp, key="tipos_tab2")
                else:
                    tipos_sel = []
            
            # Aplicar filtros
            if anos_sel:
                df = df[df["Ano"].isin(anos_sel)]
            if tipos_sel:
                df = df[df["Tipo"].isin(tipos_sel)]
            
            # Campo de busca
            busca = st.text_input(
                "Filtrar proposições",
                value="",
                placeholder="Ex.: PL 1234/2025 | 'servidor' | 'previdência'",
                help="Busque por sigla/número/ano ou palavras na ementa",
                key="busca_tab2"
            )
            
            if busca.strip():
                busca_lower = busca.lower()
                mask = df.apply(lambda row: busca_lower in str(row).lower(), axis=1)
                df = df[mask]
            
            st.caption(f"Resultados: {len(df)} proposições")
            
            # Tabela com seleção
            sel = render_tabela_materias(df, "_tab2")
            
            # Mostrar detalhes da proposição selecionada
            if sel and sel.selection and sel.selection.rows:
                idx_selecionado = sel.selection.rows[0]
                if idx_selecionado < len(df):
                    row_selecionada = df.iloc[idx_selecionado]
                    
                    st.markdown("---")
                    st.markdown("### 📄 Detalhes da Proposição Selecionada")
                    
                    col_det1, col_det2 = st.columns(2)
                    
                    with col_det1:
                        st.markdown(f"**Proposição:** {row_selecionada.get('Proposição', 'N/A')}")
                        st.markdown(f"**Tipo:** {row_selecionada.get('Tipo', 'N/A')}")
                        st.markdown(f"**Ano:** {row_selecionada.get('Ano', 'N/A')}")
                        st.markdown(f"**Situação:** {row_selecionada.get('Situação atual', 'N/A')}")
                        st.markdown(f"**Órgão:** {row_selecionada.get('Órgão (sigla)', 'N/A')}")
                        st.markdown(f"**Relator(a):** {row_selecionada.get('Relator(a)', 'Aguardando')}")
                    
                    with col_det2:
                        st.markdown(f"**Nível de Alerta:** {row_selecionada.get('Nível Alerta', 'N/A')}")
                        st.markdown(f"**Score:** {row_selecionada.get('Score', 'N/A')}")
                        st.markdown(f"**Parado há:** {row_selecionada.get('Parado (dias)', 'N/A')} dias")
                        st.markdown(f"**Data do Status:** {row_selecionada.get('Data do status', 'N/A')}")
                        st.markdown(f"**Temas:** {row_selecionada.get('Temas Match', 'N/A')}")
                        st.markdown(f"**Palavras Match:** {row_selecionada.get('Palavras Match', 'N/A')}")
                    
                    st.markdown("**Ementa:**")
                    st.info(row_selecionada.get('Ementa', 'Sem ementa disponível'))
                    
                    st.markdown(f"**Último andamento:** {row_selecionada.get('Último andamento', 'N/A')}")
                    
                    st.markdown(f"**Autor(es):** {row_selecionada.get('Autor', 'N/A')}")
                    
                    # Link para a Câmara
                    link = row_selecionada.get('Link', '')
                    if link:
                        st.markdown(f"🔗 [Abrir na Câmara dos Deputados]({link})")
            
            # Downloads
            st.markdown("---")
            col1, col2 = st.columns(2)
            
            with col1:
                try:
                    xlsx_bytes, xlsx_mime, xlsx_ext = to_xlsx_bytes(df, "Matérias")
                    st.download_button(
                        "⬇️ Baixar Excel",
                        data=xlsx_bytes,
                        file_name=f"materias_monitoradas_{datetime.date.today()}.xlsx",
                        mime=xlsx_mime,
                        key="download_xlsx_tab2"
                    )
                except Exception as e:
                    st.error(f"Erro ao gerar Excel: {e}")
            
            with col2:
                try:
                    pdf_bytes, pdf_mime, pdf_ext = gerar_relatorio_pdf(
                        df,
                        "Relatório de Matérias Monitoradas",
                        f"Período: {filtros['data_inicio']} a {filtros['data_fim']}",
                        config
                    )
                    st.download_button(
                        "⬇️ Baixar PDF",
                        data=pdf_bytes,
                        file_name=f"relatorio_materias_{datetime.date.today()}.pdf",
                        mime=pdf_mime
                    )
                except Exception as e:
                    st.warning(f"Erro ao gerar PDF: {e}")
        else:
            st.info("Carregue os dados na aba Dashboard primeiro.")
    
    # ============================================================
    # ABA 3: AGENDA DA SEMANA
    # ============================================================
    with tab3:
        st.markdown("### 📅 Agenda Legislativa")
        
        st.info("💡 Busque eventos e pautas de comissões para um período específico. A API da Câmara pode não ter eventos para todas as datas.")
        
        col1, col2 = st.columns(2)
        with col1:
            agenda_inicio = st.date_input(
                "Data inicial",
                datetime.date.today() - datetime.timedelta(days=7),
                key="agenda_inicio"
            )
        with col2:
            agenda_fim = st.date_input(
                "Data final",
                datetime.date.today() + datetime.timedelta(days=7),
                key="agenda_fim"
            )
        
        # Validar período
        if agenda_fim < agenda_inicio:
            st.error("❌ Data final deve ser maior que data inicial")
        elif (agenda_fim - agenda_inicio).days > 60:
            st.warning("⚠️ Período muito longo. Recomendamos no máximo 60 dias.")
        
        if st.button("🔄 Buscar Eventos", type="primary", key="btn_agenda"):
            with st.spinner("Buscando eventos na API da Câmara..."):
                try:
                    # Buscar eventos diretamente
                    eventos = buscar_eventos_periodo(agenda_inicio, agenda_fim, config.comissoes_estrategicas)
                    
                    if eventos:
                        st.success(f"✅ {len(eventos)} eventos encontrados no período")
                        
                        # Organizar por data
                        eventos_por_dia = {}
                        for evento in eventos:
                            data_evento = evento.get("dataHoraInicio", "")[:10]
                            if data_evento:
                                if data_evento not in eventos_por_dia:
                                    eventos_por_dia[data_evento] = []
                                eventos_por_dia[data_evento].append(evento)
                        
                        # Exibir por dia
                        for data, eventos_dia in sorted(eventos_por_dia.items()):
                            with st.expander(f"📅 {data} ({len(eventos_dia)} eventos)", expanded=True):
                                for evento in eventos_dia:
                                    hora = evento.get("dataHoraInicio", "")[11:16] if evento.get("dataHoraInicio") else ""
                                    orgaos = ", ".join([o.get("sigla", "") for o in evento.get("orgaos", [])])
                                    descricao = evento.get("descricaoTipo", "") or evento.get("descricao", "")
                                    local = evento.get("localExterno", "") or evento.get("localCamara", {}).get("nome", "")
                                    situacao = evento.get("descricaoSituacao", "")
                                    
                                    st.markdown(f"""
                                    **⏰ {hora}** - **{orgaos}**
                                    
                                    📋 {descricao}
                                    
                                    📍 Local: {local or 'Não informado'}
                                    
                                    📊 Situação: {situacao or 'Não informada'}
                                    """)
                                    
                                    # Buscar pauta do evento
                                    evento_id = str(evento.get("id", ""))
                                    if evento_id:
                                        pauta = buscar_pauta_evento(evento_id)
                                        if pauta:
                                            st.markdown(f"**📜 Pauta ({len(pauta)} itens):**")
                                            for item_pauta in pauta[:5]:  # Limitar a 5 itens
                                                prop_titulo = item_pauta.get("titulo", "")
                                                prop_ementa = item_pauta.get("ementa", "")[:150]
                                                st.caption(f"• {prop_titulo}: {prop_ementa}...")
                                    
                                    st.markdown("---")
                    else:
                        st.warning(f"""
                        ⚠️ Nenhum evento encontrado no período de {agenda_inicio} a {agenda_fim}.
                        
                        **Possíveis motivos:**
                        - Não há reuniões agendadas neste período
                        - Período de recesso parlamentar
                        - Eventos ainda não cadastrados na API
                        
                        **Sugestões:**
                        - Tente um período diferente
                        - Verifique datas próximas à semana atual
                        - Para eventos passados, dados podem não estar disponíveis
                        """)
                        
                except Exception as e:
                    st.error(f"❌ Erro ao buscar eventos: {e}")
        
        # Opção de buscar pautas específicas de comissões
        st.markdown("---")
        st.markdown("#### 🏛️ Comissões Monitoradas")
        comissoes_str = ", ".join(config.comissoes_estrategicas) if config.comissoes_estrategicas else "Todas"
        st.caption(f"Comissões configuradas: **{comissoes_str}**")
    
    # ============================================================
    # ABA 4: RELATÓRIOS
    # ============================================================
    with tab4:
        st.markdown("### 📈 Geração de Relatórios")
        
        tipo_relatorio = st.selectbox(
            "Tipo de Relatório",
            options=[
                "Agenda Semanal",
                "Retrospectiva Semanal",
                "Relatório Executivo Mensal",
                "Matérias por Tema"
            ]
        )
        
        if st.button("📄 Gerar Relatório", type="primary"):
            if 'df_matches' not in st.session_state:
                st.warning("Carregue os dados primeiro na aba Dashboard.")
            else:
                with st.spinner("Gerando relatório..."):
                    df = st.session_state['df_matches']
                    
                    try:
                        pdf_bytes, _, _ = gerar_relatorio_pdf(
                            df,
                            tipo_relatorio,
                            f"Gerado em {get_brasilia_now().strftime('%d/%m/%Y %H:%M')}",
                            config
                        )
                        
                        st.download_button(
                            "⬇️ Baixar Relatório PDF",
                            data=pdf_bytes,
                            file_name=f"relatorio_{datetime.date.today()}.pdf",
                            mime="application/pdf"
                        )
                        
                        st.success("Relatório gerado com sucesso!")
                    except Exception as e:
                        st.error(f"Erro ao gerar relatório: {e}")
    
    # ============================================================
    # ABA 5: CONFIGURAÇÕES
    # ============================================================
    with tab5:
        st.markdown("### ⚙️ Configurações do Monitoramento")
        
        st.markdown("#### 📝 Temas Configurados")
        for tema, palavras in config.temas.items():
            with st.expander(f"**{tema}** ({len(palavras)} palavras)"):
                st.write(", ".join(palavras))
        
        st.markdown("#### 🔑 Palavras-chave Principais")
        st.write(", ".join(config.palavras_chave_principais) if config.palavras_chave_principais else "Nenhuma configurada")
        
        st.markdown("#### 🏛️ Comissões Estratégicas")
        st.write(", ".join(config.comissoes_estrategicas) if config.comissoes_estrategicas else "Todas")
        
        st.markdown("---")
        st.markdown("#### ℹ️ Informações do Sistema")
        st.caption(f"Versão: 1.0.0")
        st.caption(f"Última atualização: {get_brasilia_now().strftime('%d/%m/%Y %H:%M')}")
        st.caption(f"API: dadosabertos.camara.leg.br")
        
        st.markdown("---")
        st.markdown("#### 📧 Suporte")
        st.info("Para alterações na configuração de monitoramento, entre em contato com sua consultoria.")

    # Footer
    st.markdown("---")
    st.caption("Monitor de Interesses | Desenvolvido para monitoramento legislativo corporativo | Dados: API Dados Abertos da Câmara dos Deputados")

if __name__ == "__main__":
    main()