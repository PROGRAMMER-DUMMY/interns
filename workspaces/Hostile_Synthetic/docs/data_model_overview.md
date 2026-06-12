# Warehouse extract - data model overview

Brindle & Vance Logistics. Nightly CSV extract from TMS, FINSYS, MDM, FLEET, HR and the claims
desk. One folder per source area under `datasets/`.

## Conventions (such as they are)

Different source systems kept their own column names in the extract. The same column name does
NOT mean the same thing across tables. In particular `Id`, `Name`, `Date`, `START`, `END`,
`Amount` and `Status` are reused everywhere with table-specific meanings - check the data
dictionary per table, do not assume.

## Transactional tables

| Table | Grain | Notes |
|---|---|---|
| operations/shipments.csv | one row per shipment (consignment) | TMS order header. `START`/`END` are pickup/delivery timestamps. |
| operations/movements.csv | one row per movement leg | A shipment routes as 1-3 legs through depots/hubs. `START`/`END` are leg depart/arrive. |
| finance/invoices.csv | one row per invoice LINE | An invoice document (`inv_no`) has 1-3 lines (BASE, FUEL, ACCESSORIAL). |
| finance/payments.csv | one row per payment | Against `inv_no`. |
| finance/disputes.csv | one row per dispute | Raised against `inv_no` (document level, not line level). |
| operations/cargo_claims.csv | one row per claim | Raised against a shipment. |

## Master data

| Table | Key | Joined from |
|---|---|---|
| masters/party.csv | `party_key` | `shipments.cust_ref`, `invoices.acct`, `contracts.acct`, `quotes.cust_ref` and most `acct` columns |
| masters/carriers.csv | `scac` | `shipments.carrier_cd`, `settlements.carrier_cd` |
| masters/locs.csv | `loc_nbr` | `shipments.orig`/`dest`, `movements.from_loc`/`to_loc`, `vehicles.depot` |
| masters/svc_catalog.csv | `svc_id` | `shipments.svc` |
| masters/vehicles.csv | `vin` | `movements.veh`, `fleet/*.vin` |
| masters/drivers.csv | `drv_id` | `movements.drv` |

`masters/customers_legacy.csv` is the pre-MDM customer list. It survives in the extract because
two old reports still read it. MDM (`party.csv`) is the system of record.

## Everything else

Reference data lives under `reference/` (zones, regions, currencies, fx rates, holidays, code
tables). Pricing inputs under `pricing/` (tariffs, rate cards, contracts, quotes, fuel prices).
Fleet telematics and workshop data under `fleet/`, HR under `people/`, CRM-ish satellites under
`customer/`.

Nobody has drawn this as an ERD since the BI contractor left.
