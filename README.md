# Odradek Scraper (estudo / fins educacionais)

**Idioma / Language:** Português (Brasil) | [English](README.en.md)

Projeto **pessoal e educacional** para estudar, em ambiente local:

- painel responsivo em React/TypeScript servido pela API FastAPI
- armazenamento local com SQLite
- integração com APIs de terceiros (quando o usuário configura as próprias chaves)
- parsing de PDF e suporte ao preenchimento manual de formulários

O código da interface fica em `frontend/`. A API, a coleta e os módulos de negócio ficam em `backend/`. O banco SQLite e os arquivos persistentes continuam na raiz do projeto para preservar os dados locais anteriores.

O acesso ao banco usa Neon/PostgreSQL quando `DATABASE_URL` está definida. Sem essa variável, usa o SQLite local em `jobs.db`, inclusive para testes locais isolados.

Dentro de `backend/`, `web/` contém rotas, autenticação e respostas do painel; `data/` contém banco e segredos; `jobs/` contém fila, filtros e regras; `candidate/` contém currículo e contexto; `integrations/` contém clientes externos. `app.py` coordena a coleta e os fluxos de trabalho.

Na coleta LinkedIn/Apify existem exatamente dois filtros: o **Principal** (TI e programação, a área de maior experiência) e o **Secundário** (uma área de reserva menos afetada pela crise, opcional). Cada um é um cargo com no máximo duas frases: o título e uma única variante equivalente do mesmo cargo (por exemplo, a versão em português ou em inglês). A quantidade base de cada actor (`count`, ou `apify_job_count` quando o actor não informa `count`) vai inteira para o Principal quando só ele está ativo; com o Secundário ativo, o Principal recebe a fatia `linkedin_main_share` (padrão 70%, de 50% a 95%) e o Secundário o resto, com mínimo de 1 vaga para cada (base 25: 18 e 7). Cada combinação de frase e escopo gera buscas de 24 horas e de até 7 dias, em lotes de até 8 URLs, com os lotes de 24 horas dos dois filtros primeiro. A quantidade reduzida é enviada em cada lote; ela não é um teto global de vagas por ciclo nem uma garantia de custo fixo. O orçamento mensal é consultado antes de cada execução. Interrupções por parada ou orçamento deixam um aviso de cobertura incompleta nos logs. Vagas desses filtros não passam novamente pelas keywords do painel; as verificações de vagas fechadas, recência e duplicidade continuam valendo. Sem filtro Principal gerado, nenhuma busca LinkedIn é feita (não há fallback para as keywords gerais nem geração automática). Os filtros só são substituídos por uma regeneração explícita, e gerar um não altera o outro. Actors `custom` mantêm sua quantidade e usam seu template e as preferências gerais.

**Local e modelo de trabalho não são escolhidos pela IA.** Com a elegibilidade geográfica ligada (`geo_eligibility_enabled`, padrão ligada), as buscas são derivadas do Perfil: remoto em "Worldwide", remoto no seu país e qualquer modelo na sua cidade. A triagem aplica a mesma regra a partir de afirmações citadas e verificadas da vaga: vaga no exterior só passa se for remota **e** disser expressamente que candidatos do seu local podem se candidatar; vaga híbrida ou presencial só passa na sua cidade de residência; remota no seu país sempre passa. Desligada, país e modelo deixam de bloquear qualquer vaga e as buscas não filtram por modelo. A exigência explícita de autorização de trabalho que você não tem continua bloqueando nos dois casos.

A IA propõe cada filtro a partir do **perfil verificado** do currículo (cargos com período, habilidades com meses de uso calculados pelo código), dos fatos profissionais e do prompt editável no painel. O Principal é o cargo mais sustentado pelo currículo (mais meses datados e/ou cargo atual); cada frase precisa citar `evidence_ids` existentes no perfil, e ao menos um deles deve ser um cargo seu, senão a frase é descartada. Um qualificador de stack (como "Python") só vale com 12 meses de uso comprovado ou uso no cargo atual. Os cargos-âncora e o tempo de carreira exibidos no filtro são calculados pelo código, nunca informados pela IA. O programa usa os filtros resultantes sem remapear níveis ou descartar vagas por listas de cargos e limites fixos de anos de experiência. Cada vaga guarda o filtro que a trouxe (`search_filter_id`), e a tela mostra por filtro quantas vagas vieram (7 e 30 dias), quantas a triagem aprovou e quantas aprovadas você depois ignorou, com um aviso (nunca uma desativação automática) quando a precisão está baixa após 20 vagas triadas.

