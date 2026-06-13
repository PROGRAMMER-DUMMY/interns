"""Hostile_Synthetic workspace generator (logistics domain).

Deterministic, seeded, stdlib-only. Regenerates every CSV under
``workspaces/Hostile_Synthetic/datasets/`` from scratch.

Run:
    python workspaces/Hostile_Synthetic/generator/generate.py

Hostility goals (see GROUND_TRUTH.md in this folder):
- Colliding column names (Id, Name, Date, START, END, Amount, Status) with
  different meanings per table.
- Three plausible fact tables with similar row counts (shipments, movements,
  invoices) where only one is the correct base per KPI.
- Dimension join keys that are NOT name-matched
  (shipments.cust_ref -> party.party_key, carrier_cd -> carriers.scac,
  orig/dest -> locs.loc_nbr, svc -> svc_catalog.svc_id).
- No column anywhere supports "premium upgrade after dispatch" (KPI 5's
  false presupposition).
"""

from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 20260612
RNG = random.Random(SEED)

HERE = Path(__file__).resolve().parent
DATASETS = HERE.parent / "datasets"

DATE_LO = date(2025, 1, 1)
DATE_HI = date(2026, 5, 31)
DAY_SPAN = (DATE_HI - DATE_LO).days

N_PARTIES = 240
N_SHIPMENTS = 4200

written: list[tuple[str, int]] = []


def w(rel: str, header: list[str], rows: list[list]) -> None:
    path = DATASETS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    written.append((rel, len(rows)))


def rnd_date(lo: date = DATE_LO, hi: date = DATE_HI) -> date:
    return lo + timedelta(days=RNG.randint(0, (hi - lo).days))


def ts(d: date, hour_lo: int = 6, hour_hi: int = 21) -> str:
    return datetime(d.year, d.month, d.day, RNG.randint(hour_lo, hour_hi), RNG.randint(0, 59)).strftime(
        "%Y-%m-%d %H:%M"
    )


def money(lo: float, hi: float) -> str:
    return f"{RNG.uniform(lo, hi):.2f}"


FIRST = ["Alfa", "Bravo", "Crest", "Delta", "Ember", "Forge", "Granite", "Harbor", "Iron", "Juno",
         "Keystone", "Lumen", "Mistral", "North", "Orchard", "Pinnacle", "Quartz", "Rampart", "Solstice",
         "Tundra", "Umbra", "Vector", "Wexford", "Yarrow", "Zenith"]
SECOND = ["Retail", "Foods", "Components", "Pharma-Free", "Textiles", "Motors", "Beverages", "Paper",
          "Electrics", "Outdoors", "Packaging", "Chemicals", "Builders", "Garden", "Marine", "Print"]
SUFFIX = ["Ltd", "PLC", "Group", "& Co", "Holdings", "Trading"]


def company_name() -> str:
    return f"{RNG.choice(FIRST)} {RNG.choice(SECOND)} {RNG.choice(SUFFIX)}"


PERSON_FIRST = ["Aisha", "Boris", "Carmen", "Dev", "Elena", "Femi", "Greta", "Hamid", "Ines", "Jonas",
                "Katya", "Lars", "Mei", "Nadia", "Omar", "Priya", "Quinn", "Rosa", "Stefan", "Tomas"]
PERSON_LAST = ["Adler", "Brennan", "Costa", "Dvorak", "Eriksen", "Fuentes", "Gallo", "Hansen", "Iqbal",
               "Jensen", "Kovacs", "Lindgren", "Marek", "Novak", "Okafor", "Petrov", "Quist", "Riva",
               "Sorensen", "Tanaka"]


def person_name() -> str:
    return f"{RNG.choice(PERSON_FIRST)} {RNG.choice(PERSON_LAST)}"


# ---------------------------------------------------------------- reference
REGIONS = [("R1", "North"), ("R2", "Midlands"), ("R3", "South East"), ("R4", "South West"),
           ("R5", "Scotland"), ("R6", "Wales")]
ZONES = [(f"Z{i:02d}", f"Zone {i:02d}", REGIONS[i % len(REGIONS)][0]) for i in range(1, 13)]
COUNTRIES = [("GB", "United Kingdom"), ("IE", "Ireland"), ("FR", "France"), ("NL", "Netherlands"),
             ("DE", "Germany"), ("BE", "Belgium")]
