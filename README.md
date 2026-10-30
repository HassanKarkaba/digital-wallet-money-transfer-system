# Digital Wallet and Money Transfer System

A local Django application for simulated USD wallets, internal transfers, money requests, scheduled transfers and synthetic bank funding and withdrawals. **All displayed money is demonstration money with no cash value.** No real bank details, payment credentials or external payment providers are used.

The application uses Python 3.13, Django 5.2, PostgreSQL 17 and server rendered HTML with progressive JavaScript. It runs in Docker with separate primary and simulator databases, durable database jobs and locally captured email. Production hosting and real financial operation are outside this edition.

## Start locally

Run these commands from the repository root in a POSIX shell. Docker Engine and Compose are the only required host runtimes. The implementation environment uses Docker Engine 29.9.2 and Docker Compose 5.6.1; the Compose file requires optional `env_file` support from Compose 2.24 or later. Allocate at least 4 CPUs and 8 GiB of memory to Docker for the documented local workload.

```sh
docker compose build
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" --volume "$PWD:/bootstrap" web python manage.py init_demo_config --output /bootstrap/.env
docker compose up -d db
docker compose run --rm migrate
docker compose run --rm web python manage.py seed_demo --access-file /private/demo-access.txt
docker compose up -d web worker simulator
docker compose exec web python manage.py check_readiness --require-all --timeout 120
docker compose exec web python manage.py verify_ledger
```

