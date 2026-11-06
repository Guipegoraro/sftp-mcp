from __future__ import annotations

import json
import os

import asyncssh
import pytest

from sftp_mcp import config, server
from sftp_mcp.armazenamento import ErroRepositorio
from sftp_mcp.conexao import ErroConexao
from sftp_mcp.operacoes import ErroOperacao

from .conftest import SENHA, pasta_local


def _auditoria(perfil) -> list[dict]:
    linhas = (pasta_local(perfil) / "audit.log").read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in linhas]


# ----------------------------------------------------------------------
# Leitura


async def test_listar(ops, perfil, remoto):
    (remoto / "logs" / "console.log").write_text("ok\n")
    (remoto / "logs" / "sub").mkdir()
    resultado = await ops.listar(perfil.nome, "/logs")
    assert resultado["caminho"] == "/logs"
    assert [(e["nome"], e["tipo"]) for e in resultado["entradas"]] == [("sub", "pasta"), ("console.log", "arquivo")]


async def test_listar_com_padrao(ops, perfil, remoto):
    for nome in ("a.log", "b.LOG", "c.txt"):
        (remoto / "logs" / nome).write_text("x")
    resultado = await ops.listar(perfil.nome, "/logs", "*.log")
    assert sorted(e["nome"] for e in resultado["entradas"]) == ["a.log", "b.LOG"]


async def test_info(ops, perfil, remoto):
    (remoto / "logs" / "x.ini").write_bytes(b"12345")
    resultado = await ops.info(perfil.nome, "/logs/x.ini")
    assert resultado["tipo"] == "arquivo" and resultado["tamanho"] == 5


async def test_ler_utf8_e_cp1252(ops, perfil, remoto):
    (remoto / "logs" / "utf8.txt").write_bytes("ação\n".encode("utf-8"))
    (remoto / "logs" / "ansi.txt").write_bytes("ação\n".encode("cp1252"))
    utf8 = await ops.ler(perfil.nome, "/logs/utf8.txt")
    ansi = await ops.ler(perfil.nome, "/logs/ansi.txt")
    assert (utf8["encoding"], utf8["conteudo"]) == ("utf-8", "ação\n")
    assert (ansi["encoding"], ansi["conteudo"]) == ("cp1252", "ação\n")


async def test_ler_faixa_de_linhas(ops, perfil, remoto):
    (remoto / "logs" / "n.txt").write_bytes("".join(f"linha {i}\n" for i in range(1, 11)).encode())
    resultado = await ops.ler(perfil.nome, "/logs/n.txt", linha_inicial=3, max_linhas=2)
    assert resultado["conteudo"] == "linha 3\nlinha 4\n"
    assert resultado["ha_mais_linhas"] is True


async def test_ler_respeita_limite_de_bytes(ops, perfil, remoto):
    perfil.max_leitura_bytes = 100
    config.salvar_perfil(perfil)
    (remoto / "logs" / "grande.txt").write_text("x" * 50 + "\n" + "y" * 500 + "\n")
    resultado = await ops.ler(perfil.nome, "/logs/grande.txt")
    assert len(resultado["conteudo"]) == 100
    assert resultado["truncado_no_limite_de_bytes"] is True


async def test_ler_binario_devolve_so_metadados(ops, perfil, remoto):
    (remoto / "logs" / "rpo.rpo").write_bytes(b"\x00\x01\x02" * 10)
    resultado = await ops.ler(perfil.nome, "/logs/rpo.rpo")
    assert resultado["binario"] is True and "conteudo" not in resultado


async def test_ultimas_linhas(ops, perfil, remoto):
    (remoto / "logs" / "console.log").write_bytes("".join(f"{i}\n" for i in range(1, 50001)).encode())
    resultado = await ops.ultimas_linhas(perfil.nome, "/logs/console.log", 3)
    assert resultado["conteudo"] == "49998\n49999\n50000\n"


