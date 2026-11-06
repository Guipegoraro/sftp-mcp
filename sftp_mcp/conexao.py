"""Conexoes SFTP por perfil, reaproveitadas entre chamadas e fechadas quando ociosas."""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncssh

from . import credenciais
from .config import Perfil

# Erros que indicam canal SFTP ou conexao SSH perdidos: a entrada do cache e descartada.
_ERROS_DE_CONEXAO = (
    asyncssh.SFTPNoConnection,
    asyncssh.SFTPConnectionLost,
    asyncssh.ConnectionLost,
    asyncssh.DisconnectError,
    ConnectionError,
)


class ErroConexao(Exception):
    pass


def fingerprint(chave_publica: str) -> str:
    return asyncssh.import_public_key(chave_publica).get_fingerprint("sha256")


async def chave_do_servidor(host: str, porta: int, timeout: int = 15) -> str:
    """Le a chave publica que o servidor apresenta, para o usuario conferir no cadastro."""
    try:
        chave = await asyncio.wait_for(asyncssh.get_server_host_key(host, porta, config=None), timeout)
    except socket.gaierror as e:
        raise ErroConexao(f"Nao resolveu o host {host!r}: confira o nome, sem sftp:// nem porta.") from e
    except (OSError, asyncio.TimeoutError, asyncssh.Error) as e:
        raise ErroConexao(
            f"Nao conectou em {host}:{porta}: {e or type(e).__name__}. "
            "Confira host, porta e, se o cliente exige VPN, se ela esta ativa."
        ) from e
    if chave is None:
        raise ErroConexao(f"{host}:{porta} nao apresentou chave de host.")
    return chave.export_public_key("openssh").decode("ascii").strip()


def _opcoes_conexao(perfil: Perfil, segredo: str | None = None) -> asyncssh.SSHClientConnectionOptions:
    """segredo em memoria tem prioridade sobre o keyring: o cadastro testa o login antes de gravar."""
    opcoes = dict(
        username=perfil.usuario,
        # Somente a chave fixada no cadastro e aceita. Lista vazia faria o asyncssh
        # cair no ~/.ssh/known_hosts, por isso a chave vai sempre preenchida.
        known_hosts=([asyncssh.import_public_key(perfil.host_key)], [], []),
        # Sem ~/.ssh/config e sem Kerberos com o login do Windows: o perfil e a
        # unica fonte de identidade da conexao.
        config=None,
        gss_auth=False,
        gss_kex=False,
        connect_timeout=perfil.timeout_conexao,
        keepalive_interval=60,
    )
    if perfil.autenticacao == "senha":
        senha = segredo or credenciais.obter(perfil.nome, "senha")
        if not senha:
            raise ErroConexao(
                f"Perfil {perfil.nome} sem senha no Credential Manager. "
                f"Peca ao usuario para rodar: sftp-mcp-setup definir-senha {perfil.nome}"
            )
        opcoes.update(password=senha, client_keys=None)
    else:
        opcoes.update(
            client_keys=[perfil.chave_privada],
            passphrase=segredo or credenciais.obter(perfil.nome, "passphrase"),
            password=None,
        )
    resultado = asyncssh.SSHClientConnectionOptions(**opcoes)
    # Com client_keys preenchido o asyncssh troca agent_path=None por '' e passa a
    # oferecer as chaves do Pageant/ssh-agent. Zerar depois de montar desliga o agente.
    resultado.agent_path = None
    return resultado


@dataclass
class _Conexao:
    conexao: asyncssh.SSHClientConnection
    sftp: asyncssh.SFTPClient
    ultimo_uso: float
    ocioso_max: int
    em_uso: int = 0

    def morta(self) -> bool:
        # O asyncssh zera o transporte quando a conexao cai, e o escritor do canal
        # quando so o subsistema SFTP fecha.
        if getattr(self.conexao, "_transport", None) is None:
            return True
        escritor = getattr(getattr(self.sftp, "_handler", None), "_writer", None)
        return escritor is None or escritor.channel.is_closing()


class GerenciadorConexoes:
    def __init__(self) -> None:
        self._conexoes: dict[str, _Conexao] = {}
        self._travas: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def usar(self, perfil: Perfil, segredo: str | None = None):
        """Entrega o cliente SFTP do perfil. A conexao nao fecha por ociosidade enquanto em uso."""
        self._fechar_ociosas()
        trava = self._travas.setdefault(perfil.nome, asyncio.Lock())
        async with trava:
            atual = self._conexoes.get(perfil.nome)
            if atual is None or atual.morta():
                if atual is not None:
                    self._descartar(perfil.nome)
                atual = await self._conectar(perfil, segredo)
                self._conexoes[perfil.nome] = atual
            atual.em_uso += 1
        try:
            yield atual.sftp
        except _ERROS_DE_CONEXAO:
            self._descartar(perfil.nome, atual)
            raise
        finally:
            atual.em_uso -= 1
            atual.ultimo_uso = time.monotonic()

    async def _conectar(self, perfil: Perfil, segredo: str | None) -> _Conexao:
        try:
            conexao = await asyncssh.connect(perfil.host, perfil.porta, options=_opcoes_conexao(perfil, segredo))
        except asyncssh.HostKeyNotVerifiable as e:
            raise ErroConexao(
                f"A chave do servidor {perfil.host}:{perfil.porta} nao confere com a fixada no perfil "
                f"{perfil.nome} ({fingerprint(perfil.host_key)}). Conexao recusada. Se a troca for "
                f"legitima, o usuario confirma com: sftp-mcp-setup trocar-fingerprint {perfil.nome}"
            ) from e
        except asyncssh.PermissionDenied as e:
            raise ErroConexao(f"Autenticacao recusada pelo servidor do perfil {perfil.nome}.") from e
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as e:
            raise ErroConexao(
                f"Nao conectou em {perfil.host}:{perfil.porta} (perfil {perfil.nome}): {e}. "
                "Se o cliente exige VPN, confira se ela esta ativa."
            ) from e
        try:
            sftp = await conexao.start_sftp_client()
            # O chdir do asyncssh so resolve o caminho, nao confere se a pasta existe.
            if perfil.pasta_inicial and not await sftp.isdir(perfil.pasta_inicial):
                conexao.close()
                raise ErroConexao(f"A rota {perfil.pasta_inicial} nao existe ou nao e pasta no servidor.")
            if perfil.pasta_inicial:
                await sftp.chdir(perfil.pasta_inicial)
        except asyncssh.Error as e:
            conexao.close()
            raise ErroConexao(f"O servidor do perfil {perfil.nome} recusou o acesso SFTP: {e}") from e
        return _Conexao(conexao, sftp, time.monotonic(), perfil.ocioso_max_segundos)

    def _descartar(self, nome: str, esperada: _Conexao | None = None) -> None:
        atual = self._conexoes.get(nome)
        if atual is None or (esperada is not None and atual is not esperada):
            return
        del self._conexoes[nome]
        atual.conexao.close()

    def _fechar_ociosas(self) -> None:
        agora = time.monotonic()
        for nome, atual in list(self._conexoes.items()):
            if atual.em_uso:
                continue
            if atual.morta() or agora - atual.ultimo_uso > atual.ocioso_max:
                self._descartar(nome, atual)

    async def fechar_todas(self) -> None:
        for atual in list(self._conexoes.values()):
            atual.conexao.close()
            await atual.conexao.wait_closed()
        self._conexoes.clear()

    async def vigiar_ociosas(self, intervalo: int = 60) -> None:
        while True:
            await asyncio.sleep(intervalo)
            self._fechar_ociosas()
