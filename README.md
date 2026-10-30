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

0. Criação do projeto (estrutura e arquivos iniciais). Concluída.
1. Banco de dados e testes. Concluída (`app/database/db.py`).
2. Importador e normalização de URL. Concluída (`app/importer`, `app/collectors/common.py`, `app/profile.py`).
3. Deduplicação e alertas. Concluída (`app/database/jobs.py`).
4. Trava de execução. Concluída (`app/lock.py`).
5. Scorer e localização. Concluída (`app/matching/scorer.py`).
6. Coletores, `main` e relatório. Concluída (`app/collectors`, `app/main.py`, `app/report.py`).

## Uso (Windows 11)

```
pip install -r requirements.txt
python -m pytest
python -m app.main init-db
python -m app.main import caminho\painel_export.json
python -m app.main run                 # coleta Remotive + Greenhouse, pontua e mostra o relatório
python -m app.main run --source none   # só pontua e mostra o relatório
python -m app.main alerts              # alertas de duplicidade em aberto
python -m app.main alerts --ack 3      # marca o alerta 3 como revisado (não apaga)
```

O banco fica em `data/jobhunter.db`. Preencha `data/profile.json` com dados reais
(ou importe do painel) e cadastre empresas reais em `data/greenhouse_boards.json`.

## Limitações conhecidas

- Nota máxima efetiva 90: a comparação de formação ainda não existe (0 de 10).
- Vagas "Worldwide"/remotas sem país vão para "Verificar localização" (unknown).
  Menções a LATAM não contam como Brasil.
- A Remotive limita a 4 consultas por dia; o contador é persistente.
- A recuperação de trava velha tem uma janela de corrida mínima; se a trava mudar
  durante a recuperação, o arquivo é preservado e a execução aborta.

## Privacidade

`jobhunter.db`, `data/profile.json`, exportações do painel, documentos pessoais
e logs são ignorados pelo Git (veja `.gitignore`).
