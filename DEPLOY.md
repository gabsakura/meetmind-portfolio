# Deploy do portfólio no Render

O `render.yaml` cria o serviço web e o PostgreSQL. O processo de entrada aplica as
migrações e garante o seed sintético a cada inicialização.

## Operação privada e calendários

As contas públicas da demonstração continuam somente leitura. Para usar uploads, ações,
equipe, comunicações, Google Calendar ou Microsoft 365 no Render, configure as contas
privadas e os seguintes segredos no serviço:

```text
PORTFOLIO_CALENDAR_OAUTH_ENABLED=true
PORTFOLIO_CALENDAR_TEST_EMAIL=<seu e-mail privado de acesso ao MeetMind>
PORTFOLIO_CALENDAR_TEST_PASSWORD=<senha exclusiva com 12+ caracteres>
PORTFOLIO_MANAGER_TEST_EMAIL=<outro e-mail privado para o gestor>
PORTFOLIO_MANAGER_TEST_PASSWORD=<outra senha exclusiva com 12+ caracteres>
CALENDAR_TOKEN_ENCRYPTION_KEY=<chave Fernet>
PUBLIC_BASE_URL=https://<seu-servico>.onrender.com
```

Configure pelo menos um par completo de credenciais:

```text
GOOGLE_CALENDAR_CLIENT_ID
GOOGLE_CALENDAR_CLIENT_SECRET

MICROSOFT_CALENDAR_CLIENT_ID
MICROSOFT_CALENDAR_CLIENT_SECRET
MICROSOFT_CALENDAR_TENANT=common
```

Cadastre exatamente estas URIs como plataforma **Web**, substituindo o domínio pelo URL
real exibido pelo Render:

```text
https://<seu-servico>.onrender.com/perfil/calendario/callback/google
https://<seu-servico>.onrender.com/perfil/calendario/callback/microsoft
```

No Google Cloud, habilite a Calendar API e adicione a conta usada como test user quando
a tela de consentimento estiver em Testing. No Microsoft Entra, selecione contas de
qualquer organização e contas pessoais caso queira aceitar Outlook/Hotmail, e mantenha o
tenant `common`.

Depois de salvar os segredos, faça um novo deploy. O seed criará uma equipe privada sem
exibir suas contas na tela de login. Entre como analista com
`PORTFOLIO_CALENDAR_TEST_EMAIL`, abra **Perfil → Agenda e trabalho** e conecte o
provedor. Depois entre com `PORTFOLIO_MANAGER_TEST_EMAIL`, abra **Equipe**, consulte a
rotina do analista e use **Ações → Entregas da equipe** para atribuir e agendar.

Notificações por e-mail são opcionais. Para habilitá-las, preencha `SMTP_HOST`,
`SMTP_USERNAME`, `SMTP_PASSWORD` e `SMTP_FROM`; sem isso, as notificações continuam
disponíveis dentro do MeetMind.

Nunca coloque client secrets, senha privada, chave Fernet ou tokens no Git.
