# RIPD técnico — MeetMind Portfólio

Versão 1.0 — 26 de setembro de 2026
Status: concluído para o escopo sintético de portfólio. Deve ser refeito e aprovado pelo
controlador e pelo encarregado antes de qualquer tratamento de reuniões reais.

## 1. Identificação e escopo

- **Operação avaliada:** demonstração pública do fluxo reunião → evidência → plano → ação.
- **Controlador no portfólio:** responsável pela publicação do MeetMind, limitado a contas,
  sessão, logs técnicos e segurança da demonstração.
- **Operadores:** fornecedores de infraestrutura efetivamente configurados no deploy.
- **TOTVS:** não controla nem opera a demonstração e não é apresentada como parceira.
- **Titulares:** visitantes autenticados na demonstração.
- **Canal:** endereço configurado em `PRIVACY_CONTACT_EMAIL` e publicado na Política de
  Privacidade.
- **Dados de negócio:** exclusivamente sintéticos, identificados por `metadata.demo=true` e
  `data_classification=synthetic_demo`.

## 2. Finalidade e hipótese de tratamento

A finalidade é demonstrar competências técnicas do projeto a recrutadores e avaliadores.
Não há monitoramento de clientes, treinamento de modelos, tomada de decisão comercial ou
prestação de serviço pela demonstração. Dados técnicos mínimos de acesso são tratados para
segurança, prevenção de abuso e diagnóstico. A hipótese legal concreta deve ser confirmada
pelo responsável pelo deploy segundo sua operação e jurisdição; o projeto não presume
consentimento como resposta universal.

## 3. Inventário e fluxo

| Artefato | Origem | Conteúdo | Destino | Regra no portfólio |
|---|---|---|---|---|
| Sessão | Navegador | Identificador e papel da conta demo | Aplicação/Redis | Cookie HttpOnly, SameSite e expiração da sessão |
| Log de segurança | Requisição | horário, rota, IP mascarado e hashes | Banco/logs | finalidade exclusiva de proteção e diagnóstico |
| Reuniões demo | Seed controlado | nomes, clientes e falas fictícias | PostgreSQL | reconstruíveis e sem origem real |
| Análises demo | Seed controlado | resultado sintético com evidências | PostgreSQL | provedor `demo`, zero tokens e sem chamada externa |
| Upload | Visitante | — | — | bloqueado na organização pública |
| Calendário/avatares | Visitante | — | — | OAuth e alterações bloqueados na organização pública |

Não há transferência internacional de transcrições no portfólio porque a IA externa e as
integrações pessoais estão desativadas. A localização dos logs e da infraestrutura deve ser
declarada na configuração do deploy.

## 4. Necessidade e proporcionalidade

- O conteúdo integral de reuniões reais não é necessário para a finalidade de portfólio.
- Dados sintéticos demonstram as mesmas telas e contratos sem expor pessoas ou clientes.
- A credencial pública não é versionada; o comando gera senha temporária ou lê segredo do
  ambiente.
- Não existe usuário administrador na organização pública.
- Alterações comerciais, decisões sobre pessoas, previsão de churn e envio automático de
  mensagens são usos proibidos nesta demonstração.

## 5. Direitos dos titulares

A Política de Privacidade e os Termos são públicos, inclusive antes do login. O contato
configurado recebe pedidos de acesso, correção ou exclusão relacionados ao visitante. Como
as reuniões são sintéticas, não há titulares reais dentro das transcrições do portfólio.

## 6. Riscos, controles e risco residual

| Risco | Prob. | Impacto | Controle atual | Residual | Responsável |
|---|---:|---:|---|---:|---|
| Visitante enviar dado real | Baixa | Alto | upload bloqueado no tenant demo | Baixo | mantenedor |
| Conta pública alterar a vitrine | Baixa | Médio | sem admin, senha não publicada, mutações sensíveis bloqueadas e reset transacional | Baixo | mantenedor |
| Conectar calendário pessoal | Baixa | Alto | início, callback, sincronização e desconexão OAuth bloqueados | Baixo | mantenedor |
| Acesso entre organizações | Baixa | Alto | `organization_id`, política central, guard de ORM e testes de isolamento | Baixo | mantenedor |
| Vazamento de segredo | Baixa | Alto | `.env` fora do Git, segredo gerado e validação de produção | Médio | mantenedor |
| Prompt injection | Baixa | Médio | sem IA externa no demo; parser e validação permanecem no produto | Baixo | mantenedor |
| Inferência incorreta da IA | Baixa | Médio | resultados sintéticos identificados; nenhuma decisão real | Baixo | mantenedor |
| Confusão com produto oficial | Média | Alto | disclaimer no login, Termos, README e rodapé | Médio | mantenedor |
| Logs técnicos excessivos | Média | Médio | IP mascarado, hashes e acesso restrito | Médio | mantenedor |
| Incidente de infraestrutura | Baixa | Alto | headers, CSRF, rate limit, criptografia e runbooks | Médio | mantenedor/provedor |

## 7. Controles verificados

- dados sintéticos e IA externa desativada na organização demo;
- CSRF, CSP, cookies protegidos e limites de requisição;
- isolamento por organização e papel;
- arquivos e OAuth bloqueados no ambiente público;
- política e termos públicos;
- comando de reset limitado a `meetmind-demo` e proibido em produção;
- dependências auditadas sem vulnerabilidades conhecidas na revisão de 26/09/2026.

## 8. Riscos aceitos e revisão

O risco residual aceito para o portfólio limita-se a dados técnicos de visitantes e possível
confusão de marca, mitigada pelos avisos. A publicação deve remover logotipos de terceiros se
não houver autorização escrita. Este RIPD deve ser revisto após mudança de hospedagem,
autenticação, telemetria ou abertura de qualquer integração, e no máximo em 12 meses.

## 9. Condição para mudança de escopo

Receber uma reunião real invalida este RIPD. Antes disso, é obrigatório produzir nova versão
com controlador, operador, encarregado, base legal, participantes, retenção por artefato,
suboperadores, regiões, transferência internacional, DPA, resposta a incidentes e aprovação
formal. Consulte `docs/DATA_GOVERNANCE.md` para os gates.
