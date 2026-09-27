# Runbooks operacionais

## Fila parada

1. Consulte `GET /api/v1/admin/operations` e o dashboard do Redis.
2. Verifique workers por fila, último heartbeat e `FailedJobRegistry`.
3. Não reenvie jobs antes de confirmar a idempotency key/ID da análise.
4. Recupere somente falhas transitórias; falhas de validação permanecem bloqueadas e exigem um novo envio corrigido.
5. Registre início, impacto, ação e encerramento no incidente.

## Provedor de IA indisponível ou custo anormal

1. Suspenda novas análises automáticas alterando `external_ai_enabled=false` na organização.
2. Preserve solicitações e análises já registradas; não apague evidências.
3. Separe 401/403, 429, timeout e 5xx pelos códigos sanitizados.
4. Compare tokens, latência e quantidade de reparos com a versão anterior.
5. Faça rollback do modelo/prompt ou retome gradualmente em shadow/canary.

## ClamAV indisponível

1. O readiness deve falhar quando antivírus for obrigatório.
2. Nenhum original pode ser aberto, baixado ou enviado à IA enquanto o status não for `clean`.
3. Atualize assinaturas, valide o daemon e repita a triagem sem modificar o arquivo original.

## Banco de dados

1. Interrompa gravações e preserve logs correlacionados.
2. Acione failover gerenciado; não restaure manualmente sobre a instância primária.
3. Se necessário, restaure para uma nova instância usando PITR.
4. Valide organizações, reuniões, segmentos, análises, auditoria e hashes antes de liberar.
5. Meça RPO/RTO e documente qualquer divergência.

## Incidente de dados pessoais

1. Conter acesso e preservar evidências, sem copiar conteúdo para canais de chat.
2. Identificar organizações, titulares, categorias de dados, período e subprocessadores.
3. Acionar encarregado, jurídico e responsáveis contratuais.
4. Avaliar comunicação conforme RIPD, contrato e regulamento vigente da ANPD.
5. Registrar correção, notificação, recuperação e lições aprendidas.
