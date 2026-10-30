"""Senha e passphrase de cada perfil no keyring do sistema.

No Windows o keyring e o Credential Manager (DPAPI, por usuario). O segredo nunca
passa por parametro de tool nem vai para arquivo.
"""

from __future__ import annotations

import keyring
import keyring.errors

SERVICO = "sftp-mcp"
TIPOS = ("senha", "passphrase")


def _usuario_keyring(perfil: str, tipo: str) -> str:
    return f"{perfil}:{tipo}"


def obter(perfil: str, tipo: str) -> str | None:
    return keyring.get_password(SERVICO, _usuario_keyring(perfil, tipo))


def gravar(perfil: str, tipo: str, segredo: str) -> None:
    keyring.set_password(SERVICO, _usuario_keyring(perfil, tipo), segredo)


def remover(perfil: str) -> None:
    for tipo in TIPOS:
        try:
            keyring.delete_password(SERVICO, _usuario_keyring(perfil, tipo))
        except keyring.errors.PasswordDeleteError:
            pass