Open [the local wallet](http://localhost:8000). The host port is bound to `127.0.0.1`; the database has no published host port. If port 8000 is occupied, stop the conflicting service or use an explicit Compose override that binds another loopback port.

The bootstrap command can run before configuration exists. It uses your host user ID to create `.env` with your ownership and random private credentials, and refuses to overwrite an existing file. Never commit this file or reuse these demonstration credentials elsewhere. `.env.example` documents the configuration names without supplying secrets. Runtime settings reject non-DEMO mode and database aliases that share a name or role. Do not change monetary policy through environment variables.

The normal image contains the application, templates, migrations and collected static assets. Installer tooling and bundled bootstrap wheels are removed from the runtime stage; the separate verification stage retains its locked checking tools. It does not depend on a source bind mount. The bootstrap command above is the limited exception: the bind lets configuration be written to this checkout. Named volumes retain the database, local mail, private exports and the independent recovery marker.

## Sign in and use the application

The seeding command writes generated synthetic access details to the private volume. A local operator can intentionally inspect them with:

```sh
docker compose exec web cat /private/demo-access.txt
```

Treat that output as private. It is not served through the website. Seeding is idempotent and creates `.test` identities using the ordinary synthetic funding and accounting paths. You can instead create an account through **Register**, inspect its local verification message, verify the address, and enroll an authenticator. Enrollment shows ten recovery codes once; keep them privately.

To inspect captured verification or recovery messages locally:

```sh
docker compose exec web sh -c 'ls -1 /mail/*.eml'
docker compose exec web sh -c 'cat /mail/SELECTED_MESSAGE.eml'
```

Replace `SELECTED_MESSAGE` with the chosen filename from the first command. Captured mail remains local, private and bounded to 100 MiB. It is never sent through SMTP.

The customer navigation includes:

- **Overview and activity:** available, reserved and pending incoming totals, filtered history, stable payment references and timelines.
- **Send, add funds and withdraw:** enter an exact instruction, review it, confirm a bound authenticator proof and retrieve the persistent result. Synthetic destinations activate ten minutes after creation.
- **Requests and schedules:** exact payer requests and one-time transfers for an explicit future UTC instant with a timezone and offset. Schedules check funds when due.
- **Statements and notifications:** private CSV/HTML statements, retained in-app outcomes and optional locally captured receipts.
- **Account security:** profile, email verification and change, authenticator recovery, session revocation, private account export and closure eligibility.

Amounts at or above $1,000 enter review. Available funds and receiving capacity are reserved as required. Pending and uncertain provider results retain their obligations. Refresh the existing detail or retry the original action identity after losing a reply; an unknown result does not authorize a replacement payment. Once provider submission wins, a withdrawal cannot be cancelled.

Staff use a separate workspace. Support can inspect exact references for an audited purpose; Reviewers decide eligible cases and restrictions; Finance investigates reconciliation and proposes whole reversals; Access Administrators invite synthetic staff accounts and confirm role changes. A proposer or customer participant cannot approve their own case. An invitation grants no permissions until an administrator separately confirms a role grant for a verified, MFA-enrolled account.

## Inspect and stop

```sh
docker compose exec web python manage.py check_readiness --require-all --timeout 120
docker compose exec web python manage.py verify_ledger
docker compose exec web python manage.py reconcile
docker compose logs --tail 100 web worker simulator
docker compose stop
```

Liveness proves only that the web process responds. Readiness distinguishes primary schema, integrity, recovery identity, worker heartbeat, simulator and private storage. Removing a dependency can block dependent admission while safe history reads remain available. Mail failure does not undo committed money. Ordinary stop, restart and rebuild preserve development volumes and admitted work.

Workers retry transient database connection failures with a bounded, interruptible wait and retain their durable jobs. The simulator can continue independent provider work while its primary database heartbeat is unavailable; new application admission still requires healthy dependencies.

## Isolated verification

Use the retained wrapper:

```sh
sh scripts/verify.sh
```

It builds the verification image, creates the separate `dwmt-verify` project with `wallet_test` and `simulator_test`, runs migrations and required services, waits at most 120 seconds for readiness, executes the test runner, and always removes only verification resources through an exit trap. It publishes no host ports. Development volumes are not its targets. Do not run tests against the development configuration.

The equivalent individual commands are:

```sh
docker compose -p dwmt-verify -f compose.test.yaml build
docker compose -p dwmt-verify -f compose.test.yaml up -d db
docker compose -p dwmt-verify -f compose.test.yaml run --rm migrate
docker compose -p dwmt-verify -f compose.test.yaml up -d web worker simulator
docker compose -p dwmt-verify -f compose.test.yaml run --rm tests
docker compose -p dwmt-verify -f compose.test.yaml down -v
```

The runner rejects ordinary database names. Django transaction checks use PostgreSQL; privilege attacks connect through the restricted runtime role. Browser checks use the locked Playwright Chromium build in the verification image. Runtime and development dependencies are pinned with hashes in `requirements.lock` and `requirements-dev.lock`.

To run source and dependency checks while the verification project exists:

```sh
docker compose -p dwmt-verify -f compose.test.yaml run --rm tests sh scripts/security_checks.sh
```

To inspect the locally built runtime image with the host Docker Scout plugin:

```sh
sh scripts/security_checks.sh --image dwmt-local:latest
```

The source scan checks selected credential patterns and forbidden archive/document inputs; it is not exhaustive secret detection. Dependency and image scanners need current vulnerability data. Scanner results, actual browser observations, workload measurements, unresolved findings and the precise tested candidate belong in the separate implementation report. A passing test count is not a claim that every specified boundary was observed.

## Recovery and restoration

The primary database, independent simulator database and recovery marker share an environment UUID and generation. Keep the marker volume independent of database restore points. Never delete it to clear an integrity warning or treat a missing local operation as proof that a provider effect did not occur.

For a declared local recovery, stop application processes before changing database state. Use an incremented generation through the operator command, reconcile the databases and private marker with the selected restore plan, update `.env`, and restart only after that identity has been established:

```sh
docker compose stop web worker simulator
docker compose run --rm web python manage.py recovery_prepare --generation 2
```

The example generation must be greater than the current value. Update `RECOVERY_GENERATION` in the private configuration to the same value. A restore can replace generation records, so compare all three retained identities after restoring; inconsistency must keep admission closed. Then use the existing immutable provider keys and observations:

```sh
docker compose run --rm web python manage.py verify_ledger
docker compose run --rm web python manage.py recovery_finish
docker compose up -d web worker simulator
```

`recovery_finish` checks reconciliation and does not automatically reopen admission. Resolve findings through the permitted coordinators and obtain a separate eligible reviewer’s bound acknowledgement of a clean recheck in the staff workspace. Do not edit historical journals, fabricate provider certainty or create a new provider identity to retry an uncertain payout. An irreconcilable difference remains an incident with the gate closed. These controls qualify only the selected local simulation; no production backup or recovery service is provided.

## Local key rotation

Add the new randomly generated MFA encryption key to the private key ring with a new version and select it as `MFA_KEY_VERSION`. Keep every old decryption key configured until all retained ciphertext has been checked. Restart with that explicit configuration, then use bounded batches:

```sh
docker compose run --rm web python manage.py rotate_mfa_secrets --batch 100 --dry-run
docker compose run --rm web python manage.py rotate_mfa_secrets --batch 100
```

The command locks each owner, reseals both MFA seeds and encrypted security mail, verifies plaintext equality before writing, and records only versions and references in audit. It prints remaining counts without secrets. Repeat until both counts are zero; retain the old keys if a batch fails. Restored backups may still require their old keys. Callback keys have a separate explicitly dated overlap of at most ten minutes and independent event replay checks.

## Source map and maintenance

| Responsibility | Primary locations and searchable symbols |
| --- | --- |
| Runtime, routes and local readiness | `config/settings.py`, `config/urls.py`, `config/health.py` |
| Identity, verification, MFA and sessions | `accounts/models.py`, `accounts/services.py`, `accounts/views.py`, `accounts/middleware.py` |
| Posting and accounting protections | `ledger/posting.py`, `ledger/sql/protection.sql`, ledger migrations; `post_effect`, `open_wallet` |
| Intents, transfers and provider settlement | `payments/workflows.py`; `create_intent`, `confirm_transfer`, `confirm_rail_operation`, `apply_provider_observation` |
| Requests, schedules and staff decisions | `payments/workflows.py`; `create_request`, `execute_schedule`, `_decide_case`, `_confirm_staff` |
| Disputes, whole reversals and reviewed recovery | `oversight/`, `payments/workflows.py`, `payments/reconciliation.py`, `oversight/recovery.py`, `ledger/sql/recovery.sql` |
| Independent synthetic bank effects | `simulator/models.py`, `simulator/services.py`, `rails/adapter.py`, `rails/views.py` |
| Durable progress and local notifications | `work/services.py`, `work/management/commands/run_worker.py`, `notifications/services.py` |
| Statements, private exports and retrieval | `reports/services.py`; `generate_statement`, `generate_account_export`; `/statements/` and `/account/data/` |
| Presentation | `templates/base.html`, namespaced feature templates, `static/app/` |
| Verification | Feature `tests.py` files, `scripts/browser_checks.py`, `scripts/verify.sh`, `compose.test.yaml` |

Domain owners enforce authority and business rules; views and forms validate requests; templates and static files render authorized presentation. Financial posting goes through the controlled PostgreSQL boundary. The runtime role can execute that boundary and read accounting data but cannot directly change journals, entries, reservations or balance projections. Migration credentials remain separate.

Overlapping financial actions follow a common lock order: action advisory key, shared admission gate, user security rows by UUID, ranked business records, wallet rows, ledger accounts and daily counters. Required audit and durable jobs commit with their protected changes. External simulator work happens outside the primary transaction and is recovered using its stable key.

When updating dependencies, resolve the declared supported version families into new hash locks, inspect notices and vulnerability results, rebuild both images, and rerun affected database, browser and recovery checks. Check migrations with `makemigrations --check --dry-run` in the isolated environment. Do not remove historical migration inputs or reset a database to hide a failed migration. See `THIRD_PARTY_NOTICES.md` for upstream licensing references.

For an existing dataset, stop `web`, `worker` and `simulator` before applying migrations with `docker compose run --rm migrate`, then restart those services and check readiness. Keep the named development volumes through this upgrade sequence. Migration of older accepted actions captures compact proof references before ordinary proof expiry; it preserves the existing action keys and financial history.

For a clean reconstruction, copy or check out the retained source into a new path, generate a new private configuration, and repeat the documented image/setup and isolated verification commands. No developer-home path, original checkout, host Python installation or existing database should be required. Keep receipts, evidence and the final implementation report outside the checked candidate.

## Destructive development reset

**This deletes every synthetic development account and financial record, including the simulator history, local mail, exports and recovery marker.** Offer or download final account exports first, stop the application, and confirm that the selected Compose project is `dwmt` rather than another local project:

```sh
docker compose ls
docker compose stop
docker compose down -v
```

Only use the final command when intentionally resetting the entire development demonstration. Keep primary, simulator and recovery marker reset coordinated. Restart through the fresh setup sequence afterward. This reset is separate from routine startup, verification, shutdown and recovery.
