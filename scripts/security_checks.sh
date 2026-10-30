#!/bin/sh
# Run source/dependency checks in the verification image. Image scanning runs on
# the Docker host, against a named local image, without publishing that image.
set -eu
cd "$(dirname "$0")/.."
if [ "${1:-}" = '--image' ]; then
  [ "$#" -eq 2 ] || { echo 'Usage: security_checks.sh --image LOCAL_IMAGE' >&2; exit 2; }
  docker image inspect "$2" --format '{{.Id}}'
  exec docker scout cves --exit-code "local://$2"
fi
[ "$#" -eq 0 ] || { echo 'Usage: security_checks.sh [--image LOCAL_IMAGE]' >&2; exit 2; }
python -m ruff check --select F63,F7,F82 .
python manage.py check --deploy --fail-level ERROR
python - <<'PY'
from pathlib import Path
import re

root = Path.cwd()
excluded = {'.git', '.local', '.venv', '__pycache__', 'staticfiles', '.ruff_cache', '.pytest_cache', 'node_modules'}
patterns = [re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'), re.compile(r'AKIA[A-Z0-9]{16}')]
findings = []
for path in root.rglob('*'):
    relative = path.relative_to(root)
    if any(part in excluded for part in relative.parts) or not path.is_file() or path.name == '.env':
        continue
    if path.suffix.lower() in {'.docx', '.zip', '.sqlite3'}:
        findings.append(str(relative) + ': excluded document/archive/database input')
        continue
    if path.suffix not in {'.py', '.sh', '.yaml', '.yml', '.js', '.html', '.md', '.txt', '.lock', '.in', '.sql', '.toml'}:
        continue
    try:
        content = path.read_text()
    except (UnicodeError, OSError):
        continue
    if any(pattern.search(content) for pattern in patterns):
        findings.append(str(relative) + ': credential-shaped source content')
if findings:
    raise SystemExit('\n'.join(findings))
print('Bounded source credential/archive scan passed; this is not exhaustive secret detection.')
PY
python -m pip_audit --strict --require-hashes -r requirements.lock --progress-spinner off
