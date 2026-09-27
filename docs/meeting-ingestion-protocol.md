# Protocolo de ingestão de reuniões

Versão: `meetmind-transcript-v1`

## Objetivo

Transformar cada arquivo recebido em uma representação única, previsível e segura antes de criar registros em `meetings` e `segments`. O arquivo original é sempre considerado não confiável e nunca é executado.

## Fluxo obrigatório

1. Exigir autorização de processamento no próprio envio.
2. Aceitar somente `VTT`, `SRT`, `TXT`, `CSV`, `JSON`, `XLSX` ou `SQL` dentro do limite configurado.
3. Executar antivírus. Em produção, indisponibilidade do antivírus bloqueia o envio.
4. Interpretar o formato com limites de linhas, colunas, profundidade, expansão ZIP e tamanho normalizado.
5. Nunca executar SQL. O parser aceita apenas `INSERT ... VALUES` com valores literais.
6. Sanitizar Unicode e rejeitar controles ocultos, conteúdo ativo, fórmulas, PII e prompt injection.
7. Produzir segmentos ordenados com `speaker`, `text`, `started_at_ms` e `ended_at_ms`.
8. Validar texto obrigatório, limites por segmento, quantidade total e ordem dos timestamps.
9. Calcular SHA-256 do conteúdo canônico e deduplicar dentro da organização.
10. Gravar `Meeting` e `Segment` em transação pelo SQLAlchemy, usando parâmetros; nunca concatenar entrada em SQL.
11. Apagar o original criptografado após conclusão, deduplicação ou bloqueio.
12. Registrar protocolo, hashes, contagens e resultado no log de auditoria.

## Política fail-closed

Qualquer falha de integridade, parser, antivírus obrigatório ou regra de segurança impede a criação da reunião. Em bloqueios de conteúdo, o original é destruído e permanecem apenas metadados mínimos, hashes, códigos de erro e auditoria.

Falhas transitórias de infraestrutura usam o estado `failed`; não criam `Meeting` nem `Segment` e podem ser reprocessadas dentro da retenção configurada.

## Contrato canônico

O documento exportado segue `docs/schemas/meetmind-transcript-v1.schema.json`. A IA recebe somente essa representação canônica, delimitada como conteúdo não confiável, e nunca o upload bruto.

O protocolo é versionado. Qualquer mudança incompatível exige uma nova versão, sem alterar retroativamente reuniões já importadas.
