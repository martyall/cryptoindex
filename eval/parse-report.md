# Parser evaluation report (Phase 2)

Scores are the human's, per page, given blind (without seeing Claude's independent proposals) and optionally reconciled afterwards where the two differed. 0 scrambled, 1 partially intact, 2 faithful (EVALUATION.md §1). Formulas: math each parser recognized as math, and how many KaTeX could not render (undelimited equations count as failures). A parser that emits math as plain text has fewer formulas to fail, so read the two columns together.

| Excerpt | Pages scored | Marker score | PaddleOCR-VL score | Marker formulas (failed) | PaddleOCR-VL formulas (failed) |
|---|---|---|---|---|---|
| judson-groups | 10 of 10 | 1.70 | 1.60 | 63 (1) | 706 (25) |
| hefferon-reduction | 10 of 10 | 1.70 | 1.00 | 76 (1) | 131 (16) |
| calculus-integrals | 9 of 10 | 1.67 | 1.44 | 273 (0) | 302 (19) |
| mcs-proofs | 7 of 10 | 1.57 | 0.86 | 56 (0) | 120 (24) |
| erickson-dp | 12 of 12 | 1.33 | 1.50 | 52 (8) | 165 (9) |
| ods-pseudocode | 12 of 12 | 1.25 | 2.00 | 0 (0) | 132 (0) |
| joy-mac-hybrids | 12 of 12 | 0.67 | 1.42 | 28 (0) | 257 (11) |
| gb-rsa-experiments | 12 of 12 | 1.25 | 1.83 | 101 (1) | 600 (24) |
| **all** | 84 of 100 | **1.36** | **1.50** | **689 (13, 1.9%)** | **2854 (142, 5.0%)** |

**Not scored** (the blind pass was closed early, so these were not evaluated): leinster-functors (commutative and labelled-arrow diagrams).

Claude's proposals differed from the blind score on 106 of 168 page scores; reconciliation was skipped, so the blind scores are final.
