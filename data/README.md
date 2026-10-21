# Data

Real inputs are public SEC EDGAR filings, downloaded 2026-10-10. `manifest.csv` lists every file with its source URL.

## Borrowers

| Ticker | Company | Covenants (tested quarterly) | Latest reported (10-Q, Q2 2026) | Role in demo |
| --- | --- | --- | --- | --- |
| TRS | TriMas | Max total net leverage 4.00x; min interest coverage 3.00x | 1.77x; 21.21x | Clean pass |
| CARS | Cars.com | Max senior secured net leverage 3.5x; min interest coverage 3.0x | 0.08x; 7.20x | Clean control (simplest package) |
| ROAD | Construction Partners | Max net leverage 4.75x stepping down to 4.00x by Jun 2028; min interest coverage 2.75x | 3.17x; 5.37x | Clean, tests step-downs |
| WEST | Westrock Coffee | Max secured net leverage 4.00x (relief ended 6/30/26); min interest coverage 1.75x to 2.00x | 3.36x vs 4.00x | Near-miss |
| MEC | Mayville Engineering | Max total leverage 5.25x stepping to 3.50x; min interest coverage 2.75x | 2.91x; 3.27x | Amendment / relief case |

Ratios are as the companies reported them; verify against the filings before using them as the answer key.

## Notes
- The newest amendment for each borrower is usually a conformed copy of the full agreement: start there for covenant definitions.
- ROAD has separate term loan A/revolver and term loan B agreements; only the former is assumed to carry maintenance covenants (unverified).
- Backups not downloaded: Quad/Graphics (QUAD), Stoneridge (SRI, going-concern language).

## Still to create (synthetic, label as such)
- [ ] Compliance certificates per borrower and quarter
- [ ] Borrower email replies for the tickler
- [ ] Bank addback policy
- [ ] Answer key: hand-calculated results per borrower and quarter
