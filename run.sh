#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -q -r requirements.txt
if [[ ! -s inputs/ips.txt ]]; then .venv/bin/python experiment.py fetch; fi
exec .venv/bin/python experiment.py run --input inputs/ips.txt "$@"
