# Assistente de autópsia de Manufatura

Versão controlada: `meetmind-autopsy-manufacturing-v1`.

O assistente executa uma segunda leitura somente em reuniões cujo playbook é
`manufatura`. A árvore organiza perguntas de revisão; ela não prevê churn,
venda, contrato, saúde da relação ou qualidade da reunião.

## Modos

- `off`: não executa a segunda leitura.
- `shadow`: mantém a análise original como resultado público e armazena a
  comparação em `analysis_autopsy_runs`.
- `active`: publica o candidato assistido, preservando o resultado inicial e o
  caminho percorrido no registro de autópsia.

Configure `AUTOPSY_ASSISTANT_MODE`; o padrão local é `shadow`. A versão e o
SHA-256 do bundle participam do hash do perfil de análise, invalidando caches
de versões anteriores.

## Garantias

1. A transcrição é a única fonte factual.
2. Citações e IDs são conferidos literalmente pelo backend após cada etapa.
3. Conclusões afirmativas sem evidência válida são rebaixadas para `unclear`.
4. Frequências históricas e probabilidades das folhas não são enviadas ao
   Gemini.
5. Falha no segundo estágio não altera nem bloqueia a análise original.
6. A comparação é visível apenas para administradores e analistas.

## Acompanhamento

As execuções permanecem registradas em `analysis_autopsy_runs` e podem ser
consultadas pela API restrita de comparação e pelos relatórios operacionais.
A antiga central de revisão e publicação de datasets foi removida do produto;
o feedback simples de utilidade e precisão continua disponível no resultado.

Qualquer promoção para `active` deve ser decidida fora do fluxo operacional,
com base em relatórios versionados, validação literal e ausência de regressão.
O fallback da análise original deve permanecer preservado.

O bundle executável está em
`app/ai/autopsy/manufacturing-v1.json`; a migração correspondente é
`b8c9d0e1f2a3`.
