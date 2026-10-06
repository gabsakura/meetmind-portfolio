# Deploy do portfólio no Render

O `render.yaml` cria o serviço web e o PostgreSQL. O processo de entrada aplica as
migrações e garante o seed sintético a cada inicialização.

## Login individual e calendários

As contas públicas sintéticas continuam somente leitura. Cada visitante que quiser usar
as funções pessoais entra com a própria conta Google ou Microsoft; o MeetMind cria um
perfil individual e armazena somente os tokens criptografados desse usuário.

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
https://<seu-servico>.onrender.com/auth/oauth/callback/google
https://<seu-servico>.onrender.com/perfil/calendario/callback/google
```

No serviço atual, substitua `<seu-servico>` por `meetmind-portfolio`.

No cliente Microsoft, cadastre estas duas URIs de redirecionamento Web:

```text
https://<seu-servico>.onrender.com/auth/oauth/callback/microsoft
https://<seu-servico>.onrender.com/perfil/calendario/callback/microsoft
```

No Google Cloud, habilite as APIs **Google Calendar** e **Google Tasks** e adicione o
Gmail usado em **Público → Usuários de teste** quando a tela de consentimento estiver em
Testing. No Microsoft Entra, selecione contas de
qualquer organização e contas pessoais caso queira aceitar Outlook/Hotmail, e mantenha o
tenant `common`.

Depois de salvar os segredos e as URIs, faça um novo deploy. A tela de login exibirá
**Entrar com Google Calendar** e/ou **Entrar com Microsoft 365**. O primeiro consentimento
já autentica a pessoa e conecta sua agenda. Um administrador pode promover uma conta
individual a manager e vinculá-la a uma equipe; o próximo login preservará esse cargo.

Notificações por e-mail são opcionais. Para habilitá-las, preencha `SMTP_HOST`,
`SMTP_USERNAME`, `SMTP_PASSWORD` e `SMTP_FROM`; sem isso, as notificações continuam
disponíveis dentro do MeetMind.

Nunca coloque client secrets, senha privada, chave Fernet ou tokens no Git.
