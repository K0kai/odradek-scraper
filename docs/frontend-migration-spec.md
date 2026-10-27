# Spec inicial: migração do painel web

Status: interface e camada HTTP migradas localmente; validação no Render pendente
Data: 27/10/2026
Escopo: interface web, camada HTTP e deploy. A lógica de coleta, triagem e candidatura permanece em Python.

## Estado da implementação

- Experiências implementadas em `/`: Visão geral, Vagas para revisar, Filas de IA, Logs, Uso e cotas, Assistente, Currículos, perguntas pendentes do copiloto, configurações, regras de formulário, teste SMTP, edição visual e geração com IA dos filtros LinkedIn, com filtros, paginação, status, anotações e ações operacionais. `/new` permanece como alias.
- A navegação usa a logo existente do projeto, mantém filtros na URL e registra mudanças de tela no histórico do navegador. O link antigo de filtros foi retirado da navegação, pois essa função já está em Configurações.
- A leitura das configurações de busca mostra o prompt efetivo do LinkedIn mesmo quando o banco usa o texto padrão e apresenta o filtro salvo no formato antigo quando a lista nova ainda não foi gravada. Essa compatibilidade não altera os dados armazenados ao abrir a página.
- Leituras, ações, uploads, downloads e rotas usadas pela interface são atendidos diretamente por FastAPI. O handler `http.server`, o encaminhamento interno e a página HTML antiga foram removidos.
- O build React/TypeScript é produzido em `frontend/dist` e servido pelo mesmo Web Service Python. O Dockerfile inicia FastAPI, mas não foi validado no Render.
- O painel hospedado exige `PANEL_PASSWORD` e protege a interface, a API e os downloads com uma sessão por cookie assinado de 7 dias. A compatibilidade da extensão com acesso hospedado permanece pendente.
- Verificação local: build do frontend, leitura HTTP, ações migradas, larguras de 360, 390, 768 e 1280 px, e preservação de foco e texto durante atualização automática.
- Pendências externas: adaptação da extensão ao acesso hospedado, build Docker e deploy de teste no Render, além de medições de desempenho no ambiente hospedado.

## 1. Objetivo

Substituir o painel montado dentro de `app.py` por uma interface responsiva, com comportamento previsível durante atualizações de dados e formulários. Preservar as funções atuais e manter, inicialmente, um único Web Service no Render.

Sucesso significa que o usuário consegue acompanhar a coleta, examinar e classificar vagas, operar filas e configurar o sistema em desktop e celular sem perda de foco, filtros, dados digitados ou posição de leitura causada por atualizações automáticas.

## 2. Situação anterior

- `app.py` concentra servidor HTTP, regras de negócio, HTML, CSS e JavaScript.
- `render_page()` produz a página inteira. `live_payload()` devolve estado junto com fragmentos HTML para diversas áreas.
- O navegador chamava `/live` a cada dois segundos e substituía regiões do DOM. Havia mecanismos para evitar respostas antigas e preservar foco, mas a complexidade estava distribuída pelo script da página.
- Existe CSS para telas menores, porém a navegação, as tabelas e os formulários ainda dependem de uma estrutura feita primeiro para desktop.
- A maioria das ações usa POSTs de formulário; alguns fluxos já aceitam JSON. A extensão de navegador consome `/api/autofill-profile` e `/api/autofill-rules`.
- O painel original não possuía autenticação. A proteção por senha e sessão por cookie foi acrescentada para o Render durante a migração; a extensão ainda precisa ser adaptada para esse acesso hospedado.

## 3. Decisões de arquitetura

| Área | Decisão inicial |
| --- | --- |
| Frontend | React, TypeScript e Vite, em `frontend/` |
| Backend HTTP | FastAPI, com rotas divididas por domínio funcional |
| Domínio público | Um único Web Service, com interface e `/api/*` na mesma origem |
| Dados e processos | Reutilizar módulos Python, banco e fila existentes; não mover coleta ou IA para o navegador |
| Atualização de estado | Consultas JSON específicas por tela, com intervalo ajustado ao tipo de dado e pausa quando a aba estiver oculta |
| Tempo real | Avaliar SSE após medir o fluxo com consultas específicas; não é pré-requisito da primeira entrega |

