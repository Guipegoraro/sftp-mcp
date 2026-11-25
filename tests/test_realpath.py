"""Servidor SFTP que so resolve realpath de pasta: a leitura de arquivo nao pode depender dele."""

from __future__ import annotations

import os
import posixpath

import asyncssh
import pytest

from sftp_mcp import config
from sftp_mcp.operacoes import ErroOperacao

from .conftest import _ServidorSsh


@pytest.fixture
def lstat_segue_link():
    return False


@pytest.fixture
async def servidor(remoto, chave_host, lstat_segue_link):
    class _RealpathSoDePasta(asyncssh.SFTPServer):
        def __init__(self, canal):
            super().__init__(canal, chroot=str(remoto))

        def _local(self, path):
            return remoto / posixpath.normpath(posixpath.join("/", path.decode()))[1:]

        def lstat(self, path):
            return self.stat(path) if lstat_segue_link else super().lstat(path)

        def realpath(self, path):
            partes = path.decode().split("/")
            pastas = ["/".join(partes[:i]) for i, p in enumerate(partes) if p == ".."]
            if not all(self._local(p.encode()).is_dir() for p in [*pastas, path.decode()]):
                raise asyncssh.SFTPNoSuchFile("No such file")
            return super().realpath(path)

        def readlink(self, path):
            # O texto do link como gravado, igual ao OpenSSH.
            return os.readlink(self._local(path)).replace("\\", "/").encode()

    escuta = await asyncssh.listen(
        "127.0.0.1", 0,
        server_host_keys=[chave_host],
        server_factory=_ServidorSsh,
        sftp_factory=_RealpathSoDePasta,
    )
    yield escuta.sockets[0].getsockname()[1]
    escuta.close()
    await escuta.wait_closed()


@pytest.fixture
def so_logs(perfil):
    perfil.raizes_leitura = ["/logs"]
    config.salvar_perfil(perfil)
    return perfil


def _link(origem, destino):
    try:
        os.symlink(destino, origem)
    except OSError:
        pytest.skip("Sem permissao para criar symlink neste Windows")


async def test_le_arquivo(ops, perfil, remoto):
    (remoto / "logs" / "console.log").write_bytes(b"um\ndois\n")
    assert (await ops.info(perfil.nome, "/logs/console.log"))["caminho"] == "/logs/console.log"
    assert (await ops.ler(perfil.nome, "/logs/console.log"))["conteudo"] == "um\ndois\n"
    relativo = await ops.ultimas_linhas(perfil.nome, "logs/console.log", 1)
    assert (relativo["caminho"], relativo["conteudo"]) == ("/logs/console.log", "dois\n")
    assert (await ops.baixar(perfil.nome, "/fora/../logs/console.log"))["tamanho"] == 8


@pytest.mark.parametrize("caminho", ["/logs/nao-existe.log", "/nao-existe/console.log"])
async def test_arquivo_que_nao_existe_cita_o_caminho_pedido(ops, perfil, caminho):
    with pytest.raises(ErroOperacao, match=f"Nao existe no servidor: {caminho}$"):
        await ops.info(perfil.nome, caminho)


async def test_arquivo_fora_da_raiz_de_leitura(ops, so_logs, remoto):
    (remoto / "fora" / "segredo.txt").write_text("segredo")
    for caminho in ("/fora/segredo.txt", "/logs/../fora/segredo.txt"):
        with pytest.raises(ErroOperacao, match="fora das raizes de leitura"):
            await ops.ler(so_logs.nome, caminho)


async def test_link_que_sai_da_raiz_de_leitura(ops, so_logs, remoto):
    (remoto / "fora" / "segredo.txt").write_text("segredo")
    _link(remoto / "logs" / "atalho.log", os.path.join("..", "fora", "segredo.txt"))
    with pytest.raises(ErroOperacao, match="fora das raizes de leitura"):
        await ops.ler(so_logs.nome, "/logs/atalho.log")