CURRENCIES = [("GBP", "Pound Sterling"), ("EUR", "Euro"), ("USD", "US Dollar")]

SVC_CATALOG = [
    # svc_id, Name, sla_days, premium_flag
    ("SVC1", "Express Next-Day", 1, "Y"),
    ("SVC2", "Priority 48", 2, "Y"),
    ("SVC3", "Standard Ground", 4, "N"),
    ("SVC4", "Economy Deferred", 7, "N"),
    ("SVC5", "Pallet Freight", 5, "N"),
    ("SVC6", "Chilled Reefer", 3, "N"),
]
SVC_IDS = [s[0] for s in SVC_CATALOG]
SVC_SLA = {s[0]: s[2] for s in SVC_CATALOG}

CARRIERS = [
    # scac, Name, mode, Status, Amount = insurance coverage limit (collision trap)
    ("ARRW", "Arrowline Haulage", "ROAD", "ACTIVE", "2000000.00"),
    ("BLTC", "Baltic Crossways", "ROAD", "ACTIVE", "1500000.00"),
    ("CDRX", "Cedar Express", "ROAD", "ACTIVE", "1000000.00"),
    ("DNUB", "Danube Freight", "RAIL", "ACTIVE", "5000000.00"),
    ("EGRT", "Egret Couriers", "ROAD", "ACTIVE", "750000.00"),
    ("FNCH", "Finch Parcel Net", "ROAD", "ACTIVE", "500000.00"),
    ("GRYP", "Gryphon Air Cargo", "AIR", "ACTIVE", "8000000.00"),
    ("HLCY", "Halcyon Logistics", "ROAD", "SUSPENDED", "1200000.00"),
]
SCACS = [c[0] for c in CARRIERS if c[3] == "ACTIVE"]
# claim-rate hostility: some carriers damage more than others
CARRIER_CLAIM_RATE = {"ARRW": 0.03, "BLTC": 0.05, "CDRX": 0.11, "DNUB": 0.02,
                      "EGRT": 0.08, "FNCH": 0.06, "GRYP": 0.01}

LOC_KINDS = ["DEPOT", "HUB", "PORT", "RAILHEAD", "CUSTOMER_DOCK"]


def build_locs() -> list[list]:
    rows = []
    for i in range(1, 61):
        kind = LOC_KINDS[i % len(LOC_KINDS)]
        zone = ZONES[i % len(ZONES)][0]
        # START/END here are opening/closing HOURS -- deliberate collision trap
        open_h = RNG.choice([5, 6, 7, 8])
        close_h = RNG.choice([18, 20, 22, 23])
        rows.append([f"L-{200 + i}", f"{RNG.choice(FIRST)} {kind.title()} {i}", kind, zone,
                     f"{open_h:02d}:00", f"{close_h:02d}:00",
                     "OPERATIONAL" if RNG.random() > 0.06 else "MOTHBALLED"])
    return rows


LOCS = build_locs()
LOC_IDS = [r[0] for r in LOCS if r[6] == "OPERATIONAL"]
DEPOT_IDS = [r[0] for r in LOCS if r[2] in ("DEPOT", "HUB") and r[6] == "OPERATIONAL"]


def build_party() -> list[list]:
    rows = []
    for i in range(1, N_PARTIES + 1):
        onboarded = rnd_date(date(2022, 1, 1), date(2026, 2, 1))
        # Status values deliberately contradict the data dictionary (A/C/S, not ACTIVE/CLOSED)
        status = RNG.choices(["A", "C", "S"], weights=[78, 14, 8])[0]
        rows.append([
            f"P-{1000 + i}",                       # party_key (real join key)
            f"CRM{RNG.randint(40000, 99999)}",     # Id = legacy CRM id (trap)
            company_name(),
            status,
            onboarded.isoformat(),                  # Date = onboarding date
            RNG.choice(["SME", "ENTERPRISE", "PUBLIC", "RESELLER"]),
            RNG.choice(REGIONS)[0],
        ])
    return rows


PARTY = build_party()
PARTY_KEYS = [r[0] for r in PARTY]
ACTIVE_PARTY_KEYS = [r[0] for r in PARTY if r[3] == "A"]


