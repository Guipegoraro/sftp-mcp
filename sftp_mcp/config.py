"""Perfis de servidor SFTP gravados em ~/.sftp-mcp/perfis.toml.

O arquivo nao guarda segredo: senha e passphrase ficam no keyring do sistema
(ver credenciais.py). A chave publica do servidor fica aqui porque e ela que
fixa a identidade do host.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import tomli_w

AMBIENTES = ("prod", "hml", "dev")
AUTENTICACOES = ("senha", "chave")
NOME_VALIDO = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ErroConfig(Exception):
    pass


def diretorio_config() -> Path:
    # SFTP_MCP_HOME existe para os testes e para quem quer a config em outro lugar.
    return Path(os.environ.get("SFTP_MCP_HOME") or Path.home() / ".sftp-mcp")


def arquivo_perfis() -> Path:
    return diretorio_config() / "perfis.toml"


@dataclass
class Perfil:
    nome: str
    host: str
    usuario: str
    host_key: str
    porta: int = 22
    autenticacao: str = "senha"
    chave_privada: str = ""
    ambiente: str = "dev"
    tspace_cliente: str = ""
    repositorio: str = ""
    pasta_inicial: str = ""
    raizes_leitura: list[str] = field(default_factory=list)
    raizes_escrita: list[str] = field(default_factory=list)
    timeout_conexao: int = 15
    ocioso_max_segundos: int = 300
    max_leitura_bytes: int = 1_048_576
    busca_profundidade: int = 5
    busca_max_resultados: int = 2000

    def validar(self) -> None:
        if not NOME_VALIDO.match(self.nome):
            raise ErroConfig(
                f"Nome de perfil invalido: {self.nome!r}. Use letras minusculas, numeros e hifen."
            )
        if not self.host or not self.usuario:
            raise ErroConfig(f"Perfil {self.nome}: host e usuario sao obrigatorios.")
        if re.search(r"[/:@\s]", self.host):
            raise ErroConfig(
                f"Perfil {self.nome}: host {self.host!r} deve ser so o nome, sem sftp://, usuario ou porta."
            )
        if not self.host_key:
            raise ErroConfig(
                f"Perfil {self.nome}: sem host_key. Cadastre pelo setup para fixar a chave do servidor."
            )
        if self.autenticacao not in AUTENTICACOES:
            raise ErroConfig(f"Perfil {self.nome}: autenticacao deve ser {' ou '.join(AUTENTICACOES)}.")
        if self.autenticacao == "chave" and not self.chave_privada:
            raise ErroConfig(f"Perfil {self.nome}: autenticacao por chave sem chave_privada.")
        if self.ambiente not in AMBIENTES:
            raise ErroConfig(f"Perfil {self.nome}: ambiente deve ser {', '.join(AMBIENTES)}.")
        if self.pasta_inicial and not self.pasta_inicial.startswith("/"):
            raise ErroConfig(f"Perfil {self.nome}: pasta_inicial precisa ser caminho absoluto.")
        for raiz in self.raizes_leitura + self.raizes_escrita:
            if not raiz.startswith("/"):
                raise ErroConfig(f"Perfil {self.nome}: raiz {raiz!r} precisa ser caminho absoluto.")


_CAMPOS = {f.name for f in fields(Perfil)}


def carregar_perfis() -> dict[str, Perfil]:
    arquivo = arquivo_perfis()
    if not arquivo.exists():
        return {}
    with arquivo.open("rb") as f:
        dados = tomllib.load(f)
    perfis: dict[str, Perfil] = {}
    for nome, valores in (dados.get("perfis") or {}).items():
        desconhecidos = set(valores) - _CAMPOS
        if desconhecidos:
            raise ErroConfig(f"Perfil {nome}: campos desconhecidos {sorted(desconhecidos)}.")
        perfil = Perfil(nome=nome, **{k: v for k, v in valores.items() if k != "nome"})
        perfil.validar()
        perfis[nome] = perfil
    return perfis


def obter_perfil(nome: str) -> Perfil:
    perfis = carregar_perfis()
    if nome not in perfis:
        disponiveis = ", ".join(sorted(perfis)) or "nenhum cadastrado"
        raise ErroConfig(f"Perfil {nome!r} nao existe. Perfis: {disponiveis}.")
    return perfis[nome]


def salvar_perfis(perfis: dict[str, Perfil]) -> None:
    dados = {"perfis": {}}
    for nome in sorted(perfis):
        perfil = perfis[nome]
        perfil.validar()
        valores = asdict(perfil)
        del valores["nome"]
        dados["perfis"][nome] = valores
    arquivo = arquivo_perfis()
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo.with_suffix(".toml.tmp")
    temporario.write_bytes(tomli_w.dumps(dados).encode("utf-8"))
    os.replace(temporario, arquivo)


def salvar_perfil(perfil: Perfil) -> None:
    perfis = carregar_perfis()
    perfis[perfil.nome] = perfil
    salvar_perfis(perfis)
