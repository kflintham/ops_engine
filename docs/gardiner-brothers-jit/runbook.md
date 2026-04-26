# GBR JIT runbook

How to take this code from "all green in tests" to "actually sending POs
to Gardiners". This is a sequence; do the steps in order. Stop at the
first one that fails and ask for help — every step has a clear
"expected output" so you can tell.

If you're not sure what to type into a terminal, **stop** and ask. None
of these commands are urgent.

## 0. Where this runs

You can do these steps from your laptop or directly from the EC2 box.
For first runs **the EC2 box is easier**: Python 3.11+ is probably
already there, and the SFTP server is on the same machine, so no
network setup. Once verified there, the same steps work locally.

## 1. Get the code on the box

```sh
# SSH into the box
ssh ubuntu@ec2-63-32-88-8.eu-west-1.compute.amazonaws.com

# Pick a folder to work in
mkdir -p ~/ops_engine && cd ~/ops_engine

# Clone the repo onto the box (HTTPS clone with personal access token,
# or SSH if a key is set up)
git clone https://github.com/kflintham/ops_engine.git .
```

Expected: `git status` reports a clean tree on `main`.

## 2. Install Python dependencies

The code targets Python 3.11+. Check what's installed:

```sh
python3 --version
```

If it says 3.11 or higher, you're fine. If not, install Python 3.11
(`sudo apt install python3.11 python3.11-venv` on Ubuntu/Debian).

Create a virtual environment so we don't pollute system Python:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Expected: the last command finishes with no errors, and
`pip list | grep ops-engine` shows `ops-engine 0.1.0`.

## 3. Set up `.env.local`

Copy the template and open it in your editor:

```sh
cp .env.example .env.local
nano .env.local   # or vim, or whatever you have
```

Fill in **only** these for now (leave the GBR_JIT_* lines blank — we'll
fill them in step 5):

| Variable | What to put |
| --- | --- |
| `BRIGHTPEARL_ACCOUNT_CODE` | The bit in your Brightpearl URL: `https://eu1.brightpearlapp.com/<this>/` |
| `BRIGHTPEARL_DATACENTER` | `eu1`, `use1`, etc — the subdomain of your Brightpearl URL |
| `BRIGHTPEARL_APP_REF` | The reference your Brightpearl app was registered with |
| `BRIGHTPEARL_ACCOUNT_TOKEN` | The secret token (the long random string) |
| `GBR_SFTP_USERNAME` | `wbysltd` (per the FileZilla details) |
| `GBR_SFTP_PASSWORD` | The SFTP password for `wbysltd` |

Save the file. Then load the variables into the current shell so the
commands below can read them:

```sh
set -a; source .env.local; set +a
```

Expected: `echo $BRIGHTPEARL_ACCOUNT_CODE` prints your account code (and
not a blank line). If it's blank, the `source` didn't work — check that
`.env.local` is in the current directory and the variable isn't quoted
oddly.

## 4. Discover Brightpearl IDs

This calls Brightpearl and finds the four IDs we need:

```sh
python -m ops_engine.integrations.gardiner_brothers_jit discover
```

Expected output: a block like

```
GBR_JIT_SUPPLIER_CONTACT_ID=4242
GBR_JIT_PRICE_LIST_ID=7
GBR_JIT_STATUS_ID_REQUEST_SENT=101
GBR_JIT_STATUS_ID_PENDING=102
```

(Your numbers will differ.) If any line says `<NOT FOUND ...>`, see
"troubleshooting" below.

## 5. Update `.env.local` with the discovered IDs

Open `.env.local` again and paste those four lines into the
`# --- Gardiner Brothers JIT integration config ---` section, replacing
the blank values. Save.

The other variables in that section
(`GBR_JIT_ORDERS_PATH`, `GBR_JIT_NOTIFICATIONS_PATH`) already have
sensible defaults you don't need to change.

Reload the env:

```sh
set -a; source .env.local; set +a
```

## 6. Create the SFTP folders

