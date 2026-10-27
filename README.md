# Odradek Scraper (estudo / fins educacionais)

**Idioma / Language:** Português (Brasil) | [English](README.en.md)

Projeto **pessoal e educacional** para estudar, em ambiente local:

- painel responsivo em React/TypeScript servido pela API FastAPI
- armazenamento local com SQLite
- integração com APIs de terceiros (quando o usuário configura as próprias chaves)
- parsing de PDF e suporte ao preenchimento manual de formulários

O código da interface fica em `frontend/`. A API, a coleta e os módulos de negócio ficam em `backend/`. O banco SQLite e os arquivos persistentes continuam na raiz do projeto para preservar os dados locais anteriores.

**Não é um produto, serviço comercial nem ferramenta oferecida para uso em massa.**  
O código existe para aprendizado e experimentação **na sua máquina**.

## Aviso importante

- Respeite os **termos de uso** de qualquer site ou API que você consultar. Automação e scraping podem ser **proibidos** por essas plataformas.
- O autor **não incentiva** violar termos de serviço, contornar CAPTCHA/login nem abusar de contas.
- Qualquer uso além do estudo local é **por sua conta e risco**.
- Dados sensíveis (currículo, CPF, chaves de API, sessão de navegador) devem permanecer **apenas no seu computador** — nunca em commits.

Este repositório é fornecido **“como está”**, sem garantias. Não constitui aconselhamento jurídico.

## O que roda localmente

1. Interface web em `http://127.0.0.1:8765` (somente localhost por padrão).
2. Banco SQLite local (`jobs.db` — ignorado pelo Git).
3. Segredos (chaves de IA, tokens, senha SMTP) via **keyring** do sistema operacional, não no repositório.

## Setup (referência)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cd frontend
npm ci
npm run build
cd ..
# dependências de browser, se for estudar esses módulos:
# playwright install
python -m uvicorn api_asgi:api --app-dir backend --host 127.0.0.1 --port 8765
```

Abra `http://127.0.0.1:8765`. O painel local não exige senha por padrão. Configure `PANEL_PASSWORD` se expuser a porta além da sua máquina.

### Painel web

A interface responsiva está em `http://127.0.0.1:8765/` após compilar o frontend.

Ela cobre Visão geral, Vagas para revisar, Filas de IA, Logs, Uso e cotas, Assistente, Currículos, perguntas pendentes do copiloto, configurações, regras de formulário, teste SMTP, edição visual e geração com IA dos filtros LinkedIn. Para desenvolver o frontend, execute `npm run dev` em `frontend/` com o backend Python na porta 8765. A spec e o estado da migração estão em `docs/frontend-migration-spec.md`.

O `Dockerfile` compila o frontend e inicia FastAPI em um único Web Service. No Render, selecione o runtime Docker para o serviço existente e mantenha as variáveis e os diretórios persistentes já usados. Configure uma senha longa e exclusiva em `PANEL_PASSWORD` antes do deploy; sem ela, o painel responde com erro 503. O acesso usa uma página de senha e um cookie assinado, válido por 7 dias, com `HttpOnly`, `SameSite=Lax` e `Secure` no Render. O botão **Sair** remove o cookie do navegador. Alterar `PANEL_PASSWORD` invalida as sessões existentes. Opcionalmente, configure `PANEL_SESSION_SECRET` com um valor aleatório estável entre deploys para fortalecer a assinatura. A mesma proteção cobre a interface, a API e os downloads. Os endpoints de perfil e regras para a extensão exigem a sessão e não liberam acesso de qualquer origem. A extensão precisa de adaptação para acessar o serviço hospedado.

Cada clique em **Iniciar bot** executa um único ciclo de coleta e encerra automaticamente. Para buscar de novo, clique em **Iniciar bot** outra vez. Não existe agendamento recorrente nem intervalo automático; **Parar bot** interrompe o ciclo em andamento.

## Deploy com Neon/PostgreSQL

- A aplicação usa SQLite local quando `DATABASE_URL` não está definida e PostgreSQL quando ela aponta para Neon.
- Execute no Neon SQL Editor o schema PostgreSQL completo (tabelas, índices e valores padrão) antes de iniciar a aplicação.
- Para copiar os dados locais para Neon, pare o serviço hospedado e rode `python backend/migrate_sqlite_to_neon.py` no computador que contém `jobs.db`, com `DATABASE_URL` definido para a conexão Neon. O script importa tabelas e dados, preserva as configurações locais e substitui as regras iniciais/logs de primeiro boot pelas versões locais.
- Configure a mesma `DATABASE_URL` como variável secreta no Render. Nunca a salve no repositório.
- O Render não oferece o cofre de credenciais do sistema operacional usado pelo `keyring` local. Para salvar e alterar chaves pelo painel, configure **uma única vez** `SECRET_ENCRYPTION_KEY` em **Environment**. Gere uma chave Fernet localmente com `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, copie o resultado para o Render e mantenha uma cópia segura: sem essa mesma chave, os segredos salvos não podem ser descriptografados. As chaves de API são criptografadas com Fernet antes de serem gravadas como valores internos na tabela `settings` do Postgres. Variáveis como `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `APIFY_TOKEN` e `SMTP_PASSWORD` continuam aceitas como fallback, mas segredos cadastrados pelo painel no banco têm prioridade.
- Currículos e arquivos enviados ficam no sistema de arquivos, não no banco; configure um disco persistente no Render e defina `RESUMES_DIR` e `COPILOT_UPLOADS_DIR` para diretórios nesse disco. Copie os arquivos locais para lá antes de usar os registros migrados.
- No Render, `PANEL_PASSWORD` é obrigatória e protege o painel e a API com sessão por cookie de 7 dias. Mantenha HTTPS, fornecido pelo Render, e adapte a extensão caso ela precise acessar o serviço hospedado.
- O servidor não executa candidaturas por navegador. Em vagas com formulário, use **Abrir vaga + extensão** e importe manualmente seu perfil pelo botão da extensão; revise os campos e envie a candidatura no Chrome. A automação do servidor permanece limitada ao envio por e-mail SMTP. O endpoint `/api/autofill-rules` compartilha somente nomes e aliases de campos com a extensão.

## Privacidade e o que não versionar

Já ignorados pelo Git (entre outros):

- `jobs.db` e variantes
- PDFs em `resumes/`
- `linkedin_browser_profile/`, `copilot_uploads/`
- `docs/superpowers/` (notas internas de planejamento)
- `tests/` (suíte local)

Antes de qualquer push: `git status` e confirme que não há dados pessoais, currículos ou chaves.

## Licença

Uso educacional / estudo. Sem licença de redistribuição comercial definida.  
Se for bifurcar o código, mantenha os avisos deste README.
