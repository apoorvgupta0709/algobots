# Trading Knowledge Library Roadmap — 6 Deliverables

Generated: 2026-06-13 13:27 UTC

Status: research/paper-only. This roadmap does **not** approve live trading.

## 1. Better BankNifty/Nifty strategy cards
- Convert source-backed ideas into deterministic cards: setup, entry, stop, target, invalidation, risk, data needs, and source citations.
- Immediate cards: BankNifty constituent-led long options, NSE defined-risk payoff selector, long-options volatility/Greeks gate.
- Promotion gate: source reviewed → deterministic rule → backtest → paper journal → live-review only with explicit approval.

## 2. Bot rules derived from books/sources
- Encode rules as auditable `knowledge.rules` and config/spec checklists.
- Mandatory rules: paper-only, no undefined-risk structures, cost-aware ₹1,500 net trade-loss cap, fresh quotes, index+constituent confirmation.
- Use rules as pre-entry filters before strategy-specific logic.

## 3. Strategy research reports
- Produce source-backed reports for each theme: payoff selection, volatility/Greeks risk, price-action confirmation, position sizing, overfitting guard.
- Reports should cite `knowledge.sources/chunks` and distinguish official NSE sources from general trading books.

## 4. Backtest-ready strategy specs
- Store JSON specs under `backtest_specs/` and DB `research.strategy_versions`.
- Specs are not trading code; they define hypotheses, required data, parameters, costs/slippage, and validation metrics.
- First specs: BankNifty constituent-led long options, Nifty Tuesday expiry defined-risk, long-options volatility/Greeks gate.

## 5. Obsidian notes / research vault
- Keep source notes, strategy cards, bot rules, and roadmap notes in the Obsidian vault.
- Each card includes a processing checklist: reviewed, hypothesis, backtest, paper journal, promotion decision.

## 6. Vector DB improvements
- Ingest official/public NSE Markdown extracts into pgvector.
- Fix bad metadata (Natenberg, Mark Douglas, Al Brooks range book).
- Tag non-trading/supporting PDFs instead of deleting raw data.
- Populate concepts/rules/playbooks and improve BankNifty/NSE search quality.

## Current implementation summary
```json
{
  "ingest": {
    "processed": [
      {
        "title": "NSE Bank Nifty Option Strategies Booklet",
        "chunks": 4
      },
      {
        "title": "NSE Trading Strategies for Indian Markets",
        "chunks": 5
      },
      {
        "title": "NSE Nifty Bank Index Factsheet",
        "chunks": 1
      }
    ],
    "skipped": [],
    "failed": []
  },
  "metadata": {
    "metadata_fixed": 3,
    "tagged_non_trading": 8
  },
  "research_objects": {
    "concepts": 10,
    "rules": 7,
    "playbooks": 3,
    "hypotheses": 3,
    "strategy_versions": 3,
    "promoted_existing_playbooks": 6
  },
  "verification": {
    "sources": 26,
    "chunks": 3023,
    "embedded_chunks": 3023,
    "missing_embeddings": 0,
    "concepts": 10,
    "reviewed_rules": 7,
    "reviewed_playbooks": 9,
    "nse_sources": [
      {
        "title": "NSE Bank Nifty Option Strategies Booklet",
        "chunks": 4
      },
      {
        "title": "NSE Nifty Bank Index Factsheet",
        "chunks": 1
      },
      {
        "title": "NSE Trading Strategies for Indian Markets",
        "chunks": 5
      }
    ],
    "sample_banknifty_hits": [
      {
        "title": "NSE Bank Nifty Option Strategies Booklet",
        "page_start": 1,
        "excerpt": "# Pdf Bank Nifty Option Strategies Booklet Nse Source PDF: `/opt/data/trading-library/books/legal_sources/firecrawl/downloaded_public_pdfs/pdf_bank_nifty_option_strategies_booklet_nse.pdf` BankNifty Options Strategies Ba"
      },
      {
        "title": "NSE Trading Strategies for Indian Markets",
        "page_start": 11,
        "excerpt": "to the number of trades completed each day and is an important measure of strength and interest in a particular trade. Open interest reflects the number of contracts that are held by traders and investors in active posit"
      }
    ]
  }
}
```
