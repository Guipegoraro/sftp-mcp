"""Nome remoto que nao e UTF-8 (ex.: spool do Protheus) nao derruba listagem, busca nem download."""

from __future__ import annotations

import json

import asyncssh
import pytest

from sftp_mcp import config, credenciais, nomes, server
from sftp_mcp.conexao import GerenciadorConexoes
from sftp_mcp.operacoes import ErroOperacao, Operacoes, _validar_caminho

from .conftest import SENHA, USUARIO, _ServidorSsh, pasta_local

NOME_CRU = b"t\xe3\xadtulos.xml"
NOME_EXIBIDO = "t\\xE3\\xADtulos.xml"
NOME_NO_DISCO = b"titulos.xml"


@pytest.fixture
async def servidor_cru(remoto, chave_host):
    (remoto / "spool").mkdir()
    (remoto / "spool" / "titulos.xml").write_text("<titulos/>", encoding="utf-8")

    class _NomeCru(asyncssh.SFTPServer):
        """O NTFS nao guarda nome em bytes invalidos: o servidor mostra o arquivo com esse nome."""

        def __init__(self, canal):
            super().__init__(canal, chroot=str(remoto))

        def map_path(self, path):
            return super().map_path(path.replace(NOME_CRU, NOME_NO_DISCO))

        def reverse_map_path(self, path):
            return super().reverse_map_path(path).replace(NOME_NO_DISCO, NOME_CRU)

        async def scandir(self, path):
            async for nome in super().scandir(path):
                if nome.filename == NOME_NO_DISCO:
                    nome.filename = NOME_CRU
                yield nome

    escuta = await asyncssh.listen(
        "127.0.0.1", 0, server_host_keys=[chave_host], server_factory=_ServidorSsh, sftp_factory=_NomeCru,
    )
    yield escuta.sockets[0].getsockname()[1]
    escuta.close()
    await escuta.wait_closed()


@pytest.fixture
async def ops_cru(servidor_cru, chave_host, repositorio):
    perfil = config.Perfil(
        nome="cru-dev", host="127.0.0.1", porta=servidor_cru, usuario=USUARIO,
        host_key=chave_host.export_public_key("openssh").decode().strip(), repositorio=str(repositorio),
    )
    config.salvar_perfil(perfil)
    credenciais.gravar(perfil.nome, "senha", SENHA)
    gerenciador = GerenciadorConexoes()
    yield Operacoes(gerenciador), perfil
    await gerenciador.fechar_todas()


async def test_listar_mostra_o_byte_como_escape(ops_cru):
    ops, perfil = ops_cru
    resultado = nomes.sanear(await ops.listar(perfil.nome, "/spool"))
    assert [e["nome"] for e in resultado["entradas"]] == [NOME_EXIBIDO]
    json.dumps(resultado, ensure_ascii=False).encode("utf-8")


async def test_buscar_nao_para_no_nome_cru(ops_cru):
    ops, perfil = ops_cru
    resultado = nomes.sanear(await ops.buscar(perfil.nome, "/", "*.xml"))
    assert json.dumps(resultado, ensure_ascii=False).count(NOME_EXIBIDO.replace("\\", "\\\\")) == 1


async def test_o_escape_volta_ao_byte_para_ler_e_baixar(ops_cru):
    ops, perfil = ops_cru
    caminho = f"/spool/{NOME_EXIBIDO}"
    lido = await ops.ler(perfil.nome, caminho)
    assert lido["conteudo"] == "<titulos/>"
    baixado = nomes.sanear(await ops.baixar(perfil.nome, caminho))
    assert baixado["arquivo_local"].endswith("t_xE3_xADtulos.xml")
    auditoria = (pasta_local(perfil) / "audit.log").read_text(encoding="utf-8")
    assert NOME_EXIBIDO.replace("\\", "\\\\") in auditoria


def test_escape_so_vale_para_byte_acima_de_7f():
    assert _validar_caminho("/a/\\xE3b") == "/a/\udce3b"
    for caminho in ("/a/\\x2F..", "/a/\\x5C", "/a\\b"):
        with pytest.raises(ErroOperacao, match="barra invertida"):
            _validar_caminho(caminho)


async def test_tool_devolve_escape_e_nao_quebra_a_serializacao(ops_cru, monkeypatch):
    ops, perfil = ops_cru
    monkeypatch.setattr(server, "_operacoes", ops)
    saida = await server.mcp.call_tool("sftp_listar", {"perfil": perfil.nome, "caminho": "/spool"})
    conteudo = saida[0] if isinstance(saida, tuple) else saida
    assert json.loads(conteudo[0].text)["entradas"][0]["nome"] == NOME_EXIBIDO