O build do frontend gera arquivos estáticos. O processo Python atende `/api/*`, `/health`, downloads e os arquivos compilados, com fallback para `index.html` nas rotas da interface. Arquivos com hash podem receber cache longo; `index.html` e respostas com dados pessoais não. O deploy precisa construir tanto o frontend quanto o ambiente Python. A forma exata do build no Render deve ser fixada na etapa de implementação e validada no ambiente de teste.

FastAPI organiza a camada HTTP, mas não exige converter toda a lógica atual para funções assíncronas. Trabalho demorado continua na fila ou em processos próprios, fora do ciclo de uma requisição.

## 4. Contrato da API

As novas rotas devem responder com dados estruturados, erros estruturados e códigos HTTP adequados. A interface não deve receber HTML gerado pelo backend para desenhar tabelas, cartões, logs ou formulários. O contrato será documentado pelo OpenAPI e consumido por tipos TypeScript gerados ou mantidos a partir dele.

Proposta de agrupamento inicial, sujeita a ajuste ao extrair os casos de uso:

| Grupo | Leituras e ações necessárias |
| --- | --- |
| `GET /api/dashboard` | Totais, estado da coleta, vagas recentes e execuções recentes |
| `/api/jobs` | Lista paginada, filtros, ordenação, detalhes, status e notas |
| `/api/collector` | Estado, iniciar e solicitar parada |
| `/api/queue` | Estado, cancelar, tentar novamente, reenfileirar e limpar itens finais |
| `/api/resumes` | Estado dos currículos, upload, reanálise e downloads pertinentes |
| `/api/settings` | Leitura e gravação por grupo: busca, perfil, IA, automação, SMTP e regras |
| `/api/usage` | Uso e atualização explícita de cotas |
| `/api/logs` | Logs paginados ou limitados e limpeza explícita |
| `/api/assistant` | Envio de pergunta e resposta |
| `/api/copilot-asks` | Pergunta pendente, resposta, cancelamento e modo manual |

Os nomes finais das rotas devem ser definidos junto com os formatos de resposta. Evitar um único `/live` que agrega toda a tela. Filtros, paginação e ordenação das vagas devem ser parâmetros da API, com validação no servidor. Toda mutação precisa retornar o estado resultante ou um identificador consultável, além de uma mensagem utilizável pela interface.

Os contratos de `/api/autofill-profile` e `/api/autofill-rules` permanecem no FastAPI. Eles exigem sessão e não enviam CORS amplo. A extensão precisa de um fluxo de acesso hospedado antes de voltar a consumir esses endpoints fora da origem do painel. Não ampliar os dados pessoais expostos sem uma revisão específica.

## 5. Interface e experiência

### Estrutura

- Navegação principal com áreas: Visão geral, Vagas para revisar, Filas, Assistente, Uso, Logs e Configurações.
- Em desktop, menu lateral e área de conteúdo. Em celular, navegação compacta acessível sem ocupar a altura inicial da tela.
- Tabelas densas podem existir no desktop; no celular, as vagas devem aparecer em cartões ou linhas resumidas com detalhe expansível. Evitar rolagem horizontal da página inteira.
- Separar formulários longos por assunto e exibir claramente estado não salvo, salvando, salvo e erro.

### Comportamento

- Estado de filtros, página e ordenação da lista de vagas deve sobreviver à navegação entre áreas e, quando útil, estar refletido na URL.
- Atualizações automáticas não podem substituir um campo em edição nem fechar detalhes abertos.
- Operações longas exibem progresso ou estado consultável, com ação indisponível enquanto a mesma solicitação estiver em andamento.
- Erros de rede mostram mensagem recuperável e mantêm os dados digitados.
- Estados vazio, carregando, erro e sucesso devem existir para cada área dinâmica.
- Navegação por teclado, foco visível, rótulos de campos e anúncios de status devem fazer parte dos componentes.

