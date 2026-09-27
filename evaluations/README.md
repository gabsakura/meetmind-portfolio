# Avaliação corporativa da IA

`gold.sample.jsonl` valida apenas o contrato do avaliador e usa dados sintéticos. Ele não
constitui comprovação de precisão.

O dataset real deve ser mantido fora do Git quando contiver reuniões restritas, possuir até
20 reuniões por setor disponível, ao menos 20 sem setor e revisão independente por duas
pessoas. Divergências precisam de adjudicação registrada.

Cada linha contém o resultado esperado, o resultado da versão candidata e a revisão humana.
Para executar o gate:

```powershell
python tools\evaluate_ai_quality.py caminho\dataset-avaliado.jsonl --enforce-gates
```

O processo falha quando evidências são inválidas, macro-F1 fica abaixo de 0,85, concordância
de saúde da relação fica abaixo de 0,75, utilidade fica abaixo de 80%, afirmações sem suporte
chegam a 1%, risco depende apenas de metadados ou ocorre vazamento conhecido de PII.