def build_shipments() -> list[list]:
    """FACT A: one row per shipment. Amount = QUOTED charge (not revenue)."""
    rows = []
    for i in range(1, N_SHIPMENTS + 1):
        sid = f"SH{100000 + i}"
        booked = rnd_date(DATE_LO, DATE_HI - timedelta(days=14))
        svc = RNG.choices(SVC_IDS, weights=[14, 18, 30, 18, 12, 8])[0]
        carrier = RNG.choice(SCACS)
        orig, dest = RNG.sample(LOC_IDS, 2)
        pickup = booked + timedelta(days=RNG.randint(0, 3))
        sla = SVC_SLA[svc]
        status = RNG.choices(["DELIVERED", "IN_TRANSIT", "BOOKED", "CANCELLED"],
                             weights=[82, 8, 5, 5])[0]
        if status == "DELIVERED":
            # ~82% on time vs pickup + sla_days
            late = RNG.random() > 0.82
            slip = RNG.randint(1, 5) if late else 0
            delivered = pickup + timedelta(days=max(0, sla + slip - (0 if late else RNG.randint(0, max(0, sla - 1)))))
            end = ts(delivered)
            start = ts(pickup)
        elif status in ("IN_TRANSIT",):
            start, end = ts(pickup), ""
        else:
            start, end = "", ""
        uom = RNG.choices(["KG", "LB"], weights=[70, 30])[0]   # dictionary claims kg-only
        wgt = round(RNG.uniform(2, 1400), 1)
        quote = round(18 + wgt * (0.42 if uom == "KG" else 0.19) * (1.6 if svc in ("SVC1", "SVC2") else 1.0)
                      + RNG.uniform(-5, 25), 2)
        rows.append([sid, booked.isoformat(), RNG.choice(PARTY_KEYS), svc, carrier, orig, dest,
                     start, end, wgt, uom, RNG.randint(1, 26), f"{max(quote, 9.5):.2f}",
                     RNG.choices(["GBP", "EUR"], weights=[85, 15])[0], status])
    return rows


SHIPMENTS = build_shipments()


def build_movements() -> list[list]:
    """FACT B: leg-level moves. START/END = leg depart/arrive. Amount = internal leg cost."""
    rows = []
    mid = 700000
    for s in SHIPMENTS:
        sid, status, orig, dest, start = s[0], s[14], s[5], s[6], s[7]
        if status in ("BOOKED", "CANCELLED") or not start:
            continue
        legs = RNG.choices([1, 2, 3], weights=[78, 17, 5])[0]
        via = RNG.sample(DEPOT_IDS, k=max(0, legs - 1))
        stops = [orig] + via + [dest]
        leg_day = datetime.strptime(start, "%Y-%m-%d %H:%M").date()
        for seq in range(1, legs + 1):
            mid += 1
            depart = ts(leg_day)
            arrive = ts(leg_day + timedelta(days=RNG.choice([0, 0, 1])))
            leg_day = leg_day + timedelta(days=RNG.choice([0, 1, 1]))
            done = status == "DELIVERED" or seq < legs
            rows.append([f"MV{mid}", sid, seq, stops[seq - 1], stops[seq],
                         f"VIN{RNG.randint(100, 219)}", f"D{RNG.randint(3000, 3119)}",
                         depart, arrive if done else "", money(28, 410),
                         "DONE" if done else RNG.choice(["PLANNED", "ABORTED"])])
    return rows


MOVEMENTS = build_movements()


def build_invoices() -> list[list]:
    """FACT C: invoice LINES. Amount = billed revenue (true revenue source)."""
    rows = []
    line = 900000
    inv = 50000
    for s in SHIPMENTS:
        sid, booked, cust, status, quote, ccy = s[0], s[1], s[2], s[14], float(s[12]), s[13]
        if status != "DELIVERED":
            continue
        inv += 1
        inv_no = f"INV-{inv}"
        issued = (date.fromisoformat(booked) + timedelta(days=RNG.randint(2, 18))).isoformat()
        billed = round(quote * RNG.uniform(0.88, 1.27), 2)   # diverges from shipments.Amount
        pay_status = RNG.choices(["PAID", "OPEN", "VOID", "CREDITED"], weights=[68, 22, 4, 6])[0]
        line += 1
        rows.append([f"IL{line}", inv_no, issued, cust, sid, "BASE", f"{billed:.2f}", ccy,
                     f"{billed * 0.2:.2f}", pay_status])
        if RNG.random() < 0.34:
            line += 1
            fuel = round(billed * RNG.uniform(0.06, 0.14), 2)
            rows.append([f"IL{line}", inv_no, issued, cust, sid, "FUEL", f"{fuel:.2f}", ccy,
                         f"{fuel * 0.2:.2f}", pay_status])
        if RNG.random() < 0.08:
            line += 1
            acc = round(RNG.uniform(8, 60), 2)
            rows.append([f"IL{line}", inv_no, issued, cust, sid, "ACCESSORIAL", f"{acc:.2f}", ccy,
                         f"{acc * 0.2:.2f}", pay_status])
    return rows