async def test_buscar_com_profundidade(ops, perfil, remoto):
    profundo = remoto / "logs" / "a" / "b" / "c"
    profundo.mkdir(parents=True)
    (remoto / "logs" / "a" / "erro.log").write_text("1")
    (profundo / "erro.log").write_text("2")
    raso = await ops.buscar(perfil.nome, "/logs", "*.log", profundidade=2)
    fundo = await ops.buscar(perfil.nome, "/logs", "*.log", tipo="arquivo")
    assert [r["caminho"] for r in raso["resultados"]] == ["/logs/a/erro.log"]
    assert sorted(r["caminho"] for r in fundo["resultados"]) == ["/logs/a/b/c/erro.log", "/logs/a/erro.log"]


async def test_baixar_espelha_caminho_e_nao_sobrescreve(ops, perfil, remoto):
    (remoto / "logs" / "appserver.ini").write_text("[geral]\n")
    primeiro = await ops.baixar(perfil.nome, "/logs/appserver.ini")
    segundo = await ops.baixar(perfil.nome, "/logs/appserver.ini")
    base = pasta_local(perfil) / "downloads" / "logs"
    assert primeiro["arquivo_local"] == str(base / "appserver.ini")
    assert segundo["arquivo_local"] == str(base / "appserver (1).ini")


async def test_raiz_de_leitura_bloqueia_fora(ops, perfil, remoto):
    perfil.raizes_leitura = ["/logs"]
    config.salvar_perfil(perfil)
    (remoto / "fora" / "segredo.txt").write_text("x")
    with pytest.raises(ErroOperacao, match="fora das raizes de leitura"):
        await ops.ler(perfil.nome, "/logs/../fora/segredo.txt")


# ----------------------------------------------------------------------
# Escrita


async def test_enviar_arquivo_novo(ops, perfil, remoto, tmp_path):
    local = tmp_path / "patch.ptm"
    local.write_bytes(b"conteudo do patch")
    resultado = await ops.enviar(perfil.nome, str(local), "/escrita/")
    assert resultado["caminho_remoto"] == "/escrita/patch.ptm"
    assert (remoto / "escrita" / "patch.ptm").read_bytes() == b"conteudo do patch"
    assert resultado["sobrescreveu"] is False


async def test_enviar_existente_exige_sobrescrever_e_faz_backup(ops, perfil, remoto, tmp_path):
    (remoto / "escrita" / "appserver.ini").write_text("versao antiga")
    local = tmp_path / "appserver.ini"
    local.write_text("versao nova")
    with pytest.raises(ErroOperacao, match="sobrescrever=true"):
        await ops.enviar(perfil.nome, str(local), "/escrita/appserver.ini")
    resultado = await ops.enviar(perfil.nome, str(local), "/escrita/appserver.ini", sobrescrever=True)
    assert (remoto / "escrita" / "appserver.ini").read_text() == "versao nova"
    with open(resultado["backup_local"], encoding="utf-8") as f:
        assert f.read() == "versao antiga"


async def test_enviar_fora_da_raiz_de_escrita(ops, perfil, remoto, tmp_path):
    local = tmp_path / "x.txt"
    local.write_text("x")
    for destino in ("/fora/x.txt", "/escrita/../fora/x.txt", "/x.txt"):
        with pytest.raises(ErroOperacao, match="fora das raizes de escrita"):
            await ops.enviar(perfil.nome, str(local), destino)
    assert not (remoto / "fora" / "x.txt").exists()


async def test_enviar_por_link_que_sai_da_raiz(ops, perfil, remoto, tmp_path):
    try:
        os.symlink(remoto / "fora", remoto / "escrita" / "atalho", target_is_directory=True)
    except OSError:
        pytest.skip("Sem permissao para criar symlink neste Windows")
    local = tmp_path / "x.txt"
    local.write_text("x")
    with pytest.raises(ErroOperacao, match="fora das raizes de escrita"):
        await ops.enviar(perfil.nome, str(local), "/escrita/atalho/x.txt")
    assert not (remoto / "fora" / "x.txt").exists()


async def test_perfil_sem_raiz_de_escrita_escreve_em_qualquer_pasta(ops, perfil, remoto, tmp_path):
    perfil.raizes_escrita = []
    config.salvar_perfil(perfil)
    local = tmp_path / "x.txt"
    local.write_text("x")
    await ops.enviar(perfil.nome, str(local), "/fora/x.txt")
    assert (remoto / "fora" / "x.txt").exists()


