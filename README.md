# JobHunter

Agente pessoal, local e por linha de comando, para buscar e organizar vagas de
tecnologia (estágio e desenvolvedor júnior) no Brasil e em vagas remotas
internacionais.

**Fase 1: somente busca, organização, classificação e acompanhamento.**
Nenhuma candidatura é enviada e nenhum formulário é preenchido automaticamente.
Toda decisão final é humana.

## Princípios

- Integridade dos dados acima da continuidade da execução.
- Na dúvida (candidatura, duplicidade, trava), interromper e pedir revisão.
- Nunca inventar dados pessoais, formação, experiência ou habilidades.
- Nunca sobrescrever dados manuais nem apagar histórico de candidaturas e alertas.

## Tecnologias

- Windows 11, Python, SQLite (`sqlite3` puro), execução local.
- Dependência de execução: `requests`. Opcional: `psutil`. Testes: `pytest`.
- Fora do escopo: frameworks web, ORM, LLM, Telegram, Playwright (Fase 1),
  serviços pagos.

## Estrutura

```
app/
  main.py            CLI (etapa 6)
  config.py          configuração
  database/db.py     esquema, conexão, transações (etapa 1)
  importer/          importação do JSON do painel e normalização de URL (etapa 2)
  collectors/        Remotive e Greenhouse (etapa 6)
  matching/scorer.py pontuação e localização (etapa 5)
data/
  profile.json            perfil (vazio até ser preenchido com dados reais)
  greenhouse_boards.json  empresas Greenhouse (não configurado)
logs/
tests/
```

Os arquivos de código listados acima são criados nas etapas indicadas; na Etapa 0
existem apenas os pacotes (`__init__.py`) e os arquivos de dados e configuração.

## Etapas

0. Criação do projeto (estrutura e arquivos iniciais).
1. Banco de dados e testes.
2. Importador e normalização de URL.
3. Deduplicação e alertas.
4. Trava de execução.
5. Scorer e localização.
6. Coletores, `main` e relatório.

Cada etapa só começa após revisão e autorização.

## Privacidade

`jobhunter.db`, `data/profile.json`, exportações do painel, documentos pessoais
e logs são ignorados pelo Git (veja `.gitignore`).
