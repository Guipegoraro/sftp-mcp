from __future__ import annotations

import asyncio
import json
import sys

import pytest

from sftp_mcp import cadastro, config, credenciais
from sftp_mcp.conexao import ErroConexao
from sftp_mcp.operacoes import ErroOperacao

from .conftest import SENHA, USUARIO


def _dados(servidor, repositorio="", senha=SENHA, url=None) -> cadastro.DadosCadastro:
    return cadastro.DadosCadastro(
        nome="novo-dev",
        url=url or f"sftp://127.0.0.1:{servidor}",
        usuario=USUARIO,
        senha=senha,
        ambiente="dev",
        repositorio=repositorio,
    )


@pytest.mark.parametrize(
    "usuario, ambiente",
    [
        ("ftp_CUP8TG_development", "dev"),
        ("ftp_C56YW4_production", "prod"),
        ("ftp_C56YW4_staging", "hml"),
        ("ftp_ABC_homolog", "hml"),
        ("usuario_qualquer", "dev"),
        ("", "dev"),
    ],
)
def test_deduz_ambiente_sem_dar_erro(usuario, ambiente):
    assert cadastro.deduzir_ambiente(usuario) == ambiente


def test_senha_nao_aparece_no_repr():
    assert "segredo" not in repr(_dados(22, senha="segredo"))


async def test_cadastro_completo(servidor, chave_host, repositorio):
    chave = await cadastro.obter_chave(_dados(servidor))
    assert not cadastro.chave_conhecida(chave)
    resultado = await cadastro.concluir(_dados(servidor, str(repositorio)), chave)
    assert resultado["status"] == "cadastrado" and resultado["aviso"] is None
    assert SENHA not in json.dumps(resultado)
    perfil = config.obter_perfil("novo-dev")
    assert perfil.raizes_escrita == [] and perfil.repositorio
    assert credenciais.obter("novo-dev", "senha") == SENHA
    assert cadastro.chave_conhecida(chave)


async def test_senha_errada_nao_deixa_nada_gravado(servidor):
    chave = await cadastro.obter_chave(_dados(servidor))
    with pytest.raises(ErroConexao, match="Autenticacao recusada"):
        await cadastro.concluir(_dados(servidor, senha="errada"), chave)
    assert "novo-dev" not in config.carregar_perfis()
    assert credenciais.obter("novo-dev", "senha") is None


async def test_rota_da_url_vira_pasta_inicial(servidor, repositorio, remoto, ops):
    (remoto / "logs" / "a.txt").write_text("x")
    url = f"sftp://127.0.0.1:{servidor}/logs"
    chave = await cadastro.obter_chave(_dados(servidor))
    await cadastro.concluir(_dados(servidor, str(repositorio), url=url), chave)
    resultado = await ops.listar("novo-dev")
    assert resultado["caminho"] == "/logs" and [e["nome"] for e in resultado["entradas"]] == ["a.txt"]


async def test_cadastro_nao_toca_perfil_existente(perfil, servidor):
    """O perfil pode surgir enquanto o dialogo da chave esta aberto."""
    dados = _dados(servidor)
    dados.nome = perfil.nome
    chave = await cadastro.obter_chave(dados)
    with pytest.raises(ErroOperacao, match="ja existe"):
        await cadastro.concluir(dados, chave)
    assert config.obter_perfil(perfil.nome).raizes_escrita == ["/escrita"]
    dados.senha = "errada"
    with pytest.raises(ErroConexao):
        await cadastro.concluir(dados, chave)
    assert credenciais.obter(perfil.nome, "senha") == SENHA


async def test_falha_no_repositorio_nao_deixa_nada_gravado(servidor, repositorio):
    (repositorio / ".claude").write_text("arquivo no lugar da pasta")
    chave = await cadastro.obter_chave(_dados(servidor))
    with pytest.raises(OSError):
        await cadastro.concluir(_dados(servidor, str(repositorio)), chave)
    assert "novo-dev" not in config.carregar_perfis()
    assert credenciais.obter("novo-dev", "senha") is None