async def test_criar_pasta(ops, perfil, remoto):
    await ops.criar_pasta(perfil.nome, "/escrita/nova")
    await ops.criar_pasta(perfil.nome, "/escrita/a/b/c", criar_pais=True)
    assert (remoto / "escrita" / "nova").is_dir() and (remoto / "escrita" / "a" / "b" / "c").is_dir()
    with pytest.raises(ErroOperacao, match="fora das raizes"):
        await ops.criar_pasta(perfil.nome, "/fora/nova")


async def test_renomear_nao_sobrescreve(ops, perfil, remoto):
    (remoto / "escrita" / "a.txt").write_text("a")
    (remoto / "escrita" / "b.txt").write_text("b")
    with pytest.raises(ErroOperacao, match="nunca sobrescreve"):
        await ops.renomear(perfil.nome, "/escrita/a.txt", "/escrita/b.txt")
    await ops.renomear(perfil.nome, "/escrita/a.txt", "/escrita/c.txt")
    assert (remoto / "escrita" / "c.txt").read_text() == "a"
    with pytest.raises(ErroOperacao, match="fora das raizes"):
        await ops.renomear(perfil.nome, "/escrita/c.txt", "/fora/c.txt")


async def test_caminho_com_caractere_invisivel(ops, perfil):
    with pytest.raises(ErroOperacao, match="invisivel"):
        await ops.listar(perfil.nome, "/escrita‮")


# ----------------------------------------------------------------------
# Conexao, repositorio e auditoria


async def test_chave_de_host_divergente_recusa(ops, perfil):
    perfil.host_key = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode()
    config.salvar_perfil(perfil)
    with pytest.raises(ErroConexao, match="nao confere"):
        await ops.listar(perfil.nome, "/")


async def test_sem_repositorio_pede_para_perguntar(ops, perfil):
    perfil.repositorio = ""
    config.salvar_perfil(perfil)
    with pytest.raises(ErroRepositorio, match="Pergunte ao usuario"):
        await ops.listar(perfil.nome, "/")


async def test_vincular_repositorio(ops, perfil, tmp_path):
    with pytest.raises(ErroOperacao, match="nao e repositorio git"):
        ops.vincular_repositorio(perfil.nome, str(tmp_path / "remoto"))
    resultado = ops.vincular_repositorio(perfil.nome, perfil.repositorio)
    assert resultado["aviso"] is None
    (tmp_path / "repo-cliente" / ".gitignore").write_text("")
    assert "NAO esta ignorado" in ops.vincular_repositorio(perfil.nome, perfil.repositorio)["aviso"]


async def test_auditoria_registra_sem_segredo(ops, perfil, remoto, tmp_path):
    (remoto / "logs" / "a.txt").write_text("conteudo-sigiloso")
    await ops.ler(perfil.nome, "/logs/a.txt")
    with pytest.raises(ErroOperacao):
        await ops.ler(perfil.nome, "/logs/nao-existe.txt")
    registros = _auditoria(perfil)
    assert [(r["operacao"], r["resultado"]) for r in registros] == [("ler", "ok"), ("ler", "erro")]
    texto = (pasta_local(perfil) / "audit.log").read_text(encoding="utf-8")
    assert SENHA not in texto and "conteudo-sigiloso" not in texto


async def test_conexao_reaproveitada(ops, perfil, remoto):
    await ops.listar(perfil.nome, "/")
    primeira = ops._gerenciador._conexoes[perfil.nome].conexao
    await ops.listar(perfil.nome, "/logs")
    assert ops._gerenciador._conexoes[perfil.nome].conexao is primeira


# ----------------------------------------------------------------------
# Registro das tools


async def test_so_as_tools_de_escrita_exigem_aprovacao():
    ferramentas = {t.name: t for t in await server.mcp.list_tools()}
    escrita = {"sftp_enviar", "sftp_criar_pasta", "sftp_renomear", "sftp_vincular_repositorio"}
    for nome, ferramenta in ferramentas.items():
        exige = (ferramenta.meta or {}).get("anthropic/requiresUserInteraction") is True
        assert exige == (nome in escrita), nome
        assert ferramenta.annotations.readOnlyHint == (nome not in escrita | {"sftp_cadastrar"}), nome
    assert not any("apagar" in n or "delete" in n or "remover" in n for n in ferramentas)
