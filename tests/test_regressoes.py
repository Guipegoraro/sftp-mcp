from __future__ import annotations

import asyncio
import time

import pytest

from sftp_mcp import config
from sftp_mcp.cadastro import separar_url
from sftp_mcp.conexao import _opcoes_conexao
from sftp_mcp.operacoes import ErroOperacao, _decodificar

from .conftest import pasta_local


@pytest.mark.parametrize("destino", [r"/escrita/sub\..\..\fora\x.txt", "/escrita/a\\b.txt"])
async def test_barra_invertida_recusada(ops, perfil, remoto, tmp_path, destino):
    local = tmp_path / "x.txt"
    local.write_text("x")
    with pytest.raises(ErroOperacao, match="barra invertida"):
        await ops.enviar(perfil.nome, str(local), destino)
    assert not (remoto / "fora" / "x.txt").exists()


async def test_renomear_com_barra_invertida_nao_traz_arquivo_de_fora(ops, perfil, remoto):
    (remoto / "fora" / "segredo.txt").write_text("x")
    with pytest.raises(ErroOperacao, match="barra invertida"):
        await ops.renomear(perfil.nome, r"/escrita/a\..\..\fora\segredo.txt", "/escrita/trazido.txt")
    assert (remoto / "fora" / "segredo.txt").exists()


async def test_renomear_nao_move_a_propria_raiz(ops, perfil, remoto):
    (remoto / "outra").mkdir()
    perfil.raizes_escrita = ["/escrita", "/outra"]
    config.salvar_perfil(perfil)
    with pytest.raises(ErroOperacao, match="fora das raizes"):
        await ops.renomear(perfil.nome, "/escrita", "/outra/escrita_movida")
    assert (remoto / "escrita").is_dir()


def test_modo_chave_nao_usa_agente_nem_kerberos(perfil, tmp_path):
    chave = tmp_path / "id_ed25519"
    import asyncssh

    chave.write_bytes(asyncssh.generate_private_key("ssh-ed25519").export_private_key())
    perfil.autenticacao = "chave"
    perfil.chave_privada = str(chave)
    opcoes = _opcoes_conexao(perfil)
    assert opcoes.agent_path is None
    assert not opcoes.gss_auth and not opcoes.gss_kex


async def test_conexao_em_uso_nao_fecha_por_ociosidade(ops, perfil):
    perfil.ocioso_max_segundos = 0
    config.salvar_perfil(perfil)
    gerenciador = ops._gerenciador
    async with gerenciador.usar(perfil) as sftp:
        await asyncio.sleep(0.05)
        gerenciador._fechar_ociosas()
        assert await sftp.realpath(".") == "/"


async def test_canal_sftp_fechado_reconecta(ops, perfil, remoto):
    await ops.listar(perfil.nome, "/")
    ops._gerenciador._conexoes[perfil.nome].sftp.exit()
    await asyncio.sleep(0.1)
    resultado = await ops.listar(perfil.nome, "/logs")
    assert resultado["caminho"] == "/logs"


async def test_ultimas_linhas_com_linha_maior_que_o_limite(ops, perfil, remoto):
    perfil.max_leitura_bytes = 1000
    config.salvar_perfil(perfil)
    (remoto / "logs" / "longa.log").write_bytes(b"inicio\n" + b"x" * 5000 + b"FIM")
    resultado = await ops.ultimas_linhas(perfil.nome, "/logs/longa.log", 1)
    assert resultado["conteudo"].endswith("FIM") and len(resultado["conteudo"]) == 1000
    assert resultado["truncado_no_limite_de_bytes"] is True


async def test_ultimas_linhas_quebra_so_em_lf(ops, perfil, remoto):
    (remoto / "logs" / "progresso.log").write_bytes(b"a\rb\nc\n")
    resultado = await ops.ultimas_linhas(perfil.nome, "/logs/progresso.log", 1)
    assert resultado["conteudo"] == "c\n"


def test_cp1252_terminando_em_acento_nao_vira_utf8():
    assert _decodificar("café".encode("cp1252"), None) == ("café", "cp1252")


def test_utf8_cortado_no_limite_continua_utf8():
    dados = "ação".encode("utf-8")[:-2]  # corta no meio do "ã"
    assert _decodificar(dados[:2], None, cortado=True) == ("a", "utf-8")


async def test_downloads_em_paralelo_nao_colidem(ops, perfil, remoto):
    (remoto / "logs" / "A.log").write_text("maiuscula")
    (remoto / "logs" / "a2.log").write_text("x")
    resultados = await asyncio.gather(
        ops.baixar(perfil.nome, "/logs/A.log"), ops.baixar(perfil.nome, "/logs/A.log")
    )
    locais = {r["arquivo_local"] for r in resultados}
    assert len(locais) == 2
    assert all((pasta_local(perfil) / "downloads").exists() for _ in locais)


@pytest.mark.parametrize(
    "digitado, esperado",
    [
        ("sftp://blanks.protheus.cloudtotvs.com.br", ("blanks.protheus.cloudtotvs.com.br", None, "")),
        ("sftp://blanks.protheus.cloudtotvs.com.br:10501/a/b/", ("blanks.protheus.cloudtotvs.com.br", 10501, "/a/b")),
        ("ftp_user@host.com.br:2222", ("host.com.br", 2222, "")),
        ("host.com.br", ("host.com.br", None, "")),
    ],
)
def test_setup_aceita_host_com_esquema_e_porta(digitado, esperado):
    assert separar_url(digitado) == esperado


def test_perfil_recusa_host_com_esquema(perfil):
    perfil.host = "sftp://host.com.br"
    with pytest.raises(config.ErroConfig, match="sem sftp://"):
        perfil.validar()


async def test_host_que_nao_resolve_da_mensagem_clara():
    from sftp_mcp.conexao import ErroConexao, chave_do_servidor

    with pytest.raises(ErroConexao, match="Nao resolveu o host"):
        await chave_do_servidor("host-que-nao-existe.invalid", 22)