async def test_login_nao_grava_a_senha_antes_de_terminar(servidor, monkeypatch):
    gravacoes = []
    monkeypatch.setattr(credenciais, "gravar", lambda *a: gravacoes.append(a))
    chave = await cadastro.obter_chave(_dados(servidor))
    with pytest.raises(ErroConexao):
        await cadastro.concluir(_dados(servidor, senha="errada"), chave)
    assert gravacoes == []


async def test_rota_que_e_arquivo_nao_grava(servidor, remoto):
    (remoto / "logs" / "a.txt").write_text("x")
    url = f"sftp://127.0.0.1:{servidor}/logs/a.txt"
    chave = await cadastro.obter_chave(_dados(servidor))
    with pytest.raises(ErroConexao, match="nao e pasta"):
        await cadastro.concluir(_dados(servidor, url=url), chave)


async def test_rota_inexistente_nao_grava(servidor):
    url = f"sftp://127.0.0.1:{servidor}/nao-existe"
    chave = await cadastro.obter_chave(_dados(servidor))
    with pytest.raises(ErroConexao):
        await cadastro.concluir(_dados(servidor, url=url), chave)
    assert "novo-dev" not in config.carregar_perfis()


def test_erro_dados():
    base = dict(nome="novo", url="sftp://h", usuario="u", senha="s", ambiente="dev")
    assert cadastro.erro_dados(cadastro.DadosCadastro(**base)) is None
    assert "Nome" in cadastro.erro_dados(cadastro.DadosCadastro(**{**base, "nome": "Com Espaco"}))
    assert "URL" in cadastro.erro_dados(cadastro.DadosCadastro(**{**base, "url": ""}))
    assert "senha" in cadastro.erro_dados(cadastro.DadosCadastro(**{**base, "senha": ""}))


async def test_tool_devolve_o_que_a_janela_imprime(monkeypatch):
    saida = json.dumps({"status": "cancelado"})
    monkeypatch.setattr(cadastro, "comando_janela", lambda *_: [sys.executable, "-c", f"print({saida!r})"])
    assert await cadastro.abrir_janela("x", "") == {"status": "cancelado"}


async def test_tool_cancelada_fecha_a_janela(monkeypatch, tmp_path):
    marca = tmp_path / "viva.txt"
    script = f"import time,pathlib\nwhile True:\n    pathlib.Path({str(marca)!r}).write_text(str(time.time()))\n    time.sleep(0.05)\n"
    monkeypatch.setattr(cadastro, "comando_janela", lambda *_: [sys.executable, "-c", script])
    tarefa = asyncio.create_task(cadastro.abrir_janela("x", ""))
    while not marca.exists():
        await asyncio.sleep(0.05)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa
    await asyncio.sleep(0.2)
    valor = marca.read_text()
    await asyncio.sleep(0.3)
    assert marca.read_text() == valor


def test_janela_roda_sem_a_pasta_atual_no_path():
    assert "-P" in cadastro.comando_janela("x", "")


def test_janela_monta_e_deduz_ambiente():
    tk = pytest.importorskip("tkinter")
    from sftp_mcp.janela import JanelaCadastro

    try:
        raiz = tk.Tk()
    except tk.TclError:
        pytest.skip("Sem display")
    try:
        janela = JanelaCadastro(raiz, "cbm-dev", "C:/repo")
        janela.usuario.set("ftp_CUP8TG_production")
        raiz.update()
        assert janela.ambiente.get() == "prod"
        assert janela.perfil.get() == "cbm-dev" and janela.repositorio.get() == "C:/repo"
        assert janela.resultado == {"status": "cancelado"}
        janela._ocupado(True)
        janela._salvar()
        assert janela.mensagem.get() == "Conectando..."
    finally:
        raiz.destroy()
