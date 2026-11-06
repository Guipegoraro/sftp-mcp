"""Pasta local de cada perfil dentro do repositorio do cliente: downloads, backups e auditoria.

Estrutura: <repositorio>/.claude/sftp/<perfil>/{downloads,backups,audit.log}
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from .config import Perfil

_CARACTERES_INVALIDOS_WINDOWS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_NOMES_RESERVADOS_WINDOWS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ErroRepositorio(Exception):
    pass


def subpasta_relativa(perfil: str) -> Path:
    return Path(".claude") / "sftp" / perfil


def pasta_perfil(perfil: Perfil) -> Path:
    if not perfil.repositorio:
        raise ErroRepositorio(
            f"O perfil {perfil.nome} nao tem repositorio vinculado. Pergunte ao usuario qual e a "
            f"pasta do repositorio do cliente e chame sftp_vincular_repositorio."
        )
    repositorio = Path(perfil.repositorio)
    if not repositorio.is_dir():
        raise ErroRepositorio(
            f"O repositorio do perfil {perfil.nome} ({repositorio}) nao existe. Pergunte ao usuario "
            f"a pasta correta e chame sftp_vincular_repositorio."
        )
    pasta = repositorio / subpasta_relativa(perfil.nome)
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def raiz_git(caminho: Path) -> Path | None:
    try:
        saida = subprocess.run(
            ["git", "-C", str(caminho), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if saida.returncode != 0:
        return None
    return Path(saida.stdout.strip())


def ignorado_pelo_git(repositorio: Path, relativo: Path) -> bool:
    saida = subprocess.run(
        ["git", "-C", str(repositorio), "check-ignore", "-q", relativo.as_posix()],
        capture_output=True, timeout=15,
    )
    return saida.returncode == 0


def aviso_git(raiz: Path, perfil: str) -> str | None:
    relativo = subpasta_relativa(perfil)
    if ignorado_pelo_git(raiz, relativo):
        return None
    return (
        f"{relativo.as_posix()} NAO esta ignorado pelo git em {raiz}: downloads e backups do "
        "cliente podem entrar num commit. Avise o usuario."
    )


def _segmento_seguro(segmento: str) -> str:
    seguro = _CARACTERES_INVALIDOS_WINDOWS.sub("_", segmento).rstrip(" .") or "_"
    if seguro.split(".")[0].upper() in _NOMES_RESERVADOS_WINDOWS:
        seguro = "_" + seguro
    return seguro


def _espelho(base: Path, caminho_remoto: str) -> Path:
    segmentos = [s for s in caminho_remoto.split("/") if s not in ("", ".", "..")]
    destino = base.joinpath(*(_segmento_seguro(s) for s in segmentos))
    # Os segmentos ja vem sem '..', mas a conferencia garante que nada sai da base.
    if not destino.resolve().is_relative_to(base.resolve()):
        raise ErroRepositorio(f"Caminho local fora da pasta do perfil: {destino}")
    return destino


def _reservar(destino: Path) -> Path:
    """Cria o arquivo vazio com nome livre. A criacao exclusiva impede que duas chamadas
    em paralelo (ou nomes que so diferem na caixa, no NTFS) caiam no mesmo arquivo."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    for n in range(10_000):
        candidato = destino if n == 0 else destino.with_name(f"{destino.stem} ({n}){destino.suffix}")
        try:
            with candidato.open("xb"):
                return candidato
        except FileExistsError:
            continue
    raise ErroRepositorio(f"Arquivos demais com o mesmo nome em {destino.parent}")


def destino_download(perfil: Perfil, caminho_remoto: str) -> Path:
    return _reservar(_espelho(pasta_perfil(perfil) / "downloads", caminho_remoto))


def destino_backup(perfil: Perfil, caminho_remoto: str) -> Path:
    carimbo = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return _reservar(_espelho(pasta_perfil(perfil) / "backups" / carimbo, caminho_remoto))


def auditar(perfil: Perfil, operacao: str, resultado: str, **dados) -> None:
    """Uma linha JSON por operacao. Nunca recebe conteudo de arquivo nem segredo."""
    registro = {
        "quando": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "perfil": perfil.nome,
        "ambiente": perfil.ambiente,
        "operacao": operacao,
        "resultado": resultado,
        **dados,
    }
    with (pasta_perfil(perfil) / "audit.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")
