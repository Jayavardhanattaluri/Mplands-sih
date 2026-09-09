# MPLADS Sentinel

Anomaly detection over **real MPLADS works data** (no synthetic rows). The pipeline pulls
every MP and every work record from the public Empowered Indian API
(`https://api.empoweredindian.in/api`, the backend behind <https://empoweredindian.in/mplads>),
applies rule-based anomaly detection, computes a composite risk score per work, and serves a
static terminal-style dashboard.

```
scripts/scrape_mplads.py   -> data/cache/<mp_id>.json, data/raw_works.json, data/mps.json
scripts/build_dataset.py   -> data/index.json, data/alerts.json, data/works/<mp_id>.json
index.html                 -> dashboard (state dropdown -> MP dropdown -> project -> alert log)
```

## Run

```bash
pip install requests pandas
python3 scripts/scrape_mplads.py     # resumable; respects the API's 1000 req / 600 s limit
python3 scripts/build_dataset.py
python3 -m http.server 8000          # open http://localhost:8000
```

`scrape_mplads.py` caches one JSON file per MP under `data/cache/`, so an interrupted run
resumes where it stopped. `build_dataset.py` works off that cache even if the scrape is partial.

## Risk score

```
risk_score = 0.4 * delay_score + 0.3 * spending_score + 0.3 * vendor_score

delay_score    = clamp((elapsed_days - 365) / 365, 0, 1)
                 elapsed_days = recommendation -> completion, or recommendation -> today
                 for works that are still open
spending_score = min(|z| / 5, 1)   z = amount vs the constituency's mean/std (>= 5 works)
vendor_score   = works_by_this_agency_in_constituency / works_in_constituency
                 scored only where the constituency uses >= 3 implementing agencies
                 (a single-agency district is structural, not a red flag)

bands: high >= 0.7, medium >= 0.3, low < 0.3
```

Every work carries a `reasoning` object explaining each component and the composite arithmetic;
the dashboard prints it in the terminal log next to the alerts.

## Rules

| rule | severity | fires when |
| --- | --- | --- |
| `late_completion` | high | completed more than 365 days after recommendation |
| `stalled_work` | high | still not completed more than 365 days after recommendation |
| `unusual_spending` | medium | \|z-score\| > 3 against the constituency's amount distribution |
| `vendor_concentration` | medium | one implementing agency holds > 50% of a multi-agency constituency |
| `duplicate_work` | high | same MP, identical normalised description and identical amount |
| `overpayment` | high | payments exceed the sanctioned amount while the work is still open |

## Data limitations (real, worth stating in the pitch)

- The API publishes a recommendation date only for works that are **not yet completed**;
  completed works expose the completion date alone. `late_completion` therefore fires only
  where both dates exist, and the delay signal is carried by `stalled_work` for open works.
- There is no separate sanctioned cost vs final cost field, so cost-overrun detection is
  not possible; `unusual_spending` is a peer-comparison outlier test instead.
- "Vendor" is the implementing district authority (IDA) published with the work, not the
  contractor that was paid.
