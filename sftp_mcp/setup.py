"""Cadastro de perfis SFTP pelo terminal. Senha e passphrase vao para o Credential Manager.

Uso:
    sftp-mcp-setup adicionar <perfil>
    sftp-mcp-setup listar
    sftp-mcp-setup testar <perfil>
    sftp-mcp-setup remover <perfil>
    sftp-mcp-setup trocar-fingerprint <perfil>
    sftp-mcp-setup definir-senha <perfil>
    sftp-mcp-setup vincular-repositorio <perfil> <pasta>
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys
from pathlib import Path

from . import credenciais
from .armazenamento import raiz_git
from .config import (
    AMBIENTES, AUTENTICACOES, ErroConfig, Perfil, carregar_perfis, obter_perfil, salvar_perfil, salvar_perfis,
)
from .conexao import ErroConexao, GerenciadorConexoes, chave_do_servidor, fingerprint
from .operacoes import ERROS_ESPERADOS, Operacoes


def _perguntar(texto: str, padrao: str = "", validar=None) -> str:
    """Repete a pergunta enquanto validar devolver mensagem de erro."""
    sufixo = f" [{padrao}]" if padrao else ""
    while True:
        resposta = input(f"{texto}{sufixo}: ").strip() or padrao
        erro = validar(resposta) if validar else None
        if not erro:
            return resposta
        print(f"  {erro}")


def _separar_host(texto: str) -> tuple[str, int | None]:
    """Aceita 'host', 'host:porta', 'usuario@host' e 'sftp://host:porta/pasta'."""
    valor = re.sub(r"^[a-z][a-z0-9+.-]*://", "", texto.strip(), flags=re.IGNORECASE)
    valor = valor.split("/", 1)[0].rsplit("@", 1)[-1]
    host, _, porta = valor.partition(":")
    return host, int(porta) if porta.isdigit() else None


def _erro_porta(texto: str) -> str | None:
    return None if texto.isdigit() and 0 < int(texto) < 65536 else "Porta deve ser um numero de 1 a 65535."


def _erro_repositorio(texto: str) -> str | None:
    if not texto:
        return None
    pasta = Path(texto).expanduser()
    if not pasta.is_dir():
        return f"Pasta nao existe: {pasta}. Deixe vazio para vincular depois."
    if raiz_git(pasta) is None:
        return f"{pasta} nao e repositorio git. Deixe vazio para vincular depois."
    return None


def _lista(texto: str) -> list[str]:
    return [p.strip() for p in texto.split(",") if p.strip()]


def _confirmar_chave(host: str, porta: int, atual: str | None = None) -> str:
    chave = asyncio.run(chave_do_servidor(host, porta))
    algoritmo = chave.split()[0]
    if atual:
        print(f"Chave fixada hoje:   {atual.split()[0]} {fingerprint(atual)}")
    print(f"Chave do servidor:   {algoritmo} {fingerprint(chave)}")
    print("Confira o fingerprint com o responsavel pelo servidor antes de aceitar.")
    if input("Aceitar esta chave? Digite 'sim': ").strip().lower() != "sim":
        raise SystemExit("Chave nao aceita. Nada foi gravado.")
    return chave


def _ler_segredo(perfil: Perfil) -> None:
    if perfil.autenticacao == "senha":
        segredo = getpass.getpass("Senha: ")
        if not segredo:
            raise SystemExit("Senha vazia. Nada foi gravado.")
        credenciais.gravar(perfil.nome, "senha", segredo)
    else:
        segredo = getpass.getpass("Passphrase da chave (vazio se nao tiver): ")
        if segredo:
            credenciais.gravar(perfil.nome, "passphrase", segredo)


def _validar_chave_privada(caminho: str) -> None:
    arquivo = Path(caminho).expanduser()
    if not arquivo.is_file():
        raise SystemExit(f"Chave privada nao encontrada: {arquivo}")
    if arquivo.read_bytes()[:30].startswith(b"PuTTY-User-Key-File"):
        raise SystemExit(
            "Chave no formato PuTTY (.ppk) nao e suportada. No PuTTYgen: Load na chave, "
            "Conversions > Export OpenSSH key, e informe o arquivo exportado."
        )


def adicionar(nome: str) -> int:
    if nome in carregar_perfis():
        raise SystemExit(f"O perfil {nome} ja existe. Edite ~/.sftp-mcp/perfis.toml ou remova antes.")
    host, porta_no_host = _separar_host(
        _perguntar("Host (aceita sftp://host:porta)", validar=lambda t: None if _separar_host(t)[0] else "Host vazio.")
    )
    porta = int(_perguntar("Porta", str(porta_no_host or 22), _erro_porta))
    usuario = _perguntar("Usuario", validar=lambda t: None if t else "Usuario vazio.")
    autenticacao = _perguntar(
        "Autenticacao (senha/chave)", "senha",
        lambda t: None if t in AUTENTICACOES else "Responda senha ou chave.",
    )
    chave_privada = ""
    if autenticacao == "chave":
        chave_privada = str(Path(_perguntar("Caminho da chave privada (formato OpenSSH)")).expanduser())
        _validar_chave_privada(chave_privada)
    ambiente = _perguntar(
        f"Ambiente ({'/'.join(AMBIENTES)})", "dev",
        lambda t: None if t in AMBIENTES else f"Responda {', '.join(AMBIENTES)}.",
    )
    tspace_cliente = _perguntar("Codigo do cliente no tspace (opcional)")
    raizes_escrita = _lista(_perguntar("Raizes de escrita, separadas por virgula (vazio = sem escrita)"))
    raizes_leitura = _lista(_perguntar("Raizes de leitura, separadas por virgula (vazio = servidor todo)"))
    repositorio = _perguntar(
        "Pasta do repositorio do cliente (opcional, da para vincular depois)", validar=_erro_repositorio
    )

    host_key = _confirmar_chave(host, porta)
    perfil = Perfil(
        nome=nome, host=host, porta=porta, usuario=usuario, host_key=host_key,
        autenticacao=autenticacao, chave_privada=chave_privada, ambiente=ambiente,
        tspace_cliente=tspace_cliente, raizes_leitura=raizes_leitura, raizes_escrita=raizes_escrita,
    )
    perfil.validar()
    _ler_segredo(perfil)
    salvar_perfil(perfil)
    print(f"Perfil {nome} gravado.")
    if repositorio:
        vincular(nome, repositorio)
    return testar(nome)


def listar() -> int:
    perfis = carregar_perfis()
    if not perfis:
        print("Nenhum perfil cadastrado.")
        return 0
    for p in perfis.values():
        print(f"{p.nome:<24} {p.ambiente:<5} {p.usuario}@{p.host}:{p.porta}  repo={p.repositorio or '-'}")
    return 0


async def _testar(perfil: Perfil) -> None:
    gerenciador = GerenciadorConexoes()
    try:
        async with gerenciador.usar(perfil) as sftp:
            print(f"Conectou. Pasta inicial: {await sftp.realpath('.')}")
            for raiz in perfil.raizes_escrita + perfil.raizes_leitura:
                existe = await sftp.exists(raiz)
                print(f"  raiz {raiz}: {'ok' if existe else 'NAO EXISTE'}")
    finally:
        await gerenciador.fechar_todas()


def testar(nome: str) -> int:
    asyncio.run(_testar(obter_perfil(nome)))
    return 0


def remover(nome: str) -> int:
    perfis = carregar_perfis()
    if nome not in perfis:
        raise SystemExit(f"Perfil {nome} nao existe.")
    del perfis[nome]
    salvar_perfis(perfis)
    credenciais.remover(nome)
    print(f"Perfil {nome} e seus segredos removidos. A pasta .claude/sftp/{nome} do repositorio foi mantida.")
    return 0


def trocar_fingerprint(nome: str) -> int:
    perfil = obter_perfil(nome)
    perfil.host_key = _confirmar_chave(perfil.host, perfil.porta, atual=perfil.host_key)
    salvar_perfil(perfil)
    print("Chave atualizada.")
    return 0


def definir_senha(nome: str) -> int:
    _ler_segredo(obter_perfil(nome))
    print("Segredo gravado no Credential Manager.")
    return 0


def vincular(nome: str, pasta: str) -> int:
    resultado = Operacoes(GerenciadorConexoes()).vincular_repositorio(nome, pasta)
    print(f"Repositorio vinculado: {resultado['repositorio']}")
    print(f"Pasta local: {resultado['pasta_local']}")
    if resultado["aviso"]:
        print(f"AVISO: {resultado['aviso']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sftp-mcp-setup", description="Cadastro de perfis do sftp-mcp.")
    sub = parser.add_subparsers(dest="comando", required=True)
    for comando in ("adicionar", "testar", "remover", "trocar-fingerprint", "definir-senha"):
        sub.add_parser(comando).add_argument("perfil")
    sub.add_parser("listar")
    vinc = sub.add_parser("vincular-repositorio")
    vinc.add_argument("perfil")
    vinc.add_argument("pasta")
    args = parser.parse_args(argv)

    acoes = {
        "adicionar": lambda: adicionar(args.perfil),
        "listar": listar,
        "testar": lambda: testar(args.perfil),
        "remover": lambda: remover(args.perfil),
        "trocar-fingerprint": lambda: trocar_fingerprint(args.perfil),
        "definir-senha": lambda: definir_senha(args.perfil),
        "vincular-repositorio": lambda: vincular(args.perfil, args.pasta),
    }
    try:
        return acoes[args.comando]()
    except (ErroConfig, ErroConexao, *ERROS_ESPERADOS) as e:
        print(f"Erro: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
