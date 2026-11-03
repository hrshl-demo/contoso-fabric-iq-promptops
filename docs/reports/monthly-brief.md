# Monthly Business Brief

A six-page PDF for the head of retail banking, built offline from the gold layer by the prompt `reports.monthly-brief` ([prompts/reports/monthly-brief.md](../../prompts/reports/monthly-brief.md)). It uses no Fabric or Azure connection, so it costs nothing to build and CI produces it on every pull request (artifact `monthly-business-brief`).

| Page | Shows |
|---|---|
| 1 Business units | Portfolio by unit, a scorecard (customers, NIM, growth, target attained, health) and growth against target |
| 2 Revenue | All-unit revenue with a straight-line trend, a 3-month forecast with a 95% band and unusual months marked, then the same for each unit |
| 3 Portfolio, customers and service | Portfolio growth index, new customers, service cases opened and resolved, change by unit |
| 4 Campaigns | Reach, engaged and converted; conversion counted two ways; every campaign in a table |
| 5 Regions, risk and fraud | Deposits and advances by region, digital adoption against satisfaction, open risk alerts, fraud attempts and block rates |
| 6 Method | Data, definitions, forecast and outlier method and their limits, how to repeat it |

Each page opens with a sentence or two of plain English computed from the data. The footer names the prompt and its version, so a PDF always says which prompt produced it.

## Definitions

- Money is INR crore (1 crore = 10,000,000 rupees), a reporting aggregate and not exact paise.
- **Campaign conversion** follows the semantic model's measure `Campaign Conversion`: converted divided by reach. The source column `conversion_pct` divides converted by engaged and gives larger numbers; page 4 shows both and labels them.
- The forecast is an ordinary least squares line over 12 months with an approximate 95% prediction band. It has no seasonality and is an illustration of direction, not a plan.
- A month is marked unusual when its modified z-score (median and MAD of the residuals) exceeds 3.5.

## Run it

```powershell
cd kit
python -m contoso_onto.cli build --repo ../seed --tier full --out ../out
cd ..
python -m pip install -r analysis/requirements.txt
python -m analysis.monthly_brief.build_brief --gold out/gold --out out/reports/monthly-brief.pdf
```

The same data gives the same PDF (a test checks it byte for byte). The PDF itself is not committed; CI builds it and a release attaches it.
