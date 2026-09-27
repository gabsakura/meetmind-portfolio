# Pesquisa de campo — Manufatura

Este protocolo transforma entrevistas autorizadas em insumos de avaliação. Ele não altera prompts automaticamente e não usa região para explicar comportamento, risco ou negociação.

## Objetivo e amostra

Realizar de 12 a 15 entrevistas:

- 4 profissionais de Customer Success;
- 3 profissionais de suporte ou implantação;
- 5 a 8 usuários ou gestores de clientes industriais, com autorização;
- pelo menos 3 subsetores industriais e 3 regiões, exclusivamente para diversidade e auditoria de cobertura.

Não registrar nomes no dataset. Notas brutas e gravações ficam em armazenamento privado com retenção definida; nunca no Git.

## Cronograma de quatro semanas

1. Semana 1 — recrutamento, consentimento, roteiro e baseline das análises atuais.
2. Semana 2 — entrevistas, anonimização das notas e codificação independente.
3. Semana 3 — revisão da taxonomia por especialistas e preparação do dataset ouro.
4. Semana 4 — avaliação shadow entre `sector-playbooks-v1` e `manufatura-v2`, análise de regressões e decisão explícita de promoção ou rollback.

## Roteiro semiestruturado

1. Qual era a finalidade da última reunião operacional desse tipo?
2. Quais termos a equipe usa para planejamento, materiais, execução, qualidade, manutenção e custos?
3. Quais problemas aparecem com frequência e quais parecem semelhantes, mas têm causas diferentes?
4. Que evidências são necessárias antes de declarar impacto ou prioridade?
5. Que compromissos, responsáveis e critérios de sucesso tornam um próximo passo útil?
6. Quais recomendações genéricas ou falsos positivos reduzem a confiança na análise?
7. Que informação ausente deveria fazer a IA declarar incerteza em vez de concluir?
8. Há produto ou módulo explicitamente confirmado? Se não houver, nenhuma marca deve ser atribuída.

## Tratamento e taxonomia

- Remover nomes, e-mails, telefones, documentos, IDs externos e segredos antes da curadoria.
- Marcar cada achado como evidência, interpretação, contexto cadastral, hipótese setorial ou incerteza.
- Associar exemplos apenas a IDs pseudônimos e segmentos permitidos.
- Exigir revisão por duas pessoas para todo caso candidato a ground truth.
- Divergências seguem para adjudicação administrativa; nunca para promoção automática.
- Alterações propostas ao playbook precisam de fonte, limitação de inferência e teste de regressão.

## Métricas de decisão

- 100% das evidências referenciam segmentos válidos;
- zero fatos sustentados apenas pelo playbook;
- no máximo três achados e três ações;
- pelo menos 80% de aceitação dos próximos passos;
- melhoria mensurável da v2 sobre a v1 no mesmo conjunto;
- nenhuma regressão de segurança, privacidade ou concisão.

UF e região podem aparecer apenas em agregados de qualidade com cinco ou mais respostas. Não são exibidos rankings regionais e nenhum resultado regional representa causalidade.
