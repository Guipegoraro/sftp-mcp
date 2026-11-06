"""Cadastro de perfil a partir dos dados que o TCloud mostra: URL, usuario e senha.

A janela (janela.py) e o CLI (setup.py) usam estas funcoes. A janela roda em processo
proprio para a senha nao passar pelo servidor MCP nem pelo modelo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import armazenamento, credenciais
from .armazenamento import raiz_git
from .conexao import GerenciadorConexoes, chave_do_servidor
from .config import NOME_VALIDO, Perfil, carregar_perfis, salvar_perfil
from .operacoes import ErroOperacao

AMBIENTE_PADRAO = "dev"
_TEMPO_JANELA = 600
_DEDUCAO_AMBIENTE = (
    ("prod", re.compile(r"(^|[_-])(prod|production|producao)($|[_-])", re.IGNORECASE)),
    ("hml", re.compile(r"(^|[_-])(hml|homolog|homologacao|staging|stage|test|teste|qa)($|[_-])", re.IGNORECASE)),
    ("dev", re.compile(r"(^|[_-])(dev|development|desenvolvimento)($|[_-])", re.IGNORECASE)),
)


@dataclass
class DadosCadastro:
    nome: str
    url: str
    usuario: str
    senha: str = field(repr=False)
    ambiente: str
    repositorio: str = ""


def separar_url(texto: str) -> tuple[str, int | None, str]:
    """Aceita 'host', 'host:porta', 'usuario@host' e 'sftp://host:porta/rota'."""
    valor = re.sub(r"^[a-z][a-z0-9+.-]*://", "", texto.strip(), flags=re.IGNORECASE)
    autoridade, barra, rota = valor.partition("/")
    host, _, porta = autoridade.rsplit("@", 1)[-1].partition(":")
    rota = ("/" + rota).rstrip("/") if barra else ""
    return host, int(porta) if porta.isdigit() else None, rota


def deduzir_ambiente(usuario: str) -> str:
    """Le o sufixo do usuario do TCloud (ftp_XXXXXX_development). Sem sufixo conhecido, usa o padrao."""
    for ambiente, padrao in _DEDUCAO_AMBIENTE:
        if padrao.search(usuario):
            return ambiente
    return AMBIENTE_PADRAO


def erro_repositorio(texto: str) -> str | None:
    if not texto:
        return None
    pasta = Path(texto).expanduser()
    if not pasta.is_dir():
        return f"Pasta nao existe: {pasta}"
    if raiz_git(pasta) is None:
        return f"{pasta} nao e repositorio git."
    return None


def erro_dados(dados: DadosCadastro) -> str | None:
    if not NOME_VALIDO.match(dados.nome):
        return "Nome do perfil: use letras minusculas, numeros e hifen."
    if dados.nome in carregar_perfis():
        return f"O perfil {dados.nome} ja existe."
    if not separar_url(dados.url)[0]:
        return "Informe a URL do SFTP."
    if not dados.usuario:
        return "Informe o usuario."
    if not dados.senha:
        return "Informe a senha."
    return erro_repositorio(dados.repositorio)


def chave_conhecida(chave: str) -> bool:
    """Chave ja aceita em outro perfil: no TCloud varios ambientes ficam atras do mesmo gateway."""
    return any(p.host_key == chave for p in carregar_perfis().values())


async def obter_chave(dados: DadosCadastro) -> str:
    host, porta, _ = separar_url(dados.url)
    return await chave_do_servidor(host, porta or 22)


async def registrar(perfil: Perfil, segredo: str, repositorio: str = "") -> dict:
    """Testa o login com o segredo so em memoria e grava por ultimo: falha nao deixa nada para tras."""
    perfil.validar()
    gerenciador = GerenciadorConexoes()
    try:
        async with gerenciador.usar(perfil, segredo) as sftp:
            await sftp.realpath(".")
    finally:
        await gerenciador.fechar_todas()
    aviso = None
    if repositorio:
        raiz = raiz_git(Path(repositorio).expanduser())
        if raiz is None:
            raise ErroOperacao(f"{repositorio} nao e repositorio git.")
        perfil.repositorio = str(raiz)
        armazenamento.pasta_perfil(perfil)
        aviso = armazenamento.aviso_git(raiz, perfil.nome)
    # O dialogo da chave nao tem prazo: outro cadastro pode ter gravado o nome nesse meio tempo.
    if perfil.nome in carregar_perfis():
        raise ErroOperacao(f"O perfil {perfil.nome} ja existe.")
    if segredo:
        credenciais.gravar(perfil.nome, "senha" if perfil.autenticacao == "senha" else "passphrase", segredo)
    try:
        salvar_perfil(perfil)
    except BaseException:
        credenciais.remover(perfil.nome)
        raise
    if perfil.repositorio:
        with contextlib.suppress(OSError):
            armazenamento.auditar(perfil, "cadastrar", "ok", host=perfil.host, repositorio=perfil.repositorio)
    return {
        "status": "cadastrado",
        "perfil": perfil.nome,
        "host": perfil.host,
        "porta": perfil.porta,
        "ambiente": perfil.ambiente,
        "pasta_inicial": perfil.pasta_inicial,
        "repositorio": perfil.repositorio,
        "aviso": aviso,
    }


async def concluir(dados: DadosCadastro, chave: str) -> dict:
    host, porta, rota = separar_url(dados.url)
    perfil = Perfil(
        nome=dados.nome, host=host, porta=porta or 22, usuario=dados.usuario, host_key=chave,
        ambiente=dados.ambiente, pasta_inicial=rota,
    )
    return await registrar(perfil, dados.senha, dados.repositorio)


def comando_janela(perfil: str, repositorio: str) -> list[str]:
    # -P tira a pasta atual do sys.path: um keyring.py ou tkinter.py no repositorio aberto
    # rodaria no processo que recebe a senha.
    return [sys.executable, "-P", "-m", "sftp_mcp.janela", f"--perfil={perfil}", f"--repositorio={repositorio}"]


async def abrir_janela(perfil: str, repositorio: str) -> dict:
    """Abre a janela e espera. Devolve so o resultado: a senha fica no outro processo."""
    extras = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    # stdin precisa ser desligado: herdado, a janela disputaria o canal stdio do MCP.
    processo = await asyncio.create_subprocess_exec(
        *comando_janela(perfil, repositorio),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **extras,
    )
    try:
        saida, erros = await asyncio.wait_for(processo.communicate(), _TEMPO_JANELA)
    except asyncio.TimeoutError:
        raise ErroOperacao("A janela de cadastro ficou aberta tempo demais e foi fechada.") from None
    finally:
        # Tambem cobre a tool cancelada: a janela nao pode ficar aberta sem ninguem esperando.
        if processo.returncode is None:
            processo.kill()
            await processo.wait()
    if processo.returncode != 0:
        detalhe = erros.decode("utf-8", "replace").strip()[-500:]
        raise ErroOperacao(
            f"A janela de cadastro nao abriu ({detalhe}). Peca ao usuario para rodar sftp-mcp-setup adicionar."
        )
    return json.loads(saida)

