"""Operacoes SFTP expostas pelas tools.

Toda operacao trabalha sobre o caminho remoto ja resolvido pelo servidor (realpath),
e a checagem de raiz usa esse mesmo caminho. Assim symlink e '..' nao escapam da raiz.
"""

from __future__ import annotations

import codecs
import datetime as dt
import fnmatch
import hashlib
import posixpath
import re
import stat
from collections import deque
from pathlib import Path

import asyncssh

from . import armazenamento, config, credenciais
from .armazenamento import ErroRepositorio
from .conexao import ErroConexao, GerenciadorConexoes
from .config import ErroConfig, Perfil

_BLOCO = 65_536
_AMOSTRA_BINARIO = 8_192
# Barra invertida e separador para servidor SFTP sobre Windows: "a\..\..\x" escaparia
# da raiz sem passar pelo realpath. Caractere invisivel mascararia o destino no prompt.
_PROIBIDOS = re.compile("[\\\\\x00-\x1f\x7f​-‏‪-‮⁠-⁤⁦-⁩﻿]")


class ErroOperacao(Exception):
    pass


ERROS_ESPERADOS = (ErroOperacao, ErroConfig, ErroConexao, ErroRepositorio)


def _validar_caminho(caminho: str) -> str:
    if not caminho:
        raise ErroOperacao("Caminho remoto vazio.")
    if _PROIBIDOS.search(caminho):
        raise ErroOperacao(
            "Caminho remoto com barra invertida ou caractere de controle/invisivel. "
            "Use '/' como separador, inclusive em servidor Windows."
        )
    return posixpath.normpath(caminho)


def _dentro(caminho: str, raizes: list[str], *, estrito: bool = False) -> bool:
    for raiz in raizes:
        if caminho == raiz:
            if not estrito:
                return True
        elif caminho.startswith(raiz.rstrip("/") + "/"):
            return True
    return False


def _tipo(attrs: asyncssh.SFTPAttrs) -> str:
    modo = attrs.permissions
    if modo is None:
        return "outro"
    if stat.S_ISDIR(modo):
        return "pasta"
    if stat.S_ISREG(modo):
        return "arquivo"
    if stat.S_ISLNK(modo):
        return "link"
    return "outro"


