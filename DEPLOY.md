# Publicar a vitrine do MeetMind

Esta configuração publica somente a demonstração sintética e somente leitura. Ela não
recebe transcrições, não executa IA externa e não conecta calendários.

## Render Blueprint

1. Faça um fork ou use o repositório público do projeto.
2. No Render, escolha **New → Blueprint** e selecione o repositório.
3. Confirme o arquivo `render.yaml`.
4. Preencha os três valores solicitados:
   - `DEMO_PORTFOLIO_PASSWORD`: senha aleatória com pelo menos 16 caracteres;
   - `PRIVACY_CONTACT_EMAIL`: e-mail que receberá solicitações de privacidade;
   - `PUBLIC_BASE_URL`: URL HTTPS final do serviço, sem barra no final.
5. Aguarde a migração e o seed idempotente terminarem.
6. Entre com `ana@meetmind.example` e a senha definida no passo 4.

O Blueprint cria um serviço Docker e um PostgreSQL. `SECRET_KEY` é gerada pelo Render.
Nenhum segredo fica no Git. A especificação usa apenas recursos necessários à vitrine.

## Verificação depois do deploy

- `/api/v1/health/ready` responde `200`;
- a página de login identifica o ambiente como demonstração;
- Ana consegue navegar pelo dashboard, análises e ações;
- qualquer tentativa de alterar dados recebe bloqueio de somente leitura;
- upload, OAuth, integrações e administração não aparecem;
- `/auth/privacidade` e `/auth/termos` estão acessíveis sem login;
- cookies possuem `Secure`, `HttpOnly` e `SameSite=Lax`;
- nenhum token, transcrição real ou dado pessoal aparece nos logs.

## Ambientes completos

O `docker-compose.yml` permanece como ambiente de engenharia para executar PostgreSQL,
Redis, workers e ClamAV. Esse ambiente não deve ser exposto diretamente à internet. Uma
instalação operacional exige autenticação corporativa, armazenamento externo, antivírus,
workers e gestão formal de segredos, conforme `docs/ENTERPRISE_READINESS.md`.
