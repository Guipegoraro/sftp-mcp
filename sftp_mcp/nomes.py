"""Nome remoto com byte que nao e UTF-8 (spool do Protheus, arquivo vindo do Windows).

O cliente SFTP decodifica com surrogateescape: o byte vira U+DC80..U+DCFF e volta igual ao
servidor. Para fora (tool, log, erro) ele aparece como \\xHH, e \\xHH na entrada volta a ser o byte.
"""

from __future__ import annotations

import re

_BYTE_CRU = re.compile("[\udc80-\udcff]")
# So bytes de 0x80 a 0xFF: '/', '.' e '\' continuam impossiveis por esse caminho.
_BYTE_ESCAPADO = re.compile(r"\\x([89a-fA-F][0-9a-fA-F])")


def exibir(texto: str) -> str:
    return _BYTE_CRU.sub(lambda m: f"\\x{ord(m.group()) - 0xDC00:02X}", texto)


def sanear(valor):
    """Troca o byte cru por \\xHH em str, dict, list e tuple, para o resultado serializar em UTF-8."""
    if isinstance(valor, str):
        return exibir(valor)
    if isinstance(valor, dict):
        return {sanear(k): sanear(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return type(valor)(sanear(v) for v in valor)
    return valor


def sem_escapes(caminho: str) -> str:
    return _BYTE_ESCAPADO.sub("", caminho)


def ler(caminho: str) -> str:
    return _BYTE_ESCAPADO.sub(lambda m: chr(0xDC00 + int(m.group(1), 16)), caminho)
