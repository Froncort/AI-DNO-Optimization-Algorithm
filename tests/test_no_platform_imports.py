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

"""Fails if any platform/DB/agent import leaks into the public src/dno_core tree."""
import pathlib
import re
import sys

FORBIDDEN = re.compile(r"\b(prisma|fastapi|langgraph|langfuse|google\.cloud|Prisma)\b")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "dno_core"


def main() -> int:
    hits = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(SRC.parent.parent)}:{lineno}: {line.strip()}")
    if hits:
        print("FORBIDDEN IMPORTS FOUND:")
        for h in hits:
            print(f"  {h}")
        return 1
    print("OK — no forbidden imports in src/dno_core")
    return 0


if __name__ == "__main__":
    sys.exit(main())
