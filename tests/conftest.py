"""Servidor SFTP em processo (asyncssh), keyring em memoria e repositorio git temporario."""

from __future__ import annotations

import subprocess
from pathlib import Path

import asyncssh
import keyring
import pytest
from keyring.backend import KeyringBackend

from sftp_mcp import config, credenciais
from sftp_mcp.conexao import GerenciadorConexoes
from sftp_mcp.operacoes import Operacoes

USUARIO = "cliente"
SENHA = "segredo-de-teste"


class KeyringMemoria(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.dados: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.dados.get((service, username))

    def set_password(self, service, username, password):
        self.dados[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.dados:
            raise keyring.errors.PasswordDeleteError(username)
        del self.dados[(service, username)]


@pytest.fixture(autouse=True)
def isolamento(tmp_path, monkeypatch):
    monkeypatch.setenv("SFTP_MCP_HOME", str(tmp_path / "config"))
    anterior = keyring.get_keyring()
    keyring.set_keyring(KeyringMemoria())
    yield
    keyring.set_keyring(anterior)


class _ServidorSsh(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == USUARIO and password == SENHA


@pytest.fixture
def remoto(tmp_path) -> Path:
    raiz = tmp_path / "remoto"
    (raiz / "escrita").mkdir(parents=True)
    (raiz / "fora").mkdir()
    (raiz / "logs").mkdir()
    return raiz


@pytest.fixture
def chave_host():
    return asyncssh.generate_private_key("ssh-ed25519")


@pytest.fixture
async def servidor(remoto, chave_host):
    class _Chroot(asyncssh.SFTPServer):
        def __init__(self, canal):
            super().__init__(canal, chroot=str(remoto))

    chave_rsa = asyncssh.generate_private_key("ssh-rsa")
    escuta = await asyncssh.listen(
        "127.0.0.1", 0,
        server_host_keys=[chave_rsa, chave_host],
        server_factory=_ServidorSsh,
        sftp_factory=_Chroot,
    )
    yield escuta.sockets[0].getsockname()[1]
    escuta.close()
    await escuta.wait_closed()


@pytest.fixture
def repositorio(tmp_path) -> Path:
    repo = tmp_path / "repo-cliente"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    return repo


@pytest.fixture
def perfil(servidor, chave_host, repositorio) -> config.Perfil:
    p = config.Perfil(
        nome="cliente-hml",
        host="127.0.0.1",
        porta=servidor,
        usuario=USUARIO,
        host_key=chave_host.export_public_key("openssh").decode().strip(),
        ambiente="hml",
        repositorio=str(repositorio),
        raizes_escrita=["/escrita"],
    )
    config.salvar_perfil(p)
    credenciais.gravar(p.nome, "senha", SENHA)
    return p


@pytest.fixture
async def ops(servidor):
    # Depende do servidor para fechar a conexao antes dele: o wait_closed do
    # listener espera os clientes sairem.
    gerenciador = GerenciadorConexoes()
    yield Operacoes(gerenciador)
    await gerenciador.fechar_todas()


def pasta_local(perfil: config.Perfil) -> Path:
    return Path(perfil.repositorio) / ".claude" / "sftp" / perfil.nome