**Filtro Secundário (opcional).** É gerado só pelo botão "Gerar filtro secundário com IA", e liga sozinho ao ser gerado (há um liga/desliga). A IA escolhe uma função e um setor historicamente menos sensíveis a crise em que TI e experiência administrativa se aplicam, citando capacidades transferíveis ou cargos do currículo, com `sector` e `resilience_reason`. Um campo opcional de dica ("administrativo com sistemas") direciona a escolha, e a IA não repete a família de cargo do Principal. Vagas dele usam `transferable_minimum_score` e exigem cobertura das responsabilidades centrais.

### Currículo e triagem (v2)

1. **Perfil verificado.** O PDF é lido preservando linhas e seções. A IA extrai cargos, habilidades, formação, idiomas e capacidades transferíveis (por exemplo, suporte a usuários e organização de processos vindos da experiência administrativa), sempre com uma citação literal. O código confere cada citação no texto do PDF e descarta o que não encontra; meses por habilidade, `last_used` e tempo total de experiência são calculados pelo código, não pela IA.
2. **Requisitos × evidência.** Para cada vaga, a IA extrai os requisitos (com citação da vaga) e diz se cada um é atendido, transferível, parcial ou não atendido, citando o currículo. O código verifica as duas citações: requisito sem citação válida na vaga é descartado, e "atendido" sem citação válida no currículo vira "não atendido". Isso elimina o falso positivo por correspondência inventada.
3. **Requisitos inflados.** A IA julga cada requisito contra o próprio anúncio (por exemplo, "Estágio" pedindo 5 anos) e só o marca como inflado citando o trecho da vaga que o contradiz. Requisitos inflados saem do cálculo e não geram bloqueio, e o painel mostra quais foram desconsiderados e por quê. Localização, autorização de trabalho, idioma exigido e credenciais nunca são desconsiderados.
4. **Nota determinística, sem listas fixas.** A nota combina cobertura dos obrigatórios (peso 70%), desejáveis (15%) e adequação da família de cargo (15%); habilidade transferível verificada vale 0,75. Bloqueios comparam o que a vaga exige com o que o currículo prova (anos exigidos × meses calculados, idioma, localização, formação). Os pesos ficam em `SCORING_V2` (`backend/jobs/job_assessment.py`). O controle "tolerância da seleção" define a cobertura mínima dos obrigatórios (0 → 100%, 50 → 80%, 75 → 70%, 100 → 55%); `minimum_match_score` (padrão 70) e `transferable_minimum_score` (padrão 60) são os cortes de nota de cada trilha.
5. **Revisão cética.** Toda vaga aprovada passa por uma segunda chamada que procura motivos concretos e citados para não candidatar; uma objeção bloqueante verificada reprova a vaga.
6. **Cartas e Qpid.** A carta recebe as pontes transferíveis verificadas. O Qpid recebe trechos do currículo por seção ou cargo, sem texto gerado por IA, com os trechos que sustentam a triagem da vaga primeiro.

A auditoria completa (requisitos, evidências, bloqueios, objeções) fica em `ai_decisions.assessment_json` e aparece no card da vaga em "Por que esta nota". Para calibrar pesos e cortes com seus próprios dados, rode manualmente `python backend/checks/eval_triage.py` (chama a IA de verdade; use `--dry-run` para só listar as vagas rotuladas).

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
python -m uvicorn web.api_asgi:api --app-dir backend --host 127.0.0.1 --port 8765
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
- O PDF do currículo é salvo na tabela `resume_files` (`BYTEA` no Neon/PostgreSQL, `BLOB` no SQLite), na mesma transação do cadastro. O download do Qpid usa o banco; deploys não dependem da pasta local para preservar PDFs novos. A inicialização cria a tabela automaticamente e importa PDFs antigos acessíveis em `RESUMES_DIR`, conferindo seu SHA-256. Se o arquivo antigo já desapareceu, reenvie-o uma vez em cada idioma. A migração SQLite → Neon também copia os binários e tenta importar PDFs legados ao lado do banco de origem.
- `RESUMES_DIR` passa a servir para compatibilidade e cache de anexos de e-mail. Outros arquivos enviados em `COPILOT_UPLOADS_DIR` continuam exigindo disco persistente; esta mudança cobre os currículos.
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

O contexto de candidatura do Qpid inclui trechos do texto extraído do currículo enviado, identificados como `sourceOnly`. Eles servem como evidência para rascunhos e sugestões revisáveis, não como regras para copiar o texto inteiro em campos. O cliente seleciona trechos relevantes para cada grupo de perguntas e mantém decisões de consentimento, cidadania e disponibilidade individuais.
