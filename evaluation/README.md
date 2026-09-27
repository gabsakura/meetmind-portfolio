# Avaliação de liberação dos modos Gemini

O bundle não deve virar padrão somente porque o código e o schema passaram nos testes. Antes
da troca, copie `dataset.template.jsonl` para um arquivo privado e preencha entre 30 e 50
reuniões reais, estratificadas. Não inclua transcrições sensíveis no repositório.

Cada linha JSON representa uma reunião, seus rótulos humanos e as saídas dos dois braços da
comparação cega. Cubra reclamação TOTVS e de terceiros, palavra negativa isolada, dúvidas com
follow-up, negociação com e sem próximo passo, “te vejo amanhã” explícito e ambíguo, ausência
de reunião posterior, diferentes sentidos de servidor/replication/Progress User, instabilidade
sem causa, prompt injection e transcrições longas ou fragmentadas.

Depois da dupla revisão humana, execute:

```powershell
python tools/evaluate_modes.py caminho\para\dataset-rotulado.jsonl
```

O comando falha se houver menos de 30 ou mais de 50 casos, schema ou citação inválida, causa ou
produto não sustentado, precisão de reclamações abaixo de 90%, macro-F1 de roteamento abaixo de
0,80 ou concordância humana abaixo de 80% para tipo e risco. `blind_preference` registra a
comparação entre política atual e bundle novo sem revelar o braço ao avaliador.

Esse arquivo é deliberadamente um protocolo, não um conjunto de rótulos inventados. O ground
truth precisa ser produzido por analistas que tenham lido as reuniões reais.