### Critérios de aceitação de responsividade

- Verificar larguras de 360, 390, 768 e 1280 px em navegador real.
- Não haver rolagem horizontal do documento nessas larguras; exceção somente para um componente de dados identificado e com alternativa utilizável no celular.
- Iniciar/parar coleta, filtrar vagas, alterar status, responder ao copiloto, editar configurações e enviar currículo devem ser operáveis com toque e teclado.
- Durante atualização automática da lista, o foco e o valor do campo em edição permanecem intactos.
- O painel continua utilizável após falha temporária de uma consulta, sem precisar recarregar a página para repetir a ação.

## 6. Segurança e dados

- Antes de expor a nova interface na internet, implementar controle de acesso no servidor. Proteger também API, downloads e ações que alteram estado. Não tratar ocultação de botões no React como proteção.
- Rever o acesso da extensão após adicionar autenticação. Os dois endpoints preservam apenas os dados necessários, mas agora exigem sessão e não liberam CORS amplo; a extensão hospedada precisa de um fluxo próprio.
- Segredos continuam no backend. A API de leitura de configurações informa somente se um segredo está configurado, nunca o valor salvo.
- Currículos, CPF, notas e logs não devem entrar em logs de requisição, erros enviados ao cliente ou cache público.
- Uploads precisam manter validação de tipo e tamanho, limites de requisição e armazenamento persistente configurado para o Render.
- Preservar o uso atual de SQLite local e PostgreSQL por `DATABASE_URL`; a migração de interface não implica migração de banco.

## 7. Plano incremental

1. **Base da API:** separar inicialização e casos de uso do handler HTTP; criar FastAPI e contratos JSON para estado da coleta, dashboard e vagas.
2. **Primeira experiência vertical:** implementar navegação, Visão geral e Vagas para revisar no React, incluindo filtros, paginação, status e notas. Validar desktop e celular.
3. **Operação:** migrar filas, logs, uso, assistente, perguntas do copiloto e currículos. Confirmar que ações demoradas não bloqueiam a resposta HTTP.
4. **Configurações:** migrar formulários e regras de formulário por grupos. Verificar preservação de dados não salvos e tratamento de segredos.
5. **Deploy e substituição:** produzir build estático no mesmo Web Service, ativar acesso protegido, testar extensão e uploads no Render, servir o novo painel na rota principal e remover a página antiga. A rota principal e a remoção foram implementadas localmente; a verificação no Render permanece pendente.

Cada etapa deve deixar o serviço utilizável. A lógica de negócio permanece em Python e é chamada pela API FastAPI.

## 8. Verificação e saída

- Comparar as ações e estados disponíveis em cada aba antiga com a nova antes de desativar a antiga.
- Verificar fluxos completos em desktop e celular: coleta, revisão de vaga, fila, currículo, assistente, configurações e extensão.
- Medir no ambiente hospedado o tempo de carregamento inicial, tamanho do build, duração das consultas mais frequentes e volume de chamadas por aba aberta. Registrar valores antes e depois para avaliar se a experiência melhorou.
- Validar refresh direto de uma rota do frontend, cache dos arquivos estáticos, downloads, upload, variáveis de ambiente e persistência após redeploy.
- Só considerar o deploy concluído após validar as funções e o acesso protegido no Render.

## 9. Pontos ainda a decidir

- Acesso da extensão hospedada aos endpoints protegidos, com origem e credenciais específicas.
- Se o processo de coleta e os workers devem permanecer no Web Service ou ganhar um worker separado por confiabilidade operacional. Isso não é necessário para separar o frontend, mas afeta reinícios e escalonamento.
- Metas numéricas de desempenho, após obter medições do painel atual no Render e em celular.
- Necessidade real de SSE para estado e logs após a primeira versão com consultas JSON específicas.
