#!/bin/bash
# Copyright 2026 Froncort
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

cd "$(dirname "$0")"

find_python() {
  for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
      ver=$("$cmd" -c "import sys; print(sys.version_info[:2] >= (3, 10))" 2>/dev/null)
      if [ "$ver" = "True" ]; then
        echo "$cmd"
        return 0
      fi
    fi
  done
  return 1
}

PY=$(find_python)
if [ -z "$PY" ]; then
  osascript -e 'display alert "Python 3.10+" message "Install Python from python.org"'
  exit 1
fi

"$PY" dno_ui.py
