# MeetMind — arquitetura corporativa

## Posicionamento

O produto é um assistente auditável de Customer Success e qualidade de reuniões. Ele produz
sinais favoráveis, pontos de atenção e próximos passos sustentados por segmentos literais.
Não é um previsor estatístico de churn e não toma decisões comerciais automaticamente.

## Fronteiras de confiança

```text
Entra ID/OIDC -> Flask/Gunicorn -> PostgreSQL gerenciado
                         |       -> Redis/RQ gerenciado
                         |       -> armazenamento privado GCS + KMS
                         `-> workers por fila -> Vertex AI/Gemini
```

- O navegador nunca recebe credenciais do provedor, prompts internos ou arquivos de terceiros.
- Todo registro de negócio pertence obrigatoriamente a uma `Organization`.
- O filtro organizacional é aplicado centralmente às consultas web e reforçado nos endpoints
  sensíveis. Os testes cobrem listagem e acesso direto entre organizações.
- Apenas os workers recebem credenciais de IA. `AIExecution` registra cada tentativa, modelo,
  hashes, tokens, duração, resultado e falha.
- O arquivo original continua criptografado na aplicação. Em nuvem, `gcs` adiciona bucket
  privado, lifecycle e chave KMS configurada pela organização.

## Produção

O `docker-compose.yml` é somente para desenvolvimento e homologação local. Uma implantação
corporativa deve fornecer:

- PostgreSQL com alta disponibilidade, backup point-in-time e TLS;
- Redis gerenciado com TLS e política de memória compatível com RQ;
- GCS privado com uniform bucket-level access, lifecycle e CMEK;
- containers web e workers independentes por fila, probes `/api/v1/health/live` e
  `/api/v1/health/ready`, autoscaling e rollout gradual;
- secrets manager/workload identity; nenhuma chave persistida em imagem ou Git;
- logs JSON enviados para o observability stack homologado;
- workers separados por fila: `screening`, `analyses`, `notifications` e `maintenance`,
  evitando que processamento de IA bloqueie ingestão, integrações ou retenção.

## Identidade

Use `AUTH_MODE=oidc` com o discovery endpoint do tenant Entra. MFA e políticas condicionais
ficam no provedor de identidade. Grupos corporativos são mapeados por ID para admin, analyst e
reviewer. JIT vem desabilitado por padrão. Login local permanece disponível somente para uma
conta administrativa marcada como break-glass e deve ser monitorado.

## Provedor de IA

- `AI_PROVIDER=gemini_api`: desenvolvimento com `GEMINI_API_KEY` apenas no worker.
- `AI_PROVIDER=vertex_ai`: produção com `VERTEX_PROJECT`, `VERTEX_LOCATION` e Application
  Default Credentials/workload identity.
- A organização pode desligar IA externa sem remover o acesso aos dados já processados.
- Uma versão nova de prompt, schema, modelo ou playbook deve rodar em shadow mode e passar
  pelo avaliador em `tools/evaluate_ai_quality.py` antes da promoção.

## SLO inicial da prova de valor

| Indicador | Meta |
|---|---:|
| Disponibilidade em horário comercial | 99,5% |
| RPO | 15 minutos |
| RTO | 4 horas |
| Evidências apontando segmentos válidos | 100% |
| Próximos passos úteis na adjudicação offline | >= 80% |
| Afirmações factuais sem suporte | < 1% |
| Incidentes críticos ou vazamentos | 0 |

Estas metas são gates da prova de valor, não afirmações de desempenho já comprovado.

O CI começa com cobertura mínima obrigatória de 70% (a medição local atual é superior a
73%) e deve elevar esse piso por etapas, sem reduzir a cobertura existente em novas mudanças.

## Pendências externas de homologação

O código não pode concluir sozinho contratação de cloud, consentimento do tenant Microsoft,
RIPD aprovado, pentest independente, DPA, definição de controlador/operador, política de
suporte, preço ou propriedade intelectual. Esses itens continuam como gates formais antes de
uma venda ou implantação ampla.
