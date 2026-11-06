# Modelo de ameaças

## O que está protegido

| Ativo | Onde fica | Proteção |
|---|---|---|
| Senha ou passphrase | Keyring do sistema (serviço `sftp-mcp`, usuário `<perfil>:senha`) | DPAPI por usuário no Windows. Nunca vai para arquivo, parâmetro de tool, log ou mensagem de erro |
| Identidade do servidor | `host_key` no `perfis.toml` | Chave divergente recusa a conexão antes de enviar a senha |
| Arquivos do cliente | `<repositório>/.claude/sftp/<perfil>/` | Fora do git do cliente; o vínculo avisa se a pasta não estiver ignorada |
| Servidor do cliente | Escrita | Prompt de aprovação em toda chamada, sem apagar, com backup antes de sobrescrever. `raizes_escrita` limita as pastas, se declarada |

## Contra o próprio agente

O risco principal é o modelo escrever no servidor do cliente sem você querer, por engano ou induzido por conteúdo lido (log, `.ini`, arquivo de terceiro).

- As tools de escrita declaram `anthropic/requiresUserInteraction`. O Claude Code pede confirmação em toda chamada, sem depender de configuração. Testado com `--dangerously-skip-permissions` e sem regra no `settings.json`.
- O prompt mostra os parâmetros da chamada. Caminho com caractere de controle ou invisível (bidi, zero-width) é recusado, para o destino exibido ser o destino real.
- `sftp_enviar` só envia arquivo que já existe no disco local, e o caminho dele aparece no prompt.
- A senha é digitada na janela de cadastro, em processo próprio. Ela não passa pelo servidor MCP nem pelo modelo.
- O servidor não executa comando remoto. Só operações SFTP.

## O que NÃO está protegido

- **A aprovação depende do cliente MCP.** O prompt vem do Claude Code, por causa da marca `requiresUserInteraction`. O servidor não confere se houve aprovação: outro cliente que ignore a marca escreve sem perguntar. Nesse caso, a única trava é `raizes_escrita`, se declarada.
- **Escrita pelo shell.** O MCP só controla as próprias tools. Se o agente tiver acesso ao shell, ele pode rodar `sftp`, `curl` ou importar este pacote num script, e nada aqui impede. Com `--dangerously-skip-permissions`, isso acontece sem prompt.
- **Código malicioso rodando como o seu usuário do Windows** lê o Credential Manager, altera o `perfis.toml` (inclusive `raizes_escrita` e `host_key`) e importa este pacote para escrever sem aprovação.
- **Chave de host reaproveitada.** Chave já aceita em outro perfil entra sem confirmação no cadastro. Um servidor diferente que apresente a mesma chave precisaria ter a chave privada dela.
- **Corrida entre checagem e escrita (TOCTOU).** Entre o `realpath` e a escrita, alguém com acesso ao servidor pode trocar uma pasta por symlink. Exige um atacante já dentro do servidor do cliente.
- **Upload interrompido** deixa o arquivo remoto incompleto. A versão anterior está em `backups/`.
- **Leitura do servidor inteiro** quando `raizes_leitura` está vazio. É a configuração padrão, porque o usuário SFTP do cliente costuma já ser restrito.
- **Downloads e backups** ficam em texto puro no disco, com a proteção do `$HOME` e do repositório.