def _data(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _descrever(caminho: str, attrs: asyncssh.SFTPAttrs) -> dict:
    return {
        "caminho": caminho,
        "tipo": _tipo(attrs),
        "tamanho": attrs.size,
        "modificado": _data(attrs.mtime),
        "permissoes": stat.filemode(attrs.permissions) if attrs.permissions is not None else None,
    }


def _sem_utf8_incompleto(dados: bytes) -> bytes:
    """Remove do fim um caractere UTF-8 cortado pelo limite de leitura."""
    for recuo in range(1, min(4, len(dados)) + 1):
        byte = dados[-recuo]
        if byte & 0xC0 != 0x80:  # inicio de caractere
            tamanho = 2 if byte >= 0xC0 else 1
            tamanho = 3 if byte >= 0xE0 else tamanho
            tamanho = 4 if byte >= 0xF0 else tamanho
            return dados[:-recuo] if tamanho > recuo else dados
    return dados


def _decodificar(dados: bytes, encoding: str | None, cortado: bool = False) -> tuple[str, str]:
    if encoding:
        return dados.decode(encoding, errors="replace"), encoding
    candidatos = [dados, _sem_utf8_incompleto(dados)] if cortado else [dados]
    for candidato in candidatos:
        try:
            return candidato.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass
    return dados.decode("cp1252", errors="replace"), "cp1252"


def _validar_encoding(encoding: str | None) -> None:
    if encoding:
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise ErroOperacao(f"Encoding desconhecido: {encoding}") from None


def _sha256(arquivo: Path) -> str:
    h = hashlib.sha256()
    with arquivo.open("rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _separar_linhas(dados: bytes) -> list[bytes]:
    """Quebra so em \\n, como o sftp_ler, mantendo o fim de linha."""
    partes = [p + b"\n" for p in dados.split(b"\n")]
    ultima = partes.pop()[:-1]
    if ultima:
        partes.append(ultima)
    return partes


def _mensagem_sftp(e: asyncssh.SFTPError, caminho: str) -> str:
    if isinstance(e, asyncssh.SFTPNoSuchFile):
        return f"Nao existe no servidor: {caminho}"
    if isinstance(e, asyncssh.SFTPPermissionDenied):
        return f"Sem permissao no servidor: {caminho}"
    return f"Erro SFTP em {caminho}: {e.reason}"


class Operacoes:
    def __init__(self, gerenciador: GerenciadorConexoes) -> None:
        self._gerenciador = gerenciador

    # ------------------------------------------------------------------
    # Apoio

    def _perfil(self, nome_perfil: str) -> Perfil:
        perfil = config.obter_perfil(nome_perfil)
        # A pasta local e conferida antes de conectar: sem ela nao ha onde auditar.
        armazenamento.pasta_perfil(perfil)
        return perfil

    async def _auditado(self, perfil: Perfil, operacao: str, dados: dict, executar):
        try:
            async with self._gerenciador.usar(perfil) as sftp:
                resultado = await executar(sftp)
        except ERROS_ESPERADOS as e:
            armazenamento.auditar(perfil, operacao, "erro", **{**dados, "erro": str(e)})
            raise
        except asyncssh.SFTPError as e:
            mensagem = _mensagem_sftp(e, dados.get("caminho", ""))
            armazenamento.auditar(perfil, operacao, "erro", **{**dados, "erro": mensagem})
            raise ErroOperacao(mensagem) from None
        except Exception as e:
            armazenamento.auditar(perfil, operacao, "erro", **{**dados, "erro": f"{type(e).__name__}: {e}"})
            raise
        armazenamento.auditar(perfil, operacao, "ok", **{**dados, **resultado.pop("_auditoria", {})})
        return resultado

    async def _realpath(self, sftp: asyncssh.SFTPClient, caminho: str) -> str:
        try:
            return await sftp.realpath(caminho)
        except asyncssh.SFTPError as e:
            raise ErroOperacao(_mensagem_sftp(e, caminho)) from None

    async def _resolver_existente(self, sftp, caminho: str) -> str:
        return await self._realpath(sftp, _validar_caminho(caminho))

    async def _resolver_novo(self, sftp, caminho: str) -> str:
        """Caminho de algo que pode nao existir: resolve a pasta mae e anexa o nome."""
        normalizado = _validar_caminho(caminho)
        nome = posixpath.basename(normalizado)
        if nome in ("", ".", ".."):
            raise ErroOperacao(f"Caminho sem nome de arquivo ou pasta: {caminho}")
        mae = posixpath.dirname(normalizado) or "."
        return posixpath.join(await self._realpath(sftp, mae), nome)

    async def _resolver_com_pais(self, sftp, caminho: str) -> str:
        """Resolve o ancestral mais proximo que existe e anexa os segmentos que faltam."""
        normalizado = _validar_caminho(caminho)
        faltando: list[str] = []
        atual = normalizado
        while True:
            try:
                base = await sftp.realpath(atual)
                break
            except asyncssh.SFTPNoSuchFile:
                nome = posixpath.basename(atual)
                if nome in ("", ".", ".."):
                    raise ErroOperacao(f"Nao foi possivel resolver {caminho}") from None
                faltando.insert(0, nome)
                atual = posixpath.dirname(atual) or "."
            except asyncssh.SFTPError as e:
                raise ErroOperacao(_mensagem_sftp(e, atual)) from None
        return posixpath.join(base, *faltando) if faltando else base

    async def _raizes_reais(self, sftp, raizes: list[str]) -> list[str]:
        reais = []
        for raiz in raizes:
            try:
                reais.append(await sftp.realpath(raiz))
            except asyncssh.SFTPError:
                raise ErroOperacao(f"A raiz {raiz} do perfil nao existe no servidor.") from None
        return reais

    async def _checar_leitura(self, perfil: Perfil, sftp, caminho_real: str) -> None:
        if perfil.raizes_leitura and not _dentro(caminho_real, await self._raizes_reais(sftp, perfil.raizes_leitura)):
            raise ErroOperacao(
                f"{caminho_real} esta fora das raizes de leitura do perfil {perfil.nome}: "
                f"{', '.join(perfil.raizes_leitura)}"
            )

    async def _checar_escrita(self, perfil: Perfil, sftp, caminho_real: str, *, estrito: bool = False) -> None:
        """estrito recusa a propria raiz: mover a raiz mexe na pasta mae, fora dela."""
        if not perfil.raizes_escrita:
            raise ErroOperacao(f"O perfil {perfil.nome} nao tem raizes_escrita: escrita desabilitada.")
        if not _dentro(caminho_real, await self._raizes_reais(sftp, perfil.raizes_escrita), estrito=estrito):
            raise ErroOperacao(
                f"{caminho_real} esta fora das raizes de escrita do perfil {perfil.nome}: "
                f"{', '.join(perfil.raizes_escrita)}"
            )

    async def _lstat(self, sftp, caminho: str) -> asyncssh.SFTPAttrs | None:
        try:
            return await sftp.lstat(caminho)
        except asyncssh.SFTPNoSuchFile:
            return None

    async def _stat_leitura(self, perfil, sftp, caminho: str) -> tuple[str, asyncssh.SFTPAttrs]:
        real = await self._resolver_existente(sftp, caminho)
        await self._checar_leitura(perfil, sftp, real)
        try:
            return real, await sftp.stat(real)
        except asyncssh.SFTPError as e:
            raise ErroOperacao(_mensagem_sftp(e, real)) from None

    async def _abrir_texto(self, perfil, sftp, caminho):
        real, attrs = await self._stat_leitura(perfil, sftp, caminho)
        if _tipo(attrs) != "arquivo":
            raise ErroOperacao(f"{real} nao e arquivo regular.")
        arquivo = await sftp.open(real, "rb")
        amostra = await arquivo.read(_AMOSTRA_BINARIO, 0)
        if b"\0" in amostra:
            await arquivo.close()
            return real, attrs, None
        return real, attrs, arquivo

    # ------------------------------------------------------------------
    # Leitura

    def perfis(self) -> list[dict]:
        lista = []
        for perfil in config.carregar_perfis().values():
            tipo_segredo = "senha" if perfil.autenticacao == "senha" else "passphrase"
            lista.append({
                "perfil": perfil.nome,
                "host": perfil.host,
                "porta": perfil.porta,
                "usuario": perfil.usuario,
                "ambiente": perfil.ambiente,
                "tspace_cliente": perfil.tspace_cliente,
                "autenticacao": perfil.autenticacao,
                "segredo_cadastrado": bool(credenciais.obter(perfil.nome, tipo_segredo)),
                "repositorio": perfil.repositorio,
                "repositorio_existe": bool(perfil.repositorio) and Path(perfil.repositorio).is_dir(),
                "raizes_leitura": perfil.raizes_leitura,
                "raizes_escrita": perfil.raizes_escrita,
            })
        return lista

    async def listar(self, nome_perfil: str, caminho: str = ".", padrao: str | None = None) -> dict:
        perfil = self._perfil(nome_perfil)

        async def executar(sftp):
            real, attrs = await self._stat_leitura(perfil, sftp, caminho)
            if _tipo(attrs) != "pasta":
                raise ErroOperacao(f"{real} nao e pasta. Use sftp_info ou sftp_ler.")
            entradas = []
            truncado = False
            for item in await sftp.readdir(real):
                if item.filename in (".", ".."):
                    continue
                if padrao and not fnmatch.fnmatch(item.filename.lower(), padrao.lower()):
                    continue
                if len(entradas) >= perfil.busca_max_resultados:
                    truncado = True
                    break
                entrada = _descrever(item.filename, item.attrs)
                entrada["nome"] = entrada.pop("caminho")
                entradas.append(entrada)
            entradas.sort(key=lambda e: (e["tipo"] != "pasta", e["nome"].lower()))
            return {
                "caminho": real,
                "entradas": entradas,
                "truncado": truncado,
                "_auditoria": {"itens": len(entradas)},
            }

        return await self._auditado(perfil, "listar", {"caminho": caminho}, executar)

    async def info(self, nome_perfil: str, caminho: str) -> dict:
        perfil = self._perfil(nome_perfil)

        async def executar(sftp):
            real, attrs = await self._stat_leitura(perfil, sftp, caminho)
            descricao = _descrever(real, attrs)
            descricao.update(uid=attrs.uid, gid=attrs.gid, acessado=_data(attrs.atime))
            return descricao

        return await self._auditado(perfil, "info", {"caminho": caminho}, executar)

    async def ler(
        self,
        nome_perfil: str,
        caminho: str,
        linha_inicial: int = 1,
        max_linhas: int | None = None,
        encoding: str | None = None,
    ) -> dict:
        _validar_encoding(encoding)
        if linha_inicial < 1 or (max_linhas is not None and max_linhas < 1):
            raise ErroOperacao("linha_inicial e max_linhas comecam em 1.")
        perfil = self._perfil(nome_perfil)

        async def executar(sftp):
            real, attrs, arquivo = await self._abrir_texto(perfil, sftp, caminho)
            if arquivo is None:
                return _binario(real, attrs)
            limite = perfil.max_leitura_bytes
            saida = bytearray()
            numero = 0
            devolvidas = 0
            truncado = False
            ha_mais = False
            try:
                async for linha in _linhas(arquivo, limite):
                    numero += 1
                    if numero < linha_inicial:
                        continue
                    if max_linhas is not None and devolvidas >= max_linhas:
                        ha_mais = True
                        break
                    if len(saida) + len(linha) > limite:
                        saida += linha[: limite - len(saida)]
                        truncado = True
                        break
                    saida += linha
                    devolvidas += 1
            finally:
                await arquivo.close()
            texto, usado = _decodificar(bytes(saida), encoding, cortado=truncado)
            return {
                "caminho": real,
                "tamanho": attrs.size,
                "encoding": usado,
                "linha_inicial": linha_inicial,
                "linhas_devolvidas": devolvidas,
                "truncado_no_limite_de_bytes": truncado,
                "ha_mais_linhas": ha_mais or truncado,
                "conteudo": texto,
                "_auditoria": {"bytes": len(saida)},
            }

        return await self._auditado(perfil, "ler", {"caminho": caminho}, executar)

    async def ultimas_linhas(
        self, nome_perfil: str, caminho: str, linhas: int = 100, encoding: str | None = None
    ) -> dict:
        _validar_encoding(encoding)
        if linhas < 1:
            raise ErroOperacao("linhas comeca em 1.")
        perfil = self._perfil(nome_perfil)

        async def executar(sftp):
            real, attrs, arquivo = await self._abrir_texto(perfil, sftp, caminho)
            if arquivo is None:
                return _binario(real, attrs)
            limite = perfil.max_leitura_bytes
            posicao = attrs.size
            dados = b""
            try:
                while posicao > 0 and dados.count(b"\n") <= linhas and len(dados) <= limite:
                    tamanho = min(_BLOCO, posicao)
                    posicao -= tamanho
                    dados = await arquivo.read(tamanho, posicao) + dados
            finally:
                await arquivo.close()
            partes = _separar_linhas(dados)
            # Sem ler o arquivo todo, a primeira linha lida pode estar pela metade.
            parcial = posicao > 0 and len(partes) == 1
            if posicao > 0 and len(partes) > 1:
                partes = partes[1:]
            partes = partes[-linhas:]
            truncado = False
            while len(partes) > 1 and sum(map(len, partes)) > limite:
                partes = partes[1:]
                truncado = True
            conteudo = b"".join(partes)
            if parcial or len(conteudo) > limite:
                # Ultima linha maior que o limite: devolve o final dela, sem
                # comecar no meio de um caractere UTF-8.
                conteudo = conteudo[-limite:]
                for _ in range(3):
                    if conteudo and conteudo[0] & 0xC0 == 0x80:
                        conteudo = conteudo[1:]
                truncado = True
            texto, usado = _decodificar(conteudo, encoding)
            return {
                "caminho": real,
                "tamanho": attrs.size,
                "encoding": usado,
                "linhas_devolvidas": len(partes),
                "truncado_no_limite_de_bytes": truncado,
                "conteudo": texto,
                "_auditoria": {"linhas": len(partes)},
            }

        return await self._auditado(perfil, "ultimas_linhas", {"caminho": caminho}, executar)

    async def buscar(
        self,
        nome_perfil: str,
        caminho: str,
        padrao: str = "*",
        tipo: str = "qualquer",
        profundidade: int | None = None,
    ) -> dict:
        if tipo not in ("qualquer", "arquivo", "pasta"):
            raise ErroOperacao("tipo deve ser qualquer, arquivo ou pasta.")
        perfil = self._perfil(nome_perfil)
        limite_profundidade = min(profundidade or perfil.busca_profundidade, perfil.busca_profundidade)

        async def executar(sftp):
            real, attrs = await self._stat_leitura(perfil, sftp, caminho)
            if _tipo(attrs) != "pasta":
                raise ErroOperacao(f"{real} nao e pasta.")
            achados = []
            sem_acesso = []
            truncado = False
            fila = deque([(real, 1)])
            while fila and not truncado:
                pasta, nivel = fila.popleft()
                try:
                    itens = await sftp.readdir(pasta)
                except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPPermissionDenied):
                    sem_acesso.append(pasta)
                    continue
                for item in itens:
                    if item.filename in (".", ".."):
                        continue
                    completo = posixpath.join(pasta, item.filename)
                    tipo_item = _tipo(item.attrs)
                    # Link para pasta nao e seguido: evita laco e saida da raiz.
                    if tipo_item == "pasta" and nivel < limite_profundidade:
                        fila.append((completo, nivel + 1))
                    if tipo != "qualquer" and tipo_item != tipo:
                        continue
                    if not fnmatch.fnmatch(item.filename.lower(), padrao.lower()):
                        continue
                    if len(achados) >= perfil.busca_max_resultados:
                        truncado = True
                        break
                    achados.append(_descrever(completo, item.attrs))
            return {
                "caminho": real,
                "padrao": padrao,
                "profundidade": limite_profundidade,
                "resultados": achados,
                "truncado": truncado,
                "pastas_sem_acesso": sem_acesso,
                "_auditoria": {"padrao": padrao, "itens": len(achados)},
            }

        return await self._auditado(perfil, "buscar", {"caminho": caminho}, executar)

    async def baixar(self, nome_perfil: str, caminho: str) -> dict:
        perfil = self._perfil(nome_perfil)

        async def executar(sftp):
            real, attrs = await self._stat_leitura(perfil, sftp, caminho)
            if _tipo(attrs) != "arquivo":
                raise ErroOperacao(f"{real} nao e arquivo regular. Baixe arquivo por arquivo.")
            destino = armazenamento.destino_download(perfil, real)
            try:
                await sftp.get(real, str(destino))
            except BaseException:
                destino.unlink(missing_ok=True)  # nao deixa download pela metade
                raise
            sha = _sha256(destino)
            tamanho = destino.stat().st_size
            return {
                "caminho_remoto": real,
                "arquivo_local": str(destino),
                "tamanho": tamanho,
                "sha256": sha,
                "_auditoria": {"arquivo_local": str(destino), "tamanho": tamanho, "sha256": sha},
            }

        return await self._auditado(perfil, "baixar", {"caminho": caminho}, executar)

    # ------------------------------------------------------------------
    # Escrita (as tools exigem aprovacao do usuario em toda chamada)

    async def enviar(
        self, nome_perfil: str, arquivo_local: str, caminho_remoto: str, sobrescrever: bool = False
    ) -> dict:
        origem = Path(arquivo_local).expanduser()
        if not origem.is_file():
            raise ErroOperacao(f"Arquivo local nao existe ou nao e arquivo: {origem}")
        perfil = self._perfil(nome_perfil)

        async def executar(sftp):
            alvo = caminho_remoto + origem.name if caminho_remoto.endswith("/") else caminho_remoto
            real = await self._resolver_novo(sftp, alvo)
            atual = await self._lstat(sftp, real)
            if atual is not None and _tipo(atual) == "pasta":
                real = posixpath.join(real, origem.name)
                atual = await self._lstat(sftp, real)
            await self._checar_escrita(perfil, sftp, real, estrito=True)
            backup = None
            if atual is not None:
                if _tipo(atual) != "arquivo":
                    raise ErroOperacao(f"{real} existe e nao e arquivo regular ({_tipo(atual)}). Nada enviado.")
                if not sobrescrever:
                    raise ErroOperacao(f"{real} ja existe. Para substituir, chame de novo com sobrescrever=true.")
                backup = armazenamento.destino_backup(perfil, real)
                try:
                    await sftp.get(real, str(backup))
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
            tamanho = origem.stat().st_size
            sha = _sha256(origem)
            await sftp.put(str(origem), real)
            enviado = await sftp.stat(real)
            if enviado.size != tamanho:
                raise ErroOperacao(
                    f"Tamanho no servidor ({enviado.size}) difere do local ({tamanho}) em {real}. "
                    + (f"Versao anterior salva em {backup}." if backup else "")
                )
            dados = {
                "caminho_remoto": real,
                "arquivo_local": str(origem),
                "tamanho": tamanho,
                "sha256": sha,
                "sobrescreveu": backup is not None,
                "backup_local": str(backup) if backup else None,
            }
            return {**dados, "_auditoria": dict(dados)}

        return await self._auditado(
            perfil, "enviar", {"caminho": caminho_remoto, "sobrescrever": sobrescrever}, executar
        )

    async def criar_pasta(self, nome_perfil: str, caminho: str, criar_pais: bool = False) -> dict:
        perfil = self._perfil(nome_perfil)

        async def executar(sftp):
            if criar_pais:
                real = await self._resolver_com_pais(sftp, caminho)
            else:
                real = await self._resolver_novo(sftp, caminho)
            await self._checar_escrita(perfil, sftp, real, estrito=True)
            if await self._lstat(sftp, real) is not None:
                raise ErroOperacao(f"{real} ja existe.")
            if criar_pais:
                await sftp.makedirs(real)
            else:
                await sftp.mkdir(real)
            return {"caminho": real, "_auditoria": {"criado": real}}

        return await self._auditado(perfil, "criar_pasta", {"caminho": caminho}, executar)

    async def renomear(self, nome_perfil: str, origem: str, destino: str) -> dict:
        perfil = self._perfil(nome_perfil)

        async def executar(sftp):
            # A origem nao segue o ultimo link: renomear um link mexe so no link.
            origem_real = await self._resolver_novo(sftp, origem)
            if await self._lstat(sftp, origem_real) is None:
                raise ErroOperacao(f"Nao existe no servidor: {origem_real}")
            destino_real = await self._resolver_novo(sftp, destino)
            await self._checar_escrita(perfil, sftp, origem_real, estrito=True)
            await self._checar_escrita(perfil, sftp, destino_real, estrito=True)
            if await self._lstat(sftp, destino_real) is not None:
                raise ErroOperacao(f"{destino_real} ja existe. Renomear nunca sobrescreve.")
            await sftp.rename(origem_real, destino_real)
            return {
                "origem": origem_real,
                "destino": destino_real,
                "_auditoria": {"origem_real": origem_real, "destino_real": destino_real},
            }

        return await self._auditado(perfil, "renomear", {"caminho": origem, "destino": destino}, executar)

    def vincular_repositorio(self, nome_perfil: str, caminho_repositorio: str) -> dict:
        perfil = config.obter_perfil(nome_perfil)
        pasta = Path(caminho_repositorio).expanduser()
        if not pasta.is_dir():
            raise ErroOperacao(f"Pasta nao existe: {pasta}")
        raiz = armazenamento.raiz_git(pasta)
        if raiz is None:
            raise ErroOperacao(f"{pasta} nao e repositorio git.")
        anterior = perfil.repositorio
        perfil.repositorio = str(raiz)
        config.salvar_perfil(perfil)
        relativo = armazenamento.subpasta_relativa(perfil.nome)
        pasta_local = armazenamento.pasta_perfil(perfil)
        aviso = None
        if not armazenamento.ignorado_pelo_git(raiz, relativo):
            aviso = (
                f"{relativo.as_posix()} NAO esta ignorado pelo git em {raiz}: downloads e backups do "
                "cliente podem entrar num commit. Avise o usuario."
            )
        armazenamento.auditar(
            perfil, "vincular_repositorio", "ok", repositorio=str(raiz), anterior=anterior or None, aviso=aviso
        )
        return {"perfil": perfil.nome, "repositorio": str(raiz), "pasta_local": str(pasta_local), "aviso": aviso}


def _binario(real: str, attrs: asyncssh.SFTPAttrs) -> dict:
    descricao = _descrever(real, attrs)
    descricao["binario"] = True
    descricao["mensagem"] = "Arquivo binario: conteudo nao devolvido. Use sftp_baixar para trazer uma copia."
    return descricao


async def _linhas(arquivo, limite: int):
    """Linhas do arquivo em bytes, com o fim de linha. Linha maior que o limite vem cortada."""
    pendente = bytearray()
    cortada = False
    posicao = 0
    while True:
        bloco = await arquivo.read(_BLOCO, posicao)
        if not bloco:
            break
        posicao += len(bloco)
        inicio = 0
        while True:
            fim = bloco.find(b"\n", inicio)
            if fim < 0:
                if not cortada:
                    pendente += bloco[inicio:]
                    if len(pendente) > limite:
                        del pendente[limite + 1:]
                        cortada = True
                break
            if not cortada:
                pendente += bloco[inicio:fim + 1]
            yield bytes(pendente[: limite + 1])
            pendente.clear()
            cortada = False
            inicio = fim + 1
    if pendente:
        yield bytes(pendente)
