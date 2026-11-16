# register-ocr (prototype, not yet a component)

Domain-aware reading of handwritten municipal registers. The first form is **P.F.-1, the Provident Fund Ledger**. It
uses what is known about the form (the printed grid, the columns, the ledger's arithmetic) to read it, check it and
suggest corrections.

1. **Grid** (`grid.py`): finds the ruled lines locally, so it copes with pages that bend in a bound book. Vertical lines
   are followed down the page; row lines are taken from the column with the most lines and carried across the others.
2. **Cells**: each cell is read on its own with the PP-OCRv5 recogniser, both upright and turned 180°. Empty cells are
   skipped. The label column is the one where month names appear. A block is placed from its fixed shape (Opening
   Balance, April–March, TOTAL, Balance on 31st), even when most labels are misread.
3. **Candidates**: each amount is read as digits only, with OCR letter→digit fixes. Domain priors are added at a lower
   weight: the neighbouring months' amounts, and the column's usual amount. A clerk's notes (a date, "GPF adv") are
   recognised as notes, not amounts.
4. **Rules**:
   - opening + monthly deposits = TOTAL;
   - each other column adds up to its TOTAL;
   - deposits + contributions + interest − withdrawals = closing (±1);
   - closing on the TOTAL line = Balance on 31st (the same number, written twice).

   The solver keeps the readings where they add up. Otherwise it changes as few cells as possible: first to another
   candidate, then restoring dropped digits. A TOTAL is repaired only when it is the column's sum with digits lost
   (often written past the border). When the closing balance is written twice and both copies agree, it is used to
   work back to the one column that does not add up.
5. **Outcomes per cell**:
   - **correction**: a confident suggestion;
   - **review**: a value inferred only from a total, or an unreadable cell;
   - **ledger arithmetic?**: the sum needs a clearly written amount changed, so the clerk's sum is more likely wrong
     than the OCR.

Runs inside the `paddleocr` runtime with no network: `python3 pf1.py <ledger.pdf> <out.json>`. Ledgers hold personal
pay data, so they stay on in-house runtimes (ADR-0134 privacy rule).

Next: other forms (G-2, G-3, G-14) as form definitions (data, not code), a review screen, and confirmed cells kept as
training data.
