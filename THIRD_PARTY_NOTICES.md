# Third party notices

The application uses the unmodified upstream distributions listed below. Exact Python versions and installation hashes are retained in `requirements.lock`; development and checking dependencies are in `requirements-dev.lock`. The Dockerfile and Compose definitions pin their base image identities.

The `licenses/` directory retains the license and author files supplied by the installed runtime Python distributions, plus CPython’s image license and PostgreSQL’s packaged copyright notices. `licenses/manifest.json` records the runtime distribution versions and copied notice paths. These notices remain subject to their upstream terms; no upstream author endorses this application.

| Runtime distribution | Resolved version | Declared license or supplied notice |
| --- | --- | --- |
| Django | 5.2.18 | BSD 3 Clause; includes Python license and author notices |
| asgiref | 3.13.0 | BSD 3 Clause |
| sqlparse | 0.6.0 | BSD 3 Clause notice in `licenses/sqlparse/LICENSE` |
| psycopg | 3.3.6 | GNU LGPL version 3 |
| psycopg-binary | 3.3.6 | GNU LGPL version 3 |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT |
| cffi | 2.1.1 | MIT 0 |
| pycparser | 3.12 | BSD 3 Clause |
| PyOTP | 2.10.0 | MIT |
| cryptography | 50.0.2 | Apache 2.0 or BSD 3 Clause |
| packaging | 26.3 | Apache 2.0 or BSD 2 Clause |
| gunicorn | 25.3.0 | MIT |
| whitenoise | 6.12.0 | MIT |

Psycopg is installed as a separately replaceable Python distribution; its library code is not copied into application modules. Its upstream project and source releases are available from [Psycopg](https://www.psycopg.org/) and [the Psycopg source repository](https://github.com/psycopg/psycopg). The retained LGPL text incorporates the [GNU GPL version 3](https://www.gnu.org/licenses/gpl-3.0.html). Preserve the upstream terms and the ability to replace these libraries when redistributing an image.

The runtime uses CPython under the Python Software Foundation license and included historical notices. PostgreSQL uses the PostgreSQL license and its packaged third party notices. Debian and other image packages retain their own notices within `/usr/share/doc/` in the distributed images. The binary driver and cryptography wheels may include native libraries governed by additional upstream terms; retain their wheel metadata and supplied notices when redistributing those distributions.

The verification image adds Ruff, pip-audit, Playwright and their locked dependencies and Chromium browser components. Their installed distribution license files and browser notices remain with that image. Docker Engine, Compose and Docker Scout are host tools, not vendored application source. No stock photo, remote font, copied design asset or proprietary icon package is required by the retained UI.

To reconstruct the Python notice inventory after a dependency update, run the retained collector in the rebuilt image with an explicit writable notice directory:

```sh
mkdir -p licenses
docker run --rm --user root --volume "$PWD/licenses:/notices" --entrypoint python dwmt-local:latest scripts/collect_notices.py --output /notices
```

Review additions, removed versions, native library notices and scanner findings before distributing an updated candidate. The inventory describes retained notices and declared package metadata; it does not turn a scanner result into comprehensive license or security assurance.
