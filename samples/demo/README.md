# Sample network (demo)

**Synthetic data only** — for learning and demonstration, not a real programme network.

## What it demonstrates

- **18 health facilities** and **4 testing sites** across two districts (North / South)
- **Long-distance referrals** in the baseline for access and cost review
- **Open Referral redesign** under Access, Utilisation, Cost, and Balanced focus; accepted action counts depend on feasibility and capacity safeguards
- **Administrative partition** — optional boundary optimisation on `admin_area_2`
- Optional **device catalogue** for cost breakdown in reports

## Learning path

1. Follow the **[walkthrough guide](../../docs/walkthrough.md)** using **Use sample network** in the local UI, or the CLI commands below.
2. Review `summary.md` and `actions.csv` in your results folder to see baseline vs optimized comparison and referral recommendations.
3. When ready for programme data, copy this folder to a new location before editing — saving through the UI overwrites these files and creates timestamped `.bak` backups.

## Quick run (CLI)

```bash
dno demo -o results/demo/
```

Or explicitly:

```bash
dno optimize -n samples/demo/network.json -c samples/demo/network.catalogue.json -g referral-network -o results/demo/
```

## Related documentation

- [README](../../README.md) — overview and scope
- [walkthrough.md](../../docs/walkthrough.md) — complete UI and CLI guide
- [data-preparation.md](../../docs/data-preparation.md) — input field reference
