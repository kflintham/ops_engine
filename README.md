# ops_engine

Ops Engine is WBYS's in-house integration platform. Its job is to move data
between Brightpearl (our ERP) and our suppliers / logistics partners, mostly
via SFTP file drops.

Today, most of that work is done by a third party called **Business Solutions
in the Cloud (BSITC)**. Ops Engine is the thing we're building to replace it,
so that we own the integrations ourselves and can add or change them without
going through BSITC.

## What lives here

Each supplier / partner integration lives in its own folder under `docs/` (for
specs) and eventually under `integrations/` (for code). The shared plumbing
(SFTP client, Brightpearl client, scheduling, logging) will live under
`core/`.

| Folder | Purpose |
| --- | --- |
| `docs/<integration>/` | The contract: process PDFs, sample files, field mappings |
| `integrations/<integration>/` | The code that implements that contract *(not built yet)* |
| `core/` | Shared helpers used by every integration *(not built yet)* |

## Current integrations

| Name | Partner | Status | Replaces |
| --- | --- | --- | --- |
| [`gardiner-brothers-jit`](docs/gardiner-brothers-jit/) | Gardiner Bros & Footsure | Spec in progress | Daily email orders to GB |

Planned but not yet scoped:

- `gardiner-brothers-df` — dropship orders to GB (currently handled by BSITC)
- Additional suppliers as we onboard them

## How the integrations work (in one paragraph)

SFTP integrations are not live API calls. They're shared folders. We write a
CSV file describing an order, and the supplier's system picks it up within a
few minutes. The supplier writes CSV files back (stock levels, despatch
notifications, etc.) and our app polls for them, reads them, and updates
Brightpearl. Everything is asynchronous and file-based. The app's job is to
turn Brightpearl events into correctly-formatted files, and incoming files
back into Brightpearl updates.

## Repo conventions

- Development branches: `claude/<feature-name>` for AI-assisted work, normal
  feature branches otherwise.
- Each integration's spec documents (PDFs, sample CSVs) are checked in so the
  contract is version-controlled alongside the code that implements it.

## Status

Pre-code. We are currently documenting the first integration
(Gardiner Brothers JIT) before writing any runtime code. See
[`docs/gardiner-brothers-jit/README.md`](docs/gardiner-brothers-jit/README.md).