async def test_link_absoluto_que_sai_da_raiz_de_leitura(ops, so_logs, remoto, monkeypatch):
    (remoto / "fora" / "segredo.txt").write_text("segredo")
    _link(remoto / "logs" / "atalho.log", os.path.join("..", "fora", "segredo.txt"))
    # No Windows o alvo absoluto teria letra de unidade: o servidor simulado devolve o caminho remoto.
    original = os.readlink
    monkeypatch.setattr(os, "readlink", lambda p: "/fora/segredo.txt" if p.name == "atalho.log" else original(p))
    with pytest.raises(ErroOperacao, match="fora das raizes de leitura"):
        await ops.ler(so_logs.nome, "/logs/atalho.log")


async def test_pasta_em_link_que_sai_da_raiz_de_leitura(ops, so_logs, remoto):
    (remoto / "fora" / "segredo.txt").write_text("segredo")
    try:
        os.symlink(os.path.join("..", "fora"), remoto / "logs" / "pasta", target_is_directory=True)
    except OSError:
        pytest.skip("Sem permissao para criar symlink neste Windows")
    with pytest.raises(ErroOperacao, match="fora das raizes de leitura"):
        await ops.ler(so_logs.nome, "/logs/pasta/segredo.txt")


@pytest.mark.parametrize("lstat_segue_link", [True])
async def test_link_em_servidor_com_lstat_que_segue_link(ops, so_logs, remoto):
    (remoto / "fora" / "segredo.txt").write_text("segredo")
    _link(remoto / "logs" / "atalho.log", os.path.join("..", "fora", "segredo.txt"))
    with pytest.raises(ErroOperacao, match="fora das raizes de leitura"):
        await ops.ler(so_logs.nome, "/logs/atalho.log")


async def test_ponto_ponto_do_alvo_fica_para_o_servidor(ops, perfil, remoto):
    """realpath recebe o alvo como gravado: normalizar antes trocaria a pasta quando ela e link."""
    (remoto / "logs" / "console.log").write_text("errado")
    _link(remoto / "logs" / "atalho.log", os.path.join("sumiu", "..", "console.log"))
    with pytest.raises(ErroOperacao, match="Nao existe no servidor"):
        await ops.ler(perfil.nome, "/logs/atalho.log")


async def test_raiz_de_leitura_que_e_arquivo(ops, perfil, remoto):
    (remoto / "logs" / "console.log").write_bytes(b"ok")
    (remoto / "fora" / "outro.log").write_bytes(b"ok")
    perfil.raizes_leitura = ["/logs/console.log"]
    config.salvar_perfil(perfil)
    assert (await ops.ler(perfil.nome, "/logs/console.log"))["conteudo"] == "ok"
    with pytest.raises(ErroOperacao, match="fora das raizes de leitura"):
        await ops.ler(perfil.nome, "/fora/outro.log")


async def test_link_dentro_da_raiz_de_leitura(ops, so_logs, remoto):
    (remoto / "logs" / "console.log").write_bytes(b"ok\n")
    _link(remoto / "logs" / "atalho.log", "console.log")
    resultado = await ops.ler(so_logs.nome, "/logs/atalho.log")
    assert (resultado["caminho"], resultado["conteudo"]) == ("/logs/console.log", "ok\n")


async def test_link_para_link(ops, so_logs, remoto):
    (remoto / "logs" / "sub").mkdir()
    (remoto / "logs" / "sub" / "console.log").write_bytes(b"ok")
    _link(remoto / "logs" / "atalho.log", os.path.join("sub", "..", "outro.log"))
    _link(remoto / "logs" / "outro.log", os.path.join("sub", "console.log"))
    resultado = await ops.ler(so_logs.nome, "/logs/atalho.log")
    assert resultado["caminho"] == "/logs/sub/console.log"


async def test_link_quebrado(ops, so_logs, remoto):
    _link(remoto / "logs" / "quebrado.log", "sumiu.log")
    with pytest.raises(ErroOperacao, match="Nao existe no servidor: /logs/sumiu.log"):
        await ops.ler(so_logs.nome, "/logs/quebrado.log")


async def test_link_que_aponta_para_si_nao_trava(ops, so_logs, remoto):
    _link(remoto / "logs" / "laco.log", "laco.log")
    with pytest.raises(ErroOperacao, match="Nao foi possivel resolver"):
        await ops.ler(so_logs.nome, "/logs/laco.log")
