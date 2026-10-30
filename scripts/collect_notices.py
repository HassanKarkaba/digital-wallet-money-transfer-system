"""Copy installed distribution notices without importing application settings.

Run in the built image with an explicit writable output directory. Retain the
result with source when distributing this application's dependency notices.
"""

import argparse
import importlib.metadata
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
arguments = parser.parse_args()
output = Path(arguments.output)
output.mkdir(parents=True, exist_ok=True)
manifest = []
for distribution in sorted(
    importlib.metadata.distributions(), key=lambda item: item.metadata.get('Name', '').lower()
):
    name = distribution.metadata.get('Name', '')
    if name.lower() in {'pip', 'setuptools'}:
        continue
    notice_files = []
    wanted = set(distribution.metadata.get_all('License-File') or [])
    for relative in distribution.files or []:
        if '.dist-info/licenses/' not in str(relative) and relative.name not in wanted:
            continue
        if relative.name not in wanted and not wanted:
            continue
        source = Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        target = output / name / relative.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        notice_files.append(str(target.relative_to(output)))
    manifest.append(
        {
            'name': name,
            'version': distribution.version,
            'license_expression': distribution.metadata.get('License-Expression')
            or distribution.metadata.get('License'),
            'notices': sorted(set(notice_files)),
        }
    )
(output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'Retained notices for {len(manifest)} installed distributions.')
