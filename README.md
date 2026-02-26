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
5. Resumo opcional por LLM (`ollama` ou `openai`) com fallback automático.

## Estrutura

- `src/agent/collector.py`: `MessageCollector`, `MockCollector`, `JsonFileCollector`, `PlaywrightWhatsAppCollector`
- `src/agent/llm_summarizer.py`: integração com Ollama/OpenAI-compatible
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

### 3) Conectar LLM (grupo real)

#### Ollama local (mais barato)

Suba o Ollama com um modelo local (ex.: `qwen2.5:7b`) e rode:

```bash
python -m src.main --source whatsapp-web --group "Projeto X" --db agent.db --wa-profile-dir .wa_profile --llm-provider ollama --llm-model qwen2.5:7b --show-state --output out/resumo.md
```

Opcional: mudar URL do Ollama:

```bash
python -m src.main ... --llm-provider ollama --llm-model qwen2.5:7b --ollama-url http://localhost:11434
```

#### OpenAI-compatible

```bash
export OPENAI_API_KEY="sua_chave"
python -m src.main --source whatsapp-web --group "Projeto X" --db agent.db --wa-profile-dir .wa_profile --llm-provider openai --llm-model gpt-4o-mini --show-state --output out/resumo.md
```

Se o LLM falhar por rede/chave/configuração, o sistema usa fallback heurístico automaticamente.

### 4) Modo mock (teste rápido)

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

## Troubleshooting rápido

Se aparecer `bash: syntax error near unexpected token '('`, normalmente foi copiado algum texto extra junto com o comando (por exemplo comentários com parênteses).

Use **somente** esta linha:

```bash
python -m src.main --source json --source-json data/sample_messages.json --group "Projeto X" --db agent.db --show-state --output out/resumo.md
```

Dicas:
- Não copie o símbolo `$` do prompt.
- Não inclua observações entre parênteses na mesma linha do comando.
- Se preferir, teste primeiro sem espaço no nome do grupo: `--group ProjetoX`.
