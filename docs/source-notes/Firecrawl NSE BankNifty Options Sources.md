# Firecrawl NSE BankNifty Options Sources

Created: 2026-06-09

## Saved source files

- Bank Nifty Option Strategies Booklet PDF: `/opt/data/trading-library/books/legal_sources/firecrawl/downloaded_public_pdfs/pdf_bank_nifty_option_strategies_booklet_nse.pdf`
- Extracted text: `/opt/data/trading-library/books/legal_sources/firecrawl/extracted_text/pdf_bank_nifty_option_strategies_booklet_nse.md`
- Nifty Bank Index PDF: `/opt/data/trading-library/books/legal_sources/firecrawl/downloaded_public_pdfs/pdf_nifty_bank_index.pdf`
- Extracted text: `/opt/data/trading-library/books/legal_sources/firecrawl/extracted_text/pdf_nifty_bank_index.md`
- Trading Strategies for Indian Markets PDF: `/opt/data/trading-library/books/legal_sources/firecrawl/downloaded_public_pdfs/pdf_trading_strategies_for_indian_markets_nse.pdf`
- Extracted text: `/opt/data/trading-library/books/legal_sources/firecrawl/extracted_text/pdf_trading_strategies_for_indian_markets_nse.md`

## Bot-relevant takeaways

- The BankNifty booklet is mainly a payoff-structure catalog: long call/put, short call/put, spreads, futures+option hedges, straddle/strangle-style combinations.
- For Apoorv's current bot, the immediately usable part is not short-option selling automation; it is the payoff/risk framework for choosing whether a long option, debit spread, hedge, or no trade is appropriate.
- Short-premium structures should remain research-only until margin, gap risk, assignment/exercise behavior, expiry risk, and adjustment rules are modeled.
- The Nifty Bank Index source supports maintaining a constituent-aware BankNifty model, but weights/constituents should still be refreshed from the official CSV/API before trading.
- The NSE trading-strategies source reinforces that strategies must be objective, quantifiable, and verifiable before automation.

## Strategy-card action

Created/updated: [[BankNifty Official Payoff Structure Selector]]
