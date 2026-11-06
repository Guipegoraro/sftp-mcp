"""Janela de cadastro do perfil. Roda em processo proprio e imprime so o resultado, sem a senha."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import cadastro
from .conexao import fingerprint
from .config import AMBIENTES


class JanelaCadastro:
    def __init__(self, raiz: tk.Tk, perfil: str = "", repositorio: str = "") -> None:
        self.raiz = raiz
        self.resultado: dict = {"status": "cancelado"}
        self._ambiente_escolhido = False
        self._em_andamento = False

        raiz.title("Cadastrar SFTP do TCloud")
        raiz.resizable(False, False)
        quadro = ttk.Frame(raiz, padding=16)
        quadro.grid()
        quadro.columnconfigure(1, weight=1)

        self.url = tk.StringVar()
        self.usuario = tk.StringVar()
        self.senha = tk.StringVar()
        self.ambiente = tk.StringVar(value=cadastro.AMBIENTE_PADRAO)
        self.perfil = tk.StringVar(value=perfil)
        self.repositorio = tk.StringVar(value=repositorio)
        self.mostrar_senha = tk.BooleanVar()
        self.mensagem = tk.StringVar()

        self.campo_url = self._campo(quadro, 0, "URL", self.url)
        self._campo(quadro, 1, "Usuário", self.usuario)
        self.campo_senha = self._campo(quadro, 2, "Senha", self.senha, show="*")
        ttk.Checkbutton(
            quadro, text="Mostrar senha", variable=self.mostrar_senha, command=self._alternar_senha
        ).grid(row=3, column=1, sticky="w", pady=(0, 8))

        ttk.Label(quadro, text="Ambiente").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=3)
        caixa = ttk.Combobox(quadro, textvariable=self.ambiente, values=AMBIENTES, state="readonly", width=10)
        caixa.grid(row=4, column=1, sticky="w", pady=3)
        caixa.bind("<<ComboboxSelected>>", lambda _: setattr(self, "_ambiente_escolhido", True))
        self.usuario.trace_add("write", lambda *_: self._atualizar_ambiente())

        self._campo(quadro, 5, "Perfil", self.perfil)
        self._campo(quadro, 6, "Repositório", self.repositorio)

        ttk.Label(quadro, textvariable=self.mensagem, foreground="#b00020", wraplength=420).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        botoes = ttk.Frame(quadro)
        botoes.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(botoes, text="Cancelar", command=raiz.destroy).grid(row=0, column=0, padx=(0, 8))
        self.botao_salvar = ttk.Button(botoes, text="Salvar", command=self._salvar)
        self.botao_salvar.grid(row=0, column=1)

        raiz.bind("<Return>", lambda _: self._salvar())
        raiz.bind("<Escape>", lambda _: raiz.destroy())
        self._trazer_para_frente()
        self.campo_url.focus_set()

    def _campo(self, quadro, linha: int, rotulo: str, variavel, **opcoes) -> ttk.Entry:
        ttk.Label(quadro, text=rotulo).grid(row=linha, column=0, sticky="w", padx=(0, 12), pady=3)
        campo = ttk.Entry(quadro, textvariable=variavel, width=58, **opcoes)
        campo.grid(row=linha, column=1, sticky="ew", pady=3)
        return campo

    def _trazer_para_frente(self) -> None:
        # Aberta por um processo em segundo plano, a janela nasceria atras do terminal.
        self.raiz.attributes("-topmost", True)
        self.raiz.after(500, lambda: self.raiz.attributes("-topmost", False))
        self.raiz.focus_force()

    def _alternar_senha(self) -> None:
        self.campo_senha.configure(show="" if self.mostrar_senha.get() else "*")

    def _atualizar_ambiente(self) -> None:
        if not self._ambiente_escolhido:
            self.ambiente.set(cadastro.deduzir_ambiente(self.usuario.get()))

    def _dados(self) -> cadastro.DadosCadastro:
        return cadastro.DadosCadastro(
            nome=self.perfil.get().strip(),
            url=self.url.get().strip(),
            usuario=self.usuario.get().strip(),
            senha=self.senha.get(),
            ambiente=self.ambiente.get(),
            repositorio=self.repositorio.get().strip(),
        )

    def _salvar(self) -> None:
        # O Enter nao passa pelo botao desabilitado.
        if self._em_andamento:
            return
        dados = self._dados()
        try:
            erro = cadastro.erro_dados(dados)
        except Exception as e:
            erro = str(e) or type(e).__name__
        if erro:
            self.mensagem.set(erro)
            return
        self._ocupado(True)
        self._em_segundo_plano(cadastro.obter_chave(dados), lambda chave: self._conferir_chave(dados, chave))

    def _conferir_chave(self, dados: cadastro.DadosCadastro, chave: str) -> None:
        try:
            conhecida = cadastro.chave_conhecida(chave)
        except Exception as e:
            self._falhar(str(e) or type(e).__name__)
            return
        if not conhecida:
            aceita = messagebox.askyesno(
                "Chave do servidor",
                f"Primeira vez com esta chave:\n\n{chave.split()[0]} {fingerprint(chave)}\n\nAceitar?",
                parent=self.raiz,
            )
            if not aceita:
                self.mensagem.set("Chave não aceita. Nada foi gravado.")
                self._ocupado(False)
                return
        self._em_segundo_plano(cadastro.concluir(dados, chave), self._terminar)

    def _terminar(self, resultado: dict) -> None:
        self.resultado = resultado
        self.raiz.destroy()

    def _em_segundo_plano(self, corrotina, continuar) -> None:
        """A rede fica fora da thread do Tk; o retorno volta para ela pelo after."""

        def executar() -> None:
            try:
                valor = asyncio.run(corrotina)
            except Exception as e:
                mensagem = str(e) or type(e).__name__
                self.raiz.after(0, lambda: self._falhar(mensagem))
                return
            self.raiz.after(0, lambda: continuar(valor))

        threading.Thread(target=executar, daemon=True).start()

    def _falhar(self, mensagem: str) -> None:
        self.mensagem.set(mensagem)
        self._ocupado(False)

    def _ocupado(self, ocupado: bool) -> None:
        self._em_andamento = ocupado
        self.botao_salvar.configure(state="disabled" if ocupado else "normal")
        if ocupado:
            self.mensagem.set("Conectando...")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perfil", default="")
    parser.add_argument("--repositorio", default="")
    args = parser.parse_args(argv)
    raiz = tk.Tk()
    janela = JanelaCadastro(raiz, args.perfil, args.repositorio)
    raiz.mainloop()
    print(json.dumps(janela.resultado))
    return 0


if __name__ == "__main__":
    sys.exit(main())