```sh
python -m ops_engine.integrations.gardiner_brothers_jit setup-folders
```

Expected output:

```
OK  /JIT/Orders/
OK  /JIT/Notifications/
```

After this, log into the SFTP server with FileZilla and confirm the two
folders are visible at `/JIT/Orders` and `/JIT/Notifications`. Send the
folder paths to Sam at Gardiners so they can configure their pickup.

## 7. Send a real test PO

This is the moment of truth. Make sure there's exactly **one** GBR JIT
purchase order in your Brightpearl with the status `GBR JIT - Request
Sent`, and that all its lines:

- have product 501-style products that list `Gardiner Bros & Co (B1358)`
  among their suppliers, and
- have a SKU on the `Cost Price GBR (Net)` price list.

Then:

```sh
python -m ops_engine.integrations.gardiner_brothers_jit outbound
```

Expected output:

```
OK   PO 555 (PO-555) -> /JIT/Orders/PO-555-202604261430.csv
```

Check via FileZilla that the file exists at that path on the SFTP server,
download it, open it, and verify the contents look like Gardiners'
sample format. Then check Brightpearl: the PO's status should now be
`GBR JIT - Pending`.

If the output instead says `FAIL PO 555: ...`, see "troubleshooting".

## 8. Hand off to Gardiners

Email Sam:

> The JIT folders are now live on our SFTP at `/JIT/Orders/` and
> `/JIT/Notifications/`. The first test order is sitting in
> `/JIT/Orders/`. When you've configured pickup, please drop a
> notification into `/JIT/Notifications/` so we can confirm the
> roundtrip works.

That confirms the outbound side is working before we automate the
inbound side.

## 9. (Later) Schedule it

Once the manual run works, add a cron entry that runs the outbound
command every 15 minutes during business hours. Suggested:

```cron
*/15 7-19 * * 1-6  cd /home/ubuntu/ops_engine && set -a && source .env.local && set +a && .venv/bin/python -m ops_engine.integrations.gardiner_brothers_jit outbound >> /var/log/gbr_jit/outbound.log 2>&1
```

(Don't add this until the manual run has worked at least once.)

## Troubleshooting

### `discover` reports `<NOT FOUND>` for any value

The string the discovery searches for didn't match exactly. Common causes:
- The status was renamed (e.g. typo fix on `Recieved`).
- The supplier or price list wasn't created with that exact name.

The expected names are listed in
[`field-mapping.md`](field-mapping.md#brightpearl-purchase-order-status-workflow).
Either rename in Brightpearl to match, or look the ID up manually in the
Brightpearl UI and paste it into `.env.local` directly.

### `setup-folders` errors with "Permission denied"

The SFTP user (`wbysltd`) doesn't have write permission on the parent
directory. Either grant it (`chmod`/`chown` on the EC2 box's filesystem)
or pre-create the folders with FileZilla/another tool.

### `outbound` fails with `FAIL PO X: BrightpearlError 404`

One of the Brightpearl endpoints in `brightpearl_queries.py` doesn't
exist on your account or has a different name. Capture the failing path
from the log and check it against Brightpearl's API docs. Fix is usually
a single-line tweak in `brightpearl_queries.py`.

### `outbound` fails with `FAIL PO X: GbrJitMappingError ...`

The PO violates a JIT business rule. The error message names the broken
rule and the specific product ID. Common ones:

- *"product N does not list supplier 4242"* — the product is missing
  `Gardiner Bros & Co (B1358)` in its Suppliers tab.
- *"product N has no SKU on the Gardiners price list"* — the product's
  `Cost Price GBR (Net)` entry has a blank SKU field.

Fix in Brightpearl, then re-run `outbound`.

### Where logs go

The CLI prints to stdout/stderr. When run under cron, redirect both to
a log file (see step 9). For ad-hoc debugging, run with verbose logging:

```sh
PYTHONLOGGING=DEBUG python -m ops_engine.integrations.gardiner_brothers_jit outbound
```
