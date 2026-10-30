# sftp-mcp

Servidor MCP para acessar, pelo Claude Code, os servidores SFTP dos clientes. A leitura é livre. Toda escrita pede a sua aprovação, inclusive com `--dangerously-skip-permissions`.

## O que ele faz

| Tool | Tipo | O que faz |
|---|---|---|
| `sftp_perfis` | leitura | Lista os perfis cadastrados, sem segredo |
| `sftp_listar` | leitura | Lista uma pasta, com filtro glob opcional |
| `sftp_info` | leitura | Tipo, tamanho, datas e permissões |
| `sftp_ler` | leitura | Lê texto por faixa de linhas, até o limite do perfil (padrão 1 MB) |
| `sftp_ultimas_linhas` | leitura | Final de log sem ler o arquivo inteiro |
| `sftp_buscar` | leitura | Busca por nome, até a profundidade do perfil (padrão 5) |
| `sftp_baixar` | leitura | Copia o arquivo para a pasta local do perfil, sem sobrescrever |
| `sftp_enviar` | **escrita** | Envia arquivo local; sobrescrever exige `sobrescrever=true` e faz backup antes |
| `sftp_criar_pasta` | **escrita** | Cria pasta |
| `sftp_renomear` | **escrita** | Renomeia ou move, sem nunca sobrescrever |
| `sftp_vincular_repositorio` | **escrita** | Define o repositório local do cliente |

Não existe tool de apagar.

## Travas

- **Aprovação em toda escrita.** As tools de escrita declaram `_meta["anthropic/requiresUserInteraction"]: true`, e o Claude Code mostra o prompt de permissão em toda chamada, sem "não perguntar de novo", em qualquer modo de permissão. Como segunda camada, cadastre as mesmas tools em `permissions.ask` (veja Instalação).
- **Raízes de escrita.** Cada perfil declara `raizes_escrita`. Sem raiz, o perfil não escreve. O caminho é resolvido pelo servidor (`realpath`) antes da checagem, então `..` e symlink não saem da raiz. Escrever sobre um symlink é recusado.
- **Host key fixada.** A chave pública do servidor é gravada no cadastro, depois que você confere o fingerprint. Se o servidor apresentar outra, a conexão é recusada. O `~/.ssh/known_hosts`, o `~/.ssh/config` e o agente SSH não são usados.
- **Segredo fora do modelo.** Senha e passphrase ficam no keyring do sistema (Credential Manager no Windows) e nunca são parâmetro de tool.
- **Tudo auditado.** Cada operação gera uma linha JSON em `audit.log`, sem conteúdo de arquivo e sem segredo.

## Pasta local do cliente

Cada perfil aponta para o repositório git do cliente na sua máquina. Downloads, backups e auditoria ficam em:

```
<repositório>/.claude/sftp/<perfil>/
├── downloads/   espelha o caminho remoto
├── backups/     <data-hora>/<caminho remoto>, versão anterior de cada arquivo sobrescrito
└── audit.log
```

A pasta `.claude/` não pode entrar no git do cliente. Ao vincular, o servidor roda `git check-ignore` e avisa se ela não estiver ignorada.

Se o perfil não tiver repositório, as tools falham pedindo que o agente pergunte a pasta a você e chame `sftp_vincular_repositorio`.

## Instalação

Requer Python 3.11 ou superior.

```powershell
git clone https://github.com/guipegoraro/sftp-mcp
cd sftp-mcp
pip install -e .
```

Registre no Claude Code (escopo de usuário):

```powershell
claude mcp add -s user sftp -- python -m sftp_mcp
```

Em `~/.claude/settings.json`, acrescente a segunda camada de aprovação:

```json
{
  "permissions": {
    "ask": [
      "mcp__sftp__sftp_enviar",
      "mcp__sftp__sftp_criar_pasta",
      "mcp__sftp__sftp_renomear",
      "mcp__sftp__sftp_vincular_repositorio"
    ]
  }
}
```

## Cadastro de perfis

```powershell
sftp-mcp-setup adicionar cliente-prod     # pergunta os dados, mostra o fingerprint e grava a senha
sftp-mcp-setup listar
sftp-mcp-setup testar cliente-prod
sftp-mcp-setup definir-senha cliente-prod
sftp-mcp-setup trocar-fingerprint cliente-prod
sftp-mcp-setup vincular-repositorio cliente-prod C:\caminho\do\repo
sftp-mcp-setup remover cliente-prod
```

Os perfis ficam em `~/.sftp-mcp/perfis.toml` (ou na pasta de `SFTP_MCP_HOME`). Dá para editar à mão:

```toml
[perfis.cliente-prod]
host = "sftp.cliente.com.br"
porta = 22
usuario = "totvs"
host_key = "ssh-ed25519 AAAA..."
autenticacao = "senha"          # ou "chave", com chave_privada = "C:\\caminho\\id_ed25519"
chave_privada = ""
ambiente = "prod"               # prod, hml ou dev
tspace_cliente = ""
repositorio = "C:\\caminho\\do\\repo"
raizes_leitura = []             # vazio = servidor inteiro
raizes_escrita = ["/totvs/protheus/apo", "/totvs/protheus/bin/appserver"]
timeout_conexao = 15
ocioso_max_segundos = 300
max_leitura_bytes = 1048576
busca_profundidade = 5
busca_max_resultados = 2000
```

Chave privada no formato PuTTY (`.ppk`) não é aceita. No PuTTYgen: *Load*, depois *Conversions > Export OpenSSH key*.

## Testes

```powershell
pip install -e .[teste]
python -m pytest
```

Os testes sobem um servidor SFTP do `asyncssh` no próprio processo e usam um keyring em memória. Eles não acessam servidor real.

## Limites conhecidos

- Só SFTP. FTP e FTPS não são suportados.
- Sem jump host. VPN fica por sua conta.
- Quem roda código como o seu usuário do Windows lê o Credential Manager. Veja [SECURITY.md](SECURITY.md).
