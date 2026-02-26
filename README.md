# Agente de Resumo Incremental para WhatsApp Web (Arquitetura barata)

Implementação funcional para uso imediato com 3 fontes de coleta:

- `mock` (demo)
- `json` (produção simples)
- `whatsapp-web` (experimental com Playwright)

## O que já funciona

1. Normalização e remoção de ruído de sistema.
2. Persistência SQLite com checkpoint incremental por `last_message_ts`.
3. Micro-resumo + estado incremental (`decisions`, `pending`, `risks`, `current_status`).
4. CLI com saída em arquivo e inspeção de estado.

## Estrutura

- `src/agent/collector.py`: `MessageCollector`, `MockCollector`, `JsonFileCollector`, `PlaywrightWhatsAppCollector`
- `src/agent/normalizer.py`: normalização e filtro de ruído
- `src/agent/db.py`: SQLite (mensagens, estado, resumos)
- `src/agent/summarizer.py`: resumidor incremental heurístico
- `src/agent/pipeline.py`: orquestração
- `src/main.py`: CLI
- `data/sample_messages.json`: exemplo pronto

## Como usar agora

### 1) Modo JSON (recomendado para começar)

```bash
python -m src.main --source json --source-json data/sample_messages.json --group "Projeto X" --db agent.db --show-state --output out/resumo.md
```

Rode novamente com o mesmo `--db`: ele só processa mensagens novas.

### 2) Modo WhatsApp Web (experimental)

Instale dependências:

```bash
python -m pip install playwright
python -m playwright install chromium
```

Primeira execução (escaneie QR manualmente):

```bash
python -m src.main --source whatsapp-web --group "Projeto X" --db agent.db --wa-profile-dir .wa_profile --show-state
```

Observações:
- O WhatsApp Web muda seletores/DOM com frequência.
- Este coletor é um ponto de partida e pode exigir ajustes.
- Use `--wa-headless` se quiser rodar sem janela.

### 3) Modo mock (teste rápido)

```bash
python -m src.main --source mock --group "Projeto X" --db agent.db --show-state
```

## Formato de entrada JSON

```json
{
  "Projeto X": [
    {
      "author": "Ana",
      "timestamp": "2026-01-05T09:00:00",
      "text": "Decisão: manter deploy em janela noturna.",
      "external_id": "px-1"
    }
  ]
}
```

## Testes

```bash
python -m unittest discover -s tests -p 'test_*.py'
```
