"""Normalise the scraped MPLADS works, run the anomaly rules, compute composite
risk scores and emit the JSON files consumed by the static dashboard.

Inputs : data/raw_works.json, data/mps.json   (produced by scrape_mplads.py)
Outputs: data/index.json          - states -> MPs (dropdown source) + global stats
         data/alerts.json         - all alerts, risk-sorted
         data/works/<mp_id>.json  - per-MP works with scores, reasoning and alerts
"""

import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WORKS_DIR = DATA / "works"

DELAY_LIMIT_DAYS = 365          # works are expected to finish within a year
Z_CAP = 5.0                     # z-score cap for the spending component
CONCENTRATION_ALERT = 0.5       # >50% of a constituency's works by one agency
MIN_WORKS_FOR_STATS = 5         # need a few works before outlier maths is meaningful
# MPLADS works are executed through implementing district authorities. Where a
# constituency routes everything through one authority the 100% share is
# structural, so concentration is only scored where several agencies are used.
MIN_VENDORS_FOR_CONCENTRATION = 3

TODAY = pd.Timestamp(datetime.now(timezone.utc).date())


def norm_text(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def iso(ts):
    return None if pd.isna(ts) else pd.Timestamp(ts).strftime("%Y-%m-%d")


def load_frame():
    raw_file = DATA / "raw_works.json"
    if raw_file.exists():
        raw = json.loads(raw_file.read_text())
    else:  # fall back to the scraper's per-MP cache (allows partial rebuilds)
        raw = []
        for f in sorted((DATA / "cache").glob("*.json")):
            raw.extend(json.loads(f.read_text()))
    df = pd.DataFrame(raw)
    df["work_id"] = df["workId"].astype(str)
    df["mp_name"] = df["mpName"].fillna("Unknown")
    df["state"] = df["state"].fillna("Unknown")
    df["constituency"] = df["constituency"].fillna("Unknown")
    df["category"] = df.get("workCategory", pd.Series(dtype=object)).fillna("N/A")
    df["description"] = df["workDescription"].fillna("")
    df["agency"] = df["ida"].fillna("UNKNOWN AGENCY")
    df["vendor"] = df["agency"].str.extract(r"\(([^)]*)\)", expand=False).fillna(df["agency"])
    df["recommended_date"] = pd.to_datetime(df.get("recommendationDate"), errors="coerce", utc=True).dt.tz_localize(None)
    df["completed_date"] = pd.to_datetime(df.get("completedDate"), errors="coerce", utc=True).dt.tz_localize(None)
    df["amount"] = pd.to_numeric(
        df.get("finalAmount").fillna(df.get("recommendedAmount")), errors="coerce"
    ).fillna(0.0)
    df["recommended_amount"] = pd.to_numeric(df.get("recommendedAmount"), errors="coerce")
    df["total_paid"] = pd.to_numeric(df.get("totalPaid"), errors="coerce").fillna(0.0)
    df["payment_count"] = pd.to_numeric(df.get("paymentCount"), errors="coerce").fillna(0).astype(int)
    df["status"] = df["status"].fillna("recommended")

    # Elapsed lifetime of a work: recommendation -> completion (or -> today if unfinished).
    end = df["completed_date"].fillna(TODAY)
    df["elapsed_days"] = (end - df["recommended_date"]).dt.days
    df.loc[df["recommended_date"].isna(), "elapsed_days"] = pd.NA
    return df


def add_scores(df):
    # --- delay component -------------------------------------------------
    delay = (df["elapsed_days"] - DELAY_LIMIT_DAYS) / DELAY_LIMIT_DAYS
    df["delay_score"] = delay.clip(lower=0, upper=1.0).astype(float).fillna(0.0)

    # --- spending component: z-score of amount within the constituency ---
    grp = df.groupby("constituency")["amount"]
    df["const_mean"] = grp.transform("mean")
    df["const_std"] = grp.transform("std")
    df["const_works"] = grp.transform("size")
    z = (df["amount"] - df["const_mean"]) / df["const_std"]
    z = z.where(df["const_works"] >= MIN_WORKS_FOR_STATS)
    df["zscore"] = z.replace([float("inf"), float("-inf")], pd.NA)
    df["spending_score"] = (df["zscore"].abs() / Z_CAP).clip(upper=1.0).fillna(0.0)

    # --- vendor component: agency share of works in the constituency -----
    pair = df.groupby(["constituency", "vendor"])["work_id"].transform("size")
    df["vendor_works"] = pair
    df["const_vendors"] = df.groupby("constituency")["vendor"].transform("nunique")
    df["vendor_concentration"] = (pair / df["const_works"]).fillna(0.0)
    df["vendor_scored"] = df["const_vendors"] >= MIN_VENDORS_FOR_CONCENTRATION
    df["vendor_score"] = df["vendor_concentration"].where(df["vendor_scored"], 0.0).clip(upper=1.0)

    df["risk_score"] = (
        0.4 * df["delay_score"] + 0.3 * df["spending_score"] + 0.3 * df["vendor_score"]
    ).round(3)
    df["risk_band"] = pd.cut(
        df["risk_score"], [-0.01, 0.3, 0.7, 1.01], labels=["low", "medium", "high"]
    ).astype(str)
    return df


def duplicate_groups(df):
    """Works of the same MP with the same normalised description + amount."""
    key = df["mp_id"].astype(str) + "|" + df["description"].map(norm_text) + "|" + df["amount"].astype(int).astype(str)
    df["dup_key"] = key
    counts = key.value_counts()
    dup_keys = set(counts[counts > 1].index) - {""}
    df["dup_count"] = df["dup_key"].map(counts).fillna(1).astype(int)
    df["is_duplicate"] = df["dup_key"].isin(dup_keys) & (df["description"].map(norm_text) != "")
    return df


def money(v):
    return f"\u20b9{v/100000:,.2f} L" if v else "\u20b90"


def build_alerts(row):
    """Rule hits for one work, each with a human-readable reason."""
    out = []
    elapsed = row["elapsed_days"]
    if pd.notna(elapsed) and elapsed > DELAY_LIMIT_DAYS:
        if row["status"] == "completed":
            out.append({
                "alert_type": "late_completion",
                "severity": "high",
                "reason": (
                    f"Late completion (recommendation: {iso(row['recommended_date'])}, "
                    f"completion: {iso(row['completed_date'])}, delay: {int(elapsed)} days)"
                ),
            })
        else:
            out.append({
                "alert_type": "stalled_work",
                "severity": "high",
                "reason": (
                    f"Still not completed {int(elapsed)} days after recommendation "
                    f"({iso(row['recommended_date'])}); status: {row['status']}"
                ),
            })
    if pd.notna(row["zscore"]) and abs(row["zscore"]) > 3:
        out.append({
            "alert_type": "unusual_spending",
            "severity": "medium",
            "reason": (
                f"Spending outlier (Z-score {row['zscore']:.2f}; amount {money(row['amount'])} vs "
                f"{row['constituency']} average {money(row['const_mean'])})"
            ),
        })
    if row["vendor_scored"] and row["vendor_concentration"] > CONCENTRATION_ALERT \
            and row["const_works"] >= MIN_WORKS_FOR_STATS:
        out.append({
            "alert_type": "vendor_concentration",
            "severity": "medium",
            "reason": (
                f"Vendor concentration: {row['vendor']} executes {int(row['vendor_works'])}/"
                f"{int(row['const_works'])} works ({row['vendor_concentration']*100:.1f}%) in "
                f"{row['constituency']}, which uses {int(row['const_vendors'])} implementing agencies"
            ),
        })
    if row["is_duplicate"]:
        out.append({
            "alert_type": "duplicate_work",
            "severity": "high",
            "reason": (
                f"Duplicate entry: {int(row['dup_count'])} works with an identical description and "
                f"amount ({money(row['amount'])}) under the same MP"
            ),
        })
    if row["status"] != "completed" and pd.notna(row["recommended_amount"]) and row["recommended_amount"] > 0 \
            and row["total_paid"] > row["recommended_amount"] * 1.01:
        out.append({
            "alert_type": "overpayment",
            "severity": "high",
            "reason": (
                f"Paid {money(row['total_paid'])} against a recommended {money(row['recommended_amount'])} "
                f"({row['total_paid']/row['recommended_amount']*100:.0f}% of sanctioned) while still incomplete"
            ),
        })
    return out


def reasoning(row):
    """Explains every term of risk_score = 0.4*delay + 0.3*spending + 0.3*vendor."""
    elapsed = row["elapsed_days"]
    if pd.isna(elapsed):
        delay_note = "no recommendation date published for this work -> delay component 0"
    elif elapsed <= DELAY_LIMIT_DAYS:
        delay_note = f"{int(elapsed)} days elapsed, within the 365-day norm -> 0"
    else:
        delay_note = (
            f"min(({int(elapsed)} - 365) / 365, 1) = {row['delay_score']:.3f} "
            f"({int(elapsed)} days from recommendation to "
            f"{'completion' if row['status'] == 'completed' else 'today, still open'})"
        )
    if pd.isna(row["zscore"]):
        spend_note = f"too few works in {row['constituency']} for a reliable z-score -> 0"
    else:
        spend_note = (
            f"min(|{row['zscore']:.2f}| / 5, 1) = {row['spending_score']:.3f} "
            f"(amount {money(row['amount'])} vs constituency mean {money(row['const_mean'])})"
        )
    if row["vendor_scored"]:
        vendor_note = (
            f"{row['vendor_concentration']*100:.1f}% / 100 = {row['vendor_score']:.3f} "
            f"({int(row['vendor_works'])} of {int(row['const_works'])} works in {row['constituency']} "
            f"go to {row['vendor']}, out of {int(row['const_vendors'])} agencies used there)"
        )
    else:
        vendor_note = (
            f"{row['constituency']} routes works through only {int(row['const_vendors'])} implementing "
            f"agency(ies), so the {row['vendor_concentration']*100:.0f}% share is structural -> 0"
        )
    return {
        "delay": delay_note,
        "spending": spend_note,
        "vendor": vendor_note,
        "formula": (
            f"0.4 x {row['delay_score']:.3f} + 0.3 x {row['spending_score']:.3f} + "
            f"0.3 x {row['vendor_score']:.3f} = {row['risk_score']:.3f} -> {row['risk_band'].upper()}"
        ),
    }


def main():
    df = load_frame()
    print(f"Loaded {len(df)} works for {df['mp_id'].nunique()} MPs")
    df = add_scores(df)
    df = duplicate_groups(df)

    WORKS_DIR.exists() and shutil.rmtree(WORKS_DIR)
    WORKS_DIR.mkdir(parents=True)

    all_alerts = []
    mp_meta = {}
    for mp_id, sub in df.groupby("mp_id"):
        works = []
        for _, row in sub.sort_values("risk_score", ascending=False).iterrows():
            alerts = build_alerts(row)
            work = {
                "work_id": row["work_id"],
                "description": row["description"],
                "category": row["category"],
                "status": row["status"],
                "mp_name": row["mp_name"],
                "state": row["state"],
                "constituency": row["constituency"],
                "vendor": row["vendor"],
                "agency": row["agency"],
                "vendor_concentration": round(float(row["vendor_concentration"]), 3),
                "recommended_date": iso(row["recommended_date"]),
                "completed_date": iso(row["completed_date"]),
                "elapsed_days": None if pd.isna(row["elapsed_days"]) else int(row["elapsed_days"]),
                "amount": float(row["amount"]),
                "total_paid": float(row["total_paid"]),
                "delay_score": round(float(row["delay_score"]), 3),
                "spending_score": round(float(row["spending_score"]), 3),
                "vendor_score": round(float(row["vendor_score"]), 3),
                "risk_score": float(row["risk_score"]),
                "risk_band": row["risk_band"],
                "reasoning": reasoning(row),
                "alerts": alerts,
            }
            works.append(work)
            for a in alerts:
                all_alerts.append({
                    **a,
                    "work_id": work["work_id"],
                    "mp_id": mp_id,
                    "mp_name": work["mp_name"],
                    "state": work["state"],
                    "constituency": work["constituency"],
                    "vendor": work["vendor"],
                    "amount": work["amount"],
                    "risk_score": work["risk_score"],
                    "risk_band": work["risk_band"],
                    "recommended_date": work["recommended_date"],
                    "completed_date": work["completed_date"],
                })
        (WORKS_DIR / f"{mp_id}.json").write_text(json.dumps(works))
        first = sub.iloc[0]
        mp_meta[mp_id] = {
            "mp_id": mp_id,
            "mp_name": first["mp_name"],
            "state": first["state"],
            "constituency": first["constituency"],
            "works": int(len(sub)),
            "completed": int((sub["status"] == "completed").sum()),
            "total_amount": float(sub["amount"].sum()),
            "high_risk": int((sub["risk_band"] == "high").sum()),
            "medium_risk": int((sub["risk_band"] == "medium").sum()),
            "alerts": int(sum(len(w["alerts"]) for w in works)),
        }

    all_alerts.sort(key=lambda a: a["risk_score"], reverse=True)
    (DATA / "alerts.json").write_text(json.dumps(all_alerts[:5000], indent=1))

    states = defaultdict(list)
    for meta in mp_meta.values():
        states[meta["state"]].append(meta)
    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "https://api.empoweredindian.in/api (Empowered Indian MPLADS)",
        "stats": {
            "works": int(len(df)),
            "mps": int(df["mp_id"].nunique()),
            "states": len(states),
            "alerts": len(all_alerts),
            "high_risk": int((df["risk_band"] == "high").sum()),
            "medium_risk": int((df["risk_band"] == "medium").sum()),
            "total_amount": float(df["amount"].sum()),
        },
        "states": {s: sorted(v, key=lambda m: m["mp_name"]) for s, v in sorted(states.items())},
    }
    (DATA / "index.json").write_text(json.dumps(index, indent=1))
    print(json.dumps(index["stats"], indent=1))
    print(f"Alerts: {len(all_alerts)} -> data/alerts.json (top 5000 stored)")


if __name__ == "__main__":
    main()
