# Governança de dados do MeetMind

Versão 1.0 — 26 de setembro de 2026

Este documento define os papéis para o ambiente atual de portfólio e para uma eventual
implantação corporativa. A classificação depende das decisões efetivamente tomadas em cada
operação; contratos não devem contradizer a operação real.

## Ambiente público de portfólio

| Papel | Responsável | Escopo |
|---|---|---|
| Controlador | Responsável pela publicação do MeetMind | Contas de acesso, sessão, logs técnicos e segurança da demonstração |
| Operador de infraestrutura | Provedor de hospedagem contratado pelo responsável | Hospedagem da aplicação, banco, cache e logs conforme configuração publicada |
| Titulares | Visitantes autenticados | Dados técnicos mínimos relacionados ao próprio acesso |
| TOTVS | Nenhum papel de tratamento neste ambiente | A empresa não opera, patrocina ou endossa a demonstração pública |

A demonstração utiliza apenas dados sintéticos. Upload de reunião, OAuth de calendário,
alteração de avatar, conta administrativa e IA externa são bloqueados na organização
`meetmind-demo`. O canal público para titulares é definido por `PRIVACY_CONTACT_EMAIL` e é
obrigatório na configuração de produção.

## Eventual implantação corporativa

Antes de receber dados reais, os papéis deverão ser confirmados por contrato. O desenho
esperado, sujeito à análise jurídica da implantação, é:

| Papel | Responsável esperado | Obrigações principais |
|---|---|---|
| Controlador | Organização cliente que determina finalidade, participantes e uso das reuniões | Base legal, transparência, instruções, direitos dos titulares, retenção e decisões de negócio |
| Operador | Entidade que operar o MeetMind por instrução do cliente | Segurança, confidencialidade, registro do tratamento e auxílio ao controlador |
| Suboperadores | Hospedagem, banco, armazenamento, e-mail, observabilidade e provedores de IA aprovados | Tratar somente conforme contrato e controles do operador/controlador |
| Encarregado | Indicado pelo controlador quando aplicável | Canal com titulares e ANPD |

Google, Microsoft e o provedor de IA só podem ser habilitados após inventário dos dados,
DPA/contrato, avaliação de transferência internacional quando aplicável, escopos mínimos e
aprovação do controlador. O envio de uma reunião pelo usuário não substitui essas decisões.

## Matriz de responsabilidade

- O controlador autoriza a finalidade e define quem pode enviar reuniões.
- O operador aplica isolamento, criptografia, retenção e auditoria conforme instruções.
- O CS revisa ações e comunicações; a IA não decide churn, venda, sanção ou desempenho.
- O controlador atende pedidos de titulares; o operador fornece busca, exportação e exclusão
  compatíveis com obrigações de preservação.
- Incidentes são registrados e comunicados conforme o plano contratual e a legislação.

## Gates para dados reais

1. Contrato e DPA assinados, com subprocessadores identificados.
2. Base legal, finalidade, aviso aos participantes e mecanismo de autorização documentados.
3. RIPD aprovado pelo controlador e pelo encarregado quando aplicável.
4. Retenção definida para original, transcrição, segmentos, análises, logs e backups.
5. Canal de titulares e plano de incidentes testados.
6. Autorização de marca, propriedade intelectual e uso do dataset confirmada por escrito.

Enquanto qualquer gate estiver pendente, o ambiente público deve permanecer sintético.