INVOICES = build_invoices()
INV_NOS = sorted({r[1] for r in INVOICES})
DELIVERED = [s for s in SHIPMENTS if s[14] == "DELIVERED"]


def main() -> None:
    # ------------------------------------------------ small reference tables
    w("reference/regions.csv", ["region_cd", "Name"], [list(r) for r in REGIONS])
    w("reference/zones.csv", ["zone_cd", "Name", "region_cd"], [list(z) for z in ZONES])
    w("reference/countries.csv", ["country_cd", "Name"], [list(c) for c in COUNTRIES])
    w("reference/currencies.csv", ["ccy", "Name"], [list(c) for c in CURRENCIES])
    w("reference/fx_rates.csv", ["Date", "ccy", "rate_to_gbp"],
      [[(DATE_LO + timedelta(days=7 * k)).isoformat(), ccy, f"{RNG.uniform(*rng):.4f}"]
       for k in range(74) for ccy, rng in (("EUR", (0.82, 0.90)), ("USD", (0.74, 0.83)))])
    w("reference/holidays.csv", ["Date", "Name", "country_cd"],
      [["2025-01-01", "New Year", "GB"], ["2025-04-18", "Good Friday", "GB"],
       ["2025-05-05", "May Day", "GB"], ["2025-12-25", "Christmas", "GB"],
       ["2025-12-26", "Boxing Day", "GB"], ["2026-01-01", "New Year", "GB"],
       ["2026-04-03", "Good Friday", "GB"], ["2025-03-17", "St Patrick", "IE"]])
    w("reference/exception_codes.csv", ["exc_cd", "Name", "severity"],
      [["X01", "Address query", "LOW"], ["X02", "Weather hold", "MED"], ["X03", "Vehicle breakdown", "HIGH"],
       ["X04", "Customs hold", "MED"], ["X05", "Refused at door", "HIGH"], ["X06", "Missed linehaul", "MED"],
       ["X07", "Damaged in handling", "HIGH"], ["X08", "Label unreadable", "LOW"]])
    w("reference/return_reasons.csv", ["reason_cd", "Name"],
      [["RR1", "Wrong item shipped"], ["RR2", "Damaged on arrival"], ["RR3", "Order cancelled"],
       ["RR4", "Refused delivery"], ["RR5", "Duplicate shipment"]])
    w("reference/surcharge_types.csv", ["sur_cd", "Name", "calc_method"],
      [["S-FUEL", "Fuel surcharge", "PCT_OF_BASE"], ["S-RES", "Residential delivery", "FLAT"],
       ["S-LIFT", "Tail-lift", "FLAT"], ["S-WAIT", "Waiting time", "PER_15MIN"],
       ["S-REWT", "Reweigh correction", "DELTA"], ["S-ADR", "Hazardous handling", "PCT_OF_BASE"]])
    w("reference/packaging_types.csv", ["pkg_cd", "Name", "tare_kg"],
      [["PKG1", "Carton", "0.4"], ["PKG2", "Euro pallet", "22.0"], ["PKG3", "Half pallet", "11.0"],
       ["PKG4", "Drum", "8.5"], ["PKG5", "Crate", "15.0"], ["PKG6", "Roll cage", "28.0"]])
    w("reference/commodity_codes.csv", ["comm_cd", "Name", "hazmat_flag"],
      [[f"C{i:03d}", n, h] for i, (n, h) in enumerate(
          [("Dry food", "N"), ("Chilled food", "N"), ("Electronics", "N"), ("Paints", "Y"),
           ("Aerosols", "Y"), ("Apparel", "N"), ("Auto parts", "N"), ("Batteries", "Y"),
           ("Furniture", "N"), ("Print media", "N")], start=1)])
    w("reference/vehicle_classes.csv", ["class_cd", "Name", "capacity_kg"],
      [["VC1", "Small van", "900"], ["VC2", "Luton van", "1800"], ["VC3", "7.5t rigid", "2800"],
       ["VC4", "18t rigid", "9500"], ["VC5", "Artic 44t", "26000"]])
    w("reference/shifts.csv", ["shift_id", "Name", "START", "END"],   # START/END = times of day
      [["SH1", "Early", "06:00", "14:00"], ["SH2", "Late", "14:00", "22:00"],
       ["SH3", "Night trunk", "22:00", "06:00"], ["SH4", "Weekend", "08:00", "18:00"]])

    # ------------------------------------------------ master dimensions
    w("masters/party.csv", ["party_key", "Id", "Name", "Status", "Date", "segment", "region_cd"], PARTY)
    # stale legacy duplicate of party -- source-of-truth ambiguity
    legacy = [[f"CU{2000 + i}", r[2], "ACTIVE" if r[3] == "A" else "CLOSED",
               r[4]] for i, r in enumerate(PARTY[: int(N_PARTIES * 0.7)], start=1)]
    w("masters/customers_legacy.csv", ["cust_id", "customer_name", "status", "created"], legacy)
    w("masters/carriers.csv", ["scac", "Name", "mode", "Status", "Amount"], [list(c) for c in CARRIERS])
    w("masters/locs.csv", ["loc_nbr", "Name", "kind", "zone_cd", "START", "END", "Status"], LOCS)
    w("masters/svc_catalog.csv", ["svc_id", "Name", "sla_days", "premium_flag"],
      [list(s) for s in SVC_CATALOG])
    w("masters/lanes.csv", ["lane_id", "orig", "dest", "distance_km"],
      [[f"LN{i:04d}", *RNG.sample(LOC_IDS, 2), RNG.randint(12, 940)] for i in range(1, 161)])
    w("masters/vehicles.csv", ["vin", "reg_no", "class_cd", "depot", "Status"],
      [[f"VIN{i}", f"{RNG.choice('ABCDEFGHKLMNOPRSTUVWXY')}{RNG.choice('ABCDEFGHKLMNOPRSTUVWXY')}"
        f"{RNG.randint(10, 75)} {RNG.choice('ABCDEFGHKLMNOPRSTUVWXY')}{RNG.choice('ABCDEFGHKLMNOPRSTUVWXY')}"
        f"{RNG.choice('ABCDEFGHKLMNOPRSTUVWXY')}", RNG.choice(["VC1", "VC2", "VC3", "VC4", "VC5"]),
        RNG.choice(DEPOT_IDS), RNG.choices(["IN_SERVICE", "VOR", "DISPOSED"], weights=[84, 10, 6])[0]]
       for i in range(100, 220)])
    w("masters/drivers.csv", ["drv_id", "Name", "depot", "licence_class", "Status"],
      [[f"D{i}", person_name(), RNG.choice(DEPOT_IDS), RNG.choice(["B", "C1", "C", "CE"]),
        RNG.choices(["ACTIVE", "LEFT", "BANNED"], weights=[86, 12, 2])[0]] for i in range(3000, 3120)])
    w("masters/employees.csv", ["emp_id", "Name", "role", "depot", "START", "END"],  # employment dates
      [[f"E{i}", person_name(), RNG.choice(["LOADER", "PLANNER", "SUPERVISOR", "CS_AGENT", "FITTER"]),
        RNG.choice(DEPOT_IDS), rnd_date(date(2018, 1, 1), date(2025, 10, 1)).isoformat(),
        rnd_date(date(2025, 11, 1), DATE_HI).isoformat() if RNG.random() < 0.15 else ""]
       for i in range(500, 640)])
    w("masters/vendors.csv", ["vend_id", "Name", "category", "Status"],
      [[f"V{i:03d}", company_name(), RNG.choice(["TYRES", "FUEL", "IT", "AGENCY", "PARTS", "FACILITIES"]),
        RNG.choice(["APPROVED", "TRIAL", "BLOCKED"])] for i in range(1, 41)])
    w("masters/dock_doors.csv", ["door_id", "loc_nbr", "door_type"],
      [[f"DD{i:03d}", RNG.choice(DEPOT_IDS), RNG.choice(["LEVEL", "RAISED", "RAMP"])]
       for i in range(1, 91)])
    w("masters/wh_bins.csv", ["bin_id", "loc_nbr", "bin_capacity_m3"],
      [[f"B{i:04d}", RNG.choice(DEPOT_IDS), f"{RNG.uniform(0.5, 6.0):.1f}"] for i in range(1, 201)])

    # ------------------------------------------------ contracts / pricing
    w("pricing/tariffs.csv", ["tariff_id", "svc_id", "zone_from", "zone_to", "rate_per_kg", "min_charge", "effective"],
      [[f"T{i:04d}", RNG.choice(SVC_IDS), RNG.choice(ZONES)[0], RNG.choice(ZONES)[0],
        f"{RNG.uniform(0.1, 0.9):.3f}", money(8, 30), rnd_date(date(2024, 1, 1), date(2026, 1, 1)).isoformat()]
       for i in range(1, 181)])
    w("pricing/rate_cards.csv", ["rc_id", "acct", "svc_id", "discount_pct", "START", "END"],  # validity window
      [[f"RC{i:04d}", RNG.choice(PARTY_KEYS), RNG.choice(SVC_IDS), f"{RNG.uniform(0, 22):.1f}",
        (d := rnd_date(date(2024, 6, 1), date(2025, 12, 1))).isoformat(),
        (d + timedelta(days=RNG.choice([180, 365, 730]))).isoformat()] for i in range(1, 131)])
    w("pricing/contracts.csv", ["contract_no", "acct", "START", "END", "Status", "Amount"],  # Amount = committed annual value
      [[f"CTR{i:04d}", RNG.choice(PARTY_KEYS), (d := rnd_date(date(2024, 1, 1), date(2025, 9, 1))).isoformat(),
        (d + timedelta(days=365)).isoformat(), RNG.choice(["LIVE", "EXPIRED", "DRAFT"]), money(5000, 250000)]
       for i in range(1, 96)])
    w("pricing/quotes.csv", ["Id", "cust_ref", "Date", "lane_id", "Amount", "accepted_flag"],  # quoted, mostly unconverted
      [[f"Q{80000 + i}", RNG.choice(PARTY_KEYS), rnd_date().isoformat(), f"LN{RNG.randint(1, 160):04d}",
        money(15, 900), RNG.choice(["Y", "N", "N"])] for i in range(1, 701)])
    w("pricing/fuel_prices.csv", ["Date", "pence_per_litre"],
      [[(DATE_LO + timedelta(days=7 * k)).isoformat(), f"{RNG.uniform(138, 169):.1f}"] for k in range(74)])

    # ------------------------------------------------ the three facts
    w("operations/shipments.csv",
      ["Id", "Date", "cust_ref", "svc", "carrier_cd", "orig", "dest", "START", "END",
       "wgt", "wgt_uom", "pieces", "Amount", "ccy", "Status"], SHIPMENTS)
    w("operations/movements.csv",
      ["Id", "ship_ref", "seq", "from_loc", "to_loc", "veh", "drv", "START", "END", "Amount", "Status"],
      MOVEMENTS)
    w("finance/invoices.csv",
      ["Id", "inv_no", "Date", "acct", "ship_no", "charge_type", "Amount", "ccy", "tax", "Status"],
      INVOICES)

    # ------------------------------------------------ finance satellites
    paid_invs = sorted({r[1] for r in INVOICES if r[9] == "PAID"})
    w("finance/payments.csv", ["Id", "inv_no", "Date", "Amount", "method"],
      [[f"PMT{60000 + i}", inv, rnd_date().isoformat(), money(20, 1500),
        RNG.choice(["BACS", "CARD", "DD", "CHEQUE"])] for i, inv in enumerate(paid_invs, start=1)])
    w("finance/credit_notes.csv", ["Id", "inv_no", "Date", "Amount", "reason_cd"],
      [[f"CN{7000 + i}", RNG.choice(INV_NOS), rnd_date().isoformat(), money(5, 320),
        RNG.choice(["RR1", "RR2", "RR3", "RR4", "RR5"])] for i in range(1, 181)])
    w("finance/disputes.csv", ["Id", "inv_no", "opened", "Status", "reason_cd", "Amount"],  # Amount = disputed amt
      [[f"DSP{4000 + i}", RNG.choice(INV_NOS), rnd_date().isoformat(),
        RNG.choices(["OPEN", "RESOLVED", "REJECTED"], weights=[30, 55, 15])[0],
        RNG.choice(["RR1", "RR2", "RR5", "X07"]), money(10, 600)] for i in range(1, 261)])
    w("finance/settlements.csv", ["Id", "carrier_cd", "period", "Amount", "Status"],  # carrier self-bill
      [[f"STL{i:04d}", RNG.choice(SCACS), f"2025-{m:02d}", money(8000, 90000),
        RNG.choice(["APPROVED", "QUERIED"])] for i, m in enumerate(
          [m for m in range(1, 13) for _ in SCACS], start=1)])
    w("finance/purchase_orders.csv", ["po_no", "vend_id", "Date", "Amount", "Status"],
      [[f"PO{30000 + i}", f"V{RNG.randint(1, 40):03d}", rnd_date().isoformat(), money(50, 9000),
        RNG.choice(["OPEN", "RECEIPTED", "CANCELLED"])] for i in range(1, 221)])
    w("finance/accessorial_charges.csv", ["Id", "ship_ref", "sur_cd", "Amount"],
      [[f"AC{9000 + i}", RNG.choice(DELIVERED)[0], RNG.choice(["S-RES", "S-LIFT", "S-WAIT", "S-REWT", "S-ADR"]),
        money(4, 85)] for i in range(1, 341)])

    # ------------------------------------------------ shipment events
    claims = []
    cid = 5000
    for s in DELIVERED:
        if RNG.random() < CARRIER_CLAIM_RATE.get(s[4], 0.05):
            cid += 1
            claimed = round(RNG.uniform(20, 2400), 2)
            st = RNG.choices(["FILED", "SETTLED", "DENIED"], weights=[25, 55, 20])[0]
            claims.append([f"CLM{cid}", s[0], rnd_date().isoformat(),
                           RNG.choices(["DAMAGE", "LOSS", "DELAY"], weights=[62, 18, 20])[0],
                           f"{claimed:.2f}", st,
                           f"{claimed * RNG.uniform(0.3, 0.95):.2f}" if st == "SETTLED" else ""])
    w("operations/cargo_claims.csv",
      ["Id", "ship_ref", "Date", "claim_type", "Amount", "Status", "settled_amount"], claims)
    w("operations/exceptions.csv", ["Id", "ship_ref", "Date", "exc_cd", "note"],
      [[f"EX{20000 + i}", RNG.choice(SHIPMENTS)[0], rnd_date().isoformat(),
        f"X{RNG.randint(1, 8):02d}", RNG.choice(["", "see CS ticket", "driver note", "auto-flag"])]
       for i in range(1, 521)])
    w("operations/pod_scans.csv", ["Id", "ship_ref", "scan_ts", "loc", "signee"],
      [[f"POD{40000 + i}", s[0], s[8], s[6], person_name()] for i, s in enumerate(DELIVERED, start=1)])
    w("operations/pickup_requests.csv",
      ["Id", "cust_ref", "req_date", "window_start", "window_end", "Status"],
      [[f"PU{50000 + i}", RNG.choice(PARTY_KEYS), rnd_date().isoformat(),
        f"{RNG.randint(7, 11):02d}:00", f"{RNG.randint(13, 18):02d}:00",
        RNG.choice(["FULFILLED", "MISSED", "CANCELLED"])] for i in range(1, 641)])
    w("operations/returns.csv", ["Id", "orig_ship", "Date", "reason_cd", "Status"],
      [[f"RT{6000 + i}", RNG.choice(DELIVERED)[0], rnd_date().isoformat(),
        f"RR{RNG.randint(1, 5)}", RNG.choice(["RECEIVED", "IN_TRANSIT", "REFUSED"])]
       for i in range(1, 191)])
    w("operations/edi_messages.csv", ["Id", "ship_ref", "msg_type", "ts", "Status"],
      [[f"EDI{70000 + i}", RNG.choice(SHIPMENTS)[0], RNG.choice(["214", "210", "990", "997"]),
        ts(rnd_date()), RNG.choice(["ACK", "NACK", "PENDING"])] for i in range(1, 801)])
    w("operations/temperature_logs.csv", ["Id", "ship_ref", "ts", "temp_c"],
      [[f"TL{i:05d}", s[0], ts(rnd_date()), f"{RNG.uniform(1.0, 7.5):.1f}"]
       for i, s in enumerate([x for x in SHIPMENTS if x[3] == "SVC6"][:260], start=1)])

    # ------------------------------------------------ fleet / people
    w("fleet/fuel_logs.csv", ["Id", "vin", "Date", "litres", "Amount"],  # Amount = pump cost
      [[f"FL{i:05d}", f"VIN{RNG.randint(100, 219)}", rnd_date().isoformat(),
        f"{RNG.uniform(20, 240):.1f}", money(30, 380)] for i in range(1, 901)])
    w("fleet/maintenance.csv", ["Id", "vin", "Date", "work_cd", "Amount", "downtime_hrs"],
      [[f"MN{i:04d}", f"VIN{RNG.randint(100, 219)}", rnd_date().isoformat(),
        RNG.choice(["SERVICE", "MOT", "TYRES", "BRAKES", "BODYWORK"]), money(60, 2200),
        RNG.randint(1, 72)] for i in range(1, 281)])
    w("fleet/incidents.csv", ["Id", "Date", "vin", "drv", "severity", "description"],
      [[f"IN{i:04d}", rnd_date().isoformat(), f"VIN{RNG.randint(100, 219)}",
        f"D{RNG.randint(3000, 3119)}", RNG.choice(["MINOR", "MAJOR", "RIDDOR"]),
        RNG.choice(["reversing scrape", "load shift", "slip at dock", "near miss", "RTC third party"])]
       for i in range(1, 71)])
    w("fleet/gps_pings.csv", ["Id", "vin", "ts", "lat", "lon"],
      [[f"GP{i:05d}", f"VIN{RNG.randint(100, 219)}", ts(rnd_date()),
        f"{RNG.uniform(50.1, 57.9):.5f}", f"{RNG.uniform(-5.5, 1.4):.5f}"] for i in range(1, 401)])
    w("people/timesheets.csv", ["Id", "emp_id", "Date", "shift_id", "hours"],
      [[f"TS{i:05d}", f"E{RNG.randint(500, 639)}", rnd_date().isoformat(),
        f"SH{RNG.randint(1, 4)}", f"{RNG.uniform(4, 12):.1f}"] for i in range(1, 1101)])
    w("people/driver_infringements.csv", ["Id", "drv", "Date", "kind", "points"],
      [[f"DI{i:04d}", f"D{RNG.randint(3000, 3119)}", rnd_date().isoformat(),
        RNG.choice(["TACHO", "SPEED", "HOURS", "WALKAROUND"]), RNG.randint(1, 6)] for i in range(1, 121)])

    # ------------------------------------------------ customer-facing misc
    w("customer/surveys.csv", ["Id", "acct", "Date", "nps"],
      [[f"SV{i:04d}", RNG.choice(PARTY_KEYS), rnd_date().isoformat(), RNG.randint(0, 10)]
       for i in range(1, 421)])
    w("customer/complaints.csv", ["Id", "acct", "Date", "channel", "Status"],
      [[f"CP{i:04d}", RNG.choice(PARTY_KEYS), rnd_date().isoformat(),
        RNG.choice(["PHONE", "EMAIL", "PORTAL"]), RNG.choice(["NEW", "WORKING", "CLOSED"])]
       for i in range(1, 261)])
    w("customer/contacts.csv", ["Id", "acct", "Name", "role", "email_domain"],
      [[f"CT{i:04d}", RNG.choice(PARTY_KEYS), person_name(),
        RNG.choice(["BUYER", "WAREHOUSE", "FINANCE", "OPS"]),
        RNG.choice(["example.com", "mail.test", "corp.example"])] for i in range(1, 361)])
    w("customer/addr.csv", ["Id", "acct", "line1", "post_area", "country_cd"],
      [[f"AD{i:04d}", RNG.choice(PARTY_KEYS), f"{RNG.randint(1, 240)} {RNG.choice(FIRST)} Way",
        RNG.choice(["AB", "B", "CF", "EH", "G", "LS", "M", "NE", "SO", "TR"]),
        RNG.choices(["GB", "IE", "FR", "NL"], weights=[80, 8, 6, 6])[0]] for i in range(1, 381)])
    w("customer/audit_log.csv", ["Id", "ts", "user", "action", "entity"],
      [[f"AU{i:05d}", ts(rnd_date()), RNG.choice(["system", "ops1", "ops2", "finance1"]),
        RNG.choice(["CREATE", "UPDATE", "VOID", "EXPORT"]),
        RNG.choice(["shipment", "invoice", "party", "rate_card"])] for i in range(1, 301)])

    total = sum(n for _, n in written)
    print(f"tables={len(written)} rows={total}")
    for rel, n in written:
        print(f"  {rel}: {n}")


if __name__ == "__main__":
    main()
