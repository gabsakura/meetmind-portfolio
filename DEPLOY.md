# Deploy do portfólio no Render

O `render.yaml` cria o serviço web e o PostgreSQL. O processo de entrada aplica as
migrações e garante o seed sintético a cada inicialização.

## Contas controladas e calendários

O acesso ao MeetMind sempre começa por uma conta previamente cadastrada no banco. Google
e Microsoft não criam usuários e não substituem o login do produto. Depois de entrar, o
usuário conecta sua própria agenda em **Perfil → Agenda e trabalho**; os tokens ficam
criptografados e vinculados àquela conta interna.

```text
PORTFOLIO_CALENDAR_OAUTH_ENABLED=true
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

No cliente Google, cadastre estas duas URIs autorizadas:

```text
https://<seu-servico>.onrender.com/perfil/calendario/callback/google
```

No serviço atual, substitua `<seu-servico>` por `meetmind-portfolio`.

No cliente Microsoft, cadastre estas duas URIs de redirecionamento Web:

```text
https://<seu-servico>.onrender.com/perfil/calendario/callback/microsoft
```

No Google Cloud, habilite as APIs **Google Calendar** e **Google Tasks** e adicione o
Gmail usado em **Público → Usuários de teste** quando a tela de consentimento estiver em
Testing. No Microsoft Entra, selecione contas de
qualquer organização e contas pessoais caso queira aceitar Outlook/Hotmail, e mantenha o
tenant `common`.

Depois de salvar os segredos e as URIs, faça um novo deploy. Entre com uma conta interna
do MeetMind e conecte o provedor pelo Perfil. Nunca compartilhe a senha de uma conta
enquanto ela estiver vinculada a uma agenda pessoal real.

Notificações por e-mail são opcionais. Para habilitá-las, preencha `SMTP_HOST`,
`SMTP_USERNAME`, `SMTP_PASSWORD` e `SMTP_FROM`; sem isso, as notificações continuam
disponíveis dentro do MeetMind.

Nunca coloque client secrets, senha privada, chave Fernet ou tokens no Git.
